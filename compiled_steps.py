"""Execution engine for compiled step files (compiled/<case_name>.json).

Ports the relevant subset of the manual recording tool's own execution
semantics (playwright_tool_v5.1.158/runner/execution/step-executor/*.js)
into plain Playwright Python calls, with these deliberate simplifications
made when porting:

- Only `type` in DIRECT_ELIGIBLE_TYPES is directly executable; anything
  else (selectOption, dragAndDrop, pressKey, uploadFile, ...) always
  routes to agent fallback — same precedent as the old
  DIRECT_ELIGIBLE_ACTIONS = {"click"} in direct_executor.py.
- `clickBy: "text"` is not ported — it depends on translatable UI text,
  which the locator-selector rule already forbids recording. Compiled
  entries only ever use selector-based clicks.
- `hasDisambiguation` / `matchCount` are recording-time-only metadata,
  same as in the source tool — never read back here.
- The source tool's automatic "retry a broken selector in other frames"
  behavior and its separate whole-test-rerun layer are NOT ported. A
  broken frame path or selector here just fails the entry, which
  direct_executor.py turns into a live agent-fallback-and-patch instead.
- onFailure recovery actions are executed via execute_entry() too (the
  same dispatcher used for primary entries), and never read their own
  onFailure field, so recursive recovery is structurally impossible —
  matching the source tool exactly.
"""

import json
import sys
import time
from datetime import datetime, timezone

DIRECT_ELIGIBLE_TYPES = {"click", "fill", "check", "waitFor"}
OPTIONAL_ELIGIBLE_TYPES = {"click", "waitFor"}

# Generous enough to absorb the app's occasional load/animation delay without
# a needless AI fallback (a real broken selector still fails and falls
# back, just ~7s slower to detect) — see console-logging plan notes.
DEFAULT_ACTION_TIMEOUT_MS = 12000
FRAME_HOP_TIMEOUT_MS = 10000

# Thresholds for the timing instrumentation below, calibrated relative to
# the timeouts above so only genuinely slow calls are logged (an
# already-loaded frame hop or a normal click is routinely a few hundred ms
# and would otherwise drown the log if we logged everything).
SLOW_FRAME_HOP_LOG_THRESHOLD_MS = 500
SLOW_ENTRY_LOG_THRESHOLD_MS = 1000

# Used only by fillAction "type-replace" (see _type_replace) to select all
# existing content via a real keyboard shortcut before typing.
SELECT_ALL_KEY = "Meta+A" if sys.platform == "darwin" else "Control+A"


def _ts():
    return time.strftime("%H:%M:%S")


def log(msg):
    print(f"[{_ts()}] {msg}")


# Bookkeeping/default fields a hand-authored SHORTHAND entry (see README's
# "Hand-editing compiled files") is never expected to supply — filled in
# automatically by normalize_entry. Keys here match COMPILED_STEP_SCHEMA
# in prompts/system_prompt.py exactly.
_ENTRY_DEFAULTS = {
    "clickBy": "selector",
    "selectorNth": None,
    "hasDisambiguation": False,
    "matchCount": None,
    "optional": False,
    "onFailure": None,
    "locale_unsafe": False,
    "last_patched_by_agent": None,
    "patched_locales": [],
}


def _normalize_frame_path(iframe):
    """Shorthand "iframe" -> canonical "framePathSelectors": null/absent
    becomes [] (top-level page, no frame), a bare string becomes a
    one-element list, and a list passes through as-is (nested frame path,
    same semantics as framePathSelectors already has).
    """
    if iframe is None:
        return []
    if isinstance(iframe, str):
        return [iframe]
    if isinstance(iframe, list):
        return list(iframe)
    raise ValueError(f"invalid 'iframe'/'framePathSelectors' value: {iframe!r} "
                      f"(expected null, a string, or a list of strings)")


