"""Regression: ProgrammingGradingOutput must accept ``"logs": null``.

W1 baseline caught a P0 credibility bug: the LLM naturally emits
``"logs": null`` (the prompt asks for a `logs` field but there is often nothing
to log), yet ``ProgrammingGradingOutput.logs`` was a non-Optional ``str``.
Pydantic rejected the null → every JSON-repair attempt in
``extract_and_parse_json`` hit the same ``ValidationError`` → the skill fell
back to ``_blank_result`` → a completely correct code answer was recorded as
0 / confidence 0 ("AI 返回格式异常无法解析").

The fix (commit 6db1ccc) made ``logs: Optional[str]``. These tests lock that
fix so nobody re-tightens the field and silently turns correct answers back
into zeros.
"""
from __future__ import annotations

import os

os.environ["SMARTAI_HTTP_PROXY"] = ""
os.environ["SMARTAI_HTTPS_PROXY"] = ""

import pytest

from backend.skills.programming import ProgrammingGradingOutput
from backend.tools.structured_llm import extract_and_parse_json


# ─── 1. Model layer: null logs must validate, not raise ──────────────────────

def test_programming_grading_output_accepts_null_logs():
    """Direct model validation: ``"logs": null`` must not trip Pydantic.

    This is the exact regression of the P0 — before the fix this raised
    ValidationError, which is what turned a correct grade into a 0.
    """
    out = ProgrammingGradingOutput.model_validate({
        "score": 10.0, "max_score": 10.0, "confidence": 0.95,
        "comment": "Clean code.", "steps": [], "logs": None,
    })
    assert out.score == 10.0
    assert out.logs in (None, "")  # Optional[str], null tolerated


def test_programming_grading_output_accepts_missing_logs():
    """A missing ``logs`` field must also validate (default="")."""
    out = ProgrammingGradingOutput.model_validate({
        "score": 8.0, "max_score": 10.0, "confidence": 0.8,
        "comment": "ok", "steps": [],
    })
    assert out.score == 8.0
    assert out.logs == ""


def test_programming_grading_output_accepts_string_logs():
    """Normal string logs still validate (non-regression of the non-null path)."""
    out = ProgrammingGradingOutput.model_validate({
        "score": 9.0, "max_score": 10.0, "confidence": 0.9,
        "comment": "ok", "steps": [], "logs": "sandbox: 3/3 passed",
    })
    assert out.logs == "sandbox: 3/3 passed"


# ─── 2. Parse layer: realistic LLM JSON with null logs parses cleanly ────────

def test_extract_and_parse_json_handles_null_logs_in_raw_output():
    """The full ``extract_and_parse_json`` path (the one the skill actually
    calls) must parse a realistic LLM response containing ``"logs": null``
    without falling back to a confidence-0 blank result.

    Before the fix this raised ValidationError on every repair attempt.
    """
    raw = (
        "```json\n"
        "{\n"
        '  "score": 10.0,\n'
        '  "max_score": 10.0,\n'
        '  "confidence": 0.95,\n'
        '  "comment": "All test cases passed.",\n'
        '  "steps": [],\n'
        '  "logs": null\n'
        "}\n"
        "```"
    )
    parsed = extract_and_parse_json(raw, ProgrammingGradingOutput)
    assert parsed.score == 10.0
    assert parsed.confidence == 0.95
    assert parsed.logs in (None, "")


def test_extract_and_parse_json_handles_null_logs_with_inline_newlines():
    """null logs combined with another known parser hazard (inline newlines
    in comment) must still parse — the null-logs fix must not regress the
    JSON-repair logic.
    """
    raw = (
        "{\n"
        '  "score": 8,\n'
        '  "max_score": 10,\n'
        '  "confidence": 0.8,\n'
        '  "comment": "line1\nline2",\n'
        '  "steps": [],\n'
        '  "logs": null\n'
        "}"
    )
    parsed = extract_and_parse_json(raw, ProgrammingGradingOutput)
    assert parsed.score == 8.0
    assert "line1" in parsed.comment
    assert parsed.logs in (None, "")
