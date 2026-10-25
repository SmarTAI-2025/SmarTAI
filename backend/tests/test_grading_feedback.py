"""Read-only presentation of existing grades using their own final evidence."""
from copy import deepcopy
import json

import pytest

from backend.models import Correction, ExpertResult, StepScore
from backend.services.grading_feedback import _concise_steps, _combined_steps, concise_result_feedback
from backend.skills.math_grading import TRACE_VERSION, weighted_math_steps
from backend.tests.test_math_grading import assessment, plan_for
from backend.tests.test_grading_checkpoints import prepare, stop
from backend.tests.test_task_grading_retry import case, start  # noqa: F401


def legacy_fixture(*, reversed_awards=False, reason="Correct setup."):
    plan = plan_for()
    draft = assessment()
    draft.awards[0].reason = reason
    steps = [dict(step_no=index + 1, desc=f"{item.description}\n6*7=41\n{award.reason}",
                  score=award.score, is_correct=award.score == item.points)
             for index, (item, award) in enumerate(zip(plan.routes[0].items, draft.awards))]
    if reversed_awards:
        draft.awards.reverse()
    trace = {"version": TRACE_VERSION, "plan_id": plan.plan_id, "assessment": draft.model_dump()}
    return plan, steps, trace


@pytest.mark.parametrize("suffix", ["", "\n0 + 3 × 0.5 = 1.5", "\n定向复核依据：保留遗漏条件的扣分。",
                                  "\nTool verification remains unresolved: check the domain."])
def test_legacy_projection_matches_ids_and_preserves_scores_and_suffixes(suffix):
    plan, steps, trace = legacy_fixture(reversed_awards=True)
    steps[0]["desc"] += suffix
    original = deepcopy((steps, trace))
    result = _concise_steps(steps, trace, plan)
    assert [step["desc"] for step in result] == ["Correct setup." + suffix, "Result checked."]
    assert [{k: v for k, v in step.items() if k != "desc"} for step in result] == [
        {k: v for k, v in step.items() if k != "desc"} for step in steps]
    assert (steps, trace) == original


@pytest.mark.parametrize("failure", ["trace_missing", "wrong_version", "wrong_plan", "bad_assessment",
                                    "missing_claim", "duplicate_award", "combined_step", "edited_desc"])
def test_unmatched_evidence_never_causes_heuristic_deletion(failure):
    plan, steps, trace = legacy_fixture()
    if failure == "trace_missing": trace = None
    elif failure == "wrong_version": trace["version"] = "older"
    elif failure == "wrong_plan": trace["plan_id"] = "0" * 64
    elif failure == "bad_assessment": trace["assessment"] = {}
    elif failure == "missing_claim": trace["assessment"]["claims"] = []
    elif failure == "duplicate_award": trace["assessment"]["awards"][1] = trace["assessment"]["awards"][0]
    elif failure == "combined_step": steps = [{**steps[0], "desc": "\n\n".join(s["desc"] for s in steps)}]
    elif failure == "edited_desc":
        steps = [{**s, "desc": "Independent explanation:\n" + s["desc"]} for s in steps]
    original = deepcopy(steps)
    assert _concise_steps(steps, trace, plan) == original


def test_omission_source_is_not_displayed_and_new_feedback_is_idempotent():
    plan, steps, trace = legacy_fixture()
    claim = trace["assessment"]["claims"][0]
    claim.update(kind="omission", quote="", source_quote="Shared rubric obligation.")
    steps = [{**s, "desc": s["desc"].replace("6*7=41", claim["source_quote"])} for s in steps]
    result = _concise_steps(steps, trace, plan)
    assert [s["desc"] for s in result] == ["Correct setup.", "Result checked."]
    assert _concise_steps(result, trace, plan) == result
    assert claim["source_quote"] == "Shared rubric obligation."