def normalize_entry(entry):
    """Expand a hand-authored SHORTHAND compiled entry into the full
    canonical schema (COMPILED_STEP_SCHEMA in prompts/system_prompt.py),
    in memory only — never rewrites the on-disk shorthand form itself
    (see load_compiled). Shorthand and canonical keys can be mixed
    per-entry within the same file: "locator" maps to "selector" and
    "iframe" maps to "framePathSelectors" only when the canonical key
    isn't already present, so an already-fully-populated entry (anything
    the recording/patch code itself wrote) passes through unchanged
    except for filling in whichever bookkeeping fields happen to be
    missing.
    """
    entry = dict(entry)

    if "selector" not in entry:
        entry["selector"] = entry.pop("locator", None)
    else:
        entry.pop("locator", None)

    if "framePathSelectors" not in entry:
        entry["framePathSelectors"] = _normalize_frame_path(entry.pop("iframe", None))
    else:
        entry.pop("iframe", None)

    for key, default in _ENTRY_DEFAULTS.items():
        entry.setdefault(key, default)

    if entry.get("type") == "fill":
        entry.setdefault("fillAction", "replace")
    if entry.get("type") == "check":
        entry.setdefault("checkMode", "check")

    return entry


def _validate_entry(entry, step_number):
    """Reject a malformed entry (shorthand or full) at load time, with a
    clear message, instead of failing later and more confusingly mid-run
    (a KeyError deep inside execute_entry, or a selector that silently
    never matches anything). Deliberately does NOT restrict "type" to
    DIRECT_ELIGIBLE_TYPES — an entry with some other type is still valid,
    just never directly executable (see try_compiled_step); only a
    missing/empty "type" or "selector" is treated as malformed.
    "selector" is allowed to be absent/empty ONLY for a locale_unsafe
    placeholder entry (see mark_step_locale_unsafe), which is recorded
    with selector=None on purpose.
    """
    entry_type = entry.get("type")
    if not isinstance(entry_type, str) or not entry_type.strip():
        raise ValueError(f"compiled entry for step_number {step_number} is missing a valid 'type'")

    if not entry.get("locale_unsafe"):
        selector = entry.get("selector")
        if not isinstance(selector, str) or not selector.strip():
            raise ValueError(
                f"compiled entry for step_number {step_number} (type {entry_type!r}) is missing a "
                f"non-empty 'selector'/'locator'")


def _load_raw(path):
    """Read compiled/<case_name>.json exactly as it is on disk, with NO
    shorthand normalization — used only internally by the read-modify-write
    writers below (patch_step/patch_partial_step/mark_step_locale_unsafe/
    bulk_replace_frame_path), so that entries they don't touch are written
    back byte-for-byte identical to how they were authored (shorthand stays
    shorthand, full stays full) rather than every save silently expanding
    every hand-written shorthand entry in the file into full canonical
    form. The entries a writer actually replaces are always constructed
    fresh in full canonical form regardless (see _plan_action_to_entry) —
    this only affects entries a given write leaves alone.
    """
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_compiled(path):
    """Load compiled/<case_name>.json, normalizing every entry to the full
    canonical schema in memory (see normalize_entry) and validating it
    (see _validate_entry) — the one place shorthand entries are expanded,
    so every CONSUMER (direct_executor.py, self_heal.py, try_compiled_step,
    etc.) always sees/works with fully-populated, valid entries regardless
    of how they were actually authored on disk. This normalization is
    purely in-memory: it never writes anything back (see _load_raw for the
    writers' own read step, which deliberately skips this).
    """
    data = _load_raw(path)

    normalized_steps = []
    for entry in data.get("steps", []):
        step_number = entry.get("step_number")
        if not isinstance(step_number, int):
            raise ValueError(f"compiled entry is missing a valid integer 'step_number': {entry!r}")
        normalized = normalize_entry(entry)
        _validate_entry(normalized, step_number)
        normalized_steps.append(normalized)
    data["steps"] = normalized_steps
    return data


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _write_compiled(path, data):
    """Sole writer for compiled/<case_name>.json, used by self_heal.py's
    patch_step/mark_step_locale_unsafe. Bumps last_updated and writes with
    a fixed, predictable indent — the file's prior on-disk formatting was
    a Claude-authoring artifact, not something callers depend on.
    """
    data["last_updated"] = now_iso()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _plan_action_to_entry(action, step_number, locale):
    """Convert one self-heal plan action (see self_heal.FALLBACK_RESPONSE_SCHEMA)
    into a full compiled-file entry, filling in the bookkeeping fields a
    hand-authored/agent-recorded entry would carry (see COMPILED_STEP_SCHEMA
    in prompts/system_prompt.py).
    """
    selector_nth = action.get("selectorNth")
    entry = {
        "step_number": step_number,
        "type": action["type"],
        "selector": action.get("selector"),
        "clickBy": "selector",
        "framePathSelectors": action.get("framePathSelectors") or [],
        "selectorNth": selector_nth,
        "hasDisambiguation": selector_nth is not None,
        "matchCount": None,
        "optional": False,
        "onFailure": None,
        "locale_unsafe": False,
        "last_patched_by_agent": now_iso(),
        "patched_locales": [locale],
    }
    if action.get("timeout") is not None:
        entry["timeout"] = action["timeout"]
    if action["type"] == "fill":
        entry["value"] = action.get("value", "")
        entry["fillAction"] = action.get("fillAction") or "replace"
    if action["type"] == "check":
        entry["checkMode"] = action.get("checkMode") or "check"
    return entry


