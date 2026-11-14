"""Review regressions exercise preparation, persisted edits and actual settlement."""
import json
import pytest
from backend.agents.ingest_agent import _extract_problems_call, generate_missing_question_materials
from backend.services.rubric_drafts import compile_parts_draft, saved_draft
from backend.skills.part_scoring import PartDecision, settle_parts
from backend.tests.test_teacher_frozen_allocations import make_row, proposed
from backend.tests.test_question_material_format_repair import Provider


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["第2题：", "第2题，", "第2题", "Question 2, ", "Q2: "])
async def test_question_notes_never_cap_other_questions(prefix):
    from backend.tests.test_alternative_route import material, alternate, ANSWER, STEM
    row, original = material(alternative={"deduct_per_error": 1})
    setup = {"teacher_notes": prefix + "其他正确解法最多1分"}
    scores = []
    for number in ("1", "2", "3"):
        current = {**row, "q_id": "q" + number, "number": number}
        provider = Provider([])
        draft = saved_draft(current, grading_setup=setup) or await compile_parts_draft(current, provider, grading_setup=setup)
        assert saved_draft({**current, "rubric_draft": draft.model_dump(mode="json")}, grading_setup=setup)
        scores.append(settle_parts(draft.plan.part_rubric, [alternate("complete")], ANSWER, STEM)[0])
        assert not provider.calls
    assert scores == [10, 1, 10]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "Nonblank answers receive at least 2 points",
    "any non-empty answer gets at least 2 points",
    "ALL NON-EMPTY ANSWERS EARN AT LEAST 2 POINTS",
    "非空作答至少得分2分", "非空作答不少于2分",
])
async def test_source_floor_binds_before_freeze_reload_and_scoring(text):
    row = make_row(text)
    raw = proposed()
    raw["nonblank_floor"] = {"pts": 0}  # The model omits the explicit policy.
    provider = Provider([json.dumps({"part_rubric": raw})])
    draft = await compile_parts_draft(row, provider)
    rules = saved_draft({**row, "rubric_draft": draft.model_dump(mode="json")}).plan.part_rubric
    assert rules.nonblank_floor.resolve(10) == 2
    vote = PartDecision(part_id="p", target_quote="part:p", student_quote="student:1",
                        method="reference", relevance="unrelated", progress="无关作答")
    assert settle_parts(rules, [vote], "随意写字", row["stem"])[0] == 2
    blank = vote.model_copy(update={"relevance": "unanswered", "student_quote": ""})
    assert settle_parts(rules, [blank], "", row["stem"])[0] == 0
    assert len(provider.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["inconsistent", "uncertain"])
async def test_unparsed_nonblank_floor_is_audited_instead_of_assuming_zero(verdict):
    from backend.domain.errors import ValidationError
    from backend.tests.test_teacher_policy_audit import PolicyProvider
    row = make_row("Every non-empty answer is awarded at least one fifth of the maximum points")
    raw = proposed()
    raw["nonblank_floor"] = {"pts": 0}
    provider = PolicyProvider(raw, verdict=verdict)
    with pytest.raises(ValidationError, match="could not be verified"):
        await compile_parts_draft(row, provider)
    assert len(provider.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["organized", "extract_from_source"])
async def test_ambiguous_ocr_heading_is_not_asserted_as_a_question_number(mode):
    text = "1. 2 个数的和为10。\n3. 24 名学生排队。\n1. 10 ① 求值。"
    provider = Provider([json.dumps({"problems": [{"q_id": "q1", "number": "1", "type": "计算题", "stem": "2 个数的和为10。", "criterion": ""}]})])
    await _extract_problems_call(text, provider, structure_mode=mode)
    message = provider.calls[0][-1].content
    assert text in message
    hints = json.loads(message.split("[Source heading typography]", 1)[1].split("\n", 1)[1])
    assert hints[0]["possible_numbers"] == ["1.2", "1"]
    assert hints[1]["possible_numbers"] == ["3.24", "3"]
    assert hints[2]["possible_numbers"] == ["1.10", "1"]
    assert all("number" not in hint for hint in hints)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    "解得 x=2。也可以用代入法验证，解得 x=2 成立。",
    "Some students write a=b. That is wrong. We need to recalculate with the corrected definition.",
])
async def test_verification_and_explaining_others_errors_need_no_generation_retry(answer):
    target = dict(q_id="q1", target="reference_answer", target_id="q1:reference_answer")
    provider = Provider([json.dumps({"candidates": [{**target, "text_value": answer}]})])
    result, = await generate_missing_question_materials({"q1": {"type": "推理题", "stem": "Explain the equality.", "max_score": 10}}, [target], 1, provider)
    assert result.text_value == answer and len(provider.calls) == 1


def test_formula_sentence_stop_save_reuses_persisted_rule_without_model(monkeypatch):
    from backend.db import assignment_repository
    from backend.services import task_facade
    from backend.services.math_grading_context import problem_from_question
    from backend.tests.test_frozen_part_rubric import _persist_method_question
    args = _persist_method_question()
    before, = assignment_repository.list_questions(args["task_id"], teacher_id=args["owner_id"])
    old = saved_draft(problem_from_question(before)).plan.part_rubric
    assert before.stem.endswith("。")
    monkeypatch.setattr(task_facade, "_compile_review_edit", lambda *a: pytest.fail("Display edit called the model"))
    task_facade.update_problem(**args, patch={"stem": before.stem[:-1]})
    after, = assignment_repository.list_questions(args["task_id"], teacher_id=args["owner_id"])
    new = saved_draft(problem_from_question(after)).plan.part_rubric
    assert after.stem == before.stem[:-1] and after.version == before.version + 1
    assert new.parts == old.parts and new.nonblank_floor == old.nonblank_floor


@pytest.mark.parametrize("before,after", [("求n!", "求n"), ("求0.5", "求05"), ("```\n文字。\n```", "```\n文字\n```")])
def test_significant_math_and_code_punctuation_still_changes_scoring_input(before, after):
    from backend.services.question_structure import scoring_stem
    assert scoring_stem(before) != scoring_stem(after)


@pytest.mark.parametrize("notes", ["第2、3题其他正确解法最多1分", "对于第2题，其他正确解法最多1分", "第十二题：最多1分"])
def test_unresolved_question_scope_cannot_fall_back_to_all_questions(notes):
    from backend.services.rubric_drafts import _question_notes, RubricAllocationError
    with pytest.raises(RubricAllocationError, match="question heading"):
        _question_notes({"teacher_notes": notes}, "1")


@pytest.mark.parametrize("targets", [
    ["设x>0，证明x²>0", "证明x²>0"],
    ["Prove existence. Decide uniqueness.", "Decide uniqueness."],
])
def test_nested_proof_goal_quote_cannot_spend_the_same_budget_twice(targets):
    from backend.services.proof_goals import ProofOutline, compile_proof_goals
    from backend.tests.test_proof_judgment_review import material
    _, plan = material()
    outline = ProofOutline.model_validate({"parts": [{"part_id": "p", "goals": [
        {"target": target, "source_id": "question:1", "points": 5} for target in targets]}]})
    with pytest.raises(ValueError, match="allocate twice"):
        compile_proof_goals(plan.part_rubric, outline, {"p": {"question:1": targets[0]}}, language="en")
