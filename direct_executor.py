"""Fast-path Playwright executor for a single (case, env, locale) run.

- Compiled mode (--compiled-path points at an existing compiled file, the
  default, "Execute" phase): owns one Chromium instance (via Playwright's
  Python API) for the whole run. Login is always hardcoded plain
  Playwright (see run_login_handoff; the login form is stable
  enough not to need agent reasoning) and always runs first. For each
  scenario step, looks up compiled/<case_name>.json (see compiled_steps.py)
  and, if it has entries for that step whose types are all directly
  executable, runs them via plain Playwright — no LLM call
  ("direct-success"/"direct-failed"). Otherwise (no entries yet, an
  ineligible type, or a locale_unsafe entry) or if a direct attempt fails
  at runtime, hands that ONE step to self_heal.self_heal_step
  ("Self-heal" phase) — see self_heal.py's own docstring for how that
  works. It never touches the browser itself; our own code, on the SAME
  already-authenticated page, executes the plan it returns. A successful
  self-heal permanently patches that step's entries in the compiled file,
  so future runs (any locale) execute it directly too. If self-heal can't
  find a safe plan at all, this (case, env, locale) run stops — that's the
  one legitimate stop condition, not a bug. No MCP/live-browser-control
  path is ever used in this mode.
- Recording mode (--record, "Compile" phase): this process launches NO
  browser of its own at all — it hands the whole case off to a single
  `claude -p` + `@playwright/mcp` invocation, running in @playwright/mcp's
  normal DEFAULT mode (no --cdp-endpoint), which launches and fully owns
  its own fresh browser instance via MCP tools. Login is part of the case
  itself now, performed by the agent as ordinary agent-driven steps using
  credentials passed into the prompt (see CASE_RECORDING_PROMPT_TEMPLATE)
  — this process never hardcodes login for a recording run, and this is
  the ONE place in the whole pipeline where a model ever sees credentials.
  The agent drives every scenario step and writes compiled/<case_name>.json
  as it goes. Used for a case's first run (against the "en" locale only —
  see runner.py), or --recompile. Takes no screenshots; that's Execute's
  job.

  This reverts an earlier CDP-attach design for Compile (attaching
  `claude -p` to a Python-launched, already-logged-in browser over
  --cdp-endpoint), which proved unreliable: our own Python-controlled
  page (page.url) would confirm landing on the authenticated app while
  the attached agent reported seeing a genuine, unauthenticated
  sign-in page instead — a direct contradiction indicating the CDP
  attach was sometimes targeting the wrong tab/session. Letting
  @playwright/mcp launch and own its own browser end-to-end, with no
  attach step at all, sidesteps that class of bug entirely. Execute mode
  never used CDP-attach and is unaffected by any of this.

Invoked as its own subprocess by runner.py (see build_direct_cmd) — not
imported — so it's independently runnable/debuggable, e.g.:
  python direct_executor.py --case activate-s3 --env qa --locale en \\
      --url https://... --email ... --password ... \\
      --screenshots-dir results/<case>/qa/en/screenshots \\
      --compiled-path compiled/<case>.json \\
      --knowledge-path knowledge/locator-library.json
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import compiled_steps
import self_heal
from cases import load_case, case_path
from runner import ALLOWED_TOOLS
from prompts.system_prompt import CASE_RECORDING_PROMPT_TEMPLATE

# runner.py decodes this process's stdout as UTF-8, but that only governs
# how the PARENT reads our bytes — it does nothing for how WE encode print()
# output ourselves. Without this, print() falls back to the OS default
# codepage (e.g. cp1251 on this machine), which can't represent Japanese
# (or various accented) characters and crashes the whole run mid-step the
# moment an agent response contains any.
# runner.py already imports this module, which reconfigures stdout/stderr
# too (encoding only) as an import side effect before this call runs; ours
# runs last, so line_buffering=True here is what actually determines
# whether --debug sees our output live vs. buffered until exit/flush.
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

VIEWPORT = {"width": 1920, "height": 1080}

LOGIN_FIELD_TIMEOUT_MS = 30000
ACCOUNT_CHOOSER_TIMEOUT_MS = 5000
LOGIN_REDIRECT_TIMEOUT_MS = 30000
IMS_DOMAIN_HINTS = ("adobelogin.com", "services.adobe.com")


def _ts():
    return time.strftime("%H:%M:%S")


def log(msg):
    print(f"[{_ts()}] {msg}")


def build_recording_mcp_config():
    """Default (non-CDP) @playwright/mcp config — the agent launches and
    fully owns its own fresh browser instance directly via MCP tools,
    exactly as @playwright/mcp works out of the box. Used only for the
    Compile pass's single claude -p invocation; Execute/Self-heal never
    use MCP at all, and no CDP endpoint or Python-launched browser is
    involved here in any way.
    """
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": ["@playwright/mcp@latest"],
            }
        }
    }


def run_login_handoff(page, url, email, password):
    """Plain Playwright login against the email/password login form —
    no agent reasoning needed since these selectors are stable.
    """
    page.goto(url)
    try:
        page.fill('input[name="username"]', email, timeout=LOGIN_FIELD_TIMEOUT_MS)
        page.click('[type="submit"]', timeout=LOGIN_FIELD_TIMEOUT_MS)
    except Exception as e:
        return False, f"login failed entering email: {e}", ""

    try:
        page.click('button[data-id="AccountChooser-AccountList-individual"]',
                    timeout=ACCOUNT_CHOOSER_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        pass  # account-chooser screen is optional; not every login shows it

    try:
        page.fill('input[name="password"]', password, timeout=LOGIN_FIELD_TIMEOUT_MS)
        page.click('[type="submit"]', timeout=LOGIN_FIELD_TIMEOUT_MS)
    except Exception as e:
        return False, f"login failed entering password: {e}", ""

    # The login provider sometimes shows a SECOND account-chooser-style
    # screen after password submit (e.g. an "adaptive sign-in"/"Welcome
    # back" interstitial) before finally redirecting to the app — try the
    # same account-tile click here too, best-effort.
    try:
        page.click('button[data-id="AccountChooser-AccountList-individual"]',
                    timeout=ACCOUNT_CHOOSER_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        pass

    # The login form submitting successfully client-side does NOT mean
    # the session is actually established yet — the SSO provider's OAuth
    # redirect chain can take a while, and an intermediate interstitial
    # (like the one above, if the click didn't match it) can strand the
    # page mid-flow indefinitely. Returning True while still on the SSO
    # domain is a false positive that would strand Execute mode on what
    # looks like an authenticated page but is actually still an auth
    # screen. Wait for the URL to actually leave the SSO/auth domain
    # before declaring success.
    try:
        page.wait_for_url(lambda u: not any(hint in u for hint in IMS_DOMAIN_HINTS),
                           timeout=LOGIN_REDIRECT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        return False, (f"login form submitted but never redirected away from the SSO domain "
                        f"(stuck at {page.url!r} after {LOGIN_REDIRECT_TIMEOUT_MS}ms)"), ""

    return True, None, ""


STABILITY_CHECK_INTERVAL_MS = 200
SLOW_SETTLE_LOG_THRESHOLD_MS = 500


def _screenshot_hash(page):
    return hashlib.sha256(page.screenshot()).hexdigest()


def wait_for_page_settle(page, networkidle_timeout_ms, stability_checks):
    """Best-effort adaptive wait before a screenshot capture. Two cheap,
    deterministic layers, no AI/LLM involved, never blocks indefinitely and
    never raises: (1) network-idle wait, near-instant if already idle; (2)
    a short poll comparing successive in-memory screenshot hashes, to catch
    client-side re-render/animation that networkidle alone misses (common
    in React apps that finish fetching but keep repainting briefly).
    Returns elapsed milliseconds, for logging.
    """
    start = time.monotonic()
    try:
        page.wait_for_load_state("networkidle", timeout=networkidle_timeout_ms)
    except PlaywrightTimeoutError:
        pass  # proceed anyway with best-effort capture

    previous_hash = None
    for _ in range(stability_checks):
        try:
            current_hash = _screenshot_hash(page)
        except Exception:
            break
        if current_hash == previous_hash:
            break
        previous_hash = current_hash
        time.sleep(STABILITY_CHECK_INTERVAL_MS / 1000)

    return int((time.monotonic() - start) * 1000)


def take_screenshot(page, screenshots_dir, case_name, step_number,
                     networkidle_timeout_ms, stability_checks):
    elapsed_ms = wait_for_page_settle(page, networkidle_timeout_ms, stability_checks)
    slow_note = " (slow)" if elapsed_ms > SLOW_SETTLE_LOG_THRESHOLD_MS else ""
    log(f"STEP {step_number}: [WAIT] adaptive wait took {elapsed_ms}ms before screenshot{slow_note}")
    path = Path(screenshots_dir) / f"{case_name}_{step_number:02d}.png"
    page.screenshot(path=str(path))
    return path


def run_fallback_step(page, case_name, env, locale, step_number, step_description,
                       screenshots_dir, compiled_path, knowledge_path, fallback_timeout,
                       screenshot_networkidle_timeout, screenshot_stability_checks,
                       no_screenshots=False, frame_path_selectors=None, failing_global_index=None):
    """Thin wrapper around self_heal.self_heal_step — no MCP/CDP/browser
    tool access for the model at all (see self_heal.py's own docstring).
    Returns (ok, detail); the caller (run_case) decides whether `ok=False`
    should stop this (case, env, locale) run.
    """
    log(f"STEP {step_number}: [SELF-HEAL] falling back - [{step_description}]")
    ok, detail = self_heal.self_heal_step(
        page, case_name, env, locale, step_number, step_description,
        compiled_path, knowledge_path, fallback_timeout, frame_path_selectors,
        failing_global_index=failing_global_index,
    )

    # Safety net: self-heal never takes the named per-step screenshot
    # artifact itself (its own screenshot is prompt-only); back-fill one
    # from the current page state so the run's screenshot sequence has no
    # gap, whether this step ultimately succeeded or not.
    if not no_screenshots:
        screenshot_path = Path(screenshots_dir) / f"{case_name}_{step_number:02d}.png"
        if not screenshot_path.exists():
            try:
                take_screenshot(page, screenshots_dir, case_name, step_number,
                                 screenshot_networkidle_timeout, screenshot_stability_checks)
            except Exception:
                pass

    return ok, detail


def run_recording(case_name, steps, env, locale, url, email, password, screenshots_dir,
                   compiled_path, knowledge_path, record_timeout):
    """Hand the WHOLE case off to one claude -p + @playwright/mcp
    invocation, running in @playwright/mcp's normal default mode — the
    agent launches and fully owns its own fresh browser via MCP tools, no
    CDP-attach and no dependency on any Python-launched browser at all.
    Login is part of the case itself now, performed by the agent using
    the credentials passed into the prompt. Relays the agent's own stdout
    verbatim (its STEP.../SUMMARY lines are already in the exact format
    runner.py expects) rather than re-deriving them ourselves.
    """
    numbered_steps = "\n".join(f"{i}. {step}" for i, step in enumerate(steps, start=1))
    prompt = CASE_RECORDING_PROMPT_TEMPLATE.format(
        case_name=case_name, env=env, locale=locale,
        scenario_steps=numbered_steps, url=url, email=email, password=password,
        compiled_path=compiled_path, knowledge_path=knowledge_path,
    )

    config_path = Path(screenshots_dir).parent / "mcp_config.json"
    config_path.write_text(json.dumps(build_recording_mcp_config(), indent=2), encoding="utf-8")

    log("MODE: RECORDING - entire case (including login) driven by AI, "
        f"own agent-owned browser, writes {compiled_path}")
    cmd = [
        "claude", "-p", prompt,
        "--mcp-config", str(config_path),
        "--allowedTools", ALLOWED_TOOLS,
        "--output-format", "text",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=record_timeout)
        stdout, timed_out = result.stdout, False
    except subprocess.TimeoutExpired as e:
        stdout, timed_out = (e.stdout or ""), True

    log(f"--- raw recording stdout ---\n{stdout}\n--- end raw recording stdout ---")
    if timed_out:
        log(f"SUMMARY: 0/{len(steps)} steps completed, stuck at step 1: "
            f"recording agent invocation timed out after {record_timeout}s")
        return
    print(stdout)


def run_case(case_name, steps, env, locale, url, email, password, screenshots_dir, compiled_path,
             knowledge_path, headless, fallback_timeout, record, record_timeout,
             screenshot_networkidle_timeout=3000, screenshot_stability_checks=3, no_screenshots=False):
    Path(screenshots_dir).mkdir(parents=True, exist_ok=True)

    if record:
        # No Python-launched browser at all in this mode — see the module
        # docstring for why Compile was reverted away from CDP-attach.
        run_recording(case_name, steps, env, locale, url, email, password, screenshots_dir,
                      compiled_path, knowledge_path, record_timeout)
        return

    total = len(steps)
    completed = 0
    stuck_at = None
    stuck_reason = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(viewport=VIEWPORT)
        page = context.new_page()

        login_ok, login_reason, _ = run_login_handoff(page, url, email, password)
        if not login_ok:
            log(f"LOGIN ABORTED: {login_reason}")
            browser.close()
            log(f"SUMMARY: 0/{total} steps completed, stuck at step 1: login failed - {login_reason}")
            return
        log("LOGIN: OK")

        log(f"MODE: EXECUTE - running {compiled_path} directly, self-heal per step as needed")

        for i, step_description in enumerate(steps, start=1):
            compiled_data = compiled_steps.load_compiled(compiled_path)

            # Pre-substitute any already-known-bad, locale-translatable
            # iframe frame path (see self_heal.presubstitute_frame_path)
            # BEFORE even attempting direct execution — zero LLM
            # involvement, and bulk-patches every entry across the
            # whole compiled file sharing that exact stale value in one
            # write, so this exact issue is never rediscovered step by
            # step, run after run.
            for _, entry in compiled_steps.entries_for_step(compiled_data, i):
                old_fp = entry.get("framePathSelectors")
                new_fp = self_heal.presubstitute_frame_path(old_fp, knowledge_path)
                if new_fp:
                    n = compiled_steps.bulk_replace_frame_path(compiled_path, old_fp, new_fp, locale)
                    log(f"STEP {i}: [PRE-SUBSTITUTE] known-bad frame path {old_fp} -> {new_fp} "
                        f"({n} entries updated across compiled file)")
                    compiled_data = compiled_steps.load_compiled(compiled_path)
                    break

            success, detail, failing_global_index = compiled_steps.try_compiled_step(page, compiled_data, i)
            if success:
                if not no_screenshots:
                    take_screenshot(page, screenshots_dir, case_name, i,
                                     screenshot_networkidle_timeout, screenshot_stability_checks)
                log(f"STEP {i}: [COMPILED] OK - [{detail}]")
                completed += 1
                continue

            # Not directly executable, or the direct attempt failed at
            # runtime — hand this one step to self-heal. If earlier
            # (broken) entries already existed for this step, reuse
            # the failing entry's own frame path so the snapshot is
            # scoped correctly (falling back to the first entry's, if
            # this wasn't a specific-entry runtime failure).
            existing_entries = compiled_steps.entries_for_step(compiled_data, i)
            frame_path_selectors = None
            if existing_entries:
                failing_entry = next(
                    (e for idx, e in existing_entries if idx == failing_global_index), None)
                frame_path_selectors = (failing_entry or existing_entries[0][1]).get("framePathSelectors")

            ok, heal_detail = run_fallback_step(
                page, case_name, env, locale, i, step_description,
                screenshots_dir, compiled_path, knowledge_path, fallback_timeout,
                screenshot_networkidle_timeout, screenshot_stability_checks, no_screenshots,
                frame_path_selectors, failing_global_index,
            )
            if ok:
                print(f"STEP {i}: OK - {heal_detail}")
                completed += 1
            else:
                status_word = "STATE_MISMATCH" if heal_detail.startswith("STATE_MISMATCH:") else "STUCK"
                print(f"STEP {i}: {status_word} - {heal_detail}")
                stuck_at = i
                stuck_reason = heal_detail
                break

        browser.close()

    if stuck_at is not None:
        log(f"SUMMARY: {completed}/{total} steps completed, stuck at step {stuck_at}: {stuck_reason}")
    else:
        log(f"SUMMARY: {completed}/{total} steps completed")


def main():
    parser = argparse.ArgumentParser(description="Direct Playwright executor with per-step claude -p fallback.")
    parser.add_argument("--case", required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--locale", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--screenshots-dir", required=True)
    parser.add_argument("--compiled-path", required=True)
    parser.add_argument("--knowledge-path", required=True,
                         help="Path to the shared, product-level locator knowledge library "
                              "(knowledge/locator-library.json) — see runner.py's "
                              "knowledge_library_file_path.")
    parser.add_argument("--fallback-timeout", type=int, default=600)
    parser.add_argument("--record", action="store_true",
                         help="Recording mode: hand the whole case off to one claude -p + "
                              "@playwright/mcp invocation (its own agent-owned browser, login "
                              "included) that writes --compiled-path, instead of executing "
                              "compiled steps.")
    parser.add_argument("--record-timeout", type=int, default=3600)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--screenshot-networkidle-timeout", type=int, default=3000,
                         help="Max ms to wait for network idle before each screenshot "
                              "(returns immediately if already idle).")
    parser.add_argument("--screenshot-stability-checks", type=int, default=3,
                         help="Max number of successive in-memory screenshot hashes to "
                              "compare (~200ms apart) before giving up and capturing anyway.")
    parser.add_argument("--no-screenshots", action="store_true",
                         help="Diagnostic: skip screenshot capture entirely (including the "
                              "wait_for_page_settle wait before it), for both the compiled-step "
                              "and fallback-backfill paths, to isolate its cost from other "
                              "slowness sources. Temporary flag, does not remove screenshot support.")
    args = parser.parse_args()

    case_name, steps = load_case(case_path(args.case))

    run_case(
        case_name, steps, args.env, args.locale, args.url, args.email, args.password,
        args.screenshots_dir, args.compiled_path, args.knowledge_path, args.headless, args.fallback_timeout,
        args.record, args.record_timeout,
        args.screenshot_networkidle_timeout, args.screenshot_stability_checks,
        no_screenshots=args.no_screenshots,
    )


if __name__ == "__main__":
    main()