def patch_step(path, step_number, plan_actions, locale):
    """Replace every existing entry for step_number with plan_actions,
    converted to full compiled-entry shape, in order. Only appropriate
    when there was nothing already-working to preserve for this step —
    no entries yet, or the existing ones were already known-unusable
    (ineligible type / locale_unsafe) — see try_compiled_step's
    failing_global_index (None in exactly these cases). When a SPECIFIC
    existing entry was the confirmed cause of a runtime failure instead,
    use patch_partial_step so entries that already ran successfully this
    same attempt are never touched. Called only after every action in
    plan_actions has already been executed successfully against the
    real page.
    """
    data = _load_raw(path)
    data["steps"] = [s for s in data.get("steps", []) if s.get("step_number") != step_number]
    data["steps"].extend(_plan_action_to_entry(a, step_number, locale) for a in plan_actions)
    _write_compiled(path, data)


def patch_partial_step(path, step_number, from_global_index, plan_actions, locale):
    """Like patch_step, but preserves every existing entry whose position
    in compiled_data["steps"] is BEFORE from_global_index completely
    untouched (byte-for-byte) — used when try_compiled_step's
    failing_global_index identifies the ONE specific entry that actually
    caused this run's failure, part-way through a multi-entry step.
    Entries before that point already executed successfully THIS SAME
    attempt (and were already compiled/proven before that), so they must
    never be regenerated, reordered, or rewritten — only the confirmed-
    broken entry (and anything after it, which never got a chance to run
    this attempt) is replaced with plan_actions.
    """
    data = _load_raw(path)
    kept = [
        s for i, s in enumerate(data.get("steps", []), start=1)
        if not (s.get("step_number") == step_number and i >= from_global_index)
    ]
    kept.extend(_plan_action_to_entry(a, step_number, locale) for a in plan_actions)
    data["steps"] = kept
    _write_compiled(path, data)


def mark_step_locale_unsafe(path, step_number, locale, type_hint="click"):
    """Write ONE placeholder entry for step_number when self-heal found no
    safe, locale-independent locator at all. try_compiled_step's
    eligibility pre-pass checks "type" (must be DIRECT_ELIGIBLE_TYPES) and
    then "locale_unsafe" before ever touching "selector" for an entry, so
    a placeholder with selector=None and a plain "click" type_hint is safe
    here — it always routes straight back to self-heal on every future
    run, on every locale, without KeyError'ing on a missing selector.
    """
    data = _load_raw(path)
    data["steps"] = [s for s in data.get("steps", []) if s.get("step_number") != step_number]
    data["steps"].append({
        "step_number": step_number,
        "type": type_hint if type_hint in DIRECT_ELIGIBLE_TYPES else "click",
        "selector": None,
        "clickBy": "selector",
        "framePathSelectors": [],
        "selectorNth": None,
        "hasDisambiguation": False,
        "matchCount": None,
        "optional": False,
        "onFailure": None,
        "locale_unsafe": True,
        "last_patched_by_agent": now_iso(),
        "patched_locales": [locale],
    })
    _write_compiled(path, data)


