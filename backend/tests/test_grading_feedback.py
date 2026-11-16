"""Read-only presentation of existing grades using their own final evidence."""
from copy import deepcopy
import json

import pytest

from backend.models import Correction, ExpertResult, StepScore
from backend.services.grading_feedback import _concise_steps, _combined_steps, _review_comment, concise_result_feedback
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


def test_legacy_single_expert_projection_retains_deduction_metadata_and_net_score():
    saved = [dict(step_no=1, part_id="p", score=7, is_correct=False, desc="Saved reasoning")]
    shown = [{**saved[0], "is_alternative": True, "deductions": [
        dict(desc=f"Wrong sign in equation {n}.", points=1) for n in range(1, 4)]}]
    original = [dict(provider="model", score=7, steps=saved)]
    rendered = [{**original[0], "steps": shown}]
    assert _combined_steps(saved, original, rendered, 10) == shown
    assert "deductions" not in saved[0]
    adjusted = [{**saved[0], "score": 8}]
    assert _combined_steps(adjusted, original, rendered, 10) == adjusted


def test_saved_legacy_result_is_concise_on_read_without_changing_history(case):
    from backend.db import grading_repository as grades
    from backend.db import workflow_repository as workflows
    from backend.services import task_facade
    from backend.services.math_grading_context import problem_from_question
    from backend.services.task_execution import continue_manual_grading
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
    trace["targeted_review"] = {"provider": "reviewer", "reason": "The omitted domain condition still warrants the deduction."}
    expert = ExpertResult(provider="model-a", provider_id=setup.setup["primary_provider_id"],
        score=8, max_score=10, confidence=.9, comment="Preserved summary.",
        steps=[StepScore(**s) for s in steps], raw_output=json.dumps(trace))
    correction = Correction(q_id=q.q_id, type=q.type, score=8, max_score=10, confidence=.9,
        comment="Preserved summary.", steps=expert.steps, expert_results=[expert], synthesis_method="single")
    checkpoints.save_correction(student, correction)
    for other in questions[1:]:
        checkpoints.save_correction(student, Correction(
            q_id=other.q_id, type=other.type, score=8, max_score=10,
            confidence=.9, comment="Other completed item.", steps=[], synthesis_method="single"))
    stop(case, run_id)
    before = [row.model_dump() for row in grades.list_results_for_run(run_id)]
    events_before = grades.list_events(run_id)
    payload = task_facade.task_results(task_id=case[0], owner_id=case[1])
    shown = next(c for s in payload["results"] for c in s["corrections"] if c["q_id"] == q.q_id)
    assert shown["steps"][0]["desc"] == "Correct setup."
    assert shown["expert_results"][0]["steps"][0]["desc"] == "Correct setup."
    assert shown["provisional_score"] == 8
    assert shown["expert_results"][0]["raw_output"] is None
    assert trace["targeted_review"]["reason"] in shown["expert_results"][0]["comment"]
    assert shown["comment"] == "Preserved summary."
    assert [row.model_dump() for row in grades.list_results_for_run(run_id)] == before
    assert grades.list_events(run_id) == events_before
    assert task_facade.task_results(task_id=case[0], owner_id=case[1]) == payload
    with pytest.raises(NotFound):
        task_facade.task_results(task_id=case[0], owner_id="another-owner")

    # Published-result review copies intentionally have no copied trace events.
    # Their immutable AI feedback must remain concise after a fresh GET too.
    from backend.db.models import GradingRunEventRecord
    from backend.db.session import session_scope
    from sqlalchemy import select
    continue_manual_grading(task_id=case[0], owner_id=case[1], job_id=run_id,
        expected_revision=workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision)
    for index in range(2):
        grades.release(run_id, teacher_id=case[1])
        published = grades.list_results_for_run(run_id)
        revision = workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision
        saved = task_facade.update_correction_review(
            task_id=case[0], owner_id=case[1], display_student_id=payload["results"][0]["student_id"],
            q_id=q.q_id, teacher_score=9-index, teacher_comment=f"Review {index+1}",
            confirm=True, expected_revision=revision)
        assert saved["workflow_revision"] == revision + 1
        current_run = workflows.get_live_workflow(case[0], owner_id=case[1]).grading_job_id
        assert current_run != run_id
        assert grades.get_run(current_run).released_at is None
        assert grades.list_results_for_run(run_id) == published
        stored = grades.list_results_for_run(current_run)
        fresh = task_facade.task_results(task_id=case[0], owner_id=case[1])
        assert fresh["grading_run_id"] == current_run
        shown = next(c for s in fresh["results"] for c in s["corrections"] if c["q_id"] == q.q_id)
        assert shown["steps"][0]["desc"] == "Correct setup."
        assert shown["expert_results"][0]["steps"][0]["desc"] == "Correct setup."
        assert trace["targeted_review"]["reason"] in shown["expert_results"][0]["comment"]
        assert shown["provisional_score"] == 8
        assert shown["teacher_score"] == 9-index
        assert shown["teacher_comment"] == f"Review {index+1}"
        assert grades.list_results_for_run(current_run) == stored
        run_id = current_run
    with session_scope() as session:
        event = session.scalar(select(GradingRunEventRecord).where(
            GradingRunEventRecord.grading_run_id == run_id,
            GradingRunEventRecord.message == "review_revision_created"))
        event.payload = {"source_run_id": run_id}
    assert concise_result_feedback(stored) == stored  # Cyclic/broken provenance is not guessed.


