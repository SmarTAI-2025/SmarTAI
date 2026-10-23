"""Wire compatibility and positive controls; never replace B1 failure cases."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from tools.evaluation.math_grading.reliability import evaluate
from tools.evaluation.math_grading.reliability_cases import CASES, corpus_hash
from tools.evaluation.math_grading.reliability_v2 import wire_v2, feedback_v2


def test_v2_translation_preserves_frozen_inputs_amounts_checks_and_hostile_quotes():
    frozen = Path(__file__).resolve().parents[3] / "tools/evaluation/math_grading/reports/reliability-baseline-109acaec.json"
    before = corpus_hash()
    assert before == json.loads(frozen.read_text())["corpus_sha256"]
    for case in CASES:
        original = case["wire"]
        adapted = wire_v2(original)
        assert adapted["plan"]["effective_criterion"] == original["plan"]["effective_criterion"]
        for left, right in zip(original["plan"]["routes"], adapted["plan"]["routes"]):
            assert left["mode"] == right["mode"]
            assert [(i["id"], i["points"]) for i in left["items"]] == [(i["id"], i["points"]) for i in right["items"]]
        for key in ("assessment", "feedback", "source_repair", "repair"):
            if key not in original:
                continue
            left, right = original[key], adapted[key]
            if not isinstance(left, dict):
                assert left == right
                continue
            for field in ("score", "checks", "deductions"):
                assert left.get(field) == right.get(field)
            assert [(a["item_id"], a["score"]) for a in left.get("awards", [])] == [
                (a["item_id"], a["score"]) for a in right.get("awards", [])]
            assert [(c["id"], c["quote"]) for c in left["claims"]] == [
                (c["id"], c["quote"]) for c in right["claims"] if c.get("kind") != "omission"]
    assert corpus_hash() == before


def test_evidence_ids_do_not_author_new_scores_or_verdicts():
    payload = deepcopy(CASES[0]["wire"]["assessment"])
    before = deepcopy(payload)
    result = feedback_v2(payload, [dict(evidence_id="v1", claim_id="s1", status="refuted")])
    assert result["evidence_judgments"][0]["applicability"] == "uncertain"
    assert {k:v for k,v in result.items() if k != "evidence_judgments"} == before == payload


@pytest.mark.parametrize("id,score", [("R13_follow_through", 9), ("R14_independent_second_error", 8)])
def test_supported_policy_wording_control_keeps_independent_error_semantics(id, score):
    # This separate positive control locates the observed failure in the source
    # policy recognizer. R13/R14 in the acceptance corpus remain untouched/red.
    case = deepcopy(next(c for c in CASES if c["id"] == id))
    criterion = case["problem"]["criterion"].replace("同源后续错误", "同源错误")
    case["problem"]["criterion"] = criterion
    case["wire"]["plan"]["effective_criterion"] = criterion
    row = asyncio.run(evaluate(case))
    assert row["score"] == score and not row["contract_failures"], row["contract_failures"]
    assert row["plans"][0]["routes"][0]["error_policy"] == "no_repeat_deduction"


def test_unsupported_function_is_unknown_at_real_worker_boundary():
    from backend.tools.math_verification import MathVerificationRequest, verify_math
    result = asyncio.run(verify_math(MathVerificationRequest(
        student_expression="f(1)", reference_expression="f(1)")))
    assert result.status == "unknown"
