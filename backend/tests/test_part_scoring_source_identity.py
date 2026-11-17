"""Source spelling must not duplicate losses or bypass arithmetic verification."""
from unittest.mock import AsyncMock

import pytest

from backend.agents.incremental_question_preparation import _rows
from backend.models import ProblemInfo, StudentAnswerInfo
from backend.services.math_grading_context import math_plans_context
from backend.services.part_rubric import PartRubric
from backend.services.rubric_drafts import RubricDefinition, bind_draft, generated_rubric, retarget_draft
from backend.skills import math_grading as grading
from backend.skills.calculation import CalculationSkill
from backend.skills.concept import ConceptSkill
from backend.skills.part_scoring import PartDecision, settle_parts
from backend.skills.proof import ProofSkill
from backend.tests.test_math_evidence_reliability import raw_provider


def rubric(mode="reference"):
    part = dict(id="p", total_pts=10, alternative=dict(deduct_per_error=2))
    if mode == "alternative":
        part["steps"] = [dict(id="sum", criterion="Compute the sum.", total_pts=10)]
    else:
        part["deductions"] = [dict(id="error", criterion="Independent arithmetic error.", deduct_pts=2)]
        if mode == "mixed":
            part["steps"] = [dict(id="setup", criterion="Set up the addition.", total_pts=10)]
    return PartRubric.model_validate(dict(id="q1", label="1", stem="Compute the sum.",
                                         total_pts=10, parts=[part]))


def decision(origins, mode="reference", *, evidence="student:1"):
    alternative = mode == "alternative"
    return PartDecision(part_id="p", target_quote="part:p", student_quote=evidence,
        relevance="partial", method="alternative" if alternative else "reference",
        reference_method="Direct addition" if alternative else "",
        student_method="Count successive units" if alternative else "",
        progress="The setup is valid, but the submitted sum is wrong.",
        steps=([dict(id="setup", outcome="earned", evidence=evidence,
                     reason="Both addends are included.")] if mode == "mixed" else []),
        errors=[dict(id=f"e{i}", rule_id="alternative" if alternative else "error",
                     evidence=evidence, origin_quote=origin,
                     reason="The submitted addition does not equal the stated result.")
                for i, origin in enumerate(origins)])


@pytest.mark.parametrize("mode", ["reference", "alternative", "mixed"])
@pytest.mark.parametrize("padding", [("", "  "), (" \t", ""), ("\u3000", "\t\u00a0"), ("", "")])
@pytest.mark.parametrize("reverse", [False, True])
def test_line_id_and_same_trimmed_statement_cannot_charge_twice(mode, padding, reverse):
    rules = rubric(mode)
    answer = "Earlier setup.\n" + padding[0] + "2+2=5" + padding[1] + "\r\nLater work."
    origins = ["student:2", "2+2=5"]
    if reverse:
        origins.reverse()
    for origin in origins:
        assert settle_parts(rules, [decision([origin], mode, evidence="student:2")], answer, rules.stem)[0] == 8
    with pytest.raises(ValueError, match="Duplicate error origin"):
        settle_parts(rules, [decision(origins, mode, evidence="student:2")], answer, rules.stem)


@pytest.mark.parametrize("mode", ["reference", "alternative", "mixed"])
def test_distinct_expressions_on_one_line_and_distinct_occurrences_still_charge(mode):
    rules = rubric(mode)
    for answer, origins, evidence in [
        ("  2+2=5; 3+3=7  ", ["2+2=5", "3+3=7"], "student:1"),
        ("  2+2=5  \n\t2+2=5\t", ["student:1", "student:2"], "student:1, student:2"),
    ]:
        assert settle_parts(rules, [decision(origins, mode, evidence=evidence)], answer, rules.stem)[0] == 6


def test_whitespace_does_not_duplicate_an_omitted_question_target():
    rules = rubric()
    question = "Compute the sum.\n  Justify the result.\t"
    for origin in ("question:2", "Justify the result."):
        assert settle_parts(rules, [decision([origin])], "The sum is 4.", question)[0] == 8
    with pytest.raises(ValueError, match="Duplicate error origin"):
        settle_parts(rules, [decision(["question:2", "Justify the result."])], "The sum is 4.", question)


