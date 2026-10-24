"""Source-scoped binary rules through shared grading, including fallback/review.

Provider JSON is replayed, not a paid accuracy or timing benchmark.
"""
from unittest.mock import AsyncMock

import pytest

from backend.models import ProblemInfo, StudentAnswerInfo
from backend.services.binary_scoring import binary_rule, whole_question_policy
from backend.services.math_grading_context import math_plans_context
from backend.services.math_rubric_plan import fallback_math_rubric_plan
from backend.skills.calculation import CalculationSkill
from backend.skills import math_grading as grading
from backend.tests.test_math_rubric_plan import ReplayProvider


@pytest.mark.parametrize("criterion", [
    "选B得4分，否则0分", "选择B（4）得4分，否则0分。", "答对满分，答错0分。",
    "本题选 B 得 4 分，否则 0 分。", "选项B得4分，否则0分。",
    "评分标准：答案正确得4分，否则不得分。", "答案唯一: 答对满分, 答错 0 分",
    "- Correct answer receives 4 points; otherwise 0 points：4 points",
    "Correct answer receives 4 points; otherwise 0 points.", "Correct: full credit, wrong: no credit",
    "答案正确得满分，否则得零分。", "答案正确得四分，否则得零分。",
])
def test_only_explicit_binary_rule_is_detected(criterion):
    assert binary_rule(criterion, 4)
    if "满分" not in criterion and "full credit" not in criterion:
        assert not binary_rule(criterion, 6)


@pytest.mark.parametrize("criterion", [
    "正确选项及结果 — 所有作答\n- 选择B（4）得4分，否则0分；不要求过程：4分\n只要求选项，无过程分。\n结果为4。",
    "正确选项及结果 — 所有作答\n- 选择B（4）得4分，否则0分：4分\n结果为4。",
])
def test_display_envelope_is_not_reinterpreted_as_policy_source(criterion):
    # These generated rubrics still produce 0/full in the end-to-end tests.
    # Their policy is persisted from verified items, never from this display.
    assert whole_question_policy(criterion, 4) is None


@pytest.mark.parametrize("criterion", [
    "全部选对4分，部分选对2分，错选0分；解题过程不影响评分",
    "每空2分，不计过程分", "第一空选B得4分，否则0分。第二空4分。",
    "选项2分、解释2分", "选B得2分，否则0分；解释2分",
    "选B得4分，否则0分；但漏选得2分", "选B得4分，否则0分；除非教师另有要求",
    "如果只看答案，选B得4分，否则0分", "方法4分，结论6分。",
    "不采用以下规则：选B得4分，否则0分", "答案4分，过程4分",
    "不采用以下规则。选B得4分，否则0分", "例如：\n选B得4分，否则0分",
    "选B得4分，否则0分。空白：4分", "选B得4分，否则0分。解释：50%",
    "选B得4分，否则0分；解释：4分", "选B得4分，否则0分：2分",
    "答对满分，答错0分；若有正确步骤则酌情给分",
    "正确答案可得4分，错误答案按过程给分", "选B得4分，否则0分；解释：2分",
    "Each blank: correct answer receives 4 points; otherwise 0 points.",
    "Correct answer receives 4 points; otherwise 0 points. Partial credit for a clean subset.",
    "Correct answer receives 4 points; otherwise 0 points. Explanation worth 2 points.",
    "全部选对得4分，否则0分。例外：少选且无错选得两分。",
    "全部选对得4分，否则0分。乙类作答按另一张表给分。",
    "选择B（少选得两分）得4分，否则0分。",
    "正确选项及结果 — 每个空\n- 选B得4分，否则0分：4分",
    "少选得两分 — 所有作答\n- 选B得4分，否则0分：4分",
    "正确选项及结果 — 所有作答\n- 选B得4分，否则0分：4分\n少选得两分。",
])
def test_partial_scoped_or_ambiguous_rules_are_not_flattened(criterion):
    assert binary_rule(criterion, 4) is None


def test_complete_policy_has_explicit_question_scope_and_numeric_bounds():
    policy = whole_question_policy("答案正确得二点五分，否则得零分。", 2.5)
    assert policy.scope == "question"
    assert policy.correct_score == 2.5 and policy.incorrect_score == 0
    assert policy.source_quote == "答案正确得二点五分，否则得零分"


def inputs(criterion="选B得4分，否则0分。", text="B"):
    p = ProblemInfo(q_id="q1", number="1", type="计算题", stem="计算2+2：A.3 B.4", max_score=4,
                    criterion=criterion, reference_answer="B")
    a = StudentAnswerInfo(q_id="q1", number="1", type="计算题", content=text)
    return p, a, fallback_math_rubric_plan(p)


def assessment(plan, text, correct, score=2):
    return dict(route_id=plan.routes[0].id, route_reason="按教师规定判断最终选项", score=score,
                comment="依据选项判分", rationale="模型给出不一致的分数", confidence=.9,
                claims=[dict(id="s1", quote=text, verdict="supported" if correct else "refuted", reason="最终选项判断")])


