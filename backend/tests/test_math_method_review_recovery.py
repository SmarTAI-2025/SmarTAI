"""Keep independent reassessments and require confirmation after unfinished review."""
import json
from types import SimpleNamespace

import pytest

from backend.agents.multi_expert import _apply_math_review_advisory, _weighted_average_fallback
from backend.domain.grading_review import review_state
from backend.services import task_facade
from backend.services.part_rubric import PartRubric
from backend.skills import math_grading as grading
from backend.skills.part_scoring import PartDecision
from backend.tests.test_alternative_route import alternate, error
from backend.tests.test_math_grading import expert_for
from backend.tests.test_math_method_review import case, choice, provider, reassessment, revision, run
from backend.tests.test_task_grading_retry import case as db_case, start  # noqa: F401
from backend.tools.structured_llm import PermanentLLMError


REASON = "math_method_review_incomplete"


def three_experts(reverse=False):
    p, a, plan, _, _ = case()
    rules = plan.part_rubric.model_dump(mode="json")
    rules["parts"][0]["steps"] = [
        {"id": "method", "criterion": "配方运算", "total_pts": 9},
        {"id": "roots", "criterion": "核对两个根", "total_pts": 1},
    ]
    plan = plan.model_copy(update={"part_rubric": PartRubric.model_validate(rules)})
    reference = PartDecision.model_validate({**alternate("complete").model_dump(), "method": "reference", "steps": [
        {"id": "method", "outcome": "earned", "evidence": "student:1", "reason": "已列出等价变换。"},
        {"id": "roots", "outcome": "earned", "evidence": "student:2", "reason": "已核对根。"},
    ]})
    revised = PartDecision.model_validate({**reference.model_dump(), "relevance": "partial", "steps": [
        {"id": "method", "outcome": "earned", "evidence": "student:1", "reason": "已列出等价变换。"},
        {"id": "roots", "outcome": "missed", "evidence": "student:2", "reason": "第二个根符号错误。"},
    ]})
    votes = [("a", alternate(errors=[error(line=1), error(line=2)])),
             ("b", alternate(errors=[error()])), ("c", reference)]
    if reverse:
        votes[:2] = reversed(votes[:2])
    experts = [expert_for(grading.FrozenPartAssessment(part_decisions=[decision], comment="原评语").assessment(),
                         plan, a.content).model_copy(update={"provider_id": name, "provider": name})
               for name, decision in votes]
    return p, a, plan, experts, revised


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("failure", ["unsupported", "uncertain", "missing_provider", "runtime", "invalid"])
async def test_one_expert_cannot_discard_or_prevent_another_reassessment(reverse, failure):
    p, a, plan, experts, revised = three_experts(reverse)
    before = [e.model_dump_json() for e in experts]
    indices = {e.provider_id: i for i, e in enumerate(experts)}
    main, calls = provider("primary", choice("reference"), {"revisions": []})
    first, first_calls = provider("a", reassessment(revised, index=indices["a"]))
    if failure in {"unsupported", "uncertain"}:
        declined = reassessment(revised, index=indices["b"], student_support=failure)
        declined["decisions"] = []
        replies = [declined]
    elif failure == "invalid":
        replies = [reassessment(revised, index=99)] * 2
    else:
        replies = [PermanentLLMError("offline failure")]
    second, second_calls = provider("b", *replies)
    providers = {"a": first, **({"b": second} if failure != "missing_provider" else {})}
    result = await run(p, a, plan, experts, main, providers)
    assert len(calls) == 2 and len(first_calls) == 1
    assert len(second_calls) == (0 if failure == "missing_provider" else 2 if failure == "invalid" else 1)
    final = json.loads(calls[-1][-1].content)
    assert "selected_methods" not in final
    assert "Keep these scoring routes fixed" not in calls[-1][0].content
    expected = {"a": 9, "b": 8, "c": 10}
    assert {e.provider_id: e.score for e in result} == expected
    for entry, expert in zip(final["experts"], experts):
        assessment = grading.FrozenPartAssessment(part_decisions=entry["decisions"], comment="").assessment()
        assert grading.score_assessment(assessment, plan, a.content, problem_text=p.stem)[0] == expected[expert.provider_id]
    assert grading.read_math_trace(result[indices["a"]])["method_review"]["status"] == "reassessed"
    assert choice()["choices"][0]["reason"] in result[indices["a"]].comment
    assert [e.model_dump_json() for e in experts] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_refusing_expert_remains_eligible_for_ordinary_joint_review(reverse):
    p, a, plan, experts, revised = three_experts(reverse)
    indices = {e.provider_id: i for i, e in enumerate(experts)}
    main, calls = provider("primary", choice("reference"), {"revisions": [revision(revised, indices["b"])]})
    first, _ = provider("a", reassessment(revised, indices["a"]))
    declined = reassessment(revised, indices["b"], student_support="uncertain")
    declined["decisions"] = []
    second, _ = provider("b", declined)
    result = await run(p, a, plan, experts, main, {"a": first, "b": second})
    assert len(calls) == 2 and {e.provider_id: e.score for e in result} == {"a": 9, "b": 9, "c": 10}
    assert grading.read_math_trace(result[indices["b"]])["targeted_review"]