@pytest.mark.parametrize("suffix", ["", "\nTeacher floor: 6/10", "\n复核：保留该扣分。"])
def test_saved_deductions_keep_amounts_policy_and_suffixes(suffix):
    from backend.skills.math_grading import MathAssessment, Claim, Deduction
    plan = plan_for(mode="deductions")
    route = plan.routes[0].model_copy(update={"error_policy": "no_repeat_deduction",
                                           "error_policy_source_quote": "Do not deduct a dependent error twice."})
    plan = plan.model_copy(update={"routes": (route,)})
    draft = MathAssessment(route_id="main", route_reason="Teacher rule", comment="Check arithmetic.",
        claims=[Claim(id="s1", quote=r"$x=2+2$ [a]", item_ids=["error"], verdict="refuted", reason="First error"),
                Claim(id="s2", quote="Follow-up calculation", item_ids=["error"], verdict="refuted", reason="Dependent")],
        deductions=[Deduction(error_id="e1", rule_id="error", claim_id="s1", reason="First arithmetic error."),
                    Deduction(error_id="e2", rule_id="error", claim_id="s2", caused_by="s1", reason="Follow-through.")])
    old = r"Independent arithmetic error: −0.50; $x=2+2$ [a]" + "\nFirst arithmetic error.\n\n" + (
        "Independent arithmetic error: −0; Follow-up calculation\nFollow-through.\nDo not deduct a dependent error twice.")
    steps = [dict(step_no=1, score=9.5, is_correct=False, desc=old+suffix)]
    trace = {"version": TRACE_VERSION, "plan_id": plan.plan_id, "assessment": draft.model_dump()}
    result = _concise_steps(steps, trace, plan)
    assert result == [{**steps[0], "desc": "−0.50: First arithmetic error.\n\n−0: Follow-through.\nDo not deduct a dependent error twice."+suffix}]
    assert _concise_steps(result, trace, plan) == result
    edited = [{**steps[0], "desc": "Other explanation:\n"+old}]
    assert _concise_steps(edited, trace, plan) == edited


def test_exempt_error_keeps_teacher_policy_in_new_and_saved_feedback():
    from backend.skills.math_grading import MathAssessment, Claim, Deduction, score_assessment
    from backend.services.math_rubric_plan import MathRubricExemption
    plan = plan_for(mode="deductions")
    exemption = MathRubricExemption(id="minor", description="Minor errors are exempt.", source_quote="Minor errors are exempt.")
    plan = plan.model_copy(update={"routes": (plan.routes[0].model_copy(update={"exemptions": (exemption,)}),)})
    draft = MathAssessment(route_id="main", route_reason="Teacher rule", comment="Check notation.",
        claims=[Claim(id="s1", quote="work", item_ids=["minor"], verdict="refuted", reason="Typo")],
        deductions=[Deduction(error_id="e1", rule_id="minor", claim_id="s1", reason="A harmless notation slip.")])
    score, steps = score_assessment(draft, plan, "work")
    assert score == 10
    assert steps[0].desc == "−0: A harmless notation slip.\nMinor errors are exempt."
    trace = {"version": TRACE_VERSION, "plan_id": plan.plan_id, "assessment": draft.model_dump()}
    stored = [{**steps[0].model_dump(), "desc": "Minor errors are exempt.: −0; work\nA harmless notation slip."}]
    assert _concise_steps(stored, trace, plan) == [s.model_dump() for s in steps]


def test_weighted_feedback_groups_identical_reasons_but_keeps_distinct_votes():
    from decimal import Decimal
    plan, steps, trace = legacy_fixture()
    experts = [ExpertResult(provider=name, score=8, max_score=10, confidence=.9,
        comment="Summary", steps=[StepScore(**s) for s in _concise_steps(steps, trace, plan)],
        raw_output=json.dumps(trace)) for name in ("Model A", "Model B")]
    joined = weighted_math_steps(experts, [Decimal(1), Decimal(1)], 10, 8)
    assert joined[0].desc == "Model A, Model B: Correct setup."
    assert [s.score for s in joined] == [8, 0]
    experts[1].steps[0].desc = "A missing condition warrants partial credit."
    joined = weighted_math_steps(experts, [Decimal(1), Decimal(1)], 10, 8)
    assert joined[0].desc == "Model A: Correct setup.\n\nModel B: A missing condition warrants partial credit."


def test_legacy_aggregate_preserves_other_explanations_and_does_not_mutate():
    plan, steps, trace = legacy_fixture()
    old = [dict(provider=name, score=8, steps=steps) for name in ("Model A", "Model B")]
    new = [{**expert, "steps": _concise_steps(steps, trace, plan)} for expert in old]
    combined = [{**step, "desc": "\n\n".join(f"{expert['provider']}: {step['desc']}" for expert in old)}
                for step in steps]
    result = _combined_steps(combined, old, new, 10)
    assert [s["desc"] for s in result] == ["Model A, Model B: Correct setup.", "Model A, Model B: Result checked."]
    assert _combined_steps([{**combined[0], "desc": "Independent judge conclusion."}], old, new, 10)[0]["desc"] == "Independent judge conclusion."


