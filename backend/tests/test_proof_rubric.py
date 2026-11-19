"""Generation/audit/settlement contracts. Fixed replies do not measure AI quality."""
import json

import pytest

from backend.agents.ingest_agent import generate_missing_question_materials
from backend.agents.incremental_question_preparation import _rows
from backend.services import proof_rubric as proof
from backend.services.part_rubric import PartRubric
from backend.services.rubric_drafts import RubricDraft, saved_draft
from backend.services.rubric_display import rubric_display
from backend.skills.math_grading import ProofPartAssessment
from backend.skills.part_scoring import settle_parts
from backend.tests.test_question_material_format_repair import Provider


STEM = r"In the dihedral group, prove every non-rotation has order 2. Deduce that s and sr generate the group."
REFERENCE = r"Use rs=sr^{-1} to write a non-rotation as sr^k. Then (sr^k)^2=r^{-k}r^k=1. Reflections are nonidentity, so their order is 2. In particular s and sr have order 2, and s(sr)=r proves generation."
POINTS = [
    ("normal", 3, r"用群关系将非旋转元素表示为 $sr^k$，并说明这种形式覆盖所有非旋转元素"),
    ("square", 3, r"利用 $r^ks=sr^{-k}$ 证明 $(sr^k)^2=1$"),
    ("order", 2, r"结合非单位性得出元素的阶恰为 2，包括 $s$ 和 $sr$"),
    ("generate", 2, r"由 $s(sr)=r$ 说明 $s,sr$ 生成原来的两个生成元，从而生成整个群"),
]


def rubric():
    return PartRubric.model_validate(dict(id="r", label="1", stem=STEM, total_pts=10,
        parts=[dict(id="p1", total_pts=10,
                    steps=[dict(id=key, criterion=text, total_pts=points) for key, points, text in POINTS],
                    alternative=dict(deduct_per_error=1.5))]))


def audit(verdict="consistent", reason="Concrete proof obligations, no duplicate awards.", part_id="p1", *, bundled_steps=()):
    return json.dumps({"parts": [dict(part_id=part_id, verdict=verdict, reason=reason, bundled_steps=list(bundled_steps))]})


def initial(rules=None, *, reference=True):
    candidates = [dict(target_id="q1:criterion", rubric_definition={"part_rubric": (rules or rubric()).model_dump(mode="json")})]
    if reference:
        # Deliberately put the reference last: binding must not rely on model array order.
        candidates.append(dict(target_id="q1:reference_answer", text_value=REFERENCE))
    return json.dumps({"candidates": candidates})


