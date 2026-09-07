"""Phase 3 (Self-heal): per-step recovery when a compiled entry isn't
directly executable or fails at runtime.

Unlike the old CDP-attach fallback, the model here gets NO browser/tool
access at all — it never touches the page. Our own already-authenticated
Playwright page (owned by direct_executor.py) captures a snapshot
(accessibility tree text + screenshot image) and relevant shared
knowledge-library context, sends all of it to `claude -p` as a single
plain text+vision completion (no MCP, no tools), and gets back a
structured JSON action plan. Our own code validates every selector in
that plan against the locator-exclusion rules (LOCATOR_SELECTOR_RULE in
prompts/system_prompt.py, ported to regex form below) before executing
it via compiled_steps.execute_entry, then patches compiled/<case_name>.json
directly (see compiled_steps.patch_step). This closes the prompt-only
enforcement gap called out in the README: the model can still propose an
unsafe selector, but our own code will never accept one.

Invocation mechanics (verified empirically against the installed CLI):
  claude -p --input-format stream-json --output-format stream-json
    --verbose --tools "" --strict-mcp-config --json-schema '<schema>'
stdin: one JSONL line, a user message with a text content block (the
prompt) and an image content block (base64 PNG). --strict-mcp-config
plus --tools "" together guarantee zero MCP servers and zero tool access
("mcp_servers":[] / "tools":["StructuredOutput"] confirmed in the
session's own init line) — a pure request/response, not an agent
session. --output-format stream-json (required whenever --input-format
is stream-json) emits multiple JSON lines; the line with "type":"result"
carries the schema-validated answer in its "structured_output" field.
"""

import base64
import json
import re
import subprocess
import time

import compiled_steps
from prompts.system_prompt import SINGLE_STEP_SNAPSHOT_PROMPT_TEMPLATE

MAX_FALLBACK_ROUNDS = 7

# Generous enough to absorb a slow/animating page without a needless
# empty snapshot, but this is a one-shot best-effort wait, never a hard
# requirement — self-heal's job is to see *some* real, current state,
# not a perfectly settled one.
SNAPSHOT_NETWORKIDLE_TIMEOUT_MS = 3000

FALLBACK_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["plan", "notes", "step_complete"],
    "additionalProperties": False,
    "properties": {
        "plan": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "selector"],
                "additionalProperties": False,
                "properties": {
                    "type": {"enum": ["click", "fill", "check", "waitFor"]},
                    "selector": {"type": ["string", "null"]},
                    "selectorNth": {"type": ["integer", "null"]},
                    "framePathSelectors": {"type": "array", "items": {"type": "string"}},
                    "timeout": {"type": ["integer", "null"]},
                    "value": {"type": ["string", "null"]},
                    "fillAction": {"type": ["string", "null"]},
                    "checkMode": {"type": ["string", "null"]},
                },
            },
        },
        "knowledge_library_suggestion": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "pattern_name": {"type": "string"},
                "description": {"type": "string"},
                "selector": {"type": ["string", "null"]},
                "frame_path": {"type": ["object", "null"]},
                "gotcha": {"type": ["string", "null"]},
                "generic_widget": {"type": "boolean"},
            },
        },
        "state_mismatch": {
            "type": ["boolean", "null"],
        },
        "step_complete": {
            "type": "boolean",
        },
        "notes": {"type": "string"},
    },
}

# Product-level widgets confirmed obvious/safe-to-record-on-sight without
# needing cross-case confirmation first (see maybe_accept_knowledge_suggestion) —
# matches the real entries already present in knowledge/locator-library.json.
ALWAYS_SAFE_PATTERN_NAMES = {
    "app_main_iframe",
    "workflow_next_button",
    "meatball_menu_button",
    "meatball_menu_activate_item",
}


def _ts():
    return time.strftime("%H:%M:%S")


def log(msg):
    print(f"[{_ts()}] {msg}")


# --------------------------------------------------------------------------
# Selector validation — Python port of LOCATOR_SELECTOR_RULE
# (prompts/system_prompt.py). Anything matching these is rejected outright;
# this is deliberately conservative (a false rejection just costs one more
# self-heal round; a false acceptance poisons the compiled file).
# --------------------------------------------------------------------------

DYNAMIC_ID_RE = re.compile(r'#?:?r[0-9a-z]+:|react-select-\d+|[a-zA-Z][a-zA-Z-]*-\d{10,}')
HASH_CLASS_RE = re.compile(r'\.(css-[a-z0-9]+|sc-[A-Za-z0-9]+)\b')
LONG_DIGIT_RUN_RE = re.compile(r'\d{9,}')
FORBIDDEN_ATTR_RE = re.compile(r'aria-label\s*=|placeholder\s*=|\balt\s*=|text\s*=|:has-text\(')


def _selector_is_safe(selector):
    """True if selector doesn't match any known-unsafe pattern. A missing/
    empty selector is never "safe" to execute (though it's fine as a
    locale_unsafe placeholder written by mark_step_locale_unsafe, which
    doesn't call this function).
    """
    if not selector or not isinstance(selector, str):
        return False
    if DYNAMIC_ID_RE.search(selector):
        return False
    if HASH_CLASS_RE.search(selector):
        return False
    if LONG_DIGIT_RUN_RE.search(selector):
        return False
    if FORBIDDEN_ATTR_RE.search(selector):
        return False
    return True


