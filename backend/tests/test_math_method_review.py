"""Route agreement is established before rescoring, never by copying a total."""
import json
from types import SimpleNamespace

import pytest

from backend.models import ProblemInfo, StudentAnswerInfo, TaskGradingSetup
from backend.services.math_grading_context import math_plans_context
from backend.services.rubric_drafts import bind_draft, retarget_draft
from backend.skills import math_grading as grading
from backend.tests.test_alternative_route import ANSWER, alternate, error, material
from backend.tests.test_math_evidence_reliability import raw_provider
from backend.tests.test_math_grading import expert_for
from backend.tools.structured_llm import DailyQuotaError
from backend.tests.test_task_grading_retry import case as db_case, start  # noqa: F401


def case(*, same_total=False, proof=False, reference=None, teacher="参考解配方得10分。"):
    row, draft = material(teacher=teacher, alternative={"deduct_per_error": 2})
    p = ProblemInfo(**row)
    if reference is not None:
        p = p.model_copy(update={"reference_answer": reference})
        draft = retarget_draft(draft, p)
    if proof:
        p = p.model_copy(update={"type": "推理题"})
        draft = retarget_draft(draft, p)
    plan = bind_draft(draft, p)
    a = StudentAnswerInfo(q_id=p.q_id, number="1", type=p.type, content=ANSWER)
    right = alternate("complete" if same_total else "partial", errors=[] if same_total else [error()])
    wrong = right.model_copy(update={"method": "reference", "relevance": "complete" if same_total else "incorrect", "errors": ()})
    experts = []
    for pid, decision, confidence in [("a", wrong, .7), ("b", right, .9)]:
        assessment = grading.FrozenPartAssessment(part_decisions=[decision], comment="Original explanation").assessment()
        expert = expert_for(assessment, plan, a.content).model_copy(update={"provider_id": pid, "provider": pid, "confidence": confidence})
        experts.append(expert)
    return p, a, plan, experts, right


def choice(method="alternative", **updates):
    return {"choices": [{"part_id": "p", "method": method, "student_source_ids": ["student:1"],
                         "reference_quote": "用配方法解得 1 和 2。", "reason": "作答因式分解与标答配方的关键运算不同。", **updates}]}


def revision(decision, index=0):
    return {"expert_index": index, "reason": "按已判定的因式分解路线，逐项核对原作答。", "comment": "第二个根符号错误。",
            "decisions": [decision.model_dump(mode="json")]}


def provider(name, *outputs):
    value, calls = raw_provider(*(json.dumps(o, ensure_ascii=False) if isinstance(o, dict) else o for o in outputs))
    value.provider_id = name
    return value, calls


async def run(p, a, plan, experts, main, providers, **kwargs):
    with math_plans_context({p.q_id: plan}), grading.math_review_scope():
        return await grading.review_math_experts(p, a, experts, main, expert_providers=providers, language="zh", **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_total", [False, True])
@pytest.mark.parametrize("proof", [False, True])
async def test_only_wrong_route_expert_reassesses_before_final_review(same_total, proof):
    p, a, plan, experts, right = case(same_total=same_total, proof=proof)
    original = [e.model_dump(mode="json") for e in experts]
    main, main_calls = provider("primary", choice(), {"revisions": []})
    regrader, regrade_calls = provider("a", revision(right))
    untouched, untouched_calls = provider("b")
    from backend.progress.tracker import ProgressReporter
    reporter = ProgressReporter("method-review-test")
    result = await run(p, a, plan, experts, main, {"a": regrader, "b": untouched}, reporter=reporter, student_id="synthetic")
    assert [e.score for e in result] == [10 if same_total else 8] * 2
    assert [e.confidence for e in result] == [.7, .9]
    assert len(main_calls) == 2 and len(regrade_calls) == 1 and not untouched_calls
    assert [e.model_dump(mode="json") for e in experts] == original
    trace = grading.read_math_trace(result[0])
    assert trace["method_review"]["original_assessment"] == grading.read_math_trace(experts[0])["assessment"]
    assert trace["method_review"]["reassessed_by"] == "a"
    assert trace["plan_id"] == plan.plan_id
    final_input = json.loads(main_calls[1][1].content)
    assert final_input["selected_methods"][0]["method"] == "alternative"
    assert all(e["decisions"][0]["method"] == "alternative" for e in final_input["experts"])
    assert not reporter._progress.active
    assert sum(event.message.endswith("llm_grade") for event in reporter._events) == 2


@pytest.mark.asyncio
async def test_reference_and_minor_variant_do_not_add_arbitration_calls():
    p, a, plan, experts, _ = case()
    trace = grading.read_math_trace(experts[1])
    old = grading.read_math_trace(experts[0])["assessment"]
    trace["assessment"] = {**old, "part_decisions": [{**old["part_decisions"][0], "method": "minor_variant"}]}
    experts[1] = experts[0].model_copy(update={"provider_id": "b", "raw_output": json.dumps(trace)})
    main, calls = provider("primary")
    result = await run(p, a, plan, experts, main, {"a": main, "b": main})
    assert result == experts and not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["arbitration", "reassessment", "final_review", "undetermined", "missing_provider"])
