"""Harness integrity tests; passing these does NOT mean capability acceptance."""
import asyncio
from copy import deepcopy
import inspect
import json
from pathlib import Path

import pytest

from tools.evaluation.math_grading import reliability
from tools.evaluation.math_grading.reliability_cases import CASES, corpus_hash
from tools.evaluation.math_grading.reliability_probes import recovery_probes


def selected(id):
    return next(c for c in CASES if c["id"] == id)


def test_raw_boundary_does_not_consult_oracle_or_patch_parser():
    provider_source = inspect.getsource(reliability.RawProvider)
    assert '["oracle"]' not in provider_source
    evaluator_source = inspect.getsource(reliability.evaluate)
    assert 'patch.object(rubric, "structured_llm_call"' not in evaluator_source
    assert 'patch.object(math_grading, "structured_llm_call"' not in evaluator_source
    assert "actual_prepare(*args, **kwargs)" in evaluator_source
    assert "actual_verify(request)" in evaluator_source


def test_real_raw_json_to_verifier_feedback_ledger_and_public_result():
    row = asyncio.run(reliability.evaluate(selected("R01_real_tool_feedback")))
    assert row["contract_failures"] == []
    assert row["score"] == 0
    assert row["tool_calls"][0]["evidence"]["status"] == "refuted"
    assert row["tool_calls"][0]["evidence"]["method"] != "injected"
    assert row["observations"]["stage_counts"] == dict(plan=1, plan_ready=1, assessment=1, tool_feedback=1)
    assert row["events"][0]["raw_response"].startswith("```json")
    assert row["events"][1]["stage"] == "plan_ready"
    assert "student_answer" not in row["events"][0]["question_keys"]
    assert all(e.get("plan_id", row["plans"][0]["plan_id"]) == row["plans"][0]["plan_id"] for e in row["events"])
    assert row["traces"][0]["assessment"]["claims"][0]["quote"] == "41"
    assert row["traces"][0]["assessment"]["claims"][0]["verdict"] == "refuted"


def test_oracle_change_cannot_change_raw_fixture_or_grade():
    first = deepcopy(selected("R10_correct_result_wrong_work"))
    second = deepcopy(first)
    second["oracle"]["score"] = 9
    a = asyncio.run(reliability.evaluate(first))
    b = asyncio.run(reliability.evaluate(second))
    assert a["score"] == b["score"] == 2
    assert [e.get("raw_sha256") for e in a["events"]] == [e.get("raw_sha256") for e in b["events"]]
    assert not a["contract_failures"] and b["contract_failures"]


def test_denominator_retains_failure_and_cancellation():
    cases = [selected(id) for id in ("R10_correct_result_wrong_work", "R30_provider_failure", "R31_cancelled")]
    rows = [asyncio.run(reliability.evaluate(c)) for c in cases]
    summary = reliability.summarize(rows)
    assert summary["total"] == 3
    assert summary["score_available"] == dict(count=1, total=3, rate=1 / 3)
    assert summary["status_counts"] == dict(scored=1, failed=1, cancelled=1)
    assert summary["upstream_calls"] == summary["billed_cost"] == 0
    assert summary["model_accuracy"] is None


def test_checks_empty_opportunities_not_confused_with_prefilled_tool_tests():
    opportunities = [c for c in CASES if c["oracle"].get("applicable")]
    assert len(opportunities) == 6
    assert all(c["wire"]["assessment"]["checks"] == [] for c in opportunities)
    assert not selected("R01_real_tool_feedback")["oracle"].get("applicable")
    assert {c["group"] for c in CASES} >= {"math/numeric", "math/symbolic", "math/proof", "physics/numeric", "physics/concept"}


def test_cancel_retry_and_wrong_version_are_in_process_only():
    rows = asyncio.run(recovery_probes())
    assert all(r["pass_contract"] for r in rows), rows


def test_frozen_baseline_report_matches_corpus_and_has_no_harness_errors():
    root = Path(__file__).resolve().parents[3]
    report = json.loads((root / "tools/evaluation/math_grading/reports/reliability-baseline-109acaec.json").read_text())
    assert report["corpus_sha256"] == corpus_hash()
    assert len(report["results"]) == len(CASES)
    assert report["summary"] == reliability.summarize(report["results"])
    assert all(r["status"] != "harness_error" for r in report["results"])
    assert any(r["contract_failures"] for r in report["results"])


def test_network_is_unavailable_in_replay():
    with pytest.raises(AssertionError, match="forbids network"):
        reliability.no_network()