@pytest.fixture(autouse=True)
def no_external_knowledge(monkeypatch):
    monkeypatch.setattr(grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    # Isolate settlement from CAS auto-planning. The explicit review regression
    # below still executes the real review parser and scorer.
    monkeypatch.setattr(CalculationSkill, "_verify", AsyncMock(return_value=[]))


@pytest.mark.asyncio
@pytest.mark.parametrize("correct", [True, False])
async def test_holistic_rubric_enforces_binary_score(correct):
    p, a, plan = inputs(text="B" if correct else "A")
    provider = ReplayProvider(assessment(plan, a.content, correct))
    with math_plans_context({"q1": plan}):
        result = await CalculationSkill(provider).grade(p, a)
    assert not result.error_kind and result.score == (4 if correct else 0)
    assert sum(step.score for step in result.steps) == result.score
    assert len(provider.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("correct", [True, False])
async def test_direct_fallback_cannot_reintroduce_intermediate_score(correct):
    p, a, plan = inputs(text="B" if correct else "A")
    provider = ReplayProvider({}, {}, dict(score=2, binary_correct=correct, rationale="最终选项判断", comment="按标准评分"))
    with math_plans_context({"q1": plan}):
        result = await CalculationSkill(provider).grade(p, a)
    assert not result.error_kind and result.score == (4 if correct else 0)
    assert sum(step.score for step in result.steps) == result.score
    assert len(provider.messages) == 3  # Existing detailed repair and direct fallback only.


@pytest.mark.asyncio
async def test_direct_binary_judgment_uses_existing_format_repair_when_missing():
    p, a, plan = inputs()
    grade = dict(score=2, rationale="最终选项正确", comment="按标准评分")
    provider = ReplayProvider(grade, {**grade, "binary_correct": True})
    result = await CalculationSkill(provider)._direct(p, a, plan, "", None, [])
    assert result.score == 4 and len(provider.messages) == 2


@pytest.mark.asyncio
async def test_missing_judgment_never_succeeds_with_a_partial_direct_grade():
    p, a, plan = inputs()
    provider = ReplayProvider(dict(score=2, rationale="未判断最终选项", comment="按标准评分"))
    with pytest.raises(ValueError, match="binary_rule_requires_final_answer_judgment"):
        await CalculationSkill(provider)._direct(p, a, plan, "", None, [])
    assert len(provider.messages) == 2


@pytest.mark.parametrize("verdict", ["supported", "refuted", "unknown"])
def test_intermediate_claim_does_not_stand_in_for_final_answer(verdict):
    p, a, plan = inputs(text="先算2+1=3。最终选择A。")
    draft = assessment(plan, "2+1=3", True)
    draft["claims"][0]["verdict"] = verdict
    with pytest.raises(ValueError, match="final_answer_judgment"):
        grading.score_assessment(grading.MathAssessment(**draft), plan, a.content)
    draft["binary_correct"] = False
    assert grading.score_assessment(grading.MathAssessment(**draft), plan, a.content)[0] == 0


def test_explicit_judgment_cannot_contradict_whole_answer_evidence():
    p, a, plan = inputs(text="A")
    draft = assessment(plan, a.content, False)
    draft["binary_correct"] = True
    with pytest.raises(ValueError, match="conflicts_with_answer_verdict"):
        grading.score_assessment(grading.MathAssessment(**draft), plan, a.content)


@pytest.mark.asyncio
async def test_expert_revision_obeys_the_same_binary_constraint():
    from backend.tests.test_math_grading import expert_for
    p, a, plan = inputs(text="A")
    old = grading.MathAssessment(**assessment(plan, a.content, False, score=0))
    expert = expert_for(old, plan, a.content)
    trace = grading.read_math_trace(expert)
    trace["assessment"]["needs_review"] = True
    import json
    expert.raw_output = json.dumps(trace)
    revision = assessment(plan, a.content, False, score=2)
    provider = ReplayProvider(dict(revisions=[dict(expert_index=0, reason="核对选项", source_quote="A", assessment=revision)]))
    with math_plans_context({"q1": plan}), grading.math_review_scope():
        results = await grading.review_math_experts(p, a, [expert], provider)
    assert results[0].score == 0 and len(provider.messages) == 1
    assert sum(step.score for step in results[0].steps) == 0


@pytest.mark.parametrize("criterion,expected", [
    ("全部选对4分，部分选对2分，错选0分；解题过程不影响评分", 2),
    ("每空2分，不计过程分", 2), ("选项2分、解释2分", 2),
])
def test_legitimate_partial_holistic_grades_survive(criterion, expected):
    p, a, plan = inputs(criterion, text="部分完成")
    score, steps = grading.score_assessment(grading.MathAssessment(**assessment(plan, a.content, False)), plan, a.content)
    assert score == expected == sum(step.score for step in steps)