def validate_plan_selectors(plan):
    """Validate every selector/framePathSelectors entry across the WHOLE
    plan. Returns (ok, reason) — reason is empty on success. Any single
    violation rejects the whole plan; we never execute a partially-valid
    plan or guess a fix.
    """
    for i, action in enumerate(plan):
        selector = action.get("selector")
        if not _selector_is_safe(selector):
            return False, f"action {i} ({action.get('type')!r}): unsafe or missing selector {selector!r}"
        for frame_selector in action.get("framePathSelectors") or []:
            if not _selector_is_safe(frame_selector):
                return False, f"action {i} ({action.get('type')!r}): unsafe frame path selector {frame_selector!r}"
    return True, ""


# A field naming convention observed on autocomplete/combobox-style
# inputs (see the mapping_attribute_selection_modal and
# schema_attribute_selection_dialog knowledge-library entries) — typing
# into one of these is a legitimate way to trigger its suggestion list,
# but must never be the FINAL action: it's a filter, not a value.
AUTOCOMPLETE_FILL_HINT_RE = re.compile(r'auto-?complete', re.IGNORECASE)


def validate_plan_completes_selections(plan):
    """Reject a plan that types into an autocomplete/combobox-style input
    but never follows up with a real click confirming an actual rendered
    option afterward. This is the code-level enforcement behind
    "requires_selection" (see schema_attribute_selection_dialog in the
    knowledge library) — a `fill` with no following `click` in the same
    plan is exactly how a previous run fabricated non-existent attribute
    values ("a", "email", "id") that then failed real-world schema
    validation, instead of the typed text being just a filter en route to
    choosing a REAL rendered option. Returns (ok, reason).
    """
    for i, action in enumerate(plan):
        if action.get("type") == "fill" and AUTOCOMPLETE_FILL_HINT_RE.search(action.get("selector") or ""):
            if not any(a.get("type") == "click" for a in plan[i + 1:]):
                return False, (
                    f"action {i} fills an autocomplete-style input ({action.get('selector')!r}) "
                    f"but the plan never clicks a real rendered option afterward — typing must be "
                    f"followed by selecting an actual option, never left as if the typed text itself "
                    f"were the final value"
                )
    return True, ""


# Matches a click on ANY tab within the calculated-field formula editor's
# tab rail (e.g. "calculatedFieldDialog.tabs.field", "...tabs.operator",
# "...tabs.function") — used to track which tab is currently active, not
# just the Field tab specifically, so switching to a DIFFERENT tab
# correctly clears "the Field tab is open" state instead of leaking it
# across an unrelated tab switch.
CALCULATED_FIELD_TAB_SWITCH_RE = re.compile(r'\.tabs\.\w+', re.IGNORECASE)
CALCULATED_FIELD_TAB_FIELD_RE = re.compile(r'\.tabs\.field\b', re.IGNORECASE)

# Matches a fill/type-replace target that's plausibly the calculated-field
# formula editor itself (see calculated_field_editor_field_insertion in
# the knowledge library) — deliberately broad (a bare "textarea" hint
# included, not just "calculated-field"/"codemirror") since self-heal
# invents a different structural selector for this same editor almost
# every round (anchored off different stable siblings/CodeMirror internals
# — confirmed empirically across three different real selectors in one
# single run), so no single literal string reliably identifies it.
CALCULATED_FIELD_EDITOR_FILL_HINT_RE = re.compile(r'calculated-?field|codemirror|textarea', re.IGNORECASE)