def bulk_replace_frame_path(path, old_frame_path, new_frame_path, locale):
    """Replace framePathSelectors == old_frame_path with new_frame_path on
    EVERY entry across the WHOLE compiled file that still uses the exact
    old value — not just the one step that happened to trigger the fix.
    Used both for a code-level pre-substitution of an already-known-bad
    frame path (see self_heal.presubstitute_frame_path, applied before
    any direct-execution attempt) and when self-heal itself resolves a
    frame-path issue via a knowledge-library-documented substitution —
    either way, once one occurrence is confirmed fixed, every other
    entry sharing the identical stale value gets the same fix in one
    write, so it's never rediscovered step by step. Returns the number
    of entries updated (0 if none matched — a no-op, safe to call
    speculatively).
    """
    data = _load_raw(path)
    count = 0
    now = now_iso()
    for entry in data.get("steps", []):
        if entry.get("framePathSelectors") == old_frame_path:
            entry["framePathSelectors"] = new_frame_path
            entry["last_patched_by_agent"] = now
            locales = set(entry.get("patched_locales") or [])
            locales.add(locale)
            entry["patched_locales"] = sorted(locales)
            count += 1
    if count:
        _write_compiled(path, data)
    return count


def entries_for_step(compiled_data, step_number):
    """Ordered (global_index, entry) pairs sharing this step_number (a case
    step may compile to multiple raw actions, e.g. fill then click).
    global_index is the entry's 1-based position in compiled_data["steps"],
    so log lines can point at exactly which JSON array element ran.
    """
    return [
        (i, s) for i, s in enumerate(compiled_data.get("steps", []), start=1)
        if s.get("step_number") == step_number
    ]


def resolve_frame_scope(page, frame_path_selectors):
    """Descend an ordered list of iframe selectors one hop at a time.
    Any hop that isn't an iframe (or times out) aborts the whole
    resolution — no partial fallback, matching the source tool.

    Re-resolved from scratch on every call (no caching across entries in
    the same step that share the same frame_path_selectors) — the
    per-hop timing log below is diagnostic instrumentation to quantify
    whether that redundancy is actually costly in practice.
    """
    scope = page
    for selector in frame_path_selectors or []:
        hop_start = time.monotonic()
        element = scope.wait_for_selector(selector, timeout=FRAME_HOP_TIMEOUT_MS)
        frame = element.content_frame()
        elapsed_ms = int((time.monotonic() - hop_start) * 1000)
        if elapsed_ms > SLOW_FRAME_HOP_LOG_THRESHOLD_MS:
            log(f"[TIMING] frame hop {selector!r} took {elapsed_ms}ms")
        if frame is None:
            raise RuntimeError(f"framePathSelectors: target is not an iframe: {selector!r}")
        scope = frame
    return scope


def build_locator(scope, entry):
    locator = scope.locator(entry["selector"])
    nth = entry.get("selectorNth")
    if isinstance(nth, int) and nth >= 0:
        locator = locator.nth(nth)
    return locator


