"""Tests for the convergence verdict parser — the line the whole loop hinges on.

Run: python3 -m pytest tests/  (pytest is the only dev dependency; runtime is stdlib-only)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cothink import extract_verdict, verdict  # noqa: E402


# --- fenced JSON block (the authoritative signal) --------------------------- #
def test_fenced_json_pass():
    assert extract_verdict('analysis...\n```json\n{"verdict": "pass"}\n```', "VERDICT") == "PASS"


def test_fenced_json_fail():
    assert extract_verdict('```json\n{"verdict": "fail"}\n```', "VERDICT") == "FAIL"


def test_fenced_json_case_insensitive_value():
    assert extract_verdict('```json\n{"verdict": "PASS"}\n```', "VERDICT") == "PASS"


def test_result_key_for_tester():
    assert extract_verdict('```json\n{"result": "pass"}\n```', "RESULT") == "PASS"


# --- JSON beats prose: a mentioned legacy line must NOT override the block --- #
def test_json_block_overrides_prose_mention():
    text = (
        "I considered emitting `VERDICT: PASS` but found a BLOCKER.\n"
        '```json\n{"verdict": "fail"}\n```'
    )
    assert extract_verdict(text, "VERDICT") == "FAIL"


def test_last_json_block_wins():
    text = '```json\n{"verdict": "fail"}\n```\nrevised:\n```json\n{"verdict": "pass"}\n```'
    assert extract_verdict(text, "VERDICT") == "PASS"


# --- bare JSON object (no fence) -------------------------------------------- #
def test_bare_json_object():
    assert extract_verdict('done. {"verdict": "pass"}', "VERDICT") == "PASS"


# --- legacy line fallback (back-compat) ------------------------------------- #
def test_legacy_line_pass():
    assert extract_verdict("...\nVERDICT: PASS", "VERDICT") == "PASS"


def test_legacy_line_equals_form_and_last_wins():
    assert extract_verdict("VERDICT = FAIL\nVERDICT: PASS", "VERDICT") == "PASS"


# --- missing -> None (caller re-prompts); wrapper defaults FAIL -------------- #
def test_missing_returns_none():
    assert extract_verdict("no verdict here at all", "VERDICT") is None


def test_wrapper_defaults_fail_when_missing():
    assert verdict("nothing", "VERDICT") == "FAIL"


def test_malformed_json_falls_through_to_none():
    assert extract_verdict('```json\n{"verdict": }\n```', "VERDICT") is None