def reference_case(same_score=False):
    p, a, plan, experts, _ = case(same_total=same_score)
    full = alternate("complete").model_copy(update={"method": "reference"})
    experts[0] = expert_for(grading.FrozenPartAssessment(part_decisions=[full], comment="原评语").assessment(),
                            plan, a.content).model_copy(update={"provider_id": "a", "confidence": .7})
    return p, a, plan, experts, full


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("failure", ["runtime", "format", "invalid_revision"])
async def test_unfinished_final_review_keeps_valid_regrade_but_requires_teacher_decision(language, failure):
    p, a, plan, experts, full = reference_case()
    if failure == "runtime":
        replies = [PermanentLLMError("offline failure")]
    elif failure == "format":
        replies = ["not json"] * 2
    else:
        invalid = {"revisions": [revision(alternate("complete"), index=1)]}
        replies = [invalid, invalid]
    main, _ = provider("primary", choice("reference"), *replies)
    regrader, _ = provider("b", reassessment(full, index=1))
    result = await run(p, a, plan, experts, main, {"b": regrader}, language=language)
    assert [e.score for e in experts] == [10, 8]
    assert [e.score for e in result] == [10, 10]
    trace = grading.read_math_trace(result[1])
    assert trace["method_review_pending"] is True
    message = "解法已重判，最终联合复核未完成" if language == "zh" else "Method reassessed; final joint review did not complete"
    assert message in result[1].comment
    assert not any(message in step.desc for step in result[1].steps)
    assert trace["method_review"]["original_assessment"] == grading.read_math_trace(experts[1])["assessment"]
    correction = _apply_math_review_advisory(_weighted_average_fallback(p, result))
    assert correction.score == 10 and correction.requires_human_review
    assert REASON in correction.review_reasons
    assert review_state(correction.model_dump()) == (True, False)
    dto = SimpleNamespace(result_status="needs_review", effective_score=10, ai_score=10, ai_max_score=10,
        review_reasons=correction.review_reasons, initial_review_reasons=correction.review_reasons,
        ai_expert_results=[e.model_dump() for e in result], teacher_review=None)
    assert task_facade._correction_review_blocks_finalization(dto)
    dto.teacher_review = {"new_score": 10, "confirmed": False}
    assert task_facade._correction_review_blocks_finalization(dto)
    dto.teacher_review = {"new_score": 10, "confirmed": True}
    assert not task_facade._correction_review_blocks_finalization(dto)
    dto.teacher_review = {"new_score": 9, "confirmed": False}
    assert not task_facade._correction_review_blocks_finalization(dto)


