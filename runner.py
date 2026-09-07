"""Runs a UI sanity test case across a configurable (environment, locale) matrix.

Every (env, locale) pair is run by spawning direct_executor.py, which owns
one Chromium instance and always hardcodes login itself (see
run_login_handoff there) — this process never launches a browser or
performs login. This module decides, per case_name, whether/when
direct_executor.py needs to run in COMPILE mode before the requested
EXECUTE matrix runs:

- Compile (--record; runs ONCE, only if compiled/<case_name>.json doesn't
  exist yet or --recompile was passed): direct_executor.py hands the whole
  case off to one `claude -p` + `@playwright/mcp` invocation that launches
  and fully owns its OWN fresh browser (no CDP-attach, no dependency on
  this process's browser at all) — login is part of the case itself,
  performed by the agent using creds.json's credentials, passed into the
  prompt for this one-time invocation only. Writes compiled/<case_name>.json
  as it goes. Always run against locale "en" (see resolve_compile_pair) —
  regardless of which locales were actually requested — since compiled
  selectors are meant to be locale-independent. Takes no screenshots.
- Execute (the default, once a compiled file exists): direct_executor.py
  runs compiled steps directly via Playwright for every requested (env,
  locale) pair, including en — falling back to self_heal.py's snapshot-in/
  plan-out recovery per step only when needed (see direct_executor.py and
  self_heal.py). Each pair is isolated: an unhandled exception or a
  self-heal stop condition in one pair doesn't prevent the rest of the
  matrix from running.
"""

import argparse
import json
import subprocess
import sys
import threading
from pathlib import Path

from cases import load_case, case_path

# Console output here can include non-ASCII text relayed from a subprocess
# (agent reasoning in --debug mode, or a SUMMARY line), and this process's
# own stdout otherwise defaults to the OS codepage (e.g. cp1251 on this
# machine), which can't represent Japanese/many accented characters and
# would crash the whole run on the first print() of one.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CONFIG_PATH = Path("config.json")
CREDS_PATH = Path("creds.json")
RESULTS_DIR = Path("results")
COMPILED_DIR = Path("compiled")
KNOWLEDGE_DIR = Path("knowledge")
KNOWLEDGE_LIBRARY_FILENAME = "locator-library.json"
DEFAULT_TIMEOUT_SECONDS = 3600
DEFAULT_FALLBACK_TIMEOUT_SECONDS = 600
ALLOWED_TOOLS = "mcp__playwright__*,Read,Write,Edit,Bash(jq:*)"


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_pairs(envs, locales, config, creds):
    """Build the (env, locale) work list, skipping pairs missing from config/creds."""
    envs = envs or list(config.get("environments", {}).keys())
    pairs = []
    for env in envs:
        env_locales = locales or list(creds.get(env, {}).keys())
        for locale in env_locales:
            url = config.get("environments", {}).get(env, {}).get("url")
            cred = creds.get(env, {}).get(locale)
            if not url or not cred:
                print(f"WARNING: skipping {env}/{locale} — missing config or creds entry")
                continue
            pairs.append((env, locale, url, cred["email"], cred["password"]))
    return pairs


def resolve_compile_pair(args_envs, config, creds):
    """Pick the single (env, url, cred) pair to run Phase 1 (Compile)
    against — always locale "en", regardless of which locales the user
    actually requested to execute (Phase 2 runs against every requested
    locale, including en, against the file this produces).

    Tries the user's requested --envs first (or all configured envs, if
    --envs was omitted); if none of those have an "en" creds entry, falls
    back to searching every env in creds.json, logging that the compile
    pass is using an environment outside what was requested. Returns
    (env, url, cred) or (None, None, None) if no environment anywhere has
    an "en" entry.
    """
    requested_pairs = resolve_pairs(args_envs, ["en"], config, creds)
    if requested_pairs:
        env, _locale, url, email, password = requested_pairs[0]
        return env, url, {"email": email, "password": password}

    fallback_pairs = resolve_pairs(None, ["en"], config, creds)
    if fallback_pairs:
        env, _locale, url, email, password = fallback_pairs[0]
        print(f"NOTE: none of the requested environments have an 'en' creds entry — "
              f"compiling against {env}/en instead (compiled steps are shared across "
              f"every environment/locale once written)")
        return env, url, {"email": email, "password": password}

    return None, None, None


def compiled_file_path(case_name):
    """Path to this case's shared compiled-step file. Its existence is the
    compiled/uncompiled signal for which execution mode to use (see
    module docstring) — unlike the old memory file, this is never
    pre-created, since an empty file would look "already compiled".
    Compiled is per-case (not per env/locale) and cumulative across all
    runs — the runner never parses or merges it, only computes the path;
    the recording/patch agent invocations read and write it directly.
    """
    COMPILED_DIR.mkdir(parents=True, exist_ok=True)
    return (COMPILED_DIR / f"{case_name}.json").resolve()