def _type_replace(locator, value, timeout):
    """Clear a field by selecting-all + deleting via real keyboard events,
    then type the new value character-by-character via real key events
    (Locator.press_sequentially) — instead of .fill()'s bulk CDP-level
    insertText, which is what "replace" (and clear/append/prepend) use.

    Some rich/controlled-input fields (formula/code-editor-style inputs —
    e.g. a calculated-field expression editor, which auto-pairs
    brackets) implement JS-level logic that listens for individual
    keystrokes. A bulk-inserted value doesn't fire those per-key events
    the way real typing does, so such a field can react unpredictably (or
    not at all, then react to an already out-of-sync internal state) and
    end up with corrupted/duplicated content. Simulating real typing makes
    the field behave the same way it would for an actual human, at the
    cost of being slower than a plain .fill() — only worth it for fields
    that actually need it (see fillAction "type-replace" below), not a
    replacement for "replace" everywhere.
    """
    locator.click(timeout=timeout)
    locator.press(SELECT_ALL_KEY, timeout=timeout)
    locator.press("Delete", timeout=timeout)
    locator.press_sequentially(value, timeout=timeout)


def execute_entry(page, entry):
    """Perform one compiled entry's action against the real page. Raises
    on failure; callers decide how to interpret that (onFailure retry,
    optional-skip, or surfacing as a direct-execution failure).
    """
    scope = resolve_frame_scope(page, entry.get("framePathSelectors"))
    entry_type = entry.get("type")
    timeout = entry.get("timeout") or DEFAULT_ACTION_TIMEOUT_MS

    if entry_type == "click":
        build_locator(scope, entry).click(timeout=timeout)
    elif entry_type == "fill":
        locator = build_locator(scope, entry)
        value = entry.get("value", "")
        fill_action = entry.get("fillAction") or "replace"
        if fill_action == "clear":
            locator.fill("", timeout=timeout)
        elif fill_action == "append":
            current = locator.input_value(timeout=timeout)
            locator.fill(current + value, timeout=timeout)
        elif fill_action == "prepend":
            current = locator.input_value(timeout=timeout)
            locator.fill(value + current, timeout=timeout)
        elif fill_action == "type-replace":
            _type_replace(locator, value, timeout)
        else:  # "replace", or any unrecognized value
            locator.fill(value, timeout=timeout)
    elif entry_type == "check":
        locator = build_locator(scope, entry)
        if entry.get("checkMode") == "uncheck":
            locator.uncheck(timeout=timeout)
        else:
            locator.check(timeout=timeout)
    elif entry_type == "waitFor":
        scope.locator(entry["selector"]).wait_for(timeout=timeout)
    else:
        raise ValueError(f"unsupported compiled step type: {entry_type!r}")


def _condition_met(scope, condition):
    """elementExists with an empty/missing selector is treated as
    "always recover", not "never matches" — matches the source tool.
    """
    if not condition:
        return True
    if condition.get("type") == "elementExists":
        selector = condition.get("selector") or ""
        if not selector:
            return True
        return scope.locator(selector).count() > 0
    return False


def run_entry_with_onfailure(page, entry):
    """Run one compiled entry, applying its onFailure retry/recovery if
    enabled. Raises the last error if it never succeeds.
    """
    try:
        execute_entry(page, entry)
        return
    except Exception as primary_error:
        on_failure = entry.get("onFailure")
        if not on_failure or not on_failure.get("enabled"):
            raise

        retry = on_failure.get("retry") or {}
        max_retries = retry.get("maxRetries")
        max_retries = max_retries if isinstance(max_retries, int) and max_retries > 0 else 0

        last_error = primary_error
        for attempt_num in range(max_retries):
            retry_start = time.monotonic()
            try:
                scope = resolve_frame_scope(page, entry.get("framePathSelectors"))
            except Exception as e:
                last_error = e
                break
            if not _condition_met(scope, on_failure.get("condition")):
                break
            try:
                for action in on_failure.get("actions") or []:
                    if action.get("disabled"):
                        continue
                    execute_entry(page, action)
                execute_entry(page, entry)
                elapsed_ms = int((time.monotonic() - retry_start) * 1000)
                if elapsed_ms > SLOW_ENTRY_LOG_THRESHOLD_MS:
                    log(f"[TIMING] onFailure retry #{attempt_num + 1} succeeded after {elapsed_ms}ms")
                return
            except Exception as e:
                elapsed_ms = int((time.monotonic() - retry_start) * 1000)
                if elapsed_ms > SLOW_ENTRY_LOG_THRESHOLD_MS:
                    log(f"[TIMING] onFailure retry #{attempt_num + 1} failed after {elapsed_ms}ms: {e}")
                last_error = e
                continue
        raise last_error