@pytest.mark.asyncio
async def test_failed_final_review_without_changed_score_preserves_existing_policy():
    p, a, plan, experts, full = reference_case(same_score=True)
    main, _ = provider("primary", choice("reference"), PermanentLLMError("offline failure"))
    regrader, _ = provider("b", reassessment(full, index=1))
    result = await run(p, a, plan, experts, main, {"b": regrader})
    assert [e.score for e in result] == [10, 10]
    assert all(not grading.read_math_trace(e).get("method_review_pending") for e in result)
    assert REASON not in _apply_math_review_advisory(_weighted_average_fallback(p, result)).review_reasons


@pytest.mark.asyncio
@pytest.mark.parametrize("resolve", ["confirm", "edit"])
async def test_saved_reassessment_requires_decision_before_reports_and_release(db_case, resolve):
    from sqlalchemy import select
    from backend.db import grading_repository as grades
    from backend.db.models import AssignmentQuestionRecord
    from backend.db.session import session_scope
    from backend.domain.errors import ResultNotReleasable
    from backend.models import Correction
    from backend.services import grading_runs
    from backend.services.math_grading_context import problem_from_question
    from backend.services.rubric_drafts import bind_draft, retarget_draft
    from backend.tests.test_alternative_route import material
    from backend.tests.test_grading_checkpoints import prepare

    row, draft = material(teacher="参考解配方得10分。", alternative={"deduct_per_error": 2})
    with session_scope() as session:
        question = session.scalar(select(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == db_case[0], AssignmentQuestionRecord.q_id == "q1"))
        for key in ("number", "type", "stem", "criterion", "reference_answer", "max_score"):
            setattr(question, key, row[key])
        question.source = {"presentation": {"rubric_draft": draft.model_dump(mode="json")}}
    run_id = start(db_case, "unfinished-method-review")["job_id"]
    checkpoints, questions, revisions, _ = prepare(db_case, run_id)
    p = problem_from_question(questions[0])
    plan = bind_draft(retarget_draft(draft, p), p)
    _, a, _, experts, full = reference_case()
    for expert in experts:
        trace = grading.read_math_trace(expert)
        trace["plan_id"] = plan.plan_id
        expert.raw_output = json.dumps(trace)
    main, _ = provider("primary", choice("reference"), PermanentLLMError("offline failure"))
    regrader, _ = provider("b", reassessment(full, index=1))
    result = await run(p, a, plan, experts, main, {"b": regrader})
    correction = _weighted_average_fallback(p, result)
    student = revisions[0][1]
    checkpoints.save_math_plan(p, plan)
    checkpoints.save_correction(student, correction)
    checkpoints.save_correction(student, Correction(q_id="q2", type=questions[1].type,
        score=8, max_score=10, confidence=.95, comment="Unchanged control", steps=[]))
    grades.mark_completed(run_id, worker_id="checkpoint-test", completed=1, failed=0)
    args = {"task_id": db_case[0], "owner_id": db_case[1]}
    saved = next(item for item in grades.list_results_for_run(run_id) if item.q_id == "q1")
    public = next(c for s in task_facade.task_results(**args)["results"] for c in s["corrections"] if c["q_id"] == "q1")
    assert [e["score"] for e in public["expert_results"]] == [10, 10]
    assert all(e["raw_output"] is None for e in public["expert_results"])
    assert "解法已重判，最终联合复核未完成" in public["comment"]
    assert REASON in public["initial_review_reasons"]

    def assert_blocked():
        state = task_facade.finalization(**args)
        assert state["remaining_review_count"] == state["required_review_count"] == 1
        assert not state["ready_for_confirmation"]
        with pytest.raises(ResultNotReleasable, match="math_review_required"):
            grading_runs.release(teacher_id=db_case[1], run_id=run_id)
        with pytest.raises(ResultNotReleasable, match="math_review_required"):
            task_facade.generate_artifacts(**args, expected_revision=state["workflow_revision"])
        assert not task_facade.artifact_index(**args)["versions"]
    assert_blocked()
    grades.add_teacher_review(saved.id, teacher_id=db_case[1], new_score=10, new_comment="Only a note", confirm=False)
    assert_blocked()
    wanted = 10 if resolve == "confirm" else 9
    grades.add_teacher_review(saved.id, teacher_id=db_case[1], new_score=wanted,
                             new_comment="Teacher decision", confirm=resolve == "confirm")
    state = task_facade.finalization(**args)
    assert state["ready_for_confirmation"] and state["remaining_review_count"] == 0
    task_facade.generate_artifacts(**args, expected_revision=state["workflow_revision"])
    restored = next(item for item in grades.list_results_for_run(run_id) if item.id == saved.id)
    assert restored.ai_score == 10 and restored.effective_score == wanted
    assert [e["score"] for e in restored.ai_expert_results] == [10, 10]