def validate_calculated_field_insertions(plan, executed_actions=None):
    """Reject a plan that types/type-replaces text into the calculated-field
    formula editor to insert a FIELD reference, without a real click on an
    actual, rendered field-list item (from the Field tab) happening first.
    This is the same failure category as validate_plan_completes_selections
    above, extended to a third UI surface that doesn't use the word
    "autocomplete" anywhere in its own markup (see
    calculated_field_editor_field_insertion in the knowledge library) — a
    previous run clicked the Field tab (correctly, in one round), but a
    LATER round then typed a fabricated field name ("_test") directly into
    the formula instead of ever clicking a real field-list item, which
    then failed real-world schema validation downstream ("The attribute
    _test does not exist in input schema").

    `executed_actions` (the accumulated, already-executed actions from
    EARLIER self-heal rounds for this same step, if any) is checked
    together with the current `plan` as one continuous history, since the
    Field tab click and the eventual (correct-or-not) fill/type-replace
    commonly happen in DIFFERENT rounds, not the same plan — a check
    scoped to `plan` alone would miss exactly the cross-round pattern that
    caused this bug. Functions ("upper(...)") and operators ("+") typed
    directly are never flagged by this check on their own — only a fill
    that lands while the Field tab is the active tab, before any real
    click has happened since it was opened, is rejected. Returns
    (ok, reason).
    """
    history = list(executed_actions or []) + list(plan)

    field_tab_active = False
    real_selection_since_tab = False
    for action in history:
        selector = action.get("selector") or ""
        action_type = action.get("type")

        if action_type == "click" and CALCULATED_FIELD_TAB_SWITCH_RE.search(selector):
            # Switching tabs (to the Field tab, or away from it to
            # Function/Operator) always resets whether a real selection
            # has happened on the (possibly new) active tab.
            field_tab_active = bool(CALCULATED_FIELD_TAB_FIELD_RE.search(selector))
            real_selection_since_tab = False
            continue

        if action_type == "click":
            # A click on the formula editor itself (to focus it / position
            # the cursor before typing) is NOT a field selection — this is
            # exactly what a previous run did: click Field tab, click INTO
            # the editor to focus it, then type a fabricated field name,
            # which would otherwise have looked like "a click happened
            # after the tab" and slipped past this check.
            if field_tab_active and not CALCULATED_FIELD_EDITOR_FILL_HINT_RE.search(selector):
                real_selection_since_tab = True
            continue

        if (action_type == "fill" and field_tab_active and not real_selection_since_tab
                and CALCULATED_FIELD_EDITOR_FILL_HINT_RE.search(selector)):
            return False, (
                f"action fills the calculated-field formula editor ({selector!r}) while the Field "
                f"tab is open, but no real click on a rendered field-list item happened first — a "
                f"field reference must come from clicking an actual list item, never from typing/"
                f"inventing a field name directly (this is exactly how a previous run fabricated a "
                f"non-existent field, \"_test\", that then failed real-world schema validation)"
            )
    return True, ""


# --------------------------------------------------------------------------
# Snapshot capture
# --------------------------------------------------------------------------

INTERCEPTION_ERROR_RE = re.compile(
    r"intercepts pointer events|element is not attached|outside of the viewport",
    re.IGNORECASE,
)


def _looks_like_interception(exc):
    """True if exc looks like a transient blocked-runtime-state failure
    (an overlapping dialog/overlay intercepting clicks, a detached node,
    etc.) rather than the target simply not existing/matching. This
    distinction matters: a locator that's genuinely wrong should still
    surface as a normal runtime failure (or eventually locale_unsafe, if
    the model can never find one) — but an interception error means the
    selector was fine and something else was just in the way.
    """
    return bool(INTERCEPTION_ERROR_RE.search(str(exc)))


def execute_plan_with_recovery(page, plan):
    """Execute plan's actions in order via compiled_steps.execute_entry.
    If an action fails with what looks like an intercepting overlay/dialog
    (not an ordinary "no such element" locator problem), press Escape
    once (dismiss_stray_overlays) and retry that SAME action before
    giving up — a cheap, immediate recovery for a transient blocked
    runtime state that doesn't need another full snapshot/LLM round
    trip. Raises the original (or retry) exception if it still fails.
    """
    for action in plan:
        try:
            compiled_steps.execute_entry(page, action)
        except Exception as e:
            if not _looks_like_interception(e):
                raise
            dismiss_stray_overlays(page)
            compiled_steps.execute_entry(page, action)


def dismiss_stray_overlays(page):
    """Best-effort Escape keypress to close a dropdown/menu/modal left open
    from a previous action (e.g. a compiled step that opened a meatball
    menu the current step doesn't actually need) BEFORE trusting the DOM
    as a clean slate. Never raises, never assumed to have worked — it's a
    cheap mitigation, not a guarantee. A keyboard event bypasses the
    pointer-event interception such overlays' own dismiss-backdrop layer
    causes for ordinary clicks (including a click on the very toggle that
    opened it), which a click-based recovery action can't reliably do.
    """
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def knowledge_frame_path_candidates(knowledge_path):
    """Every distinct frame_path.selectors list recorded anywhere in the
    knowledge library (e.g. app_main_iframe's bare ["iframe"]) — used as
    fallback candidates when a step's own recorded frame path breaks.
    These are exactly the kind of already-documented, cross-case-confirmed
    structural fixes (see the app_main_iframe gotcha: a locale-dependent
    title-based path should be replaced with a bare structural one) that
    would otherwise require live DOM access to rediscover. Never raises;
    returns [] if the library is missing/empty.
    """
    try:
        with open(knowledge_path, "r", encoding="utf-8") as f:
            library = json.load(f)
    except Exception:
        return []

    seen = []
    for entry in library.get("patterns", {}).values():
        selectors = (entry.get("frame_path") or {}).get("selectors")
        if selectors and selectors not in seen:
            seen.append(selectors)
    return seen


# Narrow, pattern-based — NOT a general "always prefer bare iframe"
# override. Matches specifically the shape of frame path the
# app_main_iframe knowledge-library entry documents as locale-translated
# and therefore unsafe (an iframe selector keyed on its title/name
# attribute), so a known-good substitute can be tried in plain code,
# zero LLM involvement, before ever attempting the possibly-stale
# recorded path.
TRANSLATABLE_IFRAME_ATTR_RE = re.compile(r'iframe\[(title|name)\s*=', re.IGNORECASE)


