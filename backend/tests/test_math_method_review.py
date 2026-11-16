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
from backend.tools.structured_llm import PermanentLLMError



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


def reassessment(decision, index=0, *, reference_support="supported", student_support="supported"):
    return {**revision(decision, index), "evidence_checks": [{"part_id": decision.part_id,
        "reference_support": reference_support, "student_support": student_support,
        "reason": "本问标答采用配方；本问作答明确展开因式分解运算，与配方实质不同。"}]}


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
    regrader, regrade_calls = provider("a", reassessment(right))
    untouched, untouched_calls = provider("b")
    from backend.progress.tracker import ProgressReporter
    reporter = ProgressReporter("method-review-test")
    result = await run(p, a, plan, experts, main, {"a": regrader, "b": untouched}, reporter=reporter, student_id="synthetic")
    assert [e.score for e in result] == [10 if same_total else 8] * 2
    assert [e.confidence for e in result] == [.7, .9]
    assert len(main_calls) == 2 and len(regrade_calls) == 1 and not untouched_calls
    assert [e.model_dump(mode="json") for e in experts] == original
    arbitration_input = json.loads(main_calls[0][-1].content)
    assert arbitration_input["answer"] == a.content
    assert arbitration_input["reference"] == p.reference_answer
    assert [e["expert_index"] for e in arbitration_input["experts"]] == [0, 1]
    assert [e["decisions"] for e in arbitration_input["experts"]] == [
        grading.read_math_trace(e)["assessment"]["part_decisions"] for e in experts]
    trace = grading.read_math_trace(result[0])
    assert trace["method_review"]["original_assessment"] == grading.read_math_trace(experts[0])["assessment"]
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
async def test_bad_arbitration_sources_repair_before_any_reassessment():
    p, a, plan, experts, right = case()
    main, calls = provider("primary", choice(student_source_ids=["student:999"]), choice(), {"revisions": []})
    regrader, regrade_calls = provider("a", reassessment(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert [e.score for e in result] == [8, 8]
    assert len(calls) == 3 and len(regrade_calls) == 1


@pytest.mark.asyncio
async def test_reassessment_cannot_keep_wrong_route_and_repairs_with_original_provider():
    p, a, plan, experts, right = case()
    wrong = grading.MathAssessment.model_validate(grading.read_math_trace(experts[0])["assessment"]).part_decisions[0]
    main, calls = provider("primary", choice(), {"revisions": []})
    regrader, regrade_calls = provider("a", reassessment(wrong), reassessment(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert [e.score for e in result] == [8, 8]
    assert len(calls) == len(regrade_calls) == 2


@pytest.mark.asyncio
async def test_final_review_cannot_silently_switch_back_to_wrong_policy():
    p, a, plan, experts, right = case()
    wrong = grading.MathAssessment.model_validate(grading.read_math_trace(experts[0])["assessment"]).part_decisions[0]
    main, calls = provider("primary", choice(), {"revisions": [revision(wrong)]}, {"revisions": []})
    regrader, _ = provider("a", reassessment(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert [e.score for e in result] == [8, 8]
    assert len(calls) == 3
    assert all(grading.read_math_trace(e)["assessment"]["part_decisions"][0]["method"] == "alternative" for e in result)


@pytest.mark.asyncio
async def test_pipeline_is_bounded_even_if_reassessment_disagreement_remains():
    p, a, plan, experts, right = case()
    different = alternate("complete")
    main, calls = provider("primary", choice(), {"revisions": []})
    regrader, regrade_calls = provider("a", reassessment(different))
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
    regrader, regrade_calls = provider("model-a", reassessment(alternate("complete")))
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
    bad = reassessment(right)
    bad["decisions"].append(unchanged.model_dump(mode="json"))
    main, calls = provider("primary", choice(), {"revisions": []})
    regrader, regrade_calls = provider("a", bad, reassessment(right))
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
async def test_missing_reference_does_not_accept_an_invented_method_quote():
    p, a, plan, experts, right = case(reference="")
    main, calls = provider("primary", choice(reference_quote="使用韦达定理"), choice(reference_quote="参考解配方"), {"revisions": []})
    regrader, _ = provider("a", reassessment(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert [e.score for e in result] == [8, 8] and len(calls) == 3


@pytest.mark.asyncio
async def test_primary_can_select_reference_without_regrading_the_matching_expert():
    p, a, plan, experts, _ = case(same_total=True)
    reference = grading.MathAssessment.model_validate(grading.read_math_trace(experts[0])["assessment"]).part_decisions[0]
    main, calls = provider("primary", choice("reference"), {"revisions": []})
    regrader, regrade_calls = provider("b", reassessment(reference, index=1))
    untouched, untouched_calls = provider("a")
    result = await run(p, a, plan, experts, main, {"a": untouched, "b": regrader})
    assert [e.score for e in result] == [10, 10]
    assert len(calls) == 2 and len(regrade_calls) == 1 and not untouched_calls
    assert "method_review" not in grading.read_math_trace(result[0])
    assert grading.read_math_trace(result[1])["assessment"]["part_decisions"][0]["method"] == "reference"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["arbitration", "undetermined", "missing_provider", "reassessment", "invalid_reassessment"])
async def test_failure_keeps_originals_and_runs_the_original_joint_review(stage):
    p, a, plan, experts, right = case()
    wrong = grading.MathAssessment.model_validate(grading.read_math_trace(experts[0])["assessment"]).part_decisions[0]
    first = (PermanentLLMError("offline failure") if stage == "arbitration" else
             choice("undetermined", reference_quote="") if stage == "undetermined" else choice())
    main, calls = provider("primary", first, {"revisions": [revision(right)]})
    replies = [reassessment(wrong)] * 2 if stage == "invalid_reassessment" else [PermanentLLMError("offline failure")]
    regrader, regrade_calls = provider("a", *replies)
    result = await run(p, a, plan, experts, main, {"b": regrader} if stage == "missing_provider" else {"a": regrader})
    assert len(calls) == 2
    final_input = json.loads(calls[-1][-1].content)
    assert "selected_methods" not in final_input
    assert final_input["experts"][0]["decisions"][0]["method"] == "reference"
    assert [e.score for e in result] == [8, 8]  # The ordinary joint review can still correct the score.
    assert not any("method_review" in grading.read_math_trace(e) for e in result)
    assert len(regrade_calls) == (2 if stage == "invalid_reassessment" else 1 if stage == "reassessment" else 0)


@pytest.mark.asyncio
async def test_later_reassessment_failure_discards_all_staged_changes():
    from backend.skills.math_method_review import align_methods
    p, a, plan, experts, right = case()
    experts.insert(1, experts[0].model_copy(update={"provider_id": "c", "provider": "c"}))
    snapshots = [e.model_dump_json() for e in experts]
    main, calls = provider("primary", choice())
    first, first_calls = provider("a", reassessment(right))
    second, second_calls = provider("c", PermanentLLMError("offline failure"))
    traces = [grading.read_math_trace(e) for e in experts]
    assessments = [grading.MathAssessment.model_validate(t["assessment"]) for t in traces]
    result, locks = await align_methods(p, a, experts, assessments, traces, plan, main, {"a": first, "c": second},
                                        language="zh", grading_setup=None)
    assert result is experts and not locks
    assert [e.model_dump_json() for e in result] == snapshots
    assert len(calls) == len(first_calls) == len(second_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_kind", ["unknown_part", "duplicate_part", "missing_part", "unknown_source", "invented_reference"])
async def test_invalid_arbitration_is_bounded_and_never_locks_any_route(bad_kind):
    p, a, plan, experts, _ = case()
    invalid = choice()
    c = invalid["choices"][0]
    if bad_kind == "unknown_part":
        c["part_id"] = "foreign"
    elif bad_kind == "duplicate_part":
        invalid["choices"].append(c.copy())
    elif bad_kind == "missing_part":
        invalid["choices"] = []
    elif bad_kind == "unknown_source":
        c["student_source_ids"] = ["student:999"]
    else:
        c["reference_quote"] = "invented method"
    main, calls = provider("primary", invalid, invalid, {"revisions": []})
    regrader, regrade_calls = provider("a")
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert len(calls) == 3 and not regrade_calls
    assert "selected_methods" not in json.loads(calls[-1][-1].content)
    assert [e.score for e in result] == [e.score for e in experts]
    assert all("method_review" not in grading.read_math_trace(e) for e in result)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_kind", ["wrong_expert", "duplicate_part", "missing_part", "extra_part", "invented_evidence"])
async def test_invalid_reassessment_cannot_escape_its_scope(bad_kind):
    p, a, plan, experts, right, other = multipart_case()
    invalid = reassessment(right)
    if bad_kind == "wrong_expert":
        invalid["expert_index"] = 1
    elif bad_kind == "duplicate_part":
        invalid["decisions"] *= 2
    elif bad_kind == "missing_part":
        invalid["decisions"] = []
    elif bad_kind == "extra_part":
        invalid["decisions"].append(other.model_dump(mode="json"))
    else:
        invalid["decisions"][0]["student_quote"] = "invented answer"
    main, calls = provider("primary", choice(), {"revisions": []})
    regrader, regrade_calls = provider("a", invalid, invalid)
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert len(calls) == len(regrade_calls) == 2
    assert [grading.read_math_trace(e)["assessment"] for e in result] == [
        grading.read_math_trace(e)["assessment"] for e in experts]
    assert "selected_methods" not in json.loads(calls[-1][-1].content)


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", [False, True])
async def test_score_disagreement_without_method_disagreement_is_exactly_the_old_path(proof):
    p, a, plan, experts, right = case(proof=proof)
    complete = alternate("complete")
    experts[0] = expert_for(grading.FrozenPartAssessment(part_decisions=[complete], comment="Complete").assessment(),
                            plan, a.content).model_copy(update={"provider_id": "a"})
    old_main, old_calls = provider("primary", {"revisions": []})
    new_main, new_calls = provider("primary", {"revisions": []})
    old = await run(p, a, plan, experts, old_main, None)
    new = await run(p, a, plan, experts, new_main, {"a": new_main})
    assert old == new and len(old_calls) == len(new_calls) == 1
    assert old_calls == new_calls


@pytest.mark.asyncio
async def test_full_ledger_final_review_is_also_bound_to_the_adjudicated_method():
    p, a, plan, experts, right = case()
    # Pending tool evidence forces the ordinary complete-assessment review format.
    trace = grading.read_math_trace(experts[0])
    trace["evidence"] = [{"evidence_id": "v1", "status": "unknown"}]
    experts[0] = experts[0].model_copy(update={"raw_output": json.dumps(trace)})
    invalid = {"expert_index": 0, "reason": "Try to restore the old route", "source_quote": "因式分解为 (x−1)(x−2)=0。",
               "assessment": trace["assessment"]}
    main, calls = provider("primary", choice(), {"revisions": [invalid]}, {"revisions": [invalid]})
    regrader, _ = provider("a", reassessment(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert len(calls) == 3 and [e.score for e in result] == [8, 8]
    final = grading.read_math_trace(result[0])
    assert final["assessment"]["part_decisions"][0]["method"] == "alternative"
    assert final["review_diagnostic"]["validation_issues"][0][0]["code"] == "review_changed_adjudicated_method"


@pytest.mark.asyncio
async def test_final_review_may_change_credit_within_the_fixed_route_and_keeps_rationale():
    p, a, plan, experts, right = case()
    main, calls = provider("primary", choice(), {"revisions": [revision(alternate("complete"))]})
    regrader, _ = provider("a", reassessment(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert len(calls) == 2 and [e.score for e in result] == [10, 8]
    assert grading.read_math_trace(result[0])["assessment"]["part_decisions"][0]["method"] == "alternative"
    reason = choice()["choices"][0]["reason"]
    assert result[0].comment.count(reason) == 1
    assert all(reason not in s.desc for s in result[0].steps)
    assert grading.ExpertResult.model_validate_json(result[0].model_dump_json()).comment == result[0].comment


@pytest.mark.asyncio
@pytest.mark.parametrize("text,quote", [
    ("(1) 因式分解为 (x−1)(x−2)=0；(2) x=2。", "因式分解为 (x−1)(x−2)=0"),
    ("（１）因式分解为 (x−1)(x−2)=0", "因式分解为 (x−1)(x−2)=0"),
    ("(1)\nx²−3x+2=0\n(x−1)(x−2)=0", "(x−1)(x−2)=0"),
    ("x²−3x+2=0 ⇒ (x−1)(x−2)=0", "(x−1)(x−2)=0"),
    (r"\(x^2-3x+2=0\Rightarrow (x-1)(x-2)=0\)", "(x-1)(x-2)=0"),
    ("先写(2)x=4作为中间式，最后因式分解为 (x−1)(x−2)=0", "(x−1)(x−2)=0"),
])
async def test_normal_notation_does_not_acquire_a_heading_or_method_keyword_gate(text, quote):
    p, a, plan, experts, right = case(same_total=True)
    a = a.model_copy(update={"content": text})
    line = next(i for i, value in enumerate(text.splitlines(), 1) if quote in value)
    right = right.model_copy(update={"student_quote": f"student:{line}"})
    for index, expert in enumerate(experts):
        trace = grading.read_math_trace(expert)
        trace["assessment"]["part_decisions"][0]["student_quote"] = f"student:{line}"
        experts[index] = expert.model_copy(update={"raw_output": json.dumps(trace)})
    main, calls = provider("primary", choice(student_source_ids=[f"student:{line}"]), {"revisions": []})
    regrader, regrade_calls = provider("a", reassessment(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert len(calls) == 2 and len(regrade_calls) == 1
    assert [e.score for e in result] == [10, 10]


@pytest.mark.asyncio
async def test_unchanged_blank_part_stays_blank_and_zero():
    p, a, plan, experts, right, other = multipart_case()
    blank = other.model_copy(update={"relevance": "unanswered", "method": "undetermined",
                                    "student_quote": "", "errors": (), "steps": ()})
    a = a.model_copy(update={"content": ANSWER + "\n(2)"})
    for index, expert in enumerate(experts):
        assessment = grading.MathAssessment.model_validate(grading.read_math_trace(expert)["assessment"])
        assessment.part_decisions[1] = blank
        experts[index] = expert_for(assessment, plan, a.content).model_copy(update={"provider_id": expert.provider_id})
    main, calls = provider("primary", choice(), {"revisions": []})
    regrader, _ = provider("a", reassessment(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert len(calls) == 2 and [e.score for e in result] == [8, 8]
    for expert in result:
        assert grading.read_math_trace(expert)["assessment"]["part_decisions"][1] == blank.model_dump(mode="json")
        assert expert.steps[-1].part_id == "p2" and expert.steps[-1].score == 0


@pytest.mark.asyncio
async def test_two_disputed_parts_regrade_only_each_experts_wrong_part():
    p, a, plan, experts, right, other = multipart_case()
    trace = grading.read_math_trace(experts[1])
    trace["assessment"]["part_decisions"][1].update(method="reference", relevance="incorrect", errors=[])
    old = grading.MathAssessment.model_validate(trace["assessment"])
    experts[1] = expert_for(old, plan, a.content).model_copy(update={"provider_id": "b"})
    both = choice()
    both["choices"].append({**choice()["choices"][0], "part_id": "p2", "student_source_ids": ["student:3"]})
    main, calls = provider("primary", both, {"revisions": []})
    first, first_calls = provider("a", reassessment(right))
    second, second_calls = provider("b", reassessment(other, index=1))
    result = await run(p, a, plan, experts, main, {"a": first, "b": second})
    assert len(calls) == 2 and len(first_calls) == len(second_calls) == 1
    assert json.loads(first_calls[0][-1].content)["requested_part_ids"] == ["p"]
    assert json.loads(second_calls[0][-1].content)["requested_part_ids"] == ["p2"]
    assert [e.score for e in result] == [16, 16]


@pytest.mark.asyncio
async def test_multi_sample_votes_keep_sample_identity_and_use_their_shared_provider():
    p, a, plan, experts, right = case()
    for index, expert in enumerate(experts):
        experts[index] = expert.model_copy(update={"provider_id": "configuration", "provider": f"same#{index + 1}"})
    main, calls = provider("same", choice(), reassessment(right), {"revisions": []})
    result = await run(p, a, plan, experts, main, {"configuration": main})
    assert len(calls) == 3 and [e.provider for e in result] == ["same#1", "same#2"]
    assert [e.provider_id for e in result] == ["configuration"] * 2
    assert [e.confidence for e in result] == [.7, .9]
    assert [e.score for e in result] == [8, 8]


def bound_multipart_case(*, step_links=False):
    from backend.services.part_rubric import PartRubric
    p, a, plan, experts, right, other = multipart_case()
    rules = plan.part_rubric.model_dump(mode="json")
    rules["answer_segments"] = [
        {"id": "answer1", "text": "用配方法解得 1 和 2。", "origin": "teacher"},
        {"id": "answer2", "text": "用因式分解解得 1 和 2。", "origin": "teacher"},
    ]
    for part, ref in zip(rules["parts"], ("answer1", "answer2")):
        target = part["steps"][0] if step_links else part
        target["answer_refs"] = [ref]
    plan = plan.model_copy(update={"part_rubric": PartRubric.model_validate(rules)})
    return p, a, plan, experts, right, other


@pytest.mark.asyncio
@pytest.mark.parametrize("step_links", [False, True])
async def test_explicit_reference_binding_cannot_be_bypassed_through_whole_answer(step_links):
    p, a, plan, experts, right, _ = bound_multipart_case(step_links=step_links)
    invalid = choice(reference_quote="用因式分解解得 1 和 2。", student_source_ids=["student:3"])
    main, calls = provider("primary", invalid, invalid, {"revisions": []})
    borrowed = right.model_copy(update={"student_quote": "student:3"})
    regrader, regrade_calls = provider("a", reassessment(borrowed))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert not regrade_calls
    assert [e.score for e in result] == [8, 16]
    assert all("method_review" not in grading.read_math_trace(e) for e in result)
    assert "selected_methods" not in json.loads(calls[-1][-1].content)


@pytest.mark.asyncio
@pytest.mark.parametrize("step_links", [False, True])
async def test_own_bound_reference_still_allows_reassessment(step_links):
    p, a, plan, experts, right, _ = bound_multipart_case(step_links=step_links)
    main, calls = provider("primary", choice(), {"revisions": []})
    regrader, regrade_calls = provider("a", reassessment(right))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert [e.score for e in result] == [16, 16]
    assert len(calls) == 2 and len(regrade_calls) == 1
    for request in (calls[0], regrade_calls[0]):
        allowed = json.loads(request[-1].content)["allowed_reference_sources"]["p"]
        assert allowed["include_full_reference"] is False
        assert "用配方法解得 1 和 2。" in allowed["texts"]
        assert not any("因式分解" in text for text in allowed["texts"])
    assert grading.read_math_trace(result[0])["method_review"]["evidence_checks"] == reassessment(right)["evidence_checks"]


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", [False, True])
@pytest.mark.parametrize("reference,quote", [
    ("禁止使用配方。", "禁止使用配方。"),
    ("禁止使用配方。", "配方"),
    ("不得\n使用配方，应使用求根公式。", "使用配方"),
    ("正确作答得10分。", "正确作答得10分。"),
])
async def test_semantic_reference_refusal_falls_back_without_forcing_agreement(proof, reference, quote):
    # Located text is deliberately NOT sufficient: the original expert's
    # semantic verdict must support it. Fixed replies test the contract, not
    # whether a real provider will always interpret the language correctly.
    p, a, plan, experts, right = case(reference=reference, proof=proof)
    main, calls = provider("primary", choice(reference_quote=quote), {"revisions": []})
    declined = reassessment(right, reference_support="unsupported")
    declined["decisions"] = []
    declined["evidence_checks"][0]["reason"] = "引文没有陈述本问标答实际采用的配方法，不能据此比较学生方法。"
    regrader, regrade_calls = provider("a", declined)
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert len(calls) == 2 and len(regrade_calls) == 1
    assert json.loads(regrade_calls[0][-1].content)["reference"] == reference
    assert [e.score for e in result] == [e.score for e in experts]
    assert all("method_review" not in grading.read_math_trace(e) for e in result)
    assert "selected_methods" not in json.loads(calls[-1][-1].content)


@pytest.mark.asyncio
@pytest.mark.parametrize("student_support", ["unsupported", "uncertain"])
@pytest.mark.parametrize("blank", [False, True])
async def test_student_evidence_must_support_this_part_before_locking(student_support, blank):
    p, a, plan, experts, right, _ = bound_multipart_case()
    if blank:
        a = a.model_copy(update={"content": "(1)\n(2)"})
    source = "student:2" if blank else "student:3"
    main, calls = provider("primary", choice(student_source_ids=[source]), {"revisions": []})
    declined = reassessment(right, student_support=student_support)
    declined["decisions"] = []
    declined["evidence_checks"][0]["reason"] = "该引文只有题号。" if blank else "引用的是第二问的运算，不能证明第一问使用此方法。"
    regrader, regrade_calls = provider("a", declined)
    original = [e.model_dump(mode="json") for e in experts]
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert len(calls) == 2 and len(regrade_calls) == 1
    assert [e.score for e in result] == [8, 16]
    assert [e.model_dump(mode="json") for e in experts] == original
    assert all("method_review" not in grading.read_math_trace(e) for e in result)
    assert "selected_methods" not in json.loads(calls[-1][-1].content)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_kind", ["missing_checks", "empty_checks", "duplicate_check", "other_part", "missing_method"])
async def test_reassessment_requires_explicit_complete_evidence_confirmation(bad_kind):
    p, a, plan, experts, right = case()
    invalid = reassessment(right)
    if bad_kind == "missing_checks":
        del invalid["evidence_checks"]
    elif bad_kind == "empty_checks":
        invalid["evidence_checks"] = []
    elif bad_kind == "duplicate_check":
        invalid["evidence_checks"] *= 2
    elif bad_kind == "other_part":
        invalid["evidence_checks"][0]["part_id"] = "p2"
    else:
        invalid["decisions"][0]["student_method"] = " "
    main, calls = provider("primary", choice(), {"revisions": []})
    regrader, regrade_calls = provider("a", invalid, invalid)
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert len(calls) == 2 and len(regrade_calls) == 2
    assert [e.score for e in result] == [e.score for e in experts]
    assert "selected_methods" not in json.loads(calls[-1][-1].content)


@pytest.mark.asyncio
async def test_later_expert_semantic_refusal_discards_earlier_reassessment():
    p, a, plan, experts, right = case()
    experts.append(experts[0].model_copy(update={"provider_id": "c"}))
    original = [e.model_dump(mode="json") for e in experts]
    main, calls = provider("primary", choice(), {"revisions": []})
    first, first_calls = provider("a", reassessment(right))
    declined = reassessment(right, index=2, student_support="uncertain")
    declined["decisions"] = []
    second, second_calls = provider("c", declined)
    result = await run(p, a, plan, experts, main, {"a": first, "c": second})
    assert len(calls) == 2 and len(first_calls) == len(second_calls) == 1
    assert [e.score for e in result] == [e.score for e in experts]
    assert [e.model_dump(mode="json") for e in experts] == original
    assert all("method_review" not in grading.read_math_trace(e) for e in result)
    assert "selected_methods" not in json.loads(calls[-1][-1].content)


@pytest.mark.asyncio
@pytest.mark.parametrize("reading", [{"method": "undetermined"}, {"relevance": "unanswered"}])
async def test_existing_part_judgment_can_also_decline_the_proposed_route(reading):
    p, a, plan, experts, right = case()
    main, calls = provider("primary", choice(), {"revisions": []})
    regrader, regrade_calls = provider("a", reassessment(right.model_copy(update=reading)))
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert len(calls) == 2 and len(regrade_calls) == 1
    assert [e.score for e in result] == [e.score for e in experts]
    assert "selected_methods" not in json.loads(calls[-1][-1].content)


@pytest.mark.asyncio
async def test_format_repair_may_decline_without_retaining_an_invalid_candidate():
    p, a, plan, experts, right = case()
    wrong = grading.MathAssessment.model_validate(grading.read_math_trace(experts[0])["assessment"]).part_decisions[0]
    main, calls = provider("primary", choice(), {"revisions": []})
    declined = reassessment(right, student_support="uncertain")
    declined["decisions"] = []
    regrader, regrade_calls = provider("a", reassessment(wrong), declined)
    result = await run(p, a, plan, experts, main, {"a": regrader})
    assert len(calls) == len(regrade_calls) == 2
    assert [e.score for e in result] == [e.score for e in experts]
    assert all("method_review" not in grading.read_math_trace(e) for e in result)
    assert "selected_methods" not in json.loads(calls[-1][-1].content)
