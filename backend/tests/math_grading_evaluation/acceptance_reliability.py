"""Explicit capability gate: intentionally FAILS on baseline; no xfail/skip.

Run this file explicitly. It is not named test_ because B1 ships a known-red
acceptance contract separately from the harness integrity regression suite.
B2 must update only wire envelopes/observers for the finalized internal schema,
preserving independently authored meanings and expected scores.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tools.evaluation.math_grading.reliability import evaluate
from tools.evaluation.math_grading.reliability_cases import CASES
from tools.evaluation.math_grading.reliability_probes import execute_probes


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_independent_capability_contract(case):
    row = asyncio.run(evaluate(case))
    assert not row["contract_failures"], row["contract_failures"]


@pytest.fixture(scope="module")
def boundary_rows():
    root = Path(__file__).resolve().parents[3]
    frozen = json.loads((root / "tools/evaluation/math_grading/reports/reliability-baseline-109acaec.json").read_text())
    old_plan = frozen["results"][0]["plans"][0]
    return {r["id"]: r for r in asyncio.run(execute_probes(old_plan))}


@pytest.mark.parametrize("id", [*[f"J{i:02d}_{name}" for i, name in enumerate(
    ("inline_source", "display_source", "source_whitespace"), 1)],
    "S01_exact", "S02_inline_math", "S03_display_math", "S04_math_whitespace", "S05_exponent",
    "S06_quantifier", "S07_condition", "S08_chain_compression", "S09_digit_boundary", "S10_word_boundary",
    "S11_sign", "S12_ambiguous", "S13_context_resolves", "S14_unpaired_delimiter",
    "V01_engine", "V02_schema", "V03_interpretation", "V04_legacy_snapshot", "V05_legacy_trace",
    "L01_cancel_context", "L02_same_question_retry", "L03_wrong_engine_snapshot", "L04_wrong_engine_expert"])
def test_source_and_recovery_contract(id, boundary_rows):
    assert boundary_rows[id]["pass_contract"], boundary_rows[id]
