"""Keep the shared UI/browser fixtures on the real programming-test schema."""
import json
from pathlib import Path

import pytest

from backend.models import TestCase

FIXTURES = json.loads((Path(__file__).resolve().parents[2]
    / "frontend/app/src/test/fixtures/questionAnalysisMaterials.json").read_text())


@pytest.mark.parametrize("group", FIXTURES)
def test_question_analysis_materials_match_backend_schema(group):
    for case in FIXTURES[group]:
        assert TestCase.model_validate(case).model_dump() == case


def test_reported_function_returns_remain_repr_strings():
    cases = FIXTURES["functionCases"]
    assert [case["function_args"] for case in cases] == [[i] for i in range(6)]
    assert [case["expected_return"] for case in cases] == ["0", "False", "''", "None", "5", "6"]
    assert all(case["io_mode"] == "function" for case in cases)