@pytest.mark.parametrize("deductions", [[], [{"desc": "The root x=-2 fails the original equation.", "points": 2}]])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_saved_review_notes_remain_visible_outside_hidden_net_score_rows(deductions, language):
    plan, _, trace = legacy_fixture()
    reason = "原方程代入已核对，按该路线的独立错误扣分。" if language == "zh" else "Substitution in the original equation confirms the independent error."
    label = "定向复核依据：" if language == "zh" else "Targeted review: "
    advice = ("证明依据核对建议：仍需核对逆向推理。\n尤其是定义域。" if language == "zh" else
              "Proof evidence to check: Verify the reverse implication.\nEspecially the domain.")
    trace.update(targeted_review={"reason": reason}, review_advisory=True)
    expert = {"comment": "Original explanation.", "steps": [{"score": 8 if deductions else 10,
        "desc": "Net score\n" + label + reason + "\n" + advice, "deductions": deductions, "is_alternative": True}]}
    original = deepcopy(expert)
    result = _review_comment(expert, trace, plan)
    assert result == expert["comment"] + "\n\n" + label + reason + "\n\n" + advice
    assert _review_comment({**expert, "comment": result}, trace, plan) == result
    assert expert == original
    assert _review_comment(expert, {**trace, "plan_id": "other"}, plan) == expert["comment"]
    assert _review_comment(expert, {**trace, "version": "old"}, plan) == expert["comment"]
    assert _review_comment(expert, trace, None) == expert["comment"]


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


@pytest.mark.parametrize('counts', [[0,1],[0,0,0]])
def test_legacy_cumulative_rounding_survives_database_roundtrip(case,counts):
    from sqlalchemy import select
    from backend.db import grading_repository as grades
    from backend.db.models import AssignmentQuestionRecord, SubmissionAnswerRecord
    from backend.db.session import session_scope
    from backend.services import task_facade
    from backend.services.rubric_drafts import RubricDefinition, generated_rubric, bind_draft, retarget_draft
    from backend.services.math_grading_context import problem_from_question
    from backend.tests.test_frozen_part_rubric import question_row
    from backend.tests.test_scoring_structure import rules, decision, ANSWER
    from backend.skills.math_grading import MathAssessment
    from backend.skills.part_scoring import settle_parts

    template=rules(alternative={'maximum':{'fraction':'1/3'},'deduct_per_error':.001})
    rubric=template.model_copy(update={'total_pts':10*len(counts),'parts':tuple(template.parts[0].model_copy(
        update={'id':f'p{i}','label':f'({i+1})','stem':f'Prove claim {i+1}.'}) for i in range(len(counts)))})
    row=question_row(rubric.model_dump(mode='json'))
    row['reference_answer']='Use a direct argument.'
    row['ai_completion_provenance']={'criterion':{}}
    row['criterion'],draft=generated_rubric(row,RubricDefinition(part_rubric=rubric))
    with session_scope() as session:
        question=session.scalar(select(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id==case[0],AssignmentQuestionRecord.q_id=='q1'))
        for key in ('number','type','stem','criterion','reference_answer','max_score'):
            setattr(question,key,row[key])
        question.source={'presentation':{'rubric_draft':draft.model_dump(mode='json'), 'question_structure':row.get('question_structure'), 'ai_completion_provenance':row['ai_completion_provenance']}}
        for answer in session.scalars(select(SubmissionAnswerRecord).where(SubmissionAnswerRecord.question_id==question.id)):
            answer.content=ANSWER
    run_id=start(case,'rounding-history-'+str(len(counts)))['job_id']
    checkpoints,questions,revisions,setup=prepare(case,run_id)
    p=problem_from_question(questions[0]);plan=bind_draft(retarget_draft(draft,p),p)
    checkpoints.save_math_plan(p,plan)
    decisions=[decision(errors=count).model_copy(update={'part_id':f'p{i}','target_quote':f'part:p{i}'}) for i,count in enumerate(counts)]
    assessment=MathAssessment(route_id='parts',route_reason='Frozen',comment='Saved final review reason.',part_decisions=decisions)
    score,steps=settle_parts(plan.part_rubric,decisions,ANSWER,p.stem)
    assert [s.score for s in steps]==([3.33,3.34] if len(counts)==2 else [3.33,3.34,3.33])
    legacy=[StepScore(**{k:v for k,v in s.model_dump().items() if k not in {'deductions','deduction_base','is_alternative','rounding_adjustment'}}) for s in steps]
    expert=ExpertResult(provider='old-model',provider_id=setup.setup['primary_provider_id'],score=score,max_score=p.max_score,
        confidence=.99,comment=assessment.comment,steps=legacy,raw_output=json.dumps(
            {'version':TRACE_VERSION,'plan_id':plan.plan_id,'assessment':assessment.model_dump(mode='json')}))
    checkpoints.save_correction(revisions[0][1],Correction(q_id='q1',type=p.type,score=score,max_score=p.max_score,
        confidence=.99,comment=expert.comment,steps=legacy,expert_results=[expert],synthesis_method='single'))
    stop(case,run_id)
    before=[r.model_dump() for r in grades.list_results_for_run(run_id)]
    payload=task_facade.task_results(task_id=case[0],owner_id=case[1])
    shown=next(c for s in payload['results'] for c in s['corrections'] if c['q_id']=='q1')
    assert shown['expert_results'][0]['steps']==[s.model_dump(mode='json') for s in steps]
    assert shown['steps']==shown['expert_results'][0]['steps']
    assert shown['provisional_score']==score
    assert shown['expert_results'][0]['comment']=='Saved final review reason.'
    assert [r.model_dump() for r in grades.list_results_for_run(run_id)]==before
    assert task_facade.task_results(task_id=case[0],owner_id=case[1])==payload