def knowledge_library_file_path():
    """Path to the shared, product-level locator-pattern knowledge library
    (knowledge/locator-library.json) — one file for the WHOLE product,
    not per case and not per (env, locale), consulted (and occasionally
    updated) by every recording/patch agent invocation for generic,
    recurring UI patterns (see prompts/system_prompt.py). Unlike
    compiled_file_path, this file IS pre-created with an empty skeleton if
    missing: its existence isn't used as a mode signal the way a compiled
    file's is, so there's no reason to leave it absent — every invocation
    just needs it to be readable.
    """
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    path = (KNOWLEDGE_DIR / KNOWLEDGE_LIBRARY_FILENAME).resolve()
    if not path.exists():
        skeleton = {"product": None, "last_updated": None, "patterns": {}}
        path.write_text(json.dumps(skeleton, indent=2), encoding="utf-8")
    return path


def build_direct_cmd(case_name, env, locale, url, email, password, screenshots_dir, compiled_path,
                      knowledge_path, headless, fallback_timeout, record, record_timeout,
                      no_screenshots=False):
    """direct_executor.py invocation — a separate Python process, same
    arm's-length relationship runner.py already has with `claude -p`. It
    owns its own browser and hardcodes login itself in both modes; `record`
    picks whether it then runs a full-case recording claude -p invocation
    or executes compiled steps directly.

    "-u" (unbuffered mode) matters here beyond just this list: without it,
    direct_executor.py's own stdout is block-buffered since it's a pipe,
    not a terminal, so --debug's live streaming below would otherwise lag
    behind real progress by however long it takes that buffer to fill.
    """
    cmd = [
        sys.executable, "-u", "direct_executor.py",
        "--case", case_name,
        "--env", env,
        "--locale", locale,
        "--url", url,
        "--email", email,
        "--password", password,
        "--screenshots-dir", str(screenshots_dir),
        "--compiled-path", str(compiled_path),
        "--knowledge-path", str(knowledge_path),
        "--fallback-timeout", str(fallback_timeout),
        "--record-timeout", str(record_timeout),
    ]
    if record:
        cmd.append("--record")
    if headless:
        cmd.append("--headless")
    if no_screenshots:
        cmd.append("--no-screenshots")
    return cmd


def _drain_stream(stream, sink):
    """Read a subprocess stream to completion, appending each line to sink.

    Run in a background thread for stderr so it can't fill its OS pipe
    buffer and stall the child while we're reading stdout in the main
    thread (a classic Popen deadlock if only one stream is drained).
    """
    for line in stream:
        sink.append(line)
    stream.close()


def _kill_on_timeout(proc, timed_out_flag):
    timed_out_flag.set()
    proc.kill()


def extract_summary_line(log_content):
    """Return the final "SUMMARY: ..." line, or None if one was never
    printed (e.g. login aborted, or the process was killed before finishing).

    Looks for "SUMMARY:" anywhere in the line, not just at the start, since
    direct_executor.py's own lines are timestamp-prefixed (e.g.
    "[14:32:07] SUMMARY: ..."); the timestamp is dropped from the returned
    string so the displayed "SUCCESS - {summary_line}" stays clean.
    """
    for line in reversed(log_content.splitlines()):
        # Tolerate a stray leading quote character, in case the model wraps
        # its answer in quotes (seen for real with STEP/CONTINUE lines —
        # see direct_executor.py's run_fallback_step).
        stripped = line.strip().strip('"')
        idx = stripped.upper().find("SUMMARY:")
        if idx != -1:
            return stripped[idx:]
    return None


