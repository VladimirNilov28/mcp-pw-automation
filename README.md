# mcp-pw-automation

MVP runner for automating UI sanity tests. Each test case is a numbered list
of natural-language steps. The runner's pipeline has three phases:

1. **Compile** (once per case, `en` locale only): one continuous `claude -p`
   + MCP session drives the whole case and writes `compiled/<case_name>.json`.
2. **Execute** (every requested environment/locale, including `en`): plain
   Playwright runs the compiled steps directly — no LLM involved, this is
   where the actual per-step screenshots are captured.
3. **Self-heal** (inline, only when a compiled step breaks): our own code
   captures a snapshot of the current, already-authenticated page and asks
   `claude -p` — as a plain text+vision completion with **no** browser/tool
   access — for a structured JSON action plan, which our code validates and
   executes itself. A successful self-heal patches the compiled file and the
   run continues.

`runner.py` walks a configurable (environment, locale) matrix and, for each
pair, spawns `direct_executor.py`. For Execute/Self-heal, `direct_executor.py`
always launches its own browser and logs in via hardcoded plain Playwright
first (never an LLM). Compile is the one exception: `direct_executor.py`
launches no browser of its own at all — `claude -p` + `@playwright/mcp` launch
and fully own a fresh browser instead, and login is performed by the agent
itself using `creds.json`'s credentials (the only place in the pipeline a
model ever sees them). See "Fast path vs agent fallback" below for how the
three phases relate.

## Prerequisites

- **Node.js** (for `npx`) — only needed for the Compile phase: `claude -p`
  runs `npx @playwright/mcp@latest` in its normal default mode (no
  `--cdp-endpoint`), which launches and owns its own fresh browser; no
  manual install needed. Self-heal doesn't use MCP/Node at all.
- **Claude Code CLI** installed and authenticated (`claude` on your PATH,
  logged in). `direct_executor.py` shells out to `claude -p ...` — for the
  whole-case Compile pass (an uncompiled case, or `--recompile`) and for
  Self-heal's per-step, tool-free text+vision completions — but never for
  login, which is always hardcoded plain Playwright (`run_login_handoff`).
- **Python 3.11+** plus `pip install -r requirements.txt` (currently just
  `playwright`, used by `direct_executor.py` to control its own browser —
  see `requirements.txt`), then run **`playwright install chromium`** once
  to download the actual browser binary.