def presubstitute_frame_path(frame_path_selectors, knowledge_path):
    """If frame_path_selectors matches the known-risky
    iframe[title=...]/iframe[name=...] pattern AND the knowledge library
    has a recorded safe frame_path candidate, return that candidate to
    try FIRST — entirely in code, before ever attempting the recorded
    (possibly stale) frame path. Returns None if no substitution applies
    (caller should use frame_path_selectors unmodified in that case).
    """
    if not frame_path_selectors:
        return None
    if not any(TRANSLATABLE_IFRAME_ATTR_RE.search(s) for s in frame_path_selectors):
        return None
    candidates = knowledge_frame_path_candidates(knowledge_path)
    return candidates[0] if candidates else None


HTML_CAPTURE_MAX_CHARS = 100000


def _capture_html(scope):
    """Real DOM HTML for one scope — unlike aria_snapshot() (role +
    accessible-name + state only, never data-test-id/id/class), this is
    the only source self-heal has for actually discovering an attribute on
    a target or its ancestors. Truncated defensively (never raises) since
    a body can be large; correctness (the model seeing SOME real
    attributes) matters more here than trimming precisely.

    When truncation is needed, keeps BOTH the start and the end of the
    HTML (splitting the budget), not just the start — a portal-rendered
    dialog/overlay is typically appended as one of the LAST children of
    <body>, so a head-only truncation would systematically cut off
    exactly the kind of element self-heal most needs to see.
    """
    try:
        html = scope.locator("body").inner_html()
    except Exception as e:
        return f"(could not capture HTML: {e})"
    if len(html) <= HTML_CAPTURE_MAX_CHARS:
        return html
    half = HTML_CAPTURE_MAX_CHARS // 2
    return (
        html[:half]
        + "\n... (truncated — DOM larger than capture limit; showing START and END, "
          "since portal-rendered dialogs/overlays are typically appended near the END) ...\n"
        + html[-half:]
    )


def _capture_html_wide(page, scope):
    """Real DOM HTML, widened to cover both the resolved frame scope AND
    the top-level page body, when they differ. A dialog/overlay is often
    rendered via a React portal appended directly to the document's OWN
    <body> — which may be the top-level page's body, NOT the iframe body
    a step's target normally lives in — so scoping HTML capture to only
    the resolved container can silently miss exactly the element a
    self-heal round most needs to see (e.g. a modal's close/cancel
    button). Rather than trying to precisely track down a given dialog's
    portal target (fragile, differs dialog to dialog), just capture both
    candidates and label them clearly; the model reconciles using the
    screenshot for visual confirmation of what's actually on screen.
    """
    frame_html = _capture_html(scope)
    if scope is page:
        return f"=== HTML of <body> (full page; no iframe scope active) ===\n{frame_html}"

    page_html = _capture_html(page)
    return (
        f"=== HTML inside the resolved frame scope's <body> ===\n{frame_html}\n\n"
        f"=== HTML of the TOP-LEVEL page's <body> (outside any iframe) — "
        f"dialogs/overlays/portals are often appended here even when the "
        f"step's own target lives inside the iframe above ===\n{page_html}"
    )


