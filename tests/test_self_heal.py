"""Pure unit tests for self_heal.py's data-validation/writer logic — no
browser, no network, no live `claude -p` invocation. Covers exactly the
pieces that replaced the old prompt-only enforcement / prose parsing:
selector validation, compiled-file patching, and the knowledge-library
write-sparingly rule now enforced in code.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import compiled_steps
import self_heal


class _FakeLocator:
    def __init__(self, html=None, raises=None):
        self._html = html
        self._raises = raises

    def inner_html(self):
        if self._raises:
            raise self._raises
        return self._html


class _FakeScope:
    def __init__(self, html=None, raises=None):
        self._locator = _FakeLocator(html, raises)

    def locator(self, selector):
        assert selector == "body"
        return self._locator


class TestCaptureHtml(unittest.TestCase):
    def test_returns_html_unchanged_when_small(self):
        scope = _FakeScope(html="<div data-test-id='x'>hi</div>")
        self.assertEqual(self_heal._capture_html(scope), "<div data-test-id='x'>hi</div>")

    def test_truncates_large_html(self):
        big = "a" * (self_heal.HTML_CAPTURE_MAX_CHARS + 5000)
        scope = _FakeScope(html=big)
        result = self_heal._capture_html(scope)
        self.assertLessEqual(len(result), self_heal.HTML_CAPTURE_MAX_CHARS + 200)
        self.assertIn("truncated", result)

    def test_truncation_preserves_both_start_and_end(self):
        # A portal-rendered dialog is typically appended near the END of
        # <body> — a head-only truncation would always miss it.
        html = (
            '<div data-test-id="start-marker">head</div>'
            + "x" * self_heal.HTML_CAPTURE_MAX_CHARS
            + '<div data-test-id="end-marker">portal dialog</div>'
        )
        scope = _FakeScope(html=html)
        result = self_heal._capture_html(scope)
        self.assertIn("start-marker", result)
        self.assertIn("end-marker", result)

    def test_never_raises_on_capture_failure(self):
        scope = _FakeScope(raises=RuntimeError("detached frame"))
        result = self_heal._capture_html(scope)  # must not raise
        self.assertIn("could not capture HTML", result)


class TestCaptureHtmlWide(unittest.TestCase):
    def test_full_page_scope_captures_once(self):
        page = _FakeScope(html="<div>page content</div>")
        result = self_heal._capture_html_wide(page, page)  # scope IS page
        self.assertIn("page content", result)
        self.assertEqual(result.count("page content"), 1)
        self.assertIn("no iframe scope active", result)

    def test_frame_scope_captures_both_frame_and_top_level_page(self):
        page = _FakeScope(html="<div id='portal-dialog'>Cancel/Save</div>")
        frame_scope = _FakeScope(html="<div data-test-id='row'>Segment 7B</div>")
        result = self_heal._capture_html_wide(page, frame_scope)
        self.assertIn("Segment 7B", result)
        self.assertIn("portal-dialog", result)
        self.assertIn("resolved frame scope", result)
        self.assertIn("TOP-LEVEL page", result)


class TestInterceptionRecovery(unittest.TestCase):
    def test_looks_like_interception_true_for_pointer_events_message(self):
        self.assertTrue(self_heal._looks_like_interception(
            Exception('<body class="spectrum">…</body> intercepts pointer events')))

    def test_looks_like_interception_false_for_ordinary_locator_miss(self):
        self.assertFalse(self_heal._looks_like_interception(
            Exception('Locator.click: Timeout 12000ms exceeded waiting for locator("x")')))

    def test_execute_plan_with_recovery_retries_once_after_interception(self):
        calls = []

        def fake_execute_entry(page, action):
            calls.append(action["selector"])
            if len(calls) == 1:
                raise Exception("<body>…</body> intercepts pointer events")
            # second call (the retry) succeeds

        escapes = []

        orig_execute_entry = compiled_steps.execute_entry
        orig_dismiss = self_heal.dismiss_stray_overlays
        compiled_steps.execute_entry = fake_execute_entry
        self_heal.dismiss_stray_overlays = lambda page: escapes.append(1)
        try:
            self_heal.execute_plan_with_recovery(page=None, plan=[{"type": "click", "selector": "x"}])
        finally:
            compiled_steps.execute_entry = orig_execute_entry
            self_heal.dismiss_stray_overlays = orig_dismiss

        self.assertEqual(calls, ["x", "x"])  # original attempt + one retry
        self.assertEqual(escapes, [1])

    def test_execute_plan_with_recovery_does_not_retry_ordinary_failures(self):
        def fake_execute_entry(page, action):
            raise Exception('Locator.click: Timeout 12000ms exceeded waiting for locator("x")')

        orig_execute_entry = compiled_steps.execute_entry
        compiled_steps.execute_entry = fake_execute_entry
        try:
            with self.assertRaises(Exception):
                self_heal.execute_plan_with_recovery(page=None, plan=[{"type": "click", "selector": "x"}])
        finally:
            compiled_steps.execute_entry = orig_execute_entry


class TestDismissStrayOverlays(unittest.TestCase):
    def test_presses_escape(self):
        class FakeKeyboard:
            def __init__(self):
                self.pressed = []

            def press(self, key):
                self.pressed.append(key)

        class FakePage:
            def __init__(self):
                self.keyboard = FakeKeyboard()

        page = FakePage()
        self_heal.dismiss_stray_overlays(page)
        self.assertEqual(page.keyboard.pressed, ["Escape"])

    def test_never_raises_if_press_fails(self):
        class FakeKeyboard:
            def press(self, key):
                raise RuntimeError("page closed")

        class FakePage:
            def __init__(self):
                self.keyboard = FakeKeyboard()

        self_heal.dismiss_stray_overlays(FakePage())  # must not raise


class TestPresubstituteFramePath(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.path = Path(self.tmpdir) / "knowledge.json"
        self.path.write_text(json.dumps({
            "product": "TestProduct",
            "patterns": {
                "app_main_iframe": {
                    "description": "shell iframe",
                    "frame_path": {"selectors": ["iframe"], "basis": "structural-bare"},
                },
            },
        }), encoding="utf-8")

    def test_matches_title_based_iframe_selector(self):
        result = self_heal.presubstitute_frame_path(['iframe[title="Main Content"]'], self.path)
        self.assertEqual(result, ["iframe"])

    def test_matches_name_based_iframe_selector(self):
        result = self_heal.presubstitute_frame_path(['iframe[name="Main Content"]'], self.path)
        self.assertEqual(result, ["iframe"])

    def test_does_not_match_unrelated_selector(self):
        result = self_heal.presubstitute_frame_path(['iframe[data-test-id="shell"]'], self.path)
        self.assertIsNone(result)

    def test_none_for_empty_frame_path(self):
        self.assertIsNone(self_heal.presubstitute_frame_path([], self.path))
        self.assertIsNone(self_heal.presubstitute_frame_path(None, self.path))

    def test_none_when_no_knowledge_candidate_exists(self):
        empty_path = Path(self.tmpdir) / "empty.json"
        empty_path.write_text(json.dumps({"product": "TestProduct", "patterns": {}}), encoding="utf-8")
        result = self_heal.presubstitute_frame_path(['iframe[title="Main Content"]'], empty_path)
        self.assertIsNone(result)


class TestBulkReplaceFramePath(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.path = Path(self.tmpdir) / "compiled.json"
        self.path.write_text(json.dumps({
            "case_name": "test-case", "compiled_from": "cases/test-case.json", "last_updated": None,
            "steps": [
                {"step_number": 1, "type": "click", "selector": "a", "framePathSelectors": ['iframe[title="Main Content"]'],
                 "patched_locales": []},
                {"step_number": 2, "type": "click", "selector": "b", "framePathSelectors": ['iframe[title="Main Content"]'],
                 "patched_locales": ["en"]},
                {"step_number": 3, "type": "click", "selector": "c", "framePathSelectors": ["iframe"],
                 "patched_locales": []},
            ],
        }), encoding="utf-8")

    def test_replaces_only_matching_entries(self):
        count = compiled_steps.bulk_replace_frame_path(
            self.path, ['iframe[title="Main Content"]'], ["iframe"], "jp")
        self.assertEqual(count, 2)
        data = compiled_steps.load_compiled(self.path)
        by_step = {s["step_number"]: s for s in data["steps"]}
        self.assertEqual(by_step[1]["framePathSelectors"], ["iframe"])
        self.assertEqual(by_step[2]["framePathSelectors"], ["iframe"])
        self.assertEqual(by_step[3]["framePathSelectors"], ["iframe"])  # untouched, already correct
        self.assertIn("jp", by_step[1]["patched_locales"])
        self.assertIn("en", by_step[2]["patched_locales"])  # preserved, not clobbered
        self.assertIn("jp", by_step[2]["patched_locales"])

    def test_no_match_is_a_safe_noop(self):
        count = compiled_steps.bulk_replace_frame_path(self.path, ["nonexistent"], ["iframe"], "jp")
        self.assertEqual(count, 0)


class TestKnowledgeFramePathCandidates(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.path = Path(self.tmpdir) / "knowledge.json"
        self.path.write_text(json.dumps({
            "product": "TestProduct",
            "patterns": {
                "app_main_iframe": {
                    "description": "shell iframe",
                    "frame_path": {"selectors": ["iframe"], "basis": "structural-bare"},
                },
                "workflow_next_button": {
                    "description": "next button",
                    "frame_path": {"selectors": ["iframe"], "basis": "structural-bare"},
                },
                "meatball_menu_button": {
                    "description": "no frame path here",
                },
            },
        }), encoding="utf-8")

    def test_collects_distinct_frame_paths_only(self):
        candidates = self_heal.knowledge_frame_path_candidates(self.path)
        self.assertEqual(candidates, [["iframe"]])  # deduped

    def test_missing_file_returns_empty(self):
        self.assertEqual(self_heal.knowledge_frame_path_candidates(Path("/no/such/file.json")), [])

    def test_lookup_force_includes_frame_path_entry_when_broke(self):
        text = self_heal.lookup_knowledge_entries(
            self.path, "Click on 'Edit schedule' button", frame_path_broke=True)
        self.assertIn("app_main_iframe", text)

    def test_lookup_does_not_force_include_when_not_broke(self):
        text = self_heal.lookup_knowledge_entries(
            self.path, "Click on 'Edit schedule' button", frame_path_broke=False)
        self.assertNotIn("app_main_iframe", text)


class TestValidatePlanCompletesSelections(unittest.TestCase):
    def test_rejects_autocomplete_fill_with_no_following_click(self):
        plan = [
            {"type": "fill", "selector": '[data-test-id="mapper-component::sourceAttribute-auto-complete::0"]',
             "value": "acc", "fillAction": "replace"},
        ]
        ok, reason = self_heal.validate_plan_completes_selections(plan)
        self.assertFalse(ok)
        self.assertIn("auto-complete", reason)

    def test_accepts_autocomplete_fill_followed_by_click(self):
        plan = [
            {"type": "fill", "selector": '[data-test-id="mapper-component::sourceAttribute-auto-complete::0"]',
             "value": "acc", "fillAction": "replace"},
            {"type": "click", "selector": '[data-test-id="schema.tree.row"][data-node-path="accountName"]'},
        ]
        ok, reason = self_heal.validate_plan_completes_selections(plan)
        self.assertTrue(ok)

    def test_ignores_fill_on_non_autocomplete_field(self):
        plan = [
            {"type": "fill", "selector": '[data-test-id="custom-attribute-name-field"]',
             "value": "testAttribute", "fillAction": "replace"},
        ]
        ok, reason = self_heal.validate_plan_completes_selections(plan)
        self.assertTrue(ok)  # free-text fields with no autocomplete are fine as-is


class TestValidateCalculatedFieldInsertions(unittest.TestCase):
    FIELD_TAB_CLICK = {"type": "click", "selector": '[data-test-id="calculatedFieldDialog.tabs.field"]'}
    OPERATOR_TAB_CLICK = {"type": "click", "selector": '[data-test-id="calculatedFieldDialog.tabs.operator"]'}
    REAL_FIELD_ITEM_CLICK = {"type": "click", "selector": '[data-test-id="field-list-item"][data-field-path="_id"]'}
    FORMULA_FILL = {
        "type": "fill", "fillAction": "type-replace", "value": 'upper(_id)+"_test"',
        "selector": 'div:has(> [data-test-id="add-calculated-field-preview-button"]) + div textarea',
    }

    def test_rejects_fabricated_field_typed_after_opening_field_tab_same_plan(self):
        plan = [self.FIELD_TAB_CLICK, self.FORMULA_FILL]
        ok, reason = self_heal.validate_calculated_field_insertions(plan)
        self.assertFalse(ok)
        self.assertIn("field-list item", reason)

    def test_rejects_fabricated_field_typed_in_a_later_round(self):
        # Reproduces the actual bug: Field tab clicked in an EARLIER round
        # (now in executed_actions), fabricated field typed in THIS round's
        # plan, with no real list-item click anywhere in between.
        executed_actions = [self.FIELD_TAB_CLICK]
        plan = [self.FORMULA_FILL]
        ok, reason = self_heal.validate_calculated_field_insertions(plan, executed_actions)
        self.assertFalse(ok)
        self.assertIn("field-list item", reason)

    def test_accepts_fill_after_real_field_list_item_click(self):
        executed_actions = [self.FIELD_TAB_CLICK, self.REAL_FIELD_ITEM_CLICK]
        plan = [self.FORMULA_FILL]
        ok, reason = self_heal.validate_calculated_field_insertions(plan, executed_actions)
        self.assertTrue(ok)

    def test_rejects_focus_click_into_editor_disguised_as_a_selection(self):
        # Reproduces the ACTUAL sequence written to a compiled case file's
        # compiled file by the bug: Field tab click, then a click INTO the
        # formula editor itself (to focus it / position the cursor before
        # typing) — not a real field-list item click — then the fabricated
        # fill. The focus-click must not be mistaken for a real selection.
        focus_click_into_editor = {
            "type": "click",
            "selector": "div:has(> [data-test-id='add-calculated-field-preview-button']) + div .CodeMirror-line",
        }
        executed_actions = [self.FIELD_TAB_CLICK, focus_click_into_editor]
        plan = [self.FORMULA_FILL]
        ok, reason = self_heal.validate_calculated_field_insertions(plan, executed_actions)
        self.assertFalse(ok)
        self.assertIn("field-list item", reason)

    def test_ignores_formula_fill_when_field_tab_never_opened(self):
        # The original, already-accepted base case: typing a full formula
        # skeleton (function + a known-safe field name) directly, with no
        # Field-tab interaction at all, must remain unaffected.
        plan = [{"type": "fill", "fillAction": "type-replace", "value": "upper(_id)",
                 "selector": '[role="dialog"] textarea'}]
        ok, reason = self_heal.validate_calculated_field_insertions(plan)
        self.assertTrue(ok)

    def test_switching_away_from_field_tab_clears_requirement(self):
        # Clicking a different tab (Operator) after the Field tab means
        # we're no longer mid-field-insertion; a subsequent formula fill
        # is not gated on a field-list-item click that would now be
        # irrelevant to the currently active tab.
        executed_actions = [self.FIELD_TAB_CLICK, self.OPERATOR_TAB_CLICK]
        plan = [self.FORMULA_FILL]
        ok, reason = self_heal.validate_calculated_field_insertions(plan, executed_actions)
        self.assertTrue(ok)

    def test_ignores_non_formula_fill_even_with_field_tab_open(self):
        plan = [self.FIELD_TAB_CLICK,
                {"type": "fill", "selector": '[data-test-id="left-rail-search"]', "value": "id", "fillAction": "replace"}]
        ok, reason = self_heal.validate_calculated_field_insertions(plan)
        self.assertTrue(ok)  # filtering the field-list search box is not the formula editor itself


class TestSelectorValidation(unittest.TestCase):
    def test_rejects_dynamic_react_id(self):
        self.assertFalse(self_heal._selector_is_safe("#react-select-3-input"))
        self.assertFalse(self_heal._selector_is_safe(":r4a:"))

    def test_rejects_hash_like_class(self):
        self.assertFalse(self_heal._selector_is_safe(".css-1x2y3z"))
        self.assertFalse(self_heal._selector_is_safe(".sc-bZQynM"))

    def test_rejects_timestamp_like_id(self):
        self.assertFalse(self_heal._selector_is_safe(
            "#exc-app-sandbox-experiencePlatformUI-home-1786611819437"))

    def test_rejects_forbidden_attributes(self):
        self.assertFalse(self_heal._selector_is_safe('[aria-label="Activate"]'))
        self.assertFalse(self_heal._selector_is_safe("text=Activate"))
        self.assertFalse(self_heal._selector_is_safe(':has-text("Activate")'))
        self.assertFalse(self_heal._selector_is_safe('[placeholder="Search"]'))
        self.assertFalse(self_heal._selector_is_safe('[alt="icon"]'))

    def test_rejects_missing_selector(self):
        self.assertFalse(self_heal._selector_is_safe(None))
        self.assertFalse(self_heal._selector_is_safe(""))

    def test_accepts_data_test_id(self):
        self.assertTrue(self_heal._selector_is_safe('[data-test-id="primary-action-button"]'))

    def test_accepts_static_id(self):
        self.assertTrue(self_heal._selector_is_safe("#main-content"))

    def test_accepts_hand_authored_class(self):
        self.assertTrue(self_heal._selector_is_safe(".audience-actions-menu__activate"))

    def test_accepts_bare_iframe(self):
        self.assertTrue(self_heal._selector_is_safe("iframe"))

    def test_accepts_structural_path_from_stable_ancestor(self):
        # Ancestor-anchor + purely structural (tag/position) descent to a
        # target that itself has no attribute of its own — the technique
        # self-heal is now expected to try before giving up.
        self.assertTrue(self_heal._selector_is_safe(
            '[data-testid="schedule-row"] button:nth-of-type(2)'))
        self.assertTrue(self_heal._selector_is_safe(
            '.audience-actions-menu p:nth-child(1)'))

    def test_rejects_text_based_descent_even_with_safe_ancestor(self):
        # A stable ancestor doesn't excuse matching by text/aria-label
        # further down the path.
        self.assertFalse(self_heal._selector_is_safe(
            '[data-testid="schedule-row"] button:has-text("Edit")'))

    def test_validate_plan_selectors_rejects_whole_plan_on_one_bad_action(self):
        plan = [
            {"type": "click", "selector": '[data-test-id="ok"]', "framePathSelectors": []},
            {"type": "click", "selector": "text=Activate", "framePathSelectors": []},
        ]
        ok, reason = self_heal.validate_plan_selectors(plan)
        self.assertFalse(ok)
        self.assertIn("action 1", reason)

    def test_validate_plan_selectors_checks_frame_path_too(self):
        # title/name aren't categorically banned (they're conditionally
        # risky, per the locator rule — needs case-by-case confirmation,
        # not something a regex can judge) but aria-label always is.
        plan = [
            {"type": "click", "selector": '[data-test-id="ok"]',
             "framePathSelectors": ['iframe[aria-label="Main Content"]']},
        ]
        ok, reason = self_heal.validate_plan_selectors(plan)
        self.assertFalse(ok)

    def test_validate_plan_selectors_accepts_safe_plan(self):
        plan = [
            {"type": "click", "selector": '[data-test-id="ok"]', "framePathSelectors": ["iframe"]},
        ]
        ok, reason = self_heal.validate_plan_selectors(plan)
        self.assertTrue(ok)


def _skeleton_compiled(case_name="test-case"):
    return {
        "case_name": case_name,
        "compiled_from": f"cases/{case_name}.json",
        "last_updated": None,
        "steps": [
            {"step_number": 1, "type": "click", "selector": '[data-test-id="a"]',
             "clickBy": "selector", "framePathSelectors": [], "selectorNth": None,
             "hasDisambiguation": False, "matchCount": None, "optional": False,
             "onFailure": None, "locale_unsafe": False,
             "last_patched_by_agent": None, "patched_locales": []},
            {"step_number": 2, "type": "click", "selector": '[data-test-id="broken"]',
             "clickBy": "selector", "framePathSelectors": ["iframe"], "selectorNth": None,
             "hasDisambiguation": False, "matchCount": None, "optional": False,
             "onFailure": None, "locale_unsafe": False,
             "last_patched_by_agent": None, "patched_locales": []},
        ],
    }


class TestTryCompiledStepFailingIndex(unittest.TestCase):
    """try_compiled_step must identify EXACTLY which existing entry caused
    a runtime failure (global_index), and only that case, so callers can
    preserve everything else untouched.
    """

    def test_returns_none_on_success(self):
        data = _skeleton_compiled()
        # Only exercise step 1 (single entry) to keep this a pure success path.
        data["steps"] = [s for s in data["steps"] if s["step_number"] == 1]
        with mock.patch.object(compiled_steps, "execute_entry", lambda page, entry: None):
            ok, detail, failing_index = compiled_steps.try_compiled_step(None, data, 1)
        self.assertTrue(ok)
        self.assertIsNone(failing_index)

    def test_returns_none_when_locale_unsafe_pre_pass_rejects(self):
        data = _skeleton_compiled()
        data["steps"][1]["locale_unsafe"] = True
        ok, detail, failing_index = compiled_steps.try_compiled_step(None, data, 2)
        self.assertFalse(ok)
        self.assertIsNone(failing_index)

    def test_returns_exact_index_of_the_entry_that_actually_failed(self):
        data = _skeleton_compiled()
        data["steps"] = [
            {"step_number": 9, "type": "click", "selector": '[data-test-id="ok-1"]',
             "framePathSelectors": [], "selectorNth": None, "optional": False, "onFailure": None,
             "locale_unsafe": False},
            {"step_number": 9, "type": "click", "selector": '[data-test-id="broken"]',
             "framePathSelectors": [], "selectorNth": None, "optional": False, "onFailure": None,
             "locale_unsafe": False},
            {"step_number": 9, "type": "click", "selector": '[data-test-id="never-reached"]',
             "framePathSelectors": [], "selectorNth": None, "optional": False, "onFailure": None,
             "locale_unsafe": False},
        ]

        def fake_execute_entry(page, entry):
            if entry["selector"] == '[data-test-id="broken"]':
                raise RuntimeError("boom")

        with mock.patch.object(compiled_steps, "execute_entry", fake_execute_entry):
            ok, detail, failing_index = compiled_steps.try_compiled_step(None, data, 9)
        self.assertFalse(ok)
        self.assertEqual(failing_index, 2)  # the "broken" entry is #2 in the array


class TestPatchPartialStep(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = Path(self.tmpdir) / "compiled.json"
        self.path.write_text(json.dumps({
            "case_name": "test-case", "compiled_from": "cases/test-case.json", "last_updated": None,
            "steps": [
                {"step_number": 1, "type": "click", "selector": '[data-test-id="unrelated"]',
                 "framePathSelectors": [], "patched_locales": []},
                {"step_number": 9, "type": "click", "selector": '[data-test-id="ok-1"]',
                 "framePathSelectors": [], "patched_locales": []},
                {"step_number": 9, "type": "click", "selector": '[data-test-id="broken"]',
                 "framePathSelectors": [], "patched_locales": []},
                {"step_number": 9, "type": "click", "selector": '[data-test-id="never-reached"]',
                 "framePathSelectors": [], "patched_locales": []},
            ],
        }), encoding="utf-8")

    def test_preserves_entries_before_the_failing_index_byte_for_byte(self):
        before = json.loads(self.path.read_text(encoding="utf-8"))
        unrelated_step_before = before["steps"][0]
        ok_entry_before = before["steps"][1]

        new_plan = [
            {"type": "click", "selector": '[data-test-id="fixed"]', "framePathSelectors": [], "selectorNth": None},
        ]
        compiled_steps.patch_partial_step(self.path, 9, from_global_index=3, plan_actions=new_plan, locale="jp")

        # Raw disk read, not load_compiled — load_compiled normalizes
        # shorthand/minimal entries into the full canonical schema in
        # memory (see compiled_steps.normalize_entry), which would make an
        # untouched-but-minimal entry compare unequal to its pre-patch
        # form even though the WRITE itself never touched it. The
        # byte-for-byte guarantee this test checks is about what actually
        # lands on disk.
        after = json.loads(self.path.read_text(encoding="utf-8"))
        step1 = [s for s in after["steps"] if s["step_number"] == 1]
        step9 = [s for s in after["steps"] if s["step_number"] == 9]

        # Entry for a DIFFERENT step: completely untouched.
        self.assertEqual(step1[0], unrelated_step_before)
        # The entry that ran successfully before the failing one: untouched.
        self.assertEqual(step9[0]["selector"], '[data-test-id="ok-1"]')
        self.assertEqual(step9[0], ok_entry_before)
        # Only the failing entry (and whatever never ran after it) is replaced.
        self.assertEqual(len(step9), 2)
        self.assertEqual(step9[1]["selector"], '[data-test-id="fixed"]')


class TestPatchStep(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.path = Path(self.tmpdir) / "compiled.json"
        self.path.write_text(json.dumps(_skeleton_compiled()), encoding="utf-8")

    def test_patch_step_replaces_only_target_step(self):
        plan = [
            {"type": "click", "selector": '[data-test-id="fixed"]', "framePathSelectors": ["iframe"], "selectorNth": None},
        ]
        compiled_steps.patch_step(self.path, 2, plan, "jp")
        data = compiled_steps.load_compiled(self.path)

        step1 = [s for s in data["steps"] if s["step_number"] == 1]
        step2 = [s for s in data["steps"] if s["step_number"] == 2]
        self.assertEqual(len(step1), 1)
        self.assertEqual(step1[0]["selector"], '[data-test-id="a"]')  # untouched
        self.assertEqual(len(step2), 1)
        self.assertEqual(step2[0]["selector"], '[data-test-id="fixed"]')
        self.assertIn("jp", step2[0]["patched_locales"])
        self.assertIsNotNone(step2[0]["last_patched_by_agent"])
        self.assertFalse(step2[0]["locale_unsafe"])

    def test_patch_step_converts_fill_action_fields(self):
        plan = [
            {"type": "fill", "selector": '[data-test-id="input"]', "framePathSelectors": [],
             "selectorNth": None, "value": "hello", "fillAction": "append"},
        ]
        compiled_steps.patch_step(self.path, 2, plan, "en")
        data = compiled_steps.load_compiled(self.path)
        entry = [s for s in data["steps"] if s["step_number"] == 2][0]
        self.assertEqual(entry["type"], "fill")
        self.assertEqual(entry["value"], "hello")
        self.assertEqual(entry["fillAction"], "append")

    def test_mark_step_locale_unsafe_writes_placeholder(self):
        compiled_steps.mark_step_locale_unsafe(self.path, 2, "jp", type_hint="click")
        data = compiled_steps.load_compiled(self.path)
        entry = [s for s in data["steps"] if s["step_number"] == 2][0]
        self.assertTrue(entry["locale_unsafe"])
        self.assertIsNone(entry["selector"])
        self.assertEqual(entry["type"], "click")

        # try_compiled_step must route this straight back to fallback,
        # without raising on the missing selector.
        ok, detail, failing_index = compiled_steps.try_compiled_step(page=None, compiled_data=data, step_number=2)
        self.assertFalse(ok)
        self.assertIn("locale_unsafe", detail)
        self.assertIsNone(failing_index)


class TestKnowledgeSuggestion(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.path = Path(self.tmpdir) / "knowledge.json"
        self.path.write_text(json.dumps({
            "product": "TestProduct",
            "last_updated": None,
            "patterns": {
                "workflow_next_button": {
                    "description": "Generic Next button",
                    "selector": '[data-test-id="workflow.actions.next.btn"]',
                    "frame_path": {"selectors": ["iframe"], "basis": "structural-bare"},
                    "seen_in_cases": ["other-case"],
                    "verified_locales": ["en"],
                    "confidence": "high",
                },
            },
        }), encoding="utf-8")

    def test_rejects_non_generic_widget(self):
        suggestion = {"pattern_name": "foo", "selector": '[data-test-id="x"]', "generic_widget": False}
        accepted, reason = self_heal.maybe_accept_knowledge_suggestion(self.path, suggestion, "this-case", "en")
        self.assertFalse(accepted)

    def test_rejects_unsafe_selector(self):
        suggestion = {"pattern_name": "app_main_iframe", "selector": "text=Main", "generic_widget": True}
        accepted, reason = self_heal.maybe_accept_knowledge_suggestion(self.path, suggestion, "this-case", "en")
        self.assertFalse(accepted)

    def test_accepts_allowlisted_pattern_name(self):
        suggestion = {"pattern_name": "app_main_iframe", "description": "shell iframe",
                      "selector": "iframe", "generic_widget": True}
        accepted, reason = self_heal.maybe_accept_knowledge_suggestion(self.path, suggestion, "this-case", "en")
        self.assertTrue(accepted)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("app_main_iframe", data["patterns"])
        self.assertIn("this-case", data["patterns"]["app_main_iframe"]["seen_in_cases"])

    def test_accepts_cross_case_confirmed_selector(self):
        suggestion = {"pattern_name": "workflow_next_button", "description": "Generic Next button",
                      "selector": '[data-test-id="workflow.actions.next.btn"]', "generic_widget": True}
        accepted, reason = self_heal.maybe_accept_knowledge_suggestion(self.path, suggestion, "this-case", "jp")
        self.assertTrue(accepted)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        seen = data["patterns"]["workflow_next_button"]["seen_in_cases"]
        self.assertIn("other-case", seen)
        self.assertIn("this-case", seen)
        self.assertIn("jp", data["patterns"]["workflow_next_button"]["verified_locales"])

    def test_rejects_same_case_only_new_pattern(self):
        suggestion = {"pattern_name": "brand_new_widget", "description": "something specific",
                      "selector": '[data-test-id="only-here"]', "generic_widget": True}
        accepted, reason = self_heal.maybe_accept_knowledge_suggestion(self.path, suggestion, "this-case", "en")
        self.assertFalse(accepted)


def _skeleton_compiled_file(tmpdir, step_number=1):
    path = Path(tmpdir) / "compiled.json"
    path.write_text(json.dumps({
        "case_name": "test-case", "compiled_from": "cases/test-case.json", "last_updated": None,
        "steps": [],
    }), encoding="utf-8")
    return path


class TestSelfHealStepOrchestration(unittest.TestCase):
    """self_heal_step's own state machine (category-3, step_complete
    accumulation) — everything below capture_snapshot/invoke_self_heal is
    mocked out; only self_heal_step's own control flow is under test.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.compiled_path = _skeleton_compiled_file(self.tmpdir)
        self.knowledge_path = Path(self.tmpdir) / "knowledge.json"
        self.knowledge_path.write_text(json.dumps({"product": "TestProduct", "patterns": {}}), encoding="utf-8")

        patchers = [
            mock.patch.object(self_heal, "dismiss_stray_overlays", lambda page: None),
            mock.patch.object(self_heal, "capture_snapshot",
                               lambda page, fp, fallback_frame_path_candidates=None: ("aria", "html", b"png", "full page", None)),
            mock.patch.object(self_heal, "execute_plan_with_recovery", lambda page, plan: None),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_state_mismatch_does_not_write_locale_unsafe_or_patch(self):
        response = {
            "plan": [], "notes": "wrong page entirely", "state_mismatch": True, "step_complete": False,
        }
        with mock.patch.object(self_heal, "invoke_self_heal", lambda *a, **k: response):
            ok, detail = self_heal.self_heal_step(
                page=None, case_name="test-case", env="stage", locale="jp",
                step_number=26, step_description="select newly added audience",
                compiled_path=self.compiled_path, knowledge_path=self.knowledge_path,
                fallback_timeout=60,
            )
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("STATE_MISMATCH:"))
        data = compiled_steps.load_compiled(self.compiled_path)
        self.assertEqual(data["steps"], [])  # nothing patched, no locale_unsafe entry written

    def test_step_complete_false_accumulates_actions_across_rounds(self):
        responses = [
            {
                "plan": [{"type": "click", "selector": '[data-test-id="open-combobox"]',
                          "framePathSelectors": ["iframe"], "selectorNth": None}],
                "notes": "opened combobox, need to see options", "step_complete": False,
            },
            {
                "plan": [{"type": "click", "selector": '[data-test-id="option-1"]',
                          "framePathSelectors": ["iframe"], "selectorNth": None}],
                "notes": "picked an option", "step_complete": True,
            },
        ]
        call_iter = iter(responses)
        with mock.patch.object(self_heal, "invoke_self_heal", lambda *a, **k: next(call_iter)):
            ok, detail = self_heal.self_heal_step(
                page=None, case_name="test-case", env="stage", locale="jp",
                step_number=25, step_description="pick a source attribute",
                compiled_path=self.compiled_path, knowledge_path=self.knowledge_path,
                fallback_timeout=60,
            )
        self.assertTrue(ok)
        data = compiled_steps.load_compiled(self.compiled_path)
        entries = [s for s in data["steps"] if s["step_number"] == 25]
        # BOTH rounds' actions must be recorded, in order — not just the
        # last round's — so a future clean-start run replays the whole
        # sequence, not just the final step.
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["selector"], '[data-test-id="open-combobox"]')
        self.assertEqual(entries[1]["selector"], '[data-test-id="option-1"]')

    def test_fill_only_round_with_step_complete_false_is_not_rejected(self):
        # Regression: validate_plan_completes_selections must NOT reject a
        # legitimate "type to trigger suggestions this round, pick a real
        # option next round" partial step — only a plan claiming
        # step_complete=true must actually follow a fill with a click.
        responses = [
            {
                "plan": [{"type": "fill", "selector": '[data-test-id="attr-auto-complete::0"]',
                          "value": "acc", "fillAction": "replace", "framePathSelectors": ["iframe"]}],
                "notes": "typed to trigger suggestions", "step_complete": False,
            },
            {
                "plan": [{"type": "click", "selector": '[data-test-id="schema.tree.row"][data-node-path="accountName"]',
                          "framePathSelectors": ["iframe"], "selectorNth": None}],
                "notes": "picked a real rendered option", "step_complete": True,
            },
        ]
        call_iter = iter(responses)
        with mock.patch.object(self_heal, "invoke_self_heal", lambda *a, **k: next(call_iter)):
            ok, detail = self_heal.self_heal_step(
                page=None, case_name="test-case", env="stage", locale="jp",
                step_number=20, step_description="select any attribute",
                compiled_path=self.compiled_path, knowledge_path=self.knowledge_path,
                fallback_timeout=60,
            )
        self.assertTrue(ok, detail)
        data = compiled_steps.load_compiled(self.compiled_path)
        entries = [s for s in data["steps"] if s["step_number"] == 20]
        self.assertEqual(len(entries), 2)


if __name__ == "__main__":
    unittest.main()