- **`jq`** (optional) — the Compile agent is allowed to use `Bash(jq:*)`
  for quick lookups into its compiled step file (see "How the learning
  loop works" below). It can fall back to the `Read`/`Write` tools if
  `jq` isn't installed, so this isn't a hard requirement. Self-heal has no
  tool access at all, so this doesn't apply to it.

## Setup

```bash
cp creds.example.json creds.json
```

Then edit `creds.json` and fill in real passwords (and any real emails not
already present) for each (environment, locale) pair you plan to test.

> **⚠️ `creds.json` contains real plaintext passwords.**
> It is already listed in `.gitignore` — **never commit it, never push it,
> never paste its contents anywhere.** This plaintext-local-file approach is
> a deliberate, temporary MVP shortcut. Revisit before using this for
> anything beyond local/throwaway testing (e.g. a proper secrets manager).

`config.json` holds only non-secret data (environment URLs, plus the
`headless` toggle below) and is safe to commit as-is.

### Headless mode

`config.json` has a top-level `"headless"` boolean (default `true`) that
`runner.py` reads and passes straight through to `direct_executor.py` as
`--headless`, controlling the Chromium instance it launches directly via
Playwright's Python API (`p.chromium.launch(headless=..., ...)`):

```json
{
  "headless": false,
  "environments": { ... }
}
```

Set `headless: false` when you want to watch the browser while debugging a
case locally.

For Execute/Self-heal, `direct_executor.py` always launches a fresh
Chromium instance per run (never a persistent on-disk profile), so there's
no cookie/session leftover from a previous run to worry about — a login
session from one (env, locale) run (e.g. `qa`) can't silently persist and
get reused by the next run (e.g. `stage`). It also always creates its
browser context with a fixed `{"width": 1920, "height": 1080}` viewport
(`VIEWPORT` in `direct_executor.py`), so every run — headless or headed, on
any machine — renders at the same resolution. This keeps element layout
(and what fits on-screen for a screenshot) consistent across runs instead
of depending on whatever the local display/host happens to default to.
Compile is the exception: `direct_executor.py` launches no browser of its
own there at all (see below).

For the Compile pass, `direct_executor.py` generates a plain, default
`@playwright/mcp` config (`build_recording_mcp_config` — no `--cdp-endpoint`,
no reference to any browser of ours) and writes it to
`results/<case_name>/<env>/<locale>/mcp_config.json` (gitignored, alongside
that run's log); `claude -p` uses it to launch and fully own its own fresh
browser, logging in itself with credentials passed into the prompt. Self-heal
never generates or uses an MCP config at all — it's a plain text+vision
`claude -p` completion with `--tools "" --strict-mcp-config`, no browser or
MCP involved. The static `.mcp.json` in the repo root is unused by the
automation itself — it's left only as a reference example for running
`@playwright/mcp` manually.

## Adding a new test case

Add a new file `cases/<case_name>.json`:

```json
{
  "case_name": "my-new-case",
  "steps": [
    "Login to AEP",
    "Go to some page",
    "Click some button"
  ]
}
```

- `case_name` and `steps` are required; `steps` must be a non-empty list.
- The first step is treated specially if it looks like a login instruction
  (contains the word "login", case-insensitively) — `cases.py` strips it
  before building any prompt, since login is handled separately, entirely
  outside any `claude -p` call: `direct_executor.py`'s hardcoded
  `run_login_handoff` logs in directly via `creds.json`, before either
  execution mode starts. Login is never screenshotted and never counted in
  step numbering. If your case doesn't start with a login step, nothing is
  stripped.

## Adding a new environment or locale

- **New environment**: add an entry under `environments` in `config.json`
  (just a `url`), then add matching entries to `creds.json` /
  `creds.example.json` for whichever locales you support there.
- **New locale**: add an entry under each relevant environment in
  `creds.json` / `creds.example.json` with that locale's full `email` and
  `password`.
- Email addresses are stored in full per (env, locale) — they are **not**
  derived from a pattern, since both the domain and any suffix (e.g. stage's
  `+T2E`) can differ between environments even for the same locale.
- No code changes are needed for either case — the runner reads both files
  at run time.

## Running

```bash
# Full matrix (all environments x all locales defined in creds.json)
python runner.py --case activate-s3

# Single environment
python runner.py --case activate-s3 --envs prod

# Subset: en + fr on qa only
python runner.py --case activate-s3 --envs qa --locales en fr

# Custom per-run timeout (seconds), default is 3600 (60 minutes)
python runner.py --case activate-s3 --envs qa --locales en --timeout 1800

# Live progress: echo each STEP/SUMMARY line to the terminal as it happens
python runner.py --case activate-s3 --envs qa --locales en --debug

# Force a fresh Compile pass (against en), overwriting any existing compiled file
python runner.py --case activate-s3 --envs qa --locales en --recompile

# Custom per-step self-heal timeout (seconds), default is 600 (10 minutes)
python runner.py --case activate-s3 --envs qa --locales en --fallback-timeout 900
```

If a requested (env, locale) pair is missing a `url` in `config.json` or an
entry in `creds.json`, the runner prints a `WARNING: skipping ...` line and
continues with the rest of the matrix rather than crashing the whole batch.

### Debug mode (`--debug`)

By default the runner only prints a `running...` line and a final status
line per (env, locale) pair — you don't see anything in between until the
whole run finishes. With `--debug`, it also echoes each line the agent
prints matching `STEP ...`, `SUMMARY: ...`, or `LOGIN ABORTED` straight to
the terminal as soon as it arrives, so you can see which step it's
currently on without waiting for the full run or tailing `log.txt`
yourself.

This depends on `claude -p`'s own stdout not being fully buffered on its
end — the runner reads line-by-line as data arrives, but can't force the
child process to flush more eagerly than it chooses to. In practice this
means you should see step lines show up live, but if a given build of the
CLI buffers its output differently, they may still arrive in a burst.

### Final status line

Once a run finishes (or is killed), the runner prints exactly one of:

- `SUCCESS - SUMMARY: X/Y steps completed...` — the agent ran to
  completion and printed its final summary line before exiting.
- `done (no SUMMARY line found in output)` — the process exited on its own
  but never printed a `SUMMARY:` line (e.g. it hit `LOGIN ABORTED` and
  stopped early, or crashed) — check `log.txt` for what happened.
- `TIMEOUT` — the process was still running past `--timeout` and got
  killed; this does **not** mean the case failed, only that it didn't
  finish in the time allotted (see `log.txt` for whatever output was
  captured before the kill).

## Fast path vs agent fallback

`runner.py` decides, per case, whether a **Compile** pass needs to run
before the requested (env, locale) matrix (**Execute**), and
`direct_executor.py` decides, per step during Execute, whether it needs to
invoke **Self-heal**. Every (env, locale) pair is run by spawning
`direct_executor.py`, but Compile and Execute differ in how it drives the
browser:

- **Execute/Self-heal**: `direct_executor.py` launches its own Chromium
  instance directly via Playwright's Python API (headless setting from
  `config.json`, fixed 1920x1080 viewport) and keeps it open for the entire
  run, always logging in first via plain Playwright (`run_login_handoff`) —
  fill email, submit, optionally click past an account-chooser screen if one
  appears, fill password, submit. No LLM call, ever, for login — and login
  is never part of the compiled file.
- **Compile**: `direct_executor.py` launches no browser of its own at all —
  see below.

**Compile** (`--record`; runs exactly ONCE per case, only if
`compiled/<case_name>.json` doesn't exist yet or `--recompile` was passed
— see `runner.py`'s `resolve_compile_pair`): hands the whole case off to a
single `claude -p` + `@playwright/mcp` invocation, running in
`@playwright/mcp`'s normal DEFAULT mode (no `--cdp-endpoint`) — the agent
launches and fully owns its own fresh browser via MCP tools, logs in
itself (using this (env, locale) pair's `creds.json` credentials, passed
into the prompt for this one-time invocation only — the only place in the
pipeline a model ever sees credentials), then drives every scenario step
and writes `compiled/<case_name>.json` as it completes each one. Always
runs against the **`en` locale**, regardless of which locales were
actually requested for the matrix — compiled selectors are meant to be
locale-independent (see "Locator-selection rule" below), so there's no
need to repeat this per locale. Takes **no screenshots** — this is a
write-only compile pass, not a test run. See "How the learning loop works"
below for the compiled-file format.

This reverts an earlier CDP-attach design for Compile (attaching
`claude -p` to a Python-launched, already-logged-in browser over
`--cdp-endpoint`), which proved unreliable in practice: our own
Python-controlled page (`page.url`) would confirm landing on the
authenticated app while the attached agent reported seeing a genuine,
unauthenticated IMS sign-in page instead — a direct contradiction
indicating the CDP attach was sometimes targeting the wrong tab/session.
Letting `@playwright/mcp` launch and own its own browser end-to-end, with
no attach step at all, sidesteps that class of bug entirely. Execute/
Self-heal never used CDP-attach and are unaffected by this change.

**Execute** (the normal case, every requested (env, locale) pair,
including `en`, once a compiled file exists):

1. For each scenario step, looks up its entries in
   `compiled/<case_name>.json` (a step may compile to more than one raw
   entry, e.g. fill then click). If every entry's `type` is directly
   executable (see the scope note below) and none is flagged
   `locale_unsafe`, it runs them **directly via Playwright — zero LLM
   calls**, and takes this step's screenshot (the real test artifact).
   Otherwise (no entries yet, an ineligible type, or a `locale_unsafe`
   entry), or if a direct attempt fails at runtime, it hands that
   **whole step** to Self-heal instead.
2. **Self-heal** (`self_heal.py`) never attaches to the browser or gets
   any tool access at all. Our own code captures an accessibility
   snapshot + screenshot of the CURRENT page state (scoped into whatever
   frame path the broken entries specified) plus relevant
   `knowledge/aep-locator-library.json` entries, and sends all of it to
   `claude -p` as a single plain text+vision completion (`--tools ""
   --strict-mcp-config` — genuinely zero MCP/tool access, confirmed via
   the session's own init line reporting `"mcp_servers":[]`). The model
   returns a structured JSON action plan; our own code **validates every
   selector in it against the locator-exclusion rules in Python** (not
   just the prompt) before executing anything via
   `compiled_steps.execute_entry` on the same already-authenticated page.
   On success, it **permanently patches** that step's entries in
   `compiled/<case_name>.json` (`compiled_steps.patch_step`), so future
   runs (any locale) execute it directly too, and the run continues to the
   next step. If no safe, locale-independent locator can be found at all,
   the step is marked `locale_unsafe` and **this one (case, env, locale)
   run stops** — a correct, expected outcome (a human walks those steps
   manually), not a bug to route around. Other (env, locale) pairs in the
   same `runner.py` invocation are unaffected.

Whichever phase/path handled a step, the same `{case_name}_{NN}.png`
screenshot convention applies during Execute, and `log.txt` ends with the
same `SUMMARY: X/Y steps completed...` line — so `runner.py`'s
SUCCESS/TIMEOUT detection needs no changes across phases.

**Why this matters:** the original single-agent-call design paid full LLM
reasoning cost on *every* step, every run. Compile still costs that once
per case (there's nothing to execute directly yet). Every Execute run
after that should approach (browser navigation time) + (LLM time only for
the handful of steps still needing self-heal) — a large, permanent
speedup once a case has been compiled once.

**Scope limit — `click`/`fill`/`check`/`waitFor` only, for now:** the
direct executor (`compiled_steps.py`) only executes these four step
types; entries with any other `type` (e.g. `selectOption`, `dragAndDrop`)
always route to self-heal, same as a missing entry. Extending this would
mean adding a new type handler to `compiled_steps.py` — a deliberate
future step, not an oversight.

**`--recompile`** forces a fresh compile pass even if a compiled file
already exists, overwriting it — useful when the UI has changed enough
that self-heal isn't keeping up, or to regenerate a case from scratch.

## Output

For each `(case, env, locale)` run:

- `results/<case_name>/<env>/<locale>/log.txt` — full stdout of
  `direct_executor.py`, including relayed `claude -p` output verbatim for
  a Compile run, and `self_heal.py`'s own `[SELF-HEAL]`-prefixed log lines
  for Execute-phase recovery, plus stderr if any, appended under a
  `--- stderr ---` marker. If the run exceeded `--timeout`, the process is
  killed and a clear `*** TIMED OUT after Ns — process killed ***` line is
  appended.
- `results/<case_name>/<env>/<locale>/screenshots/` — one PNG per scenario
  step, named `{case_name}_{NN}.png` with `NN` zero-padded starting at `01`
  for the first scenario step (login is never screenshotted, so numbering
  never includes it). Only produced during Execute — a Compile run
  produces none.
- `results/<case_name>/<env>/<locale>/mcp_config.json` — the plain,
  default `@playwright/mcp` config `direct_executor.py` generates
  (`build_recording_mcp_config`, no `--cdp-endpoint`), used ONLY for a
  Compile run's whole-case `claude -p` call, which launches and owns its
  own browser from it — Self-heal never generates or uses one, since it
  never uses MCP or a browser at all.

`results/` is gitignored — it's local run output, not something to commit.

## Locator-selection rule (enforced in the prompt AND in code)

Both the Compile agent and Self-heal's plan are instructed, as a hard rule
(`LOCATOR_SELECTOR_RULE` in `prompts/system_prompt.py`), to never write a
dynamically-generated locator (React-generated ids, hashed/auto-generated
CSS module class names, brittle `nth-child` chains) **or any attribute
whose value is user-facing/translatable text** (`aria-label`, visible
text, `placeholder`, `alt`) into a compiled step's
`selector`/`framePathSelectors` — since the compiled file runs unmodified
across every locale. It must instead prefer, in order:
`data-test-id`/`data-testid`, a static confirmed `id`, role + structural
position, a case-by-case-confirmed `name`/`title`, or a hand-authored (not
generated) semantic CSS class as a last resort. If none of those are
available, the step is marked `"locale_unsafe": true` instead of writing
an unsafe selector, so that step always routes to self-heal, on every
locale.

Unlike before, this is now enforced at **two** levels: the prompt asks for
it, AND `self_heal.py`'s `validate_plan_selectors`/`_selector_is_safe`
mechanically reject any selector matching a dynamic-id/hash-class/
timestamp-like pattern, or using `aria-label`/`text=`/`:has-text(`/
`placeholder=`/`alt=` as a basis — for every action in a self-heal plan,
and for any `knowledge_library_suggestion` before it's accepted into
`knowledge/aep-locator-library.json`. A plan with even one unsafe selector
is rejected in full (see `self_heal.self_heal_step`) — never partially
executed or "fixed up" by guessing. This closes the gap where locator
selection used to be enforced only through prompt text.

## Selector verification (Compile writes nothing unverified)

Locale-safety (above) checks whether a selector is an acceptable *kind* of
string; it says nothing about whether the exact string is *correct* — a
plain transcription typo (e.g. writing `[test-id="..."]` instead of
`[data-test-id="..."]`) is locale-safe by every rule above and still
simply never matches anything at runtime. During Compile, the agent is
required (`SELECTOR_VERIFICATION_RULE` in `prompts/system_prompt.py`) to
run the exact selector string it's about to write — copied, not
retyped — against the real, live DOM via `browser_evaluate`
(`document.querySelectorAll(...)`, resolving into the right iframe first
when `framePathSelectors` is non-empty) and confirm it resolves to
exactly the expected element(s) before writing that entry into
`compiled/<case_name>.json` or a `knowledge_library_suggestion`. Zero
matches or an unexpectedly large match count sends the agent back to the
real HTML to re-derive and re-verify, instead of writing an unverified
string on faith. Self-heal doesn't need an equivalent check: its plan is
always executed live against the real page before `compiled_steps.patch_step`
ever writes anything (see `self_heal.self_heal_step`), which already
proves the concrete locator worked.

## How the learning loop works

Each case has a single shared **compiled step file** at
`compiled/<case_name>.json` — **per case, not per (env, locale)**. A case
with no compiled file yet is "uncompiled": `runner.py` runs a Compile pass
first (see "Fast path vs agent fallback" above), which both performs the
case (against `en`) and writes this file. Every Execute run after that,
regardless of which environment or locale it targets, reads from (and,
when a step breaks, self-heal patches) the same file — so it gets more
resilient over time instead of needing rediscovery.

This uses the same JSON shape as the team's existing manual
recording/execution tool, so it's a format already familiar to read:

```json
{
  "case_name": "activate-s3",
  "compiled_from": "cases/activate-s3.json",
  "last_updated": "<ISO timestamp>",
  "steps": [
    {
      "step_number": 3,
      "type": "click",
      "selector": "[data-test-id=\"primary-action-button\"]",
      "clickBy": "selector",
      "framePathSelectors": ["iframe[name=\"Main Content\"]"],
      "selectorNth": null,
      "hasDisambiguation": false,
      "matchCount": null,
      "optional": false,
      "onFailure": null,
      "locale_unsafe": false,
      "last_patched_by_agent": null,
      "patched_locales": []
    }
  ]
}
```

- `step_number` matches the case's numbered scenario steps (excluding the
  login step, which is never compiled — login is hardcoded plain
  Playwright, see `run_login_handoff` in `direct_executor.py`).
  **`step_number` is not unique**: a scenario step that needs more than
  one raw action (e.g. fill a search box, then click a result) compiles to
  multiple consecutive entries sharing that `step_number`, executed in
  array order. One screenshot is still taken per `step_number`, after its
  last entry runs.
- `type` is one of `click`/`fill`/`check`/`waitFor` — the only types
  `compiled_steps.py` executes directly today (see the scope note above).
  `fill` entries also carry `fillAction`/`value`; `check` entries also
  carry `checkMode`.
- `fillAction` is `replace` (default) / `clear` / `append` / `prepend` /
  `type-replace`. The first four set the value via Playwright's `.fill()`
  — a bulk, non-keystroke CDP operation, fast and correct for ordinary
  text/search inputs. `type-replace` is a targeted opt-in for rich/
  controlled-input fields — formula/expression/code-editor-style inputs
  (e.g. AEP's calculated-field editor), or any field observed to have
  auto-pairing/auto-formatting behavior (typing `(` auto-inserts a
  matching `)`) — where such a field's own JS listens for individual
  keystrokes and can corrupt/duplicate the content in response to a bulk
  insert. `type-replace` instead clicks the field, selects all existing
  content and deletes it via real keyboard events (`Control+A`/`Meta+A`
  then `Delete`), then types the value character-by-character via real
  key events (`Locator.press_sequentially`) — see `compiled_steps._type_replace`.
  Slower than a plain `.fill()`, so only worth using for fields that
  actually need it.
- `framePathSelectors` is an ordered list of iframe selectors to descend
  through before resolving `selector` — each following the same
  locator-selection rule as `selector` itself.
- `selectorNth`/`hasDisambiguation`/`matchCount` handle selectors that
  match more than one element; `hasDisambiguation`/`matchCount` are
  informational only (recorded for humans, never re-validated at runtime).
- `optional: true` (only meaningful for `click`/`waitFor`) means: if this
  entry fails, skip it and move on — don't fail the step or trigger
  fallback.
- `onFailure` (condition + recovery actions + retry) is fully supported by
  the executor for compatibility with hand-authored or externally-imported
  compiled files, but the recording/patch agent never authors one itself —
  it's left `null`. Adding automated recovery logic for a step is a manual,
  future step.
- `locale_unsafe: true` means no locale-safe selector was available when
  this step was recorded; it always routes straight to self-heal, on
  every locale, rather than being attempted directly.

⚠️ **`aria-label`, visible text, `placeholder`, and `alt` are translated
per locale — they are never locale-independent**, so they're never
acceptable as a `selector`/`framePathSelectors` value in this file (see
"Locator-selection rule" above). If nothing locale-safe exists for a step,
it's recorded with `locale_unsafe: true` instead — never with a text-based
selector as if it were safe.

### Hand-editing compiled files (a minimal shorthand form)

The full schema above is what gets written to disk by the recording/patch
agents, but a human hand-adding a step doesn't have to type all of it.
`compiled_steps.load_compiled` accepts a minimal SHORTHAND form per entry
and expands it into the full schema in memory:

```json
{
  "step_number": 2,
  "type": "click",
  "locator": "[data-test-id=\"segment-browse-tab\"]",
  "iframe": null
}
```

- `locator` → `selector`.
- `iframe` → `framePathSelectors`: `null`/omitted means the top-level page
  (`[]`), a bare string means a single frame (`["that string"]`), and a
  list is a nested frame path exactly like `framePathSelectors` already
  supports.
- For `fill`/`check` steps, add `value` (fill) / `checkMode` (check) the
  same way — still far fewer fields than the full schema.
- Every other field (`clickBy`, `selectorNth`, `hasDisambiguation`,
  `matchCount`, `optional`, `onFailure`, `locale_unsafe`,
  `last_patched_by_agent`, `patched_locales`, `fillAction`) is filled in
  automatically with the same default an agent-authored entry would use.

Shorthand and full-schema entries can be freely mixed in the same file —
detection is per-entry, not per-file — and a missing/empty `type` or
`locator`/`selector` (unless the entry is a `locale_unsafe` placeholder)
raises a clear error at load time rather than failing silently or later,
confusingly, mid-run.

This is an **input-side convenience only**: it never changes what's
actually on disk. Whenever code (Compile, self-heal) writes back to the
file, it always writes the full canonical schema for whatever entry it's
replacing — never shorthand — and any OTHER entries the write didn't
touch are left byte-for-byte exactly as they were (see
`compiled_steps._load_raw`, used internally by every writer instead of
the normalizing `load_compiled`). So a hand-added shorthand entry stays
shorthand on disk indefinitely, right up until self-heal (or a
recompile) happens to touch that specific step itself, at which point it
gets rewritten in full canonical form like everything else.

- The full locator-selection rule (`LOCATOR_SELECTOR_RULE` in
  `prompts/system_prompt.py`) applies to every `selector` and
  `framePathSelectors` value written into this file, exactly as it applies
  to what the agent searches for live — an unstable or locale-dependent
  entry would be useless (or actively misleading) on the next run.
- When a compiled step fails at runtime (or has no eligible entries), the
  direct executor hands the **whole scenario step** (not just the broken
  raw entry) to `self_heal.self_heal_step`, which never touches the
  browser itself — it captures a snapshot, gets a JSON plan back from
  `claude -p`, and executes that plan itself via `compiled_steps.execute_entry`.
  On success, `compiled_steps.patch_step` **replaces every existing entry
  for that `step_number`** with what actually worked this time (not a
  partial patch — compound steps' raw actions are often interdependent),
  and sets `last_patched_by_agent` / appends to `patched_locales` on each.
- `runner.py` only computes the path to `compiled/<case_name>.json` (via
  `compiled_file_path`) and, if it doesn't exist yet (or `--recompile` was
  given), runs one dedicated Compile invocation (always against `en`, via
  `resolve_compile_pair`) before the requested Execute matrix. Neither
  process parses or merges its contents ad hoc. During Compile, all
  reading and writing happens inside the single `claude -p` call
  `direct_executor.py` invokes, via its `Read`/`Write`/`Edit`/`Bash(jq:*)`
  tool access (see `ALLOWED_TOOLS` in `runner.py`). During Execute,
  `direct_executor.py` itself only **reads** the file (via
  `compiled_steps.load_compiled`, once per step) to decide whether a step
  is eligible for direct execution — all **writing** during Execute
  happens via `compiled_steps.patch_step`/`mark_step_locale_unsafe`,
  called directly from Python (`self_heal.py`) after validating and
  executing a self-heal plan itself, never inside a `claude -p` call.
- `compiled/` is **not** gitignored (unlike `creds.json` or `results/`) —
  it's meant to be shared and versioned so the whole team benefits from
  accumulated compilation. `compiled/.gitkeep` exists so the directory is
  present in a fresh checkout even before any case has been run.

## Shared product knowledge library

`compiled/<case_name>.json` is case-specific: exact selectors for one exact
scenario. It doesn't help a brand-new case that happens to reuse the same
recurring UI patterns dozens of other cases already use — a workflow "Next"
button, the app's main shell iframe, a meatball/"more actions" menu, a
success toast. `knowledge/aep-locator-library.json` fills that gap: one
small, shared, product-level knowledge library (not per case, not per
env/locale) that every Compile invocation and every Self-heal invocation
consults during discovery, so this kind of knowledge compounds across the
**whole test suite** instead of resetting per case.

```json
{
  "product": "AEP",
  "last_updated": "<ISO timestamp>",
  "patterns": {
    "app_main_iframe": {
      "description": "The single app-shell iframe wrapping most AEP UI content",
      "frame_path": { "selectors": ["iframe"], "basis": "structural-bare" },
      "gotcha": "Do NOT use this iframe's name/title as a selector basis — it's localized. Do NOT use its id/src either — both contain dynamic timestamp/build strings. A bare 'iframe' selector is safe here because only one iframe exists on these pages.",
      "verified_locales": ["en", "jp"],
      "seen_in_cases": ["activate-s3"],
      "confidence": "high"
    }
  }
}
```

- Entries come in two shapes: a **concrete reusable selector**
  (`selector` + optional `frame_path`) for a pattern that resolves to the
  identical selector everywhere (e.g. a workflow "Next" button, a success
  toast), or a **recognizable pattern, not a literal selector**
  (`selector_pattern` + `notes`) for widgets that are conceptually the same
  but implemented differently per page/surface (e.g. a meatball menu) —
  these save reasoning time even though the exact string still needs
  per-page confirmation. An optional free-text `gotcha` field records known
  traps (like the iframe example above) so they don't get rediscovered.
- The exact same locator-selection hard rule that applies to
  `compiled/<case_name>.json` applies here too, only more conservatively —
  a bad entry in this shared file poisons every future case that consults
  it, not just one case's compiled file.
- Written **sparingly**: most selectors an agent discovers are specific to
  one case's business content and belong only in that case's compiled
  file. During Compile, this is prompt-enforced judgment (same as before).
  During Self-heal, it's enforced in code:
  `self_heal.maybe_accept_knowledge_suggestion` only accepts a
  `knowledge_library_suggestion` when it's marked `generic_widget: true`,
  passes selector validation, AND either its `pattern_name` is in the
  reviewable `ALWAYS_SAFE_PATTERN_NAMES` allowlist (obvious app-shell
  elements) or the same concrete selector is already recorded under a
  *different* case in `seen_in_cases` — i.e. cross-case confirmation, not
  the model's unchecked say-so.
- Read on **every** Compile or Self-heal invocation, for any case (new or
  already compiled) — an RLM-style, per-pattern lookup (query only the
  specific concept relevant to the current step, never the whole file at
  once — see `self_heal.lookup_knowledge_entries`). A concrete match skips
  full discovery for that step entirely; a pattern-with-notes match
  narrows the search instead of starting blind. This is also the first
  place self-heal checks when a frame-path or selector mismatch looks like
  it might be a known, already-documented gotcha, rather than
  re-diagnosing it from scratch.
- See `KNOWLEDGE_LIBRARY_SCHEMA` / `KNOWLEDGE_LIBRARY_USAGE` in
  `prompts/system_prompt.py`, shared by both the Compile prompt and the
  Self-heal prompt so the schema/usage description never drifts apart —
  though, per above, Self-heal's actual *acceptance* decision is made in
  Python, not by the model.
- Kept as a **separate file** from `compiled/<case_name>.json` on purpose —
  case-specific exact steps vs. shared cross-case patterns are different
  concerns and are never merged.
- Not gitignored, same reasoning as `compiled/` — versioned so the whole
  team benefits. `runner.py`'s `knowledge_library_file_path()` creates it
  with an empty `{"product": "AEP", "last_updated": null, "patterns": {}}`
  skeleton if missing (unlike `compiled/<case_name>.json`, its existence
  isn't a mode signal, so there's no reason to leave it absent).
- Grows organically from real runs — it is **not** backfilled automatically
  from existing `compiled/*.json` files. As more cases run over time,
  brand-new cases should need progressively less discovery for common
  widgets, on top of the per-case speedup `compiled/` already provides.

## Known limitations (out of scope for this MVP pass)

- No pass/fail verdict logic based on screenshot content — a human reviews
  the log + screenshots.
- No result aggregation or reporting UI (e.g. an HTML screenshot gallery).
- No CI integration.
- No parallel/concurrent execution — the matrix runs sequentially.
- No file locking/concurrency handling on `compiled/<case_name>.json` — fine
  since runs are sequential in current scope, but would need addressing
  before running the matrix in parallel.
- Credentials are a plaintext local JSON file (`creds.json`), gitignored but
  not otherwise secured — fine for local/throwaway use only.
- The direct-executor path only executes `click`/`fill`/`check`/`waitFor`
  steps directly; everything else routes to self-heal (see "Fast path vs
  agent fallback").
- The manual recording tool's own automatic "retry a broken selector in
  other frames" behavior, and its separate whole-test-rerun layer, are not
  ported — a broken frame path or selector here routes straight to
  self-heal instead.
- Self-heal caps itself at `self_heal.MAX_FALLBACK_ROUNDS` (3) snapshot/
  plan/execute attempts per step before giving up — this bounds retry cost
  on a genuinely broken step, but means a step that would have succeeded
  on a 4th attempt is left for a human instead.
- If a `direct_executor.py` process is killed on `--timeout`, any nested
  `claude -p` subprocess it had spawned (a Compile pass, or a Self-heal
  invocation) is not guaranteed to be killed with it (same pre-existing
  orphan-process risk `claude -p` already has with its own `npx`/Node
  children today — not a new regression, just inherited scope; Self-heal
  itself spawns no `npx`/Node child, only `claude -p` directly).