def capture_snapshot(page, frame_path_selectors, fallback_frame_path_candidates=None):
    """Returns (aria_text, html_text, screenshot_bytes, scope_note,
    resolved_frame_path) for the CURRENT page state, scoped into
    frame_path_selectors if given (same frames the broken compiled entries
    specified). resolved_frame_path is whichever frame path actually
    worked (may differ from frame_path_selectors, if a fallback candidate
    had to be used instead), or None if nothing resolved (full-page
    scope). Best-effort settle wait first — never blocks indefinitely,
    never raises on timeout.

    html_text is real DOM markup for the same scope (see _capture_html) —
    the accessibility snapshot alone never contains data-test-id/id/class
    attributes for the target or any ancestor, so without this, self-heal's
    "walk up to find a stable ancestor" technique can only ever succeed
    when a matching selector happens to already be documented in the
    knowledge library. This is the primary source for discovering a
    genuinely new attribute; the aria snapshot and screenshot remain
    useful for role/visual/layout context alongside it.

    frame_path_selectors is exactly the frame path THIS step is broken
    because of, in the common case (e.g. a locale-dependent selector like
    iframe[title="Main Content"] that never resolves outside en) — so it
    may itself fail to resolve. Rather than giving up straight to a
    frame-less full-page snapshot (which can't see anything inside that
    iframe at all), try fallback_frame_path_candidates (see
    knowledge_frame_path_candidates) first — these are already-documented,
    known-good structural alternatives, most commonly the single
    bare-structural "iframe" pattern this whole codebase's real
    knowledge library already records for exactly this situation. Only
    once every candidate also fails does this fall back to the full page.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=SNAPSHOT_NETWORKIDLE_TIMEOUT_MS)
    except Exception:
        pass  # proceed with a best-effort snapshot regardless

    attempts = []
    if frame_path_selectors:
        attempts.append(frame_path_selectors)
    for candidate in fallback_frame_path_candidates or []:
        if candidate not in attempts:
            attempts.append(candidate)

    scope, scope_note, resolved_frame_path = page, "full page", None
    tried_and_failed = []
    for attempt in attempts:
        try:
            scope = compiled_steps.resolve_frame_scope(page, attempt)
            scope_note = f"inside frame path {attempt}" + (
                f" (the previously-recorded frame path {frame_path_selectors} did NOT resolve; "
                f"this is a fallback candidate from the knowledge library)"
                if attempt != frame_path_selectors else ""
            )
            resolved_frame_path = attempt
            break
        except Exception as e:
            tried_and_failed.append((attempt, e))

    if resolved_frame_path is None and tried_and_failed:
        failures = "; ".join(f"{sel!r} ({e})" for sel, e in tried_and_failed)
        scope_note = (
            f"full page — none of these frame paths resolved: {failures}. "
            f"They are likely stale/locale-dependent; do not reuse them as-is."
        )

    aria_text = scope.locator("body").aria_snapshot()
    html_text = _capture_html_wide(page, scope)
    # Screenshot captured last, same round, same page state as the HTML/
    # aria snapshot above (no navigation/action happens between these
    # calls) — the model can visually cross-check what's actually on
    # screen against what the HTML capture did or didn't include.
    screenshot_bytes = page.screenshot()
    return aria_text, html_text, screenshot_bytes, scope_note, resolved_frame_path


def lookup_knowledge_entries(knowledge_path, step_description, max_entries=3, frame_path_broke=False):
    """Simple case-insensitive keyword match of step_description words
    against each knowledge-library pattern's name/description — the
    library is small, so this doesn't need to be fancier than that.
    Returns formatted text (never raises; an empty/missing file just
    yields "no matches").

    When frame_path_broke is True (this round's previously-recorded
    frame path failed to resolve), ALSO force-includes every pattern that
    carries a "frame_path" key, regardless of keyword score — the step's
    own wording (e.g. "Click on 'Edit schedule' button") has no reason to
    match a structural pattern name like "app_main_iframe", but that's
    exactly the kind of already-documented gotcha (e.g. "don't use this
    iframe's title, it's locale-translated — use a bare structural
    selector instead") this situation needs, and a pure keyword match
    would otherwise never surface it.
    """
    try:
        with open(knowledge_path, "r", encoding="utf-8") as f:
            library = json.load(f)
    except Exception:
        return "(knowledge library unavailable)"

    patterns = library.get("patterns", {})
    words = {w for w in re.findall(r"[a-z0-9]+", step_description.lower()) if len(w) > 2}

    scored = []
    forced_names = set()
    for name, entry in patterns.items():
        haystack = f"{name} {entry.get('description', '')}".lower()
        score = sum(1 for w in words if w in haystack)
        if frame_path_broke and entry.get("frame_path"):
            score += 100  # always sort ahead, but still shown with its real content
            forced_names.add(name)
        if score > 0:
            scored.append((score, name, entry))

    if not scored:
        return "(no obviously relevant entries found for this step)"

    scored.sort(key=lambda t: t[0], reverse=True)
    keep = max_entries + len(forced_names)  # never crowd out a forced match
    lines = []
    for _, name, entry in scored[:keep]:
        lines.append(f"- {name}: {json.dumps(entry)}")
    return "\n".join(lines)


def build_prompt(case_name, env, locale, step_number, step_description,
                  aria_text, html_text, scope_note, knowledge_entries_text,
                  previous_attempt_context=""):
    return SINGLE_STEP_SNAPSHOT_PROMPT_TEMPLATE.format(
        case_name=case_name, env=env, locale=locale,
        step_number=step_number, step_description=step_description,
        aria_snapshot=aria_text, dom_html=html_text, snapshot_scope_note=scope_note,
        knowledge_entries_text=knowledge_entries_text,
        previous_attempt_context=(
            previous_attempt_context or "(none — this is the first attempt this step)"
        ),
    )


# --------------------------------------------------------------------------
# claude -p invocation (plain text+vision completion, no MCP/tools)
# --------------------------------------------------------------------------

_CODE_FENCE_RE = re.compile(r'^```(?:json)?\s*|\s*```$', re.MULTILINE)


def _strip_code_fences(text):
    return _CODE_FENCE_RE.sub('', text).strip()


def invoke_self_heal(prompt, screenshot_bytes, timeout):
    """Runs the plain text+vision completion described in the module
    docstring. Returns the parsed response dict, or None on timeout/
    unparseable output (caller treats None as "this round produced
    nothing usable").

    screenshot_bytes is base64-encoded directly into the stream-json
    stdin message below and never written to disk — this diagnostic
    screenshot must stay fully separate from the permanent per-step
    artifact Phase 2 writes to results/<case>/<env>/<locale>/screenshots/
    ({case_name}_{NN}.png). Keep it that way if this function ever needs
    a file path instead of inline bytes (e.g. a future CLI requirement):
    use a temp file that's deleted right after this call, never a path
    under results/.
    """
    message = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(screenshot_bytes).decode("ascii"),
                    },
                },
            ],
        },
    }
    stdin_payload = json.dumps(message) + "\n"

    cmd = [
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--tools", "",
        "--strict-mcp-config",
        "--json-schema", json.dumps(FALLBACK_RESPONSE_SCHEMA),
    ]
    try:
        result = subprocess.run(
            cmd, input=stdin_payload, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log("SELF-HEAL: claude -p invocation timed out")
        return None

    result_line = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if parsed.get("type") == "result":
            result_line = parsed
    if result_line is None:
        log(f"SELF-HEAL: no parseable result line in claude -p output "
            f"(stderr: {result.stderr.strip()[:500]!r})")
        return None

    structured = result_line.get("structured_output")
    if isinstance(structured, dict):
        return structured

    # Defensive fallback — same lesson as the old STEP-line parsing bug:
    # models sometimes wrap JSON in code fences even when told not to.
    raw = result_line.get("result")
    if not raw:
        return None
    try:
        return json.loads(_strip_code_fences(raw))
    except json.JSONDecodeError:
        log(f"SELF-HEAL: could not parse 'result' as JSON either: {raw[:500]!r}")
        return None


# --------------------------------------------------------------------------
# Knowledge-library suggestion acceptance (write-sparingly rule, in code)
# --------------------------------------------------------------------------

def maybe_accept_knowledge_suggestion(knowledge_path, suggestion, case_name, locale):
    """Returns (accepted, reason). Never raises — a bad/missing suggestion
    is just not accepted, it never fails the overall self-heal attempt
    (the step itself already succeeded by the time this is called).
    """
    if not suggestion:
        return False, "no suggestion offered"
    if not suggestion.get("generic_widget"):
        return False, "not marked generic_widget"

    name = suggestion.get("pattern_name")
    if not name:
        return False, "missing pattern_name"

    selector = suggestion.get("selector")
    if selector is not None and not _selector_is_safe(selector):
        return False, f"selector fails validation: {selector!r}"
    frame_path = suggestion.get("frame_path") or {}
    for frame_selector in frame_path.get("selectors") or []:
        if not _selector_is_safe(frame_selector):
            return False, f"frame_path selector fails validation: {frame_selector!r}"

    try:
        with open(knowledge_path, "r", encoding="utf-8") as f:
            library = json.load(f)
    except Exception as e:
        return False, f"could not read knowledge library: {e}"

    patterns = library.setdefault("patterns", {})
    existing = patterns.get(name)

    cross_case_confirmed = bool(
        existing
        and selector is not None
        and existing.get("selector") == selector
        and any(c != case_name for c in existing.get("seen_in_cases", []))
    )

    if name not in ALWAYS_SAFE_PATTERN_NAMES and not cross_case_confirmed:
        return False, "not cross-case-confirmed and not in the always-safe allowlist"

    entry = existing or {}
    entry["description"] = suggestion.get("description", entry.get("description", ""))
    if selector is not None:
        entry["selector"] = selector
    if frame_path:
        entry["frame_path"] = frame_path
    if suggestion.get("gotcha"):
        entry["gotcha"] = suggestion["gotcha"]
    seen_in_cases = set(entry.get("seen_in_cases", []))
    seen_in_cases.add(case_name)
    entry["seen_in_cases"] = sorted(seen_in_cases)
    verified_locales = set(entry.get("verified_locales", []))
    verified_locales.add(locale)
    entry["verified_locales"] = sorted(verified_locales)
    entry.setdefault("confidence", "medium")
    patterns[name] = entry
    library["patterns"] = patterns
    library["last_updated"] = compiled_steps.now_iso()

    with open(knowledge_path, "w", encoding="utf-8") as f:
        json.dump(library, f, indent=2)
    return True, "accepted"


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def self_heal_step(page, case_name, env, locale, step_number, step_description,
                    compiled_path, knowledge_path, fallback_timeout, frame_path_selectors=None,
                    failing_global_index=None):
    """Runs up to MAX_FALLBACK_ROUNDS snapshot->plan->execute rounds for
    one scenario step. failing_global_index (see
    compiled_steps.try_compiled_step) identifies the ONE existing entry
    that actually caused this run's failure, if this was a genuine
    mid-step runtime failure — when set, success patches ONLY that entry
    onward (compiled_steps.patch_partial_step), leaving every entry
    before it (already proven working, including having already executed
    successfully earlier in this exact attempt) completely untouched.
    When None (no entries yet, or the existing ones were already
    ineligible/locale_unsafe), there's nothing already-working to
    preserve, so the whole step is compiled fresh (compiled_steps.patch_step).
    Returns (ok, detail):
      - (True, "...") on success (compiled file already patched with the
        FULL accumulated sequence of actions across every round that
        contributed to it, not just the last round's plan — a step that
        needed several rounds of observe-then-act improvisation would
        otherwise only get its LAST round's actions recorded, which
        wouldn't reproduce the full sequence from a clean start on a
        future direct-execution run).
      - (False, "locale_unsafe: ...") if no safe locator was ever found —
        compiled_steps.mark_step_locale_unsafe has already been called;
        this is one legitimate stop condition for this run.
      - (False, "STATE_MISMATCH: ...") if the model reports the page/
        workflow state doesn't match what the step describes at all
        (wrong wizard step, wrong tab, wrong page) — distinct from
        "no locator exists": nothing is patched or flagged locale_unsafe,
        since a locator would likely resolve fine once actually on the
        right page; this just isn't that run.
      - (False, "...") on any other exhaustion (bad plans / runtime
        failures every round) — existing compiled entries (if any) are
        left untouched; this run should also stop, just without writing
        locale_unsafe.
    """
    last_reason = "self-heal produced no usable plan"
    previous_attempt_context = ""
    original_frame_path_selectors = frame_path_selectors
    executed_actions = []

    for round_num in range(1, MAX_FALLBACK_ROUNDS + 1):
        # A prior action (this step's own broken compiled entry, or a
        # previous round's failed plan) may have left a dropdown/menu/modal
        # open that visually and structurally covers the real target —
        # don't assume the current DOM is a clean slate.
        dismiss_stray_overlays(page)
        candidates = knowledge_frame_path_candidates(knowledge_path)
        try:
            aria_text, html_text, screenshot_bytes, scope_note, resolved_frame_path = capture_snapshot(
                page, frame_path_selectors, fallback_frame_path_candidates=candidates)
        except Exception as e:
            last_reason = f"failed to capture snapshot: {e}"
            log(f"STEP {step_number}: [SELF-HEAL] round {round_num}: {last_reason}")
            continue

        frame_path_broke = bool(frame_path_selectors) and resolved_frame_path != frame_path_selectors
        if frame_path_broke:
            log(f"STEP {step_number}: [SELF-HEAL] round {round_num}: recorded frame path "
                f"{frame_path_selectors} did not resolve — "
                + (f"using knowledge-library fallback {resolved_frame_path} instead"
                   if resolved_frame_path else "no working fallback found either"))
            frame_path_selectors = resolved_frame_path

        knowledge_text = lookup_knowledge_entries(
            knowledge_path, step_description, frame_path_broke=frame_path_broke)
        prompt = build_prompt(case_name, env, locale, step_number, step_description,
                               aria_text, html_text, scope_note, knowledge_text,
                               previous_attempt_context)

        log(f"STEP {step_number}: [SELF-HEAL] round {round_num}/{MAX_FALLBACK_ROUNDS}: "
            f"invoking claude -p (snapshot scope: {scope_note})...")
        response = invoke_self_heal(prompt, screenshot_bytes, fallback_timeout)
        if response is None:
            last_reason = "self-heal invocation produced no parseable response"
            previous_attempt_context = ""
            continue

        notes = response.get("notes", "")
        if notes:
            log(f"STEP {step_number}: [SELF-HEAL] notes: {notes}")

        if response.get("state_mismatch"):
            reason = f"STATE_MISMATCH: {notes or 'page/workflow state does not match what the step describes'}"
            log(f"STEP {step_number}: [SELF-HEAL] {reason}")
            return False, reason

        plan = response.get("plan") or []
        if not plan:
            if response.get("step_complete") is False:
                # An empty plan + step_complete=false is NOT a locator
                # failure — it means "nothing is safely actionable YET"
                # (e.g. a grid still loading, no rows rendered at all
                # to build a selector against), and the model is asking
                # to be re-invoked against a fresh snapshot rather than
                # being marked locale_unsafe. Only an empty plan WITHOUT
                # this explicit "retry me" signal means genuinely no
                # locator exists.
                last_reason = f"self-heal reports nothing safely actionable yet, retrying - {notes or 'no reason given'}"
                log(f"STEP {step_number}: [SELF-HEAL] round {round_num}: {last_reason}")
                previous_attempt_context = (
                    f"A PREVIOUS attempt this step found nothing safely actionable yet and took NO "
                    f"action, reasoning: {notes or '(no reason given)'}\n"
                    f"If the current snapshot below still shows the same transient/loading state, "
                    f"it's fine to report the same thing again. If it has now rendered further, "
                    f"proceed with a real plan."
                )
                continue

            compiled_steps.mark_step_locale_unsafe(compiled_path, step_number, locale)
            reason = f"locale_unsafe: no safe locator found - {notes or 'no reason given'}"
            log(f"STEP {step_number}: [SELF-HEAL] {reason}")
            return False, reason

        ok, why = validate_plan_selectors(plan)
        if not ok:
            last_reason = f"fallback plan rejected by selector validation: {why}"
            log(f"STEP {step_number}: [SELF-HEAL] round {round_num}: {last_reason}")
            previous_attempt_context = ""
            continue

        # Checked against the FULL cross-round history (not just this
        # round's plan), and unconditionally (not gated on step_complete
        # like the check below) — the bug this guards against spans
        # rounds (Field tab clicked in one round, fabricated field typed
        # in a LATER round), and there's no legitimate "type a field name
        # now, a later round will click to confirm it" pattern the way
        # there is for autocomplete filtering below.
        ok, why = validate_calculated_field_insertions(plan, executed_actions)
        if not ok:
            last_reason = f"fallback plan rejected: {why}"
            log(f"STEP {step_number}: [SELF-HEAL] round {round_num}: {last_reason}")
            previous_attempt_context = (
                f"A PREVIOUS plan this attempt was REJECTED before execution (nothing ran): {why}\n"
                f"Click an actual, currently-rendered field-list item from the Field tab instead — "
                f"do not type a field name directly into the formula editor."
            )
            continue

        # Only enforce "a fill must be followed by a real click" when the
        # model claims THIS round already finishes the step
        # (step_complete: true) — an intentionally partial round
        # (step_complete: false, e.g. "type to trigger suggestions, a
        # later round will pick one") is exactly the legitimate multi-
        # round improvisation pattern and must NOT be rejected here.
        if response.get("step_complete") is not False:
            ok, why = validate_plan_completes_selections(plan)
            if not ok:
                last_reason = f"fallback plan rejected: {why}"
                log(f"STEP {step_number}: [SELF-HEAL] round {round_num}: {last_reason}")
                previous_attempt_context = (
                    f"A PREVIOUS plan this attempt was REJECTED before execution (nothing ran): {why}\n"
                    f"If you still need to filter/trigger suggestions by typing, keep that action, but "
                    f"ALSO include a real click on an actual rendered option afterward in the same plan "
                    f"if you're claiming step_complete=true — or, if you genuinely can't select a real "
                    f"option yet, set step_complete=false instead (that's a normal, unrejected outcome)."
                )
                continue

        try:
            execute_plan_with_recovery(page, plan)
        except Exception as e:
            last_reason = f"plan action failed at runtime: {e}"
            interception = _looks_like_interception(e)
            log(f"STEP {step_number}: [SELF-HEAL] round {round_num}: {last_reason} -> re-planning from fresh state")
            previous_attempt_context = (
                f"A PREVIOUS attempt just now used this exact plan (against a snapshot "
                f"taken moments before this one): {json.dumps(plan)}\n"
                f"It failed at runtime with: {e}\n"
                + (
                    "This looks like an interception/overlay failure (something else was "
                    "covering the target when clicked), NOT a locator problem — the selector(s) "
                    "above may well still be correct. Do NOT conclude locale_unsafe just because "
                    "of this. If the current snapshot below shows a dialog/overlay/menu blocking "
                    "the target, include a real, HTML-confirmed close/cancel/dismiss action in "
                    "your plan before the original action(s), or simply repeat the original "
                    "action(s) if nothing needs dismissing anymore in this fresh snapshot — our "
                    "own code will also attempt an automatic Escape-key dismissal if the same "
                    "kind of interception happens again."
                    if interception else
                    "Take this into account: repeating the exact same plan is unlikely to help "
                    "unless the current snapshot below shows the state has changed."
                )
            )
            continue

        # Actions genuinely ran without error — accumulate them into the
        # full sequence for this step, regardless of whether the step is
        # fully done yet (see step_complete below). A future direct-
        # execution run needs the WHOLE sequence from a clean start, not
        # just whichever round happened to finish last.
        executed_actions.extend(plan)

        if response.get("step_complete") is False:
            log(f"STEP {step_number}: [SELF-HEAL] round {round_num}: plan executed, but the "
                f"model flagged the step as not yet complete — continuing")
            previous_attempt_context = (
                f"PREVIOUS attempts already successfully executed these actions, in order, for "
                f"THIS step ({step_number}: {step_description!r}), against earlier snapshots: "
                f"{json.dumps(executed_actions)}\n"
                f"Do NOT repeat these — they already happened and the browser is now in the "
                f"resulting state, shown fresh below. Continue from here: observe the current "
                f"snapshot and add whatever further action(s) are still needed to genuinely "
                f"finish the step as described above (e.g. if the step says 'Click Finish', "
                f"don't stop until a real click on the actual Finish button is included)."
            )
            continue

        if failing_global_index is not None:
            compiled_steps.patch_partial_step(
                compiled_path, step_number, failing_global_index, executed_actions, locale)
        else:
            compiled_steps.patch_step(compiled_path, step_number, executed_actions, locale)
        frame_path_was_substituted = (
            original_frame_path_selectors and frame_path_selectors
            and frame_path_selectors != original_frame_path_selectors
        )
        if frame_path_was_substituted:
            n = compiled_steps.bulk_replace_frame_path(
                compiled_path, original_frame_path_selectors, frame_path_selectors, locale)
            if n:
                log(f"STEP {step_number}: [SELF-HEAL] bulk-patched {n} other entries across the "
                    f"compiled file sharing the same stale frame path {original_frame_path_selectors} "
                    f"-> {frame_path_selectors}")
        accepted, why = maybe_accept_knowledge_suggestion(
            knowledge_path, response.get("knowledge_library_suggestion"), case_name, locale)
        if response.get("knowledge_library_suggestion"):
            log(f"STEP {step_number}: [SELF-HEAL] knowledge suggestion "
                f"{'accepted' if accepted else 'rejected'}: {why}")
        log(f"STEP {step_number}: [SELF-HEAL] round {round_num}: succeeded, patched compiled file "
            f"({len(executed_actions)} action(s) total)")
        return True, "self-heal succeeded"

    return False, last_reason
