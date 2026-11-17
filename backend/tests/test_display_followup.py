"""Presentation regressions preserve stored and newly settled totals."""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace

import pytest

from backend.services.grading_feedback import _part_steps
from backend.services.part_rubric import PartRubric
from backend.skills.part_scoring import PartDecision, settle_parts
from backend.tests.test_scoring_structure import ANSWER, decision, rules
from backend.tests.test_task_grading_retry import case, start  # noqa: F401


def mixed_fixture(*, rate=1, count=1, penalty=None):
    rubric = PartRubric.model_validate(dict(id="q", label="1", stem="Calculate the result.", total_pts=10,
        parts=[dict(id="p", total_pts=10,
            steps=[dict(id="setup", criterion="Setup", total_pts=5), dict(id="result", criterion="Result", total_pts=5)],
            deductions=[dict(id="error", criterion="Independent arithmetic error", deduct_pts=rate, **(penalty or {}))])]))
    reading = PartDecision(part_id="p", target_quote="part:p", student_quote="student:1", relevance="partial",
        method="reference", progress="The setup is correct but the calculation is wrong.",
        steps=[dict(id="setup", outcome="earned", evidence="student:1", reason="The setup is correct."),
               dict(id="result", outcome="missed", evidence="student:2", reason="The final result is wrong.")],
        errors=[dict(id=f"e{i}", rule_id="error", evidence=f"student:{i}", origin_quote=f"student:{i}",
                     reason=f"Calculation {i} reverses the sign of the constant.") for i in range(1, count + 1)])
    return rubric, reading


@pytest.mark.parametrize("method", ["alternative", "reference"])
def test_old_generic_reasons_replay_without_weakening_new_grading(method):
    rubric = rules(deduction=method == "reference")
    d = decision(method=method)
    d = d.model_copy(update={"errors": tuple(e.model_copy(update={"reason": f"Independent error {i}"})
                                           for i, e in enumerate(d.errors, 1))})
    saved = [dict(step_no=1, part_id="p", score=7, is_correct=False, desc="Original explanation")]
    before = deepcopy((saved, d))
    shown = _part_steps(saved, SimpleNamespace(part_decisions=[d]), rubric, answer=ANSWER)
    assert [item["points"] for item in shown[0]["deductions"]] == [1, 1, 1]
    assert [item["desc"] for item in shown[0]["deductions"]] == [e.reason for e in d.errors]
    assert shown[0].get("is_alternative", False) == (method == "alternative")
    assert shown[0]["score"] == 7 and shown[0]["desc"] == "Original explanation"
    assert _part_steps(shown, SimpleNamespace(part_decisions=[d]), rubric, answer=ANSWER) == shown
    assert (saved, d) == before
    with pytest.raises(ValueError, match="specific wrong statement"):
        settle_parts(rubric, [d], ANSWER, rubric.stem)


@pytest.mark.parametrize("rate,count,penalty,expected", [(1, 1, None, [1]), (3, 3, None, [3, 2]),
    (.125, 3, None, [.125] * 3), (1, 3, {"limit": 1}, [1]),
    (1, 3, {"max_deduction": 1.5}, [1, .5])])