async def test_optional_stage_failure_retains_valid_votes_and_visible_advice(failure_stage):
    p, a, plan, experts, right = case()
    main_outputs = [DailyQuotaError()] if failure_stage == "arbitration" else [choice("undetermined") if failure_stage == "undetermined" else choice()]
    if failure_stage == "final_review":
        main_outputs.append(DailyQuotaError())
    main, calls = provider("primary", *main_outputs)
    regrader, regrade_calls = provider("a", DailyQuotaError() if failure_stage == "reassessment" else revision(right))
    result = await run(p, a, plan, experts, main, {} if failure_stage == "missing_provider" else {"a": regrader})
    assert [e.score for e in result] == ([8, 8] if failure_stage == "final_review" else [0, 8])
    assert all(not e.error_kind and grading.read_math_trace(e)["method_review_pending"] for e in result)
    assert all("解法路线仍需核对" in e.comment for e in result)
    assert len(calls) == (2 if failure_stage == "final_review" else 1)
    assert len(regrade_calls) == (1 if failure_stage in {"reassessment", "final_review"} else 0)


@pytest.mark.asyncio
async def test_bad_arbitration_sources_repair_before_any_reassessment():
    p, a, plan, experts, right = case()
    main, calls = provider("primary", choice(student_source_ids=["student:999"]), choice(), {"revisions": []})
    regrader, regrade_calls = provider("a", revision(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert [e.score for e in result] == [8, 8]
    assert len(calls) == 3 and len(regrade_calls) == 1


@pytest.mark.asyncio
async def test_reassessment_cannot_keep_wrong_route_and_repairs_with_original_provider():
    p, a, plan, experts, right = case()
    wrong = grading.MathAssessment.model_validate(grading.read_math_trace(experts[0])["assessment"]).part_decisions[0]
    main, calls = provider("primary", choice(), {"revisions": []})
    regrader, regrade_calls = provider("a", revision(wrong), revision(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert [e.score for e in result] == [8, 8]
    assert len(calls) == len(regrade_calls) == 2


@pytest.mark.asyncio
async def test_final_review_cannot_silently_switch_back_to_wrong_policy():
    p, a, plan, experts, right = case()
    wrong = grading.MathAssessment.model_validate(grading.read_math_trace(experts[0])["assessment"]).part_decisions[0]
    main, calls = provider("primary", choice(), {"revisions": [revision(wrong)]}, {"revisions": []})
    regrader, _ = provider("a", revision(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert [e.score for e in result] == [8, 8]
    assert len(calls) == 3
    assert all(grading.read_math_trace(e)["assessment"]["part_decisions"][0]["method"] == "alternative" for e in result)


@pytest.mark.asyncio
async def test_pipeline_is_bounded_even_if_reassessment_disagreement_remains():
    p, a, plan, experts, right = case()
    different = alternate("complete")
    main, calls = provider("primary", choice(), {"revisions": []})
    regrader, regrade_calls = provider("a", revision(different))
    with math_plans_context({p.q_id: plan}), grading.math_review_scope():
        result = await grading.review_math_experts(p, a, experts, main, expert_providers={"a": regrader})
        assert [e.score for e in result] == [10, 8]
        assert await grading.review_math_experts(p, a, result, main, expert_providers={"a": regrader}) == result
    assert len(calls) == 2 and len(regrade_calls) == 1


@pytest.mark.asyncio
async def test_agent_uses_selected_primary_and_original_provider_configuration_ids(monkeypatch):
    from backend.agents import multi_expert
    p, a, plan, experts, right = case()
    main, main_calls = provider("model-b", choice(), {"revisions": []})
    regrader, regrade_calls = provider("model-a", revision(alternate("complete")))
    registry = SimpleNamespace(list_available=lambda: [regrader, main], get=lambda key: {"a": regrader, "b": main}.get(key))
    class InitialVotes:
        name = "synthetic"
        uses_math_evidence = True
        def __init__(self, provider, **kwargs):
            self.index = 0 if provider is regrader else 1
        async def grade(self, *args, **kwargs):
            return experts[self.index]
    monkeypatch.setattr(multi_expert, "get_skill_for_type", lambda _: InitialVotes)
    setup = TaskGradingSetup(selected_provider_ids=["a", "b"], primary_provider_id="b",
                             aggregation_method="weighted_average", expert_weights={"a": 1, "b": 3})
    with math_plans_context({p.q_id: plan}):
        result = await multi_expert.run_multi_expert(problem=p, answer=a, student_id="synthetic", registry=registry,
                                                     grading_setup=setup, aggregation_method="weighted_average")
    assert result.score == 8.5 and [e.provider_id for e in result.expert_results] == ["a", "b"]
    assert [e.score for e in result.expert_results] == [10, 8]
    assert len(main_calls) == 2 and len(regrade_calls) == 1


@pytest.mark.asyncio
async def test_primary_can_select_reference_and_regrade_only_alternative_vote():
    p, a, plan, experts, _ = case(same_total=True)
    reference = grading.MathAssessment.model_validate(grading.read_math_trace(experts[0])["assessment"]).part_decisions[0]
    main, calls = provider("primary", choice("reference", reason="原作答采用相同核心运算，记号不同。"), {"revisions": []})
    regrader, regrade_calls = provider("b", revision(reference, index=1))
    result = await run(p, a, plan, experts, main, {"b": regrader})
    assert [e.score for e in result] == [10, 10]
    assert len(calls) == 2 and len(regrade_calls) == 1
    assert grading.read_math_trace(result[0])["method_review"]["status"] == "already_aligned"
    assert grading.read_math_trace(result[1])["method_review"]["reassessed_by"] == "b"


def multipart_case():
    from backend.services.rubric_drafts import RubricDefinition, generated_rubric
    from backend.tests.test_frozen_part_rubric import question_row
    p, a, old_plan, old_experts, right = case()
    first = old_plan.part_rubric.parts[0].model_copy(update={"label": "(1)", "stem": p.stem})
    second = first.model_copy(update={"id": "p2", "label": "(2)"})
    rules = old_plan.part_rubric.model_copy(update={"total_pts": 20, "parts": (first, second)})
    row = question_row(rules.model_dump(mode="json"))
    row.update(criterion="每小问参考解配方得10分。",
               reference_answer="(1) 用配方法解得 1 和 2。\n(2) 用因式分解解得 1 和 2。")
    row["criterion"], draft = generated_rubric(row, RubricDefinition(part_rubric=rules))
    p = ProblemInfo(**row)
    plan = bind_draft(retarget_draft(draft, p), p)
    a = a.model_copy(update={"content": "(1) " + a.content + "\n(2) " + a.content})
    unchanged = right.model_copy(update={"part_id": "p2", "target_quote": "part:p2", "student_quote": "student:3",
        "errors": tuple(e.model_copy(update={"evidence": "student:4", "origin_quote": "student:4"}) for e in right.errors)})
    experts = []
    for expert in old_experts:
        assessment = grading.MathAssessment.model_validate(grading.read_math_trace(expert)["assessment"])
        assessment.part_decisions.append(unchanged)
        experts.append(expert_for(assessment, plan, a.content).model_copy(update={"provider_id": expert.provider_id}))
    return p, a, plan, experts, right, unchanged


@pytest.mark.asyncio
async def test_reassessment_keeps_other_parts_and_rejects_out_of_scope_edits():
    p, a, plan, experts, right, unchanged = multipart_case()
    bad = revision(right)
    bad["decisions"].append(unchanged.model_dump(mode="json"))
    main, calls = provider("primary", choice(), {"revisions": []})
    regrader, regrade_calls = provider("a", bad, revision(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert [e.score for e in result] == [16, 16]
    assert len(calls) == len(regrade_calls) == 2
    assert json.loads(regrade_calls[0][1].content)["requested_part_ids"] == ["p"]
    assert json.loads(calls[1][1].content)["disputed_item_ids"] == ["p"]
    for before, after in zip(experts, result):
        assert (grading.read_math_trace(before)["assessment"]["part_decisions"][1]
                == grading.read_math_trace(after)["assessment"]["part_decisions"][1])


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["arbitration", "reassessment"])
async def test_cancellation_propagates_and_releases_progress(stage):
    import asyncio
    from backend.progress.tracker import ProgressReporter
    p, a, plan, experts, _ = case()
    async def cancel(messages):
        raise asyncio.CancelledError
    cancelled = SimpleNamespace(provider_id="cancelled", ainvoke=cancel)
    main = cancelled if stage == "arbitration" else provider("primary", choice())[0]
    reporter = ProgressReporter("cancel-method-review")
    with pytest.raises(asyncio.CancelledError):
        await run(p, a, plan, experts, main, {"a": cancelled}, reporter=reporter, student_id="synthetic")
    assert not reporter._progress.active


@pytest.mark.asyncio
async def test_progress_storage_failure_is_not_swallowed_as_model_failure():
    from backend.progress.tracker import ProgressReporter
    from backend.services.grading_checkpoints import CheckpointWriteError
    p, a, plan, experts, _ = case()
    async def fail(*args):
        raise RuntimeError("storage unavailable")
    reporter = ProgressReporter("failed-method-review")
    reporter.substep = fail
    main, calls = provider("primary", choice())
    with pytest.raises(CheckpointWriteError):
        await run(p, a, plan, experts, main, {"a": main}, reporter=reporter)
    assert not calls and not reporter._progress.active


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", [False, True])
@pytest.mark.parametrize("reference", ["", "   "])
async def test_missing_reference_uses_scoped_frozen_method_passage(proof, reference):
    p, a, plan, experts, right = case(proof=proof, reference=reference)
    main, calls = provider("primary", choice(reference_quote="参考解配方"), {"revisions": []})
    regrader, regrade_calls = provider("a", revision(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert [e.score for e in result] == [8, 8]
    assert len(calls) == 2 and len(regrade_calls) == 1
    assert grading.read_math_trace(result[0])["method_review"]["reference_source"] == "frozen_rubric"


@pytest.mark.asyncio
async def test_missing_reference_does_not_accept_an_invented_method_quote():
    p, a, plan, experts, right = case(reference="")
    main, calls = provider("primary", choice(reference_quote="使用韦达定理"), choice(reference_quote="参考解配方"), {"revisions": []})
    regrader, _ = provider("a", revision(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert [e.score for e in result] == [8, 8] and len(calls) == 3


@pytest.mark.asyncio
async def test_missing_reference_with_undetermined_method_falls_back_to_joint_review():
    p, a, plan, experts, right = case(reference="")
    main, calls = provider("primary", choice("undetermined", reference_quote="", reason="评分条款不足以确定参考策略。"),
                           {"revisions": [revision(right)]})
    untouched, untouched_calls = provider("a")
    result = await run(p, a, plan, experts, main, {"a": untouched})
    assert len(calls) == 2 and not untouched_calls
    assert [e.score for e in result] == [8, 8]
    trace = grading.read_math_trace(result[0])
    assert trace["method_review"]["fallback"] == "joint_review"
    assert trace["targeted_review"]["provider"] == "primary"
    assert "selected_methods" not in json.loads(calls[1][1].content)


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", [False, True])
async def test_generic_rubric_cannot_establish_a_reference_method(proof):
    p, a, plan, experts, _ = case(proof=proof, reference="", teacher="正确作答得10分。")
    # The old path would ask for arbitration and accept this literal but
    # method-free quote. It must now go straight to ordinary joint review.
    from backend.llm.providers import LLMResponse
    calls = []
    async def invoke(messages):
        calls.append(messages)
        payload = json.loads(messages[1].content)
        output = choice(reference_quote="正确作答") if "disputed_part_ids" in payload else {"revisions": []}
        return LLMResponse(content=json.dumps(output), provider="offline", model="fixed-json", duration_ms=1)
    main = SimpleNamespace(provider_id="primary", ainvoke=invoke)
    regrader, regrade_calls = provider("a")
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert [e.score for e in result] == [0, 8]
    assert len(calls) == 1 and not regrade_calls
    payload = json.loads(calls[0][1].content)
    assert "disputed_part_ids" not in payload and "selected_methods" not in payload
    for expert in result:
        trace = grading.read_math_trace(expert)
        assert trace["method_review"]["status"] == "reference_unavailable"
        assert trace["method_review"]["fallback"] == "joint_review"


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["", "用配方法解得 1 和 2。"])
async def test_exhausted_invalid_citations_fall_back_without_locking_a_route(reference):
    p, a, plan, experts, _ = case(reference=reference)
    bad = choice(reference_quote="用不存在的代入步骤确定路线。")
    main, calls = provider("primary", bad, bad, {"revisions": []})
    regrader, regrade_calls = provider("a")
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert [e.score for e in result] == [0, 8]
    assert len(calls) == 3 and not regrade_calls
    assert "selected_methods" not in json.loads(calls[-1][1].content)
    assert all(grading.read_math_trace(e)["method_review"]["status"] == "invalid_method_evidence" for e in result)
    assert all(grading.read_math_trace(e)["method_review"]["fallback"] == "joint_review" for e in result)


@pytest.mark.asyncio
async def test_quota_failure_during_evidence_repair_does_not_start_another_review():
    p, a, plan, experts, _ = case(reference="")
    main, calls = provider("primary", choice(reference_quote="不存在的配方法"), DailyQuotaError())
    result = await run(p, a, plan, experts, main, {})
    assert len(calls) == 2 and [e.score for e in result] == [0, 8]
    assert all(grading.read_math_trace(e)["method_review_pending"] for e in result)
    assert all("fallback" not in grading.read_math_trace(e)["method_review"] for e in result)


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", [False, True])
async def test_another_subquestions_answer_cannot_lock_this_parts_method(repair):
    p, a, plan, experts, right, _ = multipart_case()
    bad = choice(reference_quote="用因式分解解得 1 和 2。")
    main, calls = provider("primary", bad, choice() if repair else bad, {"revisions": []})
    regrader, regrade_calls = provider("a", revision(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert [e.score for e in result] == ([16, 16] if repair else [8, 16])
    assert len(calls) == 3 and len(regrade_calls) == int(repair)
    passages = json.loads(calls[0][1].content)["method_reference_passages"]["p"]
    assert "用配方法解得 1 和 2。" in passages
    assert not any("因式分解" in text for text in passages)
    assert ("selected_methods" in json.loads(calls[-1][1].content)) == repair
    for before, after in zip(experts, result):
        assert (grading.read_math_trace(before)["assessment"]["part_decisions"][1]
                == grading.read_math_trace(after)["assessment"]["part_decisions"][1])


@pytest.mark.asyncio
async def test_generic_fragment_of_a_method_passage_is_not_method_evidence():
    p, a, plan, experts, right = case(reference="正确作答。用配方法解得 1 和 2。")
    main, calls = provider("primary", choice(reference_quote="正确作答"), choice(), {"revisions": []})
    regrader, regrade_calls = provider("a", revision(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert [e.score for e in result] == [8, 8]
    assert len(calls) == 3 and len(regrade_calls) == 1


def test_unlabelled_answer_only_uses_explicit_frozen_part_links():
    from backend.services.part_rubric import AnswerSegment
    from backend.skills.math_method_review import _method_passages
    p, _, plan, _, _, _ = multipart_case()
    texts = ["用配方法解得 1 和 2。", "用因式分解解得 1 和 2。"]
    parts = [part.model_copy(update={"answer_refs": (f"a{i}",), "steps": tuple(
        step.model_copy(update={"criterion": "正确作答", "source_quote": "", "answer_refs": ()})
        for step in part.steps)}) for i, part in enumerate(plan.part_rubric.parts)]
    rules = plan.part_rubric.model_copy(update={"parts": tuple(parts), "answer_segments": tuple(
        AnswerSegment(id=f"a{i}", text=text, origin="teacher") for i, text in enumerate(texts))})
    p = p.model_copy(update={"reference_answer": "\n".join(texts)})
    assert _method_passages(p, rules) == {"p": ["正确作答", texts[0]], "p2": ["正确作答", texts[1]]}
    unlinked = rules.model_copy(update={"parts": tuple(part.model_copy(update={"answer_refs": ()}) for part in parts)})
    assert _method_passages(p, unlinked) == {"p": ["正确作答"], "p2": ["正确作答"]}


@pytest.mark.parametrize("text,expected", [
    ("正确作答得10分", False), ("Answer correctly for 10 points", False),
    ("目标 x=1 或 y=2", False), ("x=1, y=2", False), ("x=1 and y=2", False),
    ("无需配方", False), ("Do not use substitution", False),
    ("参考解配方得10分", True), ("Use proof by induction", True),
    ("逐坐标比较两个有序对", True), ("x+2=3⇒x=1", True),
])
def test_method_evidence_requires_an_explicit_technique_or_transformation(text, expected):
    from backend.skills.math_method_review import _has_method_evidence
    assert _has_method_evidence(text) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("errors", [False, True])
@pytest.mark.parametrize("final_change", [False, True])
@pytest.mark.parametrize("legacy_comment", [False, True])
async def test_final_review_rationale_is_outside_score_rows_after_roundtrip(db_case, errors, final_change, legacy_comment):
    from sqlalchemy import select
    from backend.db import grading_repository as grades
    from backend.db.models import AssignmentQuestionRecord
    from backend.db.session import session_scope
    from backend.models import Correction
    from backend.services import task_facade
    from backend.services.math_grading_context import problem_from_question
    from backend.tests.test_grading_checkpoints import prepare, stop

    p, a, plan, experts, right = case()
    row, draft = material(teacher="参考解配方得10分。", alternative={"deduct_per_error": 2})
    with session_scope() as session:
        question = session.scalar(select(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == db_case[0], AssignmentQuestionRecord.q_id == p.q_id))
        for key in ("number", "type", "stem", "criterion", "reference_answer", "max_score"):
            setattr(question, key, row[key])
        question.source = {"presentation": {"rubric_draft": draft.model_dump(mode="json")}}
    run_id = start(db_case, f"method-review-feedback-{errors}")["job_id"]
    checkpoints, questions, revisions, _ = prepare(db_case, run_id)
    question, student = questions[0], revisions[0][1]
    p = problem_from_question(question)
    plan = bind_draft(retarget_draft(draft, p), p)
    checkpoints.save_math_plan(p, plan)
    for expert in experts:
        trace = grading.read_math_trace(expert)
        trace["plan_id"] = plan.plan_id
        expert.raw_output = json.dumps(trace)
    final = revision(right if errors else alternate("complete"))
    final["reason"] = "代入第二个根仍违反原方程，因此保留此处扣分。" if errors else "原等价变换已覆盖两个方向，不要求重抄标答。"
    main, _ = provider("primary", choice(), {"revisions": [final] if final_change else []})
    regrader, _ = provider("a", revision(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    expert = result[0]
    method_reason = choice()['choices'][0]['reason']
    if legacy_comment:
        expert = expert.model_copy(update={'comment': expert.comment.replace('\n\n解法改判依据：' + method_reason, '')})
    checkpoints.save_correction(student, Correction(q_id=p.q_id, type=p.type, score=expert.score,
        max_score=10, confidence=.9, comment=expert.comment, steps=expert.steps,
        expert_results=[expert], synthesis_method="single"))
    stop(db_case, run_id)
    before = [r.model_dump() for r in grades.list_results_for_run(run_id)]
    payload = task_facade.task_results(task_id=db_case[0], owner_id=db_case[1])
    restored = next(c for s in payload["results"] for c in s["corrections"] if c["q_id"] == p.q_id)["expert_results"][0]
    assert restored["score"] == (8 if errors or not final_change else 10)
    if final_change:
        assert final["reason"] in restored["comment"]
    assert restored['comment'].count(method_reason) == 1
    assert all(method_reason not in step['desc'] for step in restored['steps'])
    assert len(restored["steps"]) == 1 and sum(s["score"] for s in restored["steps"]) == restored["score"]
    assert restored["raw_output"] is None
    assert [r.model_dump() for r in grades.list_results_for_run(run_id)] == before
    assert task_facade.task_results(task_id=db_case[0], owner_id=db_case[1]) == payload


@pytest.mark.parametrize("language", ["zh", "en"])
def test_tool_and_proof_advice_are_visible_without_any_score_row(language):
    p, a, plan, experts, _ = case()
    trace = grading.read_math_trace(experts[0])
    trace.update(review_pending=True, evidence=[{"status": "refuted", "reason": "x=2 fails the original equation."}],
                 proof_audit={"review_concerns": [{"reason": "The reverse implication is missing."}]})
    expert = experts[0].model_copy(update={"steps": [], "raw_output": json.dumps(trace)})
    result = grading._with_pending_advice(expert, language)
    assert "x=2 fails the original equation." in result.comment
    assert "The reverse implication is missing." in result.comment
    assert result.steps == [] and result.score == expert.score
    assert grading._with_pending_advice(result, language).comment == result.comment