@pytest.mark.parametrize("missed,origin", [("student:1", "2+2=5"), ("2+2=5", "student:1")])
def test_follow_through_uses_the_same_error_identity(missed, origin):
    rules = PartRubric.model_validate(dict(id="q1", label="1", stem="Sum, then double it.", total_pts=10,
        parts=[dict(id="p", total_pts=10, steps=[
            dict(id="sum", criterion="Compute the sum.", total_pts=2),
            dict(id="double", criterion="Double the sum.", total_pts=8, follow_through_pts=4)])]))
    answer = " \t2+2=5\t \n2*5=10"
    reading = PartDecision(part_id="p", target_quote="part:p", student_quote="student:1, student:2",
        relevance="partial", method="reference", progress="The wrong sum is doubled correctly.", steps=[
            dict(id="sum", outcome="missed", evidence=missed, reason="Two plus two is four."),
            dict(id="double", outcome="follow_through", evidence="student:2", error_source=origin,
                 reason="The stated sum is correctly doubled.")])
    assert settle_parts(rules, [reading], answer, rules.stem)[0] == 4
    invalid = reading.model_dump()
    invalid["steps"][1]["error_source"] = "student:2"
    with pytest.raises(ValueError, match="distinct earlier error"):
        settle_parts(rules, [PartDecision.model_validate(invalid)], answer, rules.stem)


def grading_case(kind="计算题"):
    rules = rubric()
    row = _rows([dict(number="1", type=kind, stem=rules.stem, max_score=10,
                      criterion="每个独立计算错误扣2分。", reference_answer="2+2=4")])["q1"]
    row["criterion"], draft = generated_rubric(row, RubricDefinition(part_rubric=rules))
    problem = ProblemInfo(**row)
    problem._math_rubric_source = {"presentation": {"question_structure": row["question_structure"]}}
    return problem, StudentAnswerInfo(q_id=problem.q_id, number="1", type=kind, content="2+2=5"), bind_draft(retarget_draft(draft, row), problem)


@pytest.fixture(autouse=True)
def no_kb(monkeypatch):
    monkeypatch.setattr(grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))


@pytest.mark.asyncio
async def test_duplicate_error_is_repaired_before_returning_a_grade():
    problem, answer, plan = grading_case()
    answer.content = "2+2=5  "
    initial = grading.FrozenPartAssessment(part_decisions=[decision(["student:1", "2+2=5"])],
                                           comment="The addition is wrong.", confidence=.9)
    fixed = initial.model_copy(update={"part_decisions": [decision(["student:1"])]})
    provider, calls = raw_provider(initial, fixed)
    with math_plans_context({problem.q_id: plan}):
        result = await CalculationSkill(provider).grade(problem, answer)
    trace = grading.read_math_trace(result)
    assert result.error_kind is None and result.score == 8 and "direct" not in trace
    assert len(calls) == 2
    assert "Duplicate error origin" in calls[1][-1].content
    assert len(trace["assessment"]["part_decisions"][0]["errors"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("skill,kind", [(CalculationSkill, "计算题"), (ProofSkill, "推理题"), (ConceptSkill, "概念题")])
@pytest.mark.parametrize("use_id", [False, True])
@pytest.mark.parametrize("repair", [False, True])
async def test_validated_source_reaches_real_arithmetic_check_and_feedback(skill, kind, use_id, repair):
    problem, answer, plan = grading_case(kind)
    complete = decision([]).model_copy(update={"relevance": "complete", "progress": "The sum is stated."})
    initial = grading.FrozenPartAssessment(part_decisions=[complete], comment="The sum is stated.", confidence=.9,
        claims=[grading.Claim(id="sum", source_id="student:1" if use_id else "", quote="" if use_id else answer.content,
                              verdict="supported", reason="The stated addition is correct.")])
    reviewed = initial.model_copy(deep=True)
    reviewed.part_decisions = [decision(["student:1"])]
    reviewed.claims[0].verdict = "refuted"
    reviewed.claims[0].reason = "Two plus two is four, not five."
    reviewed.evidence_judgments = [grading.EvidenceJudgment(evidence_id="v1", applicability="applies",
        reason="The tool checked this exact written addition with no unstated assumptions.")]
    outputs = [initial, reviewed]
    if repair:
        invalid = initial.model_copy(deep=True)
        invalid.part_decisions[0] = complete.model_copy(update={"target_quote": "part:missing"})
        outputs.insert(0, invalid)
    provider, calls = raw_provider(*outputs)
    with math_plans_context({problem.q_id: plan}):
        result = await skill(provider).grade(problem, answer)
    trace = grading.read_math_trace(result)
    assert result.error_kind is None and "direct" not in trace
    assert len(calls) == 2 + int(repair)
    assert result.score == 8
    assert trace["evidence"][0]["status"] == "refuted"
    assert trace["evidence"][0]["source_binding"] == "explicit_syntax"
    assert "APPLY SCOPED EVIDENCE" in calls[-1][-1].content
    assert trace["assessment"]["claims"][0]["quote"] == answer.content
    assert trace["review_pending"] is False