def test_saved_legacy_result_is_concise_on_read_without_changing_history(case):
    from backend.db import grading_repository as grades
    from backend.services import task_facade
    from backend.services.math_grading_context import problem_from_question
    from backend.domain.errors import NotFound

    run_id = start(case, "legacy-feedback")["job_id"]
    checkpoints, questions, revisions, setup = prepare(case, run_id)
    q, student = questions[0], revisions[0][1]
    # Build a plan for this actual frozen question, then install the same two
    # point items as the synthetic example. No model or production data.
    plan = plan_for(problem_from_question(q))
    checkpoints.save_math_plan(problem_from_question(q), plan)
    _, steps, trace = legacy_fixture()
    trace["plan_id"] = plan.plan_id
    expert = ExpertResult(provider="model-a", provider_id=setup.setup["primary_provider_id"],
        score=8, max_score=10, confidence=.9, comment="Preserved summary.",
        steps=[StepScore(**s) for s in steps], raw_output=json.dumps(trace))
    correction = Correction(q_id=q.q_id, type=q.type, score=8, max_score=10, confidence=.9,
        comment="Preserved summary.", steps=expert.steps, expert_results=[expert], synthesis_method="single")
    checkpoints.save_correction(student, correction)
    stop(case, run_id)
    before = [row.model_dump() for row in grades.list_results_for_run(run_id)]
    events_before = grades.list_events(run_id)
    payload = task_facade.task_results(task_id=case[0], owner_id=case[1])
    shown = next(c for s in payload["results"] for c in s["corrections"] if c["q_id"] == q.q_id)
    assert shown["steps"][0]["desc"] == "Correct setup."
    assert shown["expert_results"][0]["steps"][0]["desc"] == "Correct setup."
    assert shown["provisional_score"] == 8
    assert shown["expert_results"][0]["raw_output"] is None
    assert shown["comment"] == "Preserved summary."
    assert [row.model_dump() for row in grades.list_results_for_run(run_id)] == before
    assert grades.list_events(run_id) == events_before
    assert task_facade.task_results(task_id=case[0], owner_id=case[1]) == payload
    with pytest.raises(NotFound):
        task_facade.task_results(task_id=case[0], owner_id="another-owner")

    # Published-result review copies intentionally have no copied trace events.
    # Their immutable AI feedback must remain concise after a fresh GET too.
    from backend.db.models import GradingRunRecord, GradingRunEventRecord
    from backend.db.session import session_scope
    from sqlalchemy import select
    for _ in range(2):
        with session_scope() as session:
            session.get(GradingRunRecord, run_id).released_at = 1
        cloned = grades.clone_released_run_for_review(run_id, teacher_id=case[1])
        stored = grades.list_results_for_run(cloned.id)
        displayed = concise_result_feedback(stored)
        shown = next(row for row in displayed if row.q_id == q.q_id)
        assert shown.ai_steps[0]["desc"] == "Correct setup."
        assert shown.ai_expert_results[0]["steps"][0]["desc"] == "Correct setup."
        assert shown.ai_score == 8
        assert [r.model_dump() for r in grades.list_results_for_run(cloned.id)] == [r.model_dump() for r in stored]
        run_id = cloned.id
    with session_scope() as session:
        event = session.scalar(select(GradingRunEventRecord).where(
            GradingRunEventRecord.grading_run_id == run_id,
            GradingRunEventRecord.message == "review_revision_created"))
        event.payload = {"source_run_id": run_id}
    assert concise_result_feedback(stored) == stored  # Cyclic/broken provenance is not guessed.


@pytest.mark.parametrize("field", ["owner_id", "assignment_id", "fingerprint"])
def test_review_copy_cannot_inherit_unrelated_evidence(case, monkeypatch, field):
    from backend.db import workflow_repository as workflows
    from backend.db.models import GradingRunEventRecord
    from backend.db.session import session_scope
    from backend.services.grading_feedback import _feedback_context
    run_id = start(case, "feedback-provenance")["job_id"]
    frozen = workflows.get_run_setup(run_id)
    with session_scope() as session:
        session.add(GradingRunEventRecord(id="feedback-source", grading_run_id=run_id, sequence=1,
            level="info", message="review_revision_created", payload={"source_run_id": "unrelated"}, created_at=1))
    def get_setup(key):
        from types import SimpleNamespace
        identity = {name: getattr(frozen, name) for name in ("owner_id", "assignment_id", "fingerprint")}
        return frozen if key == run_id else SimpleNamespace(**{**identity, field: "different"})
    monkeypatch.setattr(workflows, "get_run_setup", get_setup)
    assert _feedback_context(run_id) == ({}, {})
