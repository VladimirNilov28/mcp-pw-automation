"""Loader for test case JSON files (see cases/*.json)."""

import json
from pathlib import Path

# A case's first step is conventionally a "Login to the app"-style instruction.
# Login is handled separately by the system prompt's dedicated login section
# (using creds.json), so it must never be counted in scenario step numbering
# or screenshot filenames. We strip it here, once, so every downstream
# consumer (prompt builder, step numbering, screenshot naming) works off an
# already-clean steps list instead of each having to know to skip step 1.
LOGIN_STEP_MARKER = "login"


def load_case(path):
    """Load a case JSON file and return (case_name, steps).

    Validates that case_name is a non-empty string and steps is a non-empty
    list of strings. Strips a leading login-like step if present.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    case_name = data.get("case_name")
    steps = data.get("steps")

    if not isinstance(case_name, str) or not case_name.strip():
        raise ValueError(f"{path}: 'case_name' must be a non-empty string")
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"{path}: 'steps' must be a non-empty list")
    if not all(isinstance(s, str) and s.strip() for s in steps):
        raise ValueError(f"{path}: every entry in 'steps' must be a non-empty string")

    if LOGIN_STEP_MARKER in steps[0].lower():
        steps = steps[1:]

    if not steps:
        raise ValueError(f"{path}: no scenario steps left after stripping the login step")

    return case_name, steps


def case_path(case_name, cases_dir="cases"):
    return Path(cases_dir) / f"{case_name}.json"