async def generate(provider, *, reference=True, existing_reference="", stem=STEM, question_type="推理题", **kwargs):
    row = dict(number="1", type=question_type, stem=stem, max_score=10, reference_answer=existing_reference)
    targets = [dict(q_id="q1", target=key, target_id=f"q1:{key}")
               for key in (["criterion", "reference_answer"] if reference else ["criterion"])]
    return await generate_missing_question_materials(_rows([row]), targets, 1, provider,
        generation_language="zh", **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("reference,existing", [(True, ""), (False, REFERENCE), (False, "")])
async def test_one_generation_one_audit_freezes_concrete_steps(reference, existing):
    provider = Provider([initial(reference=reference), audit()])
    candidates = await generate(provider, reference=reference, existing_reference=existing)
    assert len(provider.calls) == 2
    assert "funded steps, including for proof questions" in provider.calls[0][0].content
    assert "SAME response" in provider.calls[0][0].content
    request = json.loads(provider.calls[1][-1].content)
    assert request["source"]["question"] == STEM
    assert request["source"]["reference_answer"] == (REFERENCE if reference or existing else "")
    assert [s["id"] for s in request["proposal"][0]["steps"]] == [key for key, _, _ in POINTS]
    candidate = next(c for c in candidates if c.target == "criterion")
    frozen = RubricDraft.model_validate(candidate.rubric_draft)
    part = frozen.plan.part_rubric.parts[0]
    assert [(s.id, s.total_pts, s.criterion) for s in part.steps] == POINTS
    assert not part.deductions
    assert part.alternative.deduct_per_error == 1.5
    assert all(s.follow_through_pts == s.total_pts / 2 for s in part.steps)
    display = rubric_display(frozen.plan.part_rubric, STEM)
    for _, points, criterion in POINTS:
        assert f"{criterion}（{points} 分）" in display
    assert "未取得有效数学进展" not in display and "配分单位" not in display
    # The same frozen plan survives serialization and standard preparation reads.
    completed = _rows([dict(number="1", type="推理题", stem=STEM, max_score=10,
        reference_answer=REFERENCE if reference or existing else "", criterion=candidate.text_value,
        rubric_draft=json.loads(frozen.model_dump_json()),
        ai_completion_provenance={"criterion": {"source_kind": "ai_generated"}})])["q1"]
    rebound = saved_draft(completed)
    assert rebound is not None and rebound.plan.part_rubric == frozen.plan.part_rubric


@pytest.mark.asyncio
async def test_semantic_failure_preserves_exact_repair_reason_and_good_sibling():
    issue = "square: the reference uses an invalid exponent identity"
    feedback, review = {}, {}
    provider = Provider([initial(), audit("inconsistent", issue)])
    candidates = await generate(provider, allow_partial=True, repair_feedback=feedback, review_feedback=review)
    assert [c.target for c in candidates] == ["reference_answer"]
    assert issue in feedback["q1:criterion"]
    extraction = review["q1:criterion"]
    assert extraction["status"] == "needs_review"
    assert any(field["path"] == "part_rubric.parts[0].steps[1].criterion" and field["value"] == POINTS[1][2]
               for field in extraction["fields"])
    assert len(provider.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stem,criteria", [
    (
        "证明系统的可靠性与完备性有什么区别？请各举一个例子",
        (
            "解释可靠性（可推导的结论在语义上有效），并给一个恰当的例子。",
            "解释完备性（语义上有效的结论均可推导），并给一个恰当的例子。",
        ),
    ),
    (
        r"For $x$ an element in $G$ show that $x$ and $x^{-1}$ have the same order.",
        ("Show finite orders coincide.", "Show infinite orders coincide."),
    ),
])
async def test_concept_question_does_not_infer_proof_audit_from_stem(stem, criteria):
    proposal = {
        "part_rubric": {
            "total_pts": 10, "parts": [{
                "id": "p1", "total_pts": 10, "alternative": {"deduct_per_error": 2},
                "steps": [
                    {"id": "s1", "criterion": criteria[0], "total_pts": 5},
                    {"id": "s2", "criterion": criteria[1], "total_pts": 5},
                ],
            }],
        },
    }
    response = json.dumps({"candidates": [{"target_id": "q1:criterion", "rubric_definition": proposal}]})
    provider = Provider([response, audit("inconsistent", "The proposed rubric has no proof steps.")])
    candidate, = await generate(provider, reference=False, stem=stem, question_type="概念题", allow_partial=True)
    assert len(provider.calls) == 1
    frozen = RubricDraft.model_validate(candidate.rubric_draft)
    assert [(s.criterion, s.total_pts) for s in frozen.plan.part_rubric.parts[0].steps] == [
        (criterion, 5) for criterion in criteria
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("bundled", [[], ["square"]])
async def test_uncertainty_keeps_valid_rules_with_review_advice_instead_of_missing_content(bundled):
    from backend.services import task_facade
    from types import SimpleNamespace
    provider = Provider([initial(), audit("uncertain", "Review the identity and its allocation.", bundled_steps=bundled)])
    feedback, review = {}, {}
    candidates = await generate(provider, allow_partial=True, repair_feedback=feedback, review_feedback=review)
    candidate = next(c for c in candidates if c.target == "criterion")
    frozen = RubricDraft.model_validate(candidate.rubric_draft)
    assert len(candidates) == 2 and not feedback and not review
    assert frozen.plan.part_rubric.low_confidence
    assert [(s.id, s.total_pts, s.criterion) for s in frozen.plan.part_rubric.parts[0].steps] == POINTS
    assert len(provider.calls) == 2
    question = SimpleNamespace(q_id="q1", source={"presentation": {}})
    issue, = task_facade._math_rubric_advisory(question, frozen.plan, "zh")
    assert issue["severity"] == "warning" and issue["code"] == "math_rubric_advisory"
    assert "评分依据" in issue["details"]["message"]


@pytest.mark.asyncio
async def test_retry_requests_only_failed_rubric_with_existing_reference_and_reason():
    feedback = {}
    provider = Provider([initial(), audit("inconsistent", "square: fix the identity"), initial(reference=False), audit()])
    good = await generate(provider, allow_partial=True, repair_feedback=feedback)
    assert len(good) == 1
    repaired = await generate(provider, reference=False, existing_reference=good[0].text_value,
                              allow_partial=True, repair_feedback=feedback)
    assert [c.target for c in repaired] == ["criterion"]
    request = json.loads(provider.calls[2][-1].content.removeprefix("[Generation request]\n"))
    assert request["known_problems"][0]["existing_reference_answer"] == REFERENCE
    assert request["validation_feedback"]["q1:criterion"].endswith("square: fix the identity")
    assert len(provider.calls) == 4


@pytest.mark.asyncio
async def test_audit_cannot_change_the_ledger_or_approve_a_foreign_part():
    rules = rubric()
    before = rules.model_dump_json()
    provider = Provider([audit()])
    result = await proof.prepare_proof_steps(provider, STEM, rules, reference_answer=REFERENCE, language="zh")
    assert result is rules and rules.model_dump_json() == before
    wrong = Provider([audit(part_id="other"), audit(part_id="other")])
    with pytest.raises(proof.ProofRubricUnverified):
        await proof.prepare_proof_steps(wrong, STEM, rules, reference_answer=REFERENCE, language="zh")
    assert len(wrong.calls) == 2


@pytest.mark.asyncio
async def test_reported_bundle_cannot_be_accepted_by_an_overall_consistent_verdict():
    feedback = {}
    issue = "square: computing the square and proving nonidentity are independently earnable goals"
    rules = rubric()
    part = rules.parts[0]
    bundled = part.steps[1].model_copy(update={"total_pts": 5,
        "criterion": "Prove the square equals 1 and prove the element is not the identity."})
    rules = rules.model_copy(update={"parts": (part.model_copy(update={
        "steps": (part.steps[0], bundled, part.steps[3])}),)})
    provider = Provider([initial(rules), audit(reason=issue, bundled_steps=["square"])])
    candidates = await generate(provider, allow_partial=True, repair_feedback=feedback)
    assert [c.target for c in candidates] == ["reference_answer"]
    assert issue in feedback["q1:criterion"]
    assert "without changing the part budget" in feedback["q1:criterion"]
    assert len(provider.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bundled", [["other"], ["square", "square"]])
async def test_bundle_check_cannot_reference_unknown_or_duplicate_steps(bundled):
    provider = Provider([audit(bundled_steps=bundled), audit(bundled_steps=bundled)])
    with pytest.raises(proof.ProofRubricUnverified):
        await proof.prepare_proof_steps(provider, STEM, rubric(), language="zh")
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_bundle_check_must_be_answered_but_does_not_require_per_step_audits():
    omitted = json.dumps({"parts": [dict(part_id="p1", verdict="consistent", reason="Seems valid")]})
    provider = Provider([omitted, audit()])
    assert await proof.prepare_proof_steps(provider, STEM, rubric(), language="zh")
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_generic_tiers_are_rejected_without_spending_an_audit_call():
    from backend.tests.proof_ledger_fixture import goal_ledger
    rules = goal_ledger(rubric(), [(STEM, 10)])
    provider = Provider([])
    with pytest.raises(proof.ProofRubricUnverified, match="concrete funded proof steps"):
        await proof.prepare_proof_steps(provider, STEM, rules, language="en")
    assert not provider.calls


@pytest.mark.asyncio
async def test_teacher_rules_are_not_replanned():
    provider = Provider([])
    rules = rubric().model_copy(update={"teacher_rubric": "按教师指定步骤给分"})
    assert await proof.prepare_proof_steps(provider, STEM, rules, language="zh") is None
    row = dict(stem=STEM, type="推理题")
    assert proof.use_proof_steps(row)
    for change in (dict(criterion="按步骤给分"), dict(rubric_draft={"saved": True}),
                   dict(teacher_subpart_points={"(1)": 5}), dict(type="计算题"),
                   dict(stem=STEM + " [3 marks for the witness]")):
        assert not proof.use_proof_steps({**row, **change})
    assert not provider.calls


@pytest.mark.parametrize("stem", [
    "（10分）" + STEM, STEM + "（10分）", "(10 points) " + STEM,
    STEM + " (10 points)", STEM + " [10 marks]", "（满分：10分）" + STEM,
    "本题满分 10 分。" + STEM, STEM + " 本题满分10分", "1. (10 points) " + STEM,
    "第1题（10分）" + STEM, "Total: 10 points. " + STEM,
])
def test_question_total_alone_does_not_disable_proof_audit(stem):
    assert proof.use_proof_steps(dict(type="推理题", stem=stem, max_score=10))


@pytest.mark.parametrize("policy", [
    " [3 marks for the witness]", " 第一步3分，第二步7分。", " No partial credit.",
    " (5 points)", " Award 10 points for the witness.", " (10 points for the witness)",
    " 第一步（10分）", " Step 1 (10 points)",
    " 第二步：证明该元素不是单位元（10分）",
    " Step 2: prove the element is not the identity (10 points)",
    " 第二步：证明该元素不是单位元（满分10分）",
    " 每步满分10分。", " Maximum 10 points per step.",
    " 证明该元素不是单位元满分10分。", " The witness earns a maximum 10 points.",
    " Total: 10 points for the witness.",
])
def test_step_allocations_and_credit_policies_still_bypass_replanning(policy):
    assert not proof.use_proof_steps(dict(type="推理题", stem=STEM + policy, max_score=10))


@pytest.mark.parametrize("maximum", [None, "unknown", 5])
def test_ambiguous_total_is_not_silently_stripped(maximum):
    assert not proof.use_proof_steps(dict(type="推理题", stem=STEM + " (10 points)", max_score=maximum))


@pytest.mark.asyncio
async def test_audit_uses_independent_budget_without_adding_a_planning_call():
    from backend.tools.structured_llm import llm_attempt_budget, ainvoke_with_retry, StructuredOutputInvalidError
    from langchain_core.messages import HumanMessage
    provider = Provider(["generation", "repair", audit(), "last attempt"])
    with llm_attempt_budget():
        for _ in range(2):
            await ainvoke_with_retry(provider, [HumanMessage(content="Generation")])
        await proof.prepare_proof_steps(provider, STEM, rubric(), language="zh")
        await ainvoke_with_retry(provider, [HumanMessage(content="Generation")])
        with pytest.raises(StructuredOutputInvalidError, match="attempt limit"):
            await ainvoke_with_retry(provider, [HumanMessage(content="Generation")])
    assert len(provider.calls) == 4


@pytest.mark.parametrize("missed,expected", [(set(), 10), ({"order"}, 8), ({"square"}, 7),
    ({"order", "generate"}, 6), ({"normal", "square", "order", "generate"}, 0)])
def test_frozen_step_achievements_settle_with_meaningful_partial_scores(missed, expected):
    rules = rubric()
    answer = REFERENCE
    inferences = [dict(evidence="student:1", reasoning=text, result="gap" if key in missed else "written",
                       step_ids=[key]) for key, _, text in POINTS]
    decision = dict(part_id="p1", target_quote="part:p1", student_quote="student:1", method="reference",
                    relevance="partial" if missed else "complete", progress="Assess the individual implications.", inferences=inferences)
    assessment = ProofPartAssessment.model_validate(dict(comment="Check concrete steps", part_decisions=[decision])).assessment()
    score, _ = settle_parts(rules, assessment.part_decisions, answer, STEM)
    assert score == expected


def test_one_sound_compressed_argument_can_support_multiple_distinct_steps():
    decision = dict(part_id="p1", target_quote="part:p1", student_quote="student:1", method="minor_variant",
        relevance="complete", progress="The compact proof establishes all obligations.",
        inferences=[dict(evidence="student:1", reasoning=REFERENCE, result="written", step_ids=[key for key, _, _ in POINTS])])
    assessment = ProofPartAssessment.model_validate(dict(comment="Valid compressed proof", part_decisions=[decision])).assessment()
    assert settle_parts(rubric(), assessment.part_decisions, REFERENCE, STEM)[0] == 10