def _frame_note(entry):
    frame_path = entry.get("framePathSelectors")
    return f" in frame {frame_path}" if frame_path else ""


def try_compiled_step(page, compiled_data, step_number):
    """Attempt a case step directly via its compiled entries. Returns
    (success, detail, failing_global_index). Prints a STEP-prefixed
    [COMPILED] progress line per entry (and per rejection reason) so
    console/log output shows exactly which entry in
    compiled_data["steps"] ran, succeeded, or triggered AI fallback.

    failing_global_index is the compiled_data["steps"] array position
    (1-based, matching entries_for_step's own numbering) of the ONE
    entry that actually failed at runtime — set ONLY for that case
    (a genuine mid-step runtime failure). It's None for every other
    outcome (success; no entries yet; an ineligible type; a
    locale_unsafe entry) — there, the whole step's compiled state is
    either absent or already known-unusable, so there's nothing
    "already working" a caller needs to avoid disturbing. Callers use
    this to patch ONLY the confirmed-broken entry (and whatever never
    got a chance to run after it) rather than regenerating the whole
    step, preserving any entries that already ran successfully earlier
    in this exact attempt untouched.
    """
    entries = entries_for_step(compiled_data, step_number)
    if not entries:
        log(f"STEP {step_number}: [COMPILED] no compiled entries yet -> falling back to AI")
        return False, "no-compiled: no entries for this step", None

    for global_index, entry in entries:
        if entry.get("type") not in DIRECT_ELIGIBLE_TYPES:
            log(f"STEP {step_number}: [COMPILED] entry #{global_index} type {entry.get('type')!r} "
                  f"not directly executable -> falling back to AI")
            return False, f"no-compiled: type {entry.get('type')!r} not directly executable yet", None
        if entry.get("locale_unsafe"):
            log(f"STEP {step_number}: [COMPILED] entry #{global_index} flagged locale_unsafe "
                  f"-> falling back to AI")
            return False, "no-compiled: entry flagged locale_unsafe", None

    n = len(entries)
    executed = []
    for j, (global_index, entry) in enumerate(entries, start=1):
        entry_type = entry.get("type")
        selector = entry.get("selector")
        frame_note = _frame_note(entry)
        entry_start = time.monotonic()
        try:
            run_entry_with_onfailure(page, entry)
            elapsed_ms = int((time.monotonic() - entry_start) * 1000)
            timing_note = f" ({elapsed_ms}ms)" if elapsed_ms > SLOW_ENTRY_LOG_THRESHOLD_MS else ""
            executed.append(entry_type)
            log(f"STEP {step_number}: [COMPILED] entry #{global_index} ({j}/{n}) "
                  f"{entry_type} {selector!r}{frame_note} -> OK{timing_note}")
        except Exception as e:
            elapsed_ms = int((time.monotonic() - entry_start) * 1000)
            timing_note = f" ({elapsed_ms}ms)" if elapsed_ms > SLOW_ENTRY_LOG_THRESHOLD_MS else ""
            if entry.get("optional") and entry_type in OPTIONAL_ELIGIBLE_TYPES:
                executed.append(f"{entry_type}(skipped-optional)")
                log(f"STEP {step_number}: [COMPILED] entry #{global_index} ({j}/{n}) "
                      f"{entry_type} {selector!r}{frame_note} -> SKIPPED (optional){timing_note}: {e}")
                continue
            log(f"STEP {step_number}: [COMPILED] entry #{global_index} ({j}/{n}) "
                  f"{entry_type} {selector!r}{frame_note} -> FAILED{timing_note}: {e} -> falling back to AI")
            return False, f"direct-failed: {entry_type} via {selector!r}: {e}", global_index

    return True, f"direct-success: {', '.join(executed)}", None