@pytest.mark.asyncio
async def test_refusal_unlocks_only_its_part_and_preserves_another_parts_lock():
    from backend.tests.test_math_method_review import multipart_case
    p, a, plan, experts, right, other = multipart_case()
    trace = grading.read_math_trace(experts[1])
    trace["assessment"]["part_decisions"][1].update(method="reference", relevance="incorrect", errors=[])
    experts[1] = expert_for(grading.MathAssessment.model_validate(trace["assessment"]), plan, a.content).model_copy(update={"provider_id": "b"})
    both = choice()
    both["choices"].append({**choice()["choices"][0], "part_id": "p2", "student_source_ids": ["student:3"]})
    main, calls = provider("primary", both, {"revisions": []})
    first, _ = provider("a", reassessment(right))
    declined = reassessment(other, index=1, reference_support="uncertain")
    declined["decisions"] = []
    second, _ = provider("b", declined)
    result = await run(p, a, plan, experts, main, {"a": first, "b": second})
    final = json.loads(calls[-1][-1].content)
    assert [c["part_id"] for c in final["selected_methods"]] == ["p"]
    assert [e.score for e in result] == [16, 8]
    assert final["experts"][1]["decisions"][1]["method"] == "reference"


@pytest.mark.asyncio
@pytest.mark.parametrize("critical", ["cancel", "lease", "checkpoint"])
async def test_later_critical_failure_still_propagates(critical, monkeypatch):
    import asyncio
    from backend.domain.errors import LeaseLost
    from backend.services.grading_checkpoints import CheckpointWriteError
    from backend.skills import math_method_review
    p, a, plan, experts, revised = three_experts()
    failure = {"cancel": asyncio.CancelledError(), "lease": LeaseLost("lease"),
               "checkpoint": CheckpointWriteError("checkpoint")}[critical]
    main, calls = provider("primary", choice("reference"), {"revisions": []})
    first, _ = provider("a", reassessment(revised))
    second, _ = provider("b")
    original_call = math_method_review.structured_llm_call
    async def checked_call(provider, **kwargs):
        if provider is second:
            raise failure
        return await original_call(provider, **kwargs)
    monkeypatch.setattr(math_method_review, "structured_llm_call", checked_call)
    with pytest.raises(type(failure)):
        await run(p, a, plan, experts, main, {"a": first, "b": second})
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_summary_model_cannot_hide_unfinished_review_notice():
    from backend.agents.multi_expert import _synthesize
    p, a, plan, experts, full = reference_case()
    main, _ = provider("primary", choice("reference"), PermanentLLMError("offline failure"))
    regrader, _ = provider("b", reassessment(full, index=1))
    result = await run(p, a, plan, experts, main, {"b": regrader})
    summary, calls = provider("summary", {"score": 10, "max_score": 10, "confidence": .99,
        "comment": "A summary omitting the incomplete review.", "steps": []})
    correction = await _synthesize(problem=p, expert_results=result, synthesis_provider=summary, language="zh", answer=a)
    assert len(calls) == 1 and correction.score == 10
    assert "解法已重判，最终联合复核未完成" in correction.comment
