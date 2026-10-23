"""Validate the evaluator itself, not the unreleased candidate engine."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.evaluation.math_grading.corpus import CASES, corpus_hash
from tools.evaluation.math_grading.metrics import assess, compare, summarize
from tools.evaluation.math_grading.run import LimitedProvider, CallBudgetExceeded, execute


def observation(case, score):
    return {"id": case["id"], "score": score, "steps": [],
            "provider_entry_calls": 0, "fixed_response_calls": 1}


def test_corpus_has_auditable_labels_and_consistent_shared_problem():
    assert len(CASES) == 40
    assert len({c["id"] for c in CASES}) == len(CASES)
    assert {c["subject"] for c in CASES} == {"math", "physics"}
    assert len(corpus_hash()) == 64
    shared = [c for c in CASES if c["shared_group"] == "linear_equation"]
    assert len({c["problem"]["criterion"] for c in shared}) == 1
    assert len({c["problem"]["stem"] for c in shared}) == 1
    for case in CASES:
        assert case["label_kind"] == "synthetic_explicit_rule"
        assert case["expected"]["rationale"]
        interval = case["expected"]["score_interval"]
        if interval:
            assert 0 <= interval[0] <= interval[1] <= case["problem"]["max_score"]
        else:
            assert case["replay"]["model_fault"]
        for s in case["replay"]["response"]["steps"]:
            assert s["score"] >= 0


def test_no_score_is_not_zero_and_unknown_metrics_are_not_success():
    blank = next(c for c in CASES if c["id"] == "C07")
    failed = next(c for c in CASES if c["id"] == "K05")
    result = summarize([blank, failed], [observation(blank, 0), observation(failed, None)])
    all_metrics = result["aggregates"]["all"]
    assert all_metrics["synthetic_interval_hit"] == {"value": 1, "observed": 1, "total": 2}
    assert all_metrics["no_score"]["value"] == .5
    assert all_metrics["quote_alignment"] == {"value": None, "observed": 0, "total": 2}
    assert result["real_teacher_interval_hit"]["observed"] == 0
    assert assess(failed, observation(failed, 0))["score_available_as_expected"] is False
    assert assess(blank, observation(blank, None))["synthetic_interval_hit"] is False
    for bad in [float("nan"), float("inf"), True, -1, 11]:
        assert assess(blank, observation(blank, bad))["no_score"] is True


def test_negative_evidence_is_detected():
    case = CASES[0]
    result = observation(case, 10)
    result.update(
        claims=[{"quote": "两边除以2"}],  # Absent in this student's answer.
        deductions=[{"error_id": "e1", "rule_id": "r1"}, {"error_id": "e1", "rule_id": "r1"}],
        verification_claims=[{"claimed_verified": True, "evidence_status": "unknown"}],
        required_handling=True,
        steps=[{"score": 2}])
    checked = assess(case, result)
    assert checked["interval_distance"] == 8
    assert checked["over_credit"] is True
    for key in ("quote_alignment", "duplicate_deduction_free", "unsupported_verified_claim_free",
                "no_unwanted_forced_handling", "step_sum_consistent"):
        assert checked[key] is False


def test_results_cannot_silently_drop_or_duplicate_cases():
    with pytest.raises(ValueError):
        summarize(CASES[:2], [observation(CASES[0], 2)])
    with pytest.raises(ValueError):
        summarize(CASES[:1], [observation(CASES[0], 2)] * 2)
    with pytest.raises(ValueError):
        compare({"corpus_sha256": "one"}, {"corpus_sha256": "two"})


def test_comparison_never_turns_failure_into_zero_delta():
    before = {"corpus_sha256": corpus_hash(), "results": [observation(CASES[0], None)]}
    after = {"corpus_sha256": corpus_hash(), "results": [observation(CASES[0], 0)]}
    assert compare(before, after)[0]["delta"] is None


def test_shared_plan_drift_and_missing_evidence():
    pair = CASES[:2]
    results = [dict(observation(c, 2), plan_id=f"plan-{i}") for i, c in enumerate(pair)]
    assert summarize(pair, results)["shared_rule_consistency"][0]["consistent"] is False
    results[1].pop("plan_id")
    assert summarize(pair, results)["shared_rule_consistency"][0]["consistent"] is None


def test_fixed_baseline_artifact_exposes_policy_failures():
    path = Path(__file__).resolve().parents[3] / "tools/evaluation/math_grading/reports/baseline-main.json"
    baseline = json.loads(path.read_text())
    assert baseline["corpus_sha256"] == corpus_hash()
    assert baseline["base_revision"] == "0304ff2e0cc294ada51c99af8d5ab07540cb61a8"
    results = {r["id"]: r for r in baseline["results"]}
    assert [results[id]["score"] for id in ("C01", "C02", "C03")] == [10, 10, 7]
    assert results["K05"]["score"] is None and results["K06"]["score"] is None
    assert baseline["metrics"]["provider_entry_calls"] == 0
    assert baseline["opposite_experts_probe"]["legacy_attention"] is False
    # Recompute to ensure the report's scores and aggregate denominators agree.
    assert summarize(CASES, baseline["results"]) == baseline["metrics"]


def test_live_guard_refuses_without_cost_ack_before_provider_build(monkeypatch, tmp_path):
    monkeypatch.setattr("tools.evaluation.math_grading.run.revision",
                        lambda: "0304ff2e0cc294ada51c99af8d5ab07540cb61a8")
    args = SimpleNamespace(mode="live", adapter=None, enable_live=False, acknowledge_cost=None,
                           output=tmp_path / "not-created.json")
    with pytest.raises(ValueError, match="explicit"):
        asyncio.run(execute(args))
    assert not args.output.exists()


def test_provider_entry_budget_and_output_cap():
    calls = []

    class Provider:
        async def ainvoke(self, messages, *, max_output_tokens):
            calls.append(max_output_tokens)
            return SimpleNamespace(input_tokens=12, output_tokens=2, duration_ms=3)

    async def run():
        provider = LimitedProvider(Provider(), calls=1, tokens=32)
        await provider.ainvoke([], max_output_tokens=100)
        with pytest.raises(CallBudgetExceeded):
            await provider.ainvoke([])
        assert provider.calls == 1

    asyncio.run(run())
    assert calls == [32]


def test_replay_does_not_reuse_or_modify_the_oracle():
    from tools.evaluation.math_grading.candidate import evaluate
    from tools.evaluation.math_grading.candidate_fixtures import AWARDS
    from unittest.mock import patch
    # A deliberately wrong fixed response stays wrong despite an expected 10.
    # This catches oracle leakage through the actual candidate chain.
    case = deepcopy(next(c for c in CASES if c["id"] == "P01"))
    with patch.dict(AWARDS, {"P01": [0, 1]}):
        result = asyncio.run(evaluate(case))
    assert result["score"] == 1
    assert assess(case, result)["synthetic_interval_hit"] is False
    assert case["expected"]["score_interval"] == [10, 10]
