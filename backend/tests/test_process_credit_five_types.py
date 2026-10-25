"""PR262 acceptance on the five live strategies, using raw provider JSON.

These cases preserve teacher scope and budgets through generation, saved draft,
reuse, and actual skill settlement. No live model or sandbox is used.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents.ingest_agent import generate_missing_question_materials
from backend.models import ProblemInfo, StudentAnswerInfo
from backend.services.math_grading_context import math_plans_context, prepare_math_plans
from backend.services.question_preparation_artifacts import ArtifactProblemV1
from backend.services.rubric_drafts import saved_draft
from backend.skills.base import get_skill_for_type
from backend.skills import math_grading
from backend.tests.test_math_rubric_plan import ReplayProvider


# criterion, complete stem, reference, submission, (source budget, points, award)
CASES = [
    ("数值3分，单位1分，不计过程分", "物体6秒移动18米，填写平均速度的数值和单位。", "3 m/s", "3 kg",
     [("数值3分", 3, 3), ("单位1分", 1, 0)]),
    ("每个空格2分，不计过程分", "填写两个空格：2+2=____，3+3=____。", "4；6", "4；7",
     [("每个空格2分", 2, 2), ("每个空格2分", 2, 0)]),
    ("第1小题答案2分，只看答案、不计过程分；第2小题过程1分、答案1分。",
     "(1)写出2+2的答案。(2)计算3×3，写出列式和结果。", "(1)4；(2)3×3=9", "(1)4；(2)3×3=8",
     [("第1小题答案2分", 2, 2), ("第2小题过程1分", 1, 1), ("答案1分", 1, 0)]),
    ("计算答案1分；只有最终答案正确才给计算过程2分；独立解释1分，与答案正误无关。",
     "计算6÷2，并独立解释为什么除数不能为0。解释分与计算答案正误无关。",
     "3；0乘任何数都是0，不能得到非零被除数。", "6÷2=4；0乘任何数都是0，不能得到非零被除数。",
     [("计算答案1分", 1, 0), ("只有最终答案正确才给计算过程2分", 2, 0), ("独立解释1分", 1, 1)]),
    ("评分标准：过程2分，答案2分", "计算3×3，写出列式和结果。", "3×3=9", "3×3=8",
     [("评分标准：过程2分", 2, 2), ("答案2分", 2, 0)]),
    ("过程占50%，答案占50%", "计算3×3，写出列式和结果。", "3×3=9", "3×3=8",
     [("过程占50%", 2, 2), ("答案占50%", 2, 0)]),
    ("过程2分，答案2分，若未作答则0分", "计算3×3，写出列式和结果。", "3×3=9", "3×3=8",
     [("过程2分", 2, 2), ("答案2分", 2, 0)]),
    ("选项2分、解释2分", "选择2+2的结果并解释：A.3；B.4。", "B；2与2相加为4", "B；不知道如何解释",
     [("选项2分", 2, 2), ("解释2分", 2, 0)]),
    ("全部选对4分，部分选对2分，错选0分；解题过程不影响评分。",
     "选出偶数：A.2；B.4；C.5；D.7。", "AB", "A", []),
]

# Independent acceptance from the original PR remains on the live paths.
# A correct per-item model judgment is never converted to a whole-question
# boolean merely because the source mentions or prohibits working.
POLICY_CASES = [
    (policy, "计算2+2，按评分规则作答。", "4", text, [("", 4, score)])
    for policy in ("不计过程分", "只看最终答案", "不计过程分（只看最终答案）", "评分时不计过程分")
    for text, score in (("3", 0), ("4", 4))
] + [
    ("每步1分", "依次计算1+1、2+2、3+3、4+4，各占一步。", "2；4；6；8", "2；4；7；9",
     [("每步1分", 1, 1), ("每步1分", 1, 1), ("每步1分", 1, 0), ("每步1分", 1, 0)]),
    ("只有答案正确才给过程2分；答案2分", "计算3×3，写出过程和答案。", "3×3=9", "3×3=8",
     [("只有答案正确才给过程2分", 2, 0), ("答案2分", 2, 0)]),
    ("答案2分，过程2分；若未作答则0分", "计算3×3，写出过程和答案。", "3×3=9", "3×3=8",
     [("答案2分", 2, 0), ("过程2分", 2, 2)]),
    ("数值错误扣3分，单位错误扣1分，不计过程分", "物体6秒移动18米，填写平均速度。", "3 m/s", "3 kg", []),
]


@pytest.fixture(autouse=True)
def isolate_external_checks(monkeypatch):
    monkeypatch.setattr(math_grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    monkeypatch.setattr(math_grading.MathGradingSkill, "_verify", AsyncMock(return_value=[]))


async def persisted_case(kind, case):
    criterion, stem, reference, text, budgets = case
    row = dict(q_id="q1", number="1", type=kind, stem=stem, criterion=criterion, max_score=4,
               reference_answer="", material_provenance={"criterion": {"source_kind": "teacher_provided"}})
    # Repeated per-blank quotes cannot uniquely bind two item IDs in the finite
    # compiler. Preserve the entire policy in its existing holistic route.
    structured = budgets and len({b[0] for b in budgets}) == len(budgets) and "若未作答" not in criterion
    items = [dict(id=f"i{i}", description=quote or "依教师原文评估最终答案", source_quote=quote, points=points,
                  source="provided" if quote else "ai") for i, (quote, points, _) in enumerate(budgets)] if structured else []
    definition = dict(routes=[dict(id="teacher", description=criterion, applies_when="按各分项原有范围",
        mode="points" if items else "holistic", items=items)],
        follow_through="每条规则只适用于其原文指定的分项或小题。", numerical_policy="按原标准分别判定数值、单位与解释。")
    generator = ReplayProvider({"candidates": [dict(target_id="q1:reference_answer", q_id="q1",
        target="reference_answer", text_value=reference, rubric_definition=definition)]})
    candidates = await generate_missing_question_materials({"q1": row}, [dict(target_id="q1:reference_answer",
        q_id="q1", target="reference_answer")], 6, generator, generation_language="zh", allow_partial=True)
    assert len(candidates) == 1 and candidates[0].rubric_draft
    row.update(reference_answer=reference, rubric_draft=candidates[0].rubric_draft,
               ai_completion_provenance={"reference_answer": {"source_kind": "ai_generated"}})
    row = ArtifactProblemV1.model_validate_json(ArtifactProblemV1(**row).model_dump_json()).model_dump(mode="json")
    assert row["criterion"] == criterion and saved_draft(row)
    registry = SimpleNamespace(pick_default=AsyncMock(side_effect=AssertionError("No second compiler")))
    plans = await prepare_math_plans({"q1": row}, registry)
    registry.pick_default.assert_not_called()
    assert len(generator.messages) == 1
    p = ProblemInfo.model_validate(row)
    p._math_rubric_source = {"presentation": {key: row[key] for key in ("rubric_draft", "ai_completion_provenance")}}
    assert plans["q1"].binary_policy is None
    expected = sum(b[2] for b in budgets) if budgets else (3 if "单位错误扣" in criterion else 2)
    response = dict(route_id="teacher", route_reason="分别应用原教师分项政策", score=expected,
        confidence=.9, comment="按原标准逐项判定", rationale="正确结果与解释独立计分，局部禁令不扩散。",
        claims=[dict(id="s1", quote=text, verdict="refuted", reason="部分正确",
                     item_ids=[item["id"] for item in items])],
        awards=[dict(item_id=f"i{i}", score=amount, claim_ids=["s1"], reason="按这一分项的原始标准")
                for i, (_, _, amount) in enumerate(budgets)] if items else [])
    return p, StudentAnswerInfo(q_id="q1", number="1", type=kind, content=text), plans, response, expected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["计算题", "概念题", "推理题", "其他"])
@pytest.mark.parametrize("case", CASES + POLICY_CASES, ids=["value-unit", "each-blank", "subpart-ban", "independent-explanation",
    "process-budget", "percentage", "trailing-condition", "choice-explanation", "multi-subset"] +
    [f"original-{i}" for i in range(len(POLICY_CASES))])
@pytest.mark.parametrize("path", ["ledger", "direct", "review", "without-context"])
async def test_teacher_scope_and_result_budgets_on_live_paths(kind, case, path):
    p, a, plans, response, expected = await persisted_case(kind, case)
    if path == "review":
        from backend.tests.test_math_grading import expert_for
        expert = expert_for(math_grading.MathAssessment(**response), plans["q1"], a.content)
        trace = math_grading.read_math_trace(expert)
        trace["assessment"]["needs_review"] = True
        expert.raw_output = json.dumps(trace)
        provider = ReplayProvider(dict(revisions=[dict(expert_index=0, source_quote=a.content,
            reason="按原分项复核", assessment=response)]))
        with math_plans_context(plans), math_grading.math_review_scope():
            result = (await math_grading.review_math_experts(p, a, [expert], provider))[0]
    else:
        provider = ReplayProvider({}, {}, dict(score=expected, rationale="遵循原分项", comment="按原标准计分")) if path == "direct" else ReplayProvider(response)
        with math_plans_context({} if path == "without-context" else plans):
            result = await get_skill_for_type(kind)(provider).grade(p, a)
    assert result.error_kind is None
    assert result.score == expected == sum(s.score for s in result.steps)
    assert len(provider.messages) == (3 if path == "direct" else 1)
    first_prompt = provider.messages[0][-1].content
    assert p.reference_answer in first_prompt
    assert p.criterion in first_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES[:4], ids=["value-unit", "each-blank", "subpart-ban", "independent-explanation"])
async def test_programming_preserves_scoped_rubric_and_passes_reference(case):
    criterion, stem, reference, text, budgets = case
    expected = sum(b[2] for b in budgets)
    code = '#include <iostream>\nint main(){std::cout << ' + json.dumps(text, ensure_ascii=False) + ';}'
    p = ProblemInfo(q_id="q1", number="1", type="编程题", stem="编写C++程序输出以下问题的答案及所要求的解释：" + stem,
        criterion=criterion, reference_answer=reference, max_score=4,
        test_cases=[dict(input="", expected_output=reference, source="teacher")])
    response = dict(score=expected, max_score=4, confidence=.9, comment="教师分项独立计分",
        steps=[dict(step_no=i+1, desc=quote, is_correct=score==points, score=score)
               for i, (quote, points, score) in enumerate(budgets)])
    provider = ReplayProvider(response)
    result = await get_skill_for_type("编程题")(provider).grade(p,
        StudentAnswerInfo(q_id="q1", number="1", type="编程题", content=code))
    assert not result.error_kind and result.score == expected
    assert sum(s.score for s in result.steps) == expected
    assert len(provider.messages) == 1
    assert criterion in provider.messages[0][-1].content
    assert reference in provider.messages[0][-1].content