def _run_subprocess_with_timeout(cmd, log_path, timeout_seconds, debug, case_name, env, locale):
    """Run cmd to completion (or kill it after timeout_seconds), streaming
    matched lines live in --debug mode, and writing the full captured
    output to log_path. Always a direct_executor.py invocation (recording
    or compiled mode) — "run this, respect the timeout, capture the
    output, report SUCCESS/TIMEOUT" is identical either way.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace", bufsize=1)

    stderr_lines = []
    stderr_thread = threading.Thread(target=_drain_stream, args=(proc.stderr, stderr_lines), daemon=True)
    stderr_thread.start()

    timed_out = threading.Event()
    timer = threading.Timer(timeout_seconds, _kill_on_timeout, args=(proc, timed_out))
    timer.start()

    # Read stdout line-by-line so --debug can echo progress as it arrives.
    # Note this depends on the child's own stdout not being fully
    # block-buffered on its end (see build_direct_cmd's "-u") — reading
    # incrementally on our side can't force a child process to flush more
    # eagerly than it chooses to.
    stdout_lines = []
    try:
        for line in proc.stdout:
            stdout_lines.append(line)
            if debug:
                print(f"[{case_name}] {env}/{locale}: {line.rstrip()}")
        proc.wait()
    finally:
        timer.cancel()
        stderr_thread.join(timeout=5)

    log_content = "".join(stdout_lines)
    if stderr_lines:
        log_content += "\n--- stderr ---\n" + "".join(stderr_lines)

    if timed_out.is_set():
        log_content += f"\n*** TIMED OUT after {timeout_seconds}s — process killed ***\n"
        log_path.write_text(log_content, encoding="utf-8")
        print(f"[{case_name}] {env}/{locale}: TIMEOUT")
        return

    log_path.write_text(log_content, encoding="utf-8")
    summary_line = extract_summary_line(log_content)
    if summary_line:
        print(f"[{case_name}] {env}/{locale}: SUCCESS - {summary_line}")
    else:
        print(f"[{case_name}] {env}/{locale}: done (no SUMMARY line found in output)")


def run_one(case_name, env, locale, url, email, password, screenshots_dir, compiled_path,
            knowledge_path, timeout_seconds, headless, debug, record, fallback_timeout,
            no_screenshots=False):
    result_dir = RESULTS_DIR / case_name / env / locale
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    log_path = result_dir / "log.txt"

    cmd = build_direct_cmd(case_name, env, locale, url, email, password, screenshots_dir,
                            compiled_path, knowledge_path, headless, fallback_timeout, record, timeout_seconds,
                            no_screenshots=no_screenshots)

    print(f"[{case_name}] {env}/{locale}: running...")
    _run_subprocess_with_timeout(cmd, log_path, timeout_seconds, debug, case_name, env, locale)


def main():
    parser = argparse.ArgumentParser(description="Run a UI sanity test case via direct_executor.py (compiled execution, or a recording run).")
    parser.add_argument("--case", required=True, help="Case name (matches cases/<name>.json)")
    parser.add_argument("--envs", nargs="+", default=None, help="Environments to run (default: all in config.json)")
    parser.add_argument("--locales", nargs="+", default=None, help="Locales to run (default: all defined in creds.json for each env)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Per-run timeout in seconds (default: 3600)")
    parser.add_argument("--fallback-timeout", type=int, default=DEFAULT_FALLBACK_TIMEOUT_SECONDS,
                         help="Timeout in seconds for each per-step agent fallback invocation in direct mode (default: 600)")
    parser.add_argument("--recompile", action="store_true",
                         help="Force a fresh full agent-driven recording run, overwriting any existing "
                              "compiled/<case>.json — useful when the UI has changed enough that patch-by-patch "
                              "fallback isn't keeping up, or to regenerate a case from scratch.")
    parser.add_argument("--debug", action="store_true", help="Echo direct_executor.py's output to the terminal live as it's printed")
    parser.add_argument("--no-screenshots", action="store_true",
                         help="Diagnostic: forwarded to direct_executor.py to skip screenshot "
                              "capture entirely, to isolate its cost from other slowness sources.")
    args = parser.parse_args()

    path = case_path(args.case)
    if not path.exists():
        print(f"ERROR: case file not found: {path}")
        sys.exit(1)
    case_name, _ = load_case(path)

    config = load_json(CONFIG_PATH)
    creds = load_json(CREDS_PATH)

    pairs = resolve_pairs(args.envs, args.locales, config, creds)
    if not pairs:
        print("ERROR: no valid (env, locale) pairs to run — check config.json/creds.json")
        sys.exit(1)

    headless = config.get("headless", True)
    compiled_path = compiled_file_path(case_name)
    knowledge_path = knowledge_library_file_path()

    needs_compile = not compiled_path.exists() or args.recompile
    if needs_compile:
        compile_env, compile_url, compile_cred = resolve_compile_pair(args.envs, config, creds)
        if compile_env is None:
            print("ERROR: no environment has an 'en' locale credential in creds.json — cannot compile")
            sys.exit(1)
        print(f"[{case_name}] {compile_env}/en: COMPILING (Phase 1) — writes {compiled_path}")
        compile_screenshots_dir = (RESULTS_DIR / case_name / compile_env / "en" / "screenshots").resolve()
        run_one(case_name, compile_env, "en", compile_url, compile_cred["email"], compile_cred["password"],
                compile_screenshots_dir, compiled_path, knowledge_path, args.timeout, headless, args.debug,
                record=True, fallback_timeout=args.fallback_timeout, no_screenshots=True)
        if not compiled_path.exists():
            print(f"ERROR: compile pass did not produce {compiled_path} — aborting")
            sys.exit(1)

    for env, locale, url, email, password in pairs:
        screenshots_dir = (RESULTS_DIR / case_name / env / locale / "screenshots").resolve()
        try:
            run_one(case_name, env, locale, url, email, password, screenshots_dir, compiled_path,
                    knowledge_path, args.timeout, headless, args.debug, record=False,
                    fallback_timeout=args.fallback_timeout, no_screenshots=args.no_screenshots)
        except Exception as e:
            print(f"[{case_name}] {env}/{locale}: ERROR - unhandled exception: {e!r}")


if __name__ == "__main__":
    main()