def test_mixed_rules_show_awards_and_effective_losses_without_adding_the_net(rate, count, penalty, expected):
    rubric, d = mixed_fixture(rate=rate, count=count, penalty=penalty)
    score, steps = settle_parts(rubric, [d], ANSWER, rubric.stem)
    expected_score = (Decimal(5) - sum(Decimal(str(value)) for value in expected)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    assert score == float(expected_score)
    assert [s.score for s in steps] == [score]
    row = steps[0].model_dump()
    assert [a["points"] for a in row["awards"]] == [5, 0]
    assert [a["item_id"] for a in row["awards"]] == ["setup", "result"]
    assert [a["desc"] for a in row["awards"]] == [s.reason for s in d.steps]
    assert [loss["points"] for loss in row["deductions"]] == expected
    assert row["deduction_base"] == 5
    assert sum(a["points"] for a in row["awards"]) - sum(expected) == pytest.approx(score - row["rounding_adjustment"])
    old = [{k: v for k, v in row.items() if k not in {"awards", "deductions", "deduction_base", "rounding_adjustment"}}]
    assert _part_steps(old, SimpleNamespace(part_decisions=[d]), rubric, answer=ANSWER) == [row]


@pytest.mark.parametrize("damage", ["missing_answer", "origin", "evidence", "rule", "duplicate", "amount"])
def test_historical_generic_reasons_still_require_original_sources_rules_and_totals(damage):
    rubric = rules()
    d = decision()
    errors = [e.model_copy(update={"reason": f"Independent error {i}"}) for i, e in enumerate(d.errors, 1)]
    answer = ANSWER
    saved = [dict(step_no=1, part_id="p", score=7, is_correct=False, desc="Saved explanation")]
    if damage == "missing_answer": answer = None
    elif damage == "amount": saved[0]["score"] = 8
    elif damage == "duplicate": errors[1] = errors[1].model_copy(update={"origin_quote": errors[0].origin_quote})
    else: errors[0] = errors[0].model_copy(update={"rule_id" if damage == "rule" else damage if damage == "evidence" else "origin_quote": "missing"})
    d = d.model_copy(update={"errors": tuple(errors)})
    assert _part_steps(saved, SimpleNamespace(part_decisions=[d]), rubric, answer=answer) == saved


def test_mixed_zero_losses_preserve_original_frozen_award_rows():
    rubric, d = mixed_fixture(count=0)
    score, rows = settle_parts(rubric, [d], ANSWER, rubric.stem)
    assert score == 5
    assert [(s.item_id, s.score, s.deductions, s.awards) for s in rows] == [
        ("setup", 5, None, None), ("result", 0, None, None)]


@pytest.mark.parametrize("weights", [[1, 1], [3, 1], [0, 1]])
def test_mixed_weighted_details_keep_the_unchanged_single_net_score(monkeypatch, weights):
    from backend.models import ExpertResult
    from backend.skills import math_grading
    monkeypatch.setattr(math_grading, "read_math_trace", lambda _: {"assessment": {"route_id": "parts", "part_decisions": [{}]}})
    experts = []
    for count in [1, 3]:
        rubric, d = mixed_fixture(count=count)
        score, rows = settle_parts(rubric, [d], ANSWER, rubric.stem)
        experts.append(ExpertResult(provider=f"Model {count}", score=score, max_score=10, comment="", steps=rows))
    weights = list(map(Decimal, weights))
    net = sum(Decimal(str(e.score)) * w for e, w in zip(experts, weights)) / sum(weights)
    merged = math_grading.weighted_math_steps(experts, weights, 10, float(net))
    assert [row.score for row in merged] == [float(net)]
    assert sum(a.points for a in merged[0].awards) - sum(d.points for d in merged[0].deductions) == pytest.approx(float(net))


def test_mixed_and_alternative_synthesis_does_not_show_losses_without_earned_balance(monkeypatch):
    from backend.models import ExpertResult
    from backend.skills import math_grading
    monkeypatch.setattr(math_grading, "read_math_trace", lambda _: {"assessment": {"route_id": "parts", "part_decisions": [{}]}})
    rubric, d = mixed_fixture()
    score, mixed = settle_parts(rubric, [d], ANSWER, rubric.stem)
    alternate_score, alternate = settle_parts(rules(), [decision()], ANSWER, rules().stem)
    experts = [ExpertResult(provider="Mixed", score=score, max_score=10, comment="", steps=mixed),
               ExpertResult(provider="Alternate", score=alternate_score, max_score=10, comment="", steps=alternate)]
    merged = math_grading.weighted_math_steps(experts, [Decimal(1), Decimal(1)], 10, 5.5)
    assert [s.score for s in merged] == [5.5]
    assert merged[0].deductions is None and merged[0].awards is None
    assert experts[0].steps[0].awards and experts[1].steps[0].deductions


def browser_fixture():
    """Actual settlement/projection DTOs for offline browser coverage."""
    rubric, d = mixed_fixture()
    score, rows = settle_parts(rubric, [d], ANSWER, rubric.stem)
    new = [row.model_dump(mode="json") for row in rows]
    old = [{k: v for k, v in row.items() if k not in {"awards", "deductions", "deduction_base"}} for row in new]
    shown = _part_steps(old, SimpleNamespace(part_decisions=[d]), rubric, answer=ANSWER)
    generic = decision()
    generic = generic.model_copy(update={"errors": tuple(e.model_copy(update={"reason": f"Independent error {i}"})
                                 for i, e in enumerate(generic.errors, 1))})
    legacy = [dict(step_no=1, part_id="p", score=7, is_correct=False, desc="Saved generic explanation")]
    restored = _part_steps(legacy, SimpleNamespace(part_decisions=[generic]), rules(), answer=ANSWER)
    return [dict(name="Mixed new", score=score, steps=new), dict(name="Mixed saved", score=score, steps=shown),
            dict(name="Historical alternative", score=7, steps=restored)]


@pytest.mark.parametrize("kind", ["mixed_new", "mixed_saved", "generic_saved"])
def test_display_details_survive_persistence_without_writing_historical_results(case, kind):
    import hashlib
    import json
    from sqlalchemy import select
    from backend.db import grading_repository as grades
    from backend.db.models import SubmissionAnswerRecord
    from backend.db.session import session_scope
    from backend.models import Correction, ExpertResult, StepScore
    from backend.services import task_facade
    from backend.services.math_grading_context import problem_from_question
    from backend.services.math_rubric_plan import MathRubricPlan
    from backend.skills.math_grading import MathAssessment, TRACE_VERSION
    from backend.tests.test_grading_checkpoints import prepare, stop
    from backend.tests.test_math_grading import plan_for

    with session_scope() as session:
        for answer in session.scalars(select(SubmissionAnswerRecord)):
            answer.content = ANSWER
    run_id = start(case, "display-followup-" + kind)["job_id"]
    checkpoints, questions, revisions, setup = prepare(case, run_id)
    problem = problem_from_question(questions[0])
    rubric, d = mixed_fixture() if kind.startswith("mixed") else (rules(), decision())
    score, rows = settle_parts(rubric, [d], ANSWER, problem.stem)
    steps = [row.model_dump(mode="json") for row in rows]
    if kind == "generic_saved":
        d = d.model_copy(update={"errors": tuple(e.model_copy(update={"reason": f"Independent error {i}"})
                             for i, e in enumerate(d.errors, 1))})
    if kind != "mixed_new":
        removed = {"awards", "deductions", "deduction_base"}
        if kind == "generic_saved": removed |= {"rounding_adjustment", "is_alternative"}
        steps = [{k: v for k, v in row.items() if k not in removed} for row in steps]
    plan_data = plan_for(problem).model_dump(mode="json", exclude={"plan_id"})
    plan_data["part_rubric"] = rubric.model_dump(mode="json")
    plan_data["plan_id"] = hashlib.sha256(json.dumps(plan_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    plan = MathRubricPlan.model_validate(plan_data)
    checkpoints.save_math_plan(problem, plan)
    assessment = MathAssessment(route_id="parts", route_reason="Frozen", comment="Original feedback.", part_decisions=[d])
    expert = ExpertResult(provider="Saved model", provider_id=setup.setup["primary_provider_id"], score=score, max_score=10,
        confidence=.99, comment=assessment.comment, steps=[StepScore(**row) for row in steps], raw_output=json.dumps(
            dict(version=TRACE_VERSION, plan_id=plan.plan_id, assessment=assessment.model_dump(mode="json"))))
    checkpoints.save_correction(revisions[0][1], Correction(q_id="q1", type=problem.type, score=score, max_score=10,
        confidence=.99, comment=expert.comment, steps=expert.steps, expert_results=[expert], synthesis_method="single"))
    stop(case, run_id)
    before = [r.model_dump() for r in grades.list_results_for_run(run_id)]
    payload = task_facade.task_results(task_id=case[0], owner_id=case[1])
    shown = next(c for s in payload["results"] for c in s["corrections"] if c["q_id"] == "q1")
    projected = shown["expert_results"][0]["steps"][0]
    assert shown["provisional_score"] == score and projected["score"] == score
    if kind == "generic_saved":
        assert projected["is_alternative"]
        assert [item["desc"] for item in projected["deductions"]] == [f"Independent error {i}" for i in range(1, 4)]
    else:
        assert [a["points"] for a in projected["awards"]] == [5, 0]
        assert [item["points"] for item in projected["deductions"]] == [1]
    assert shown["steps"] == shown["expert_results"][0]["steps"]
    assert task_facade.task_results(task_id=case[0], owner_id=case[1]) == payload
    assert [r.model_dump() for r in grades.list_results_for_run(run_id)] == before
