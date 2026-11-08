import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.agents.incremental_question_preparation import _rows
from backend.services.part_rubric import AssignmentRubric, PartRubric, read_alternative
from backend.services.rubric_drafts import (
    RubricDefinition,
    generated_rubric,
    retarget_draft,
    saved_draft,
)
from backend.services.question_structure import validate_rubric_points
from backend.skills.part_scoring import PartDecision, settle_parts


def fixture(name="alternatives"):
    return json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "part_rubrics"
            / f"four-question-parts-{name}.json"
        ).read_text()
    )


def question_row(question):
    return _rows(
        [
            dict(
                number=question["label"],
                type="计算题",
                max_score=question["total_pts"],
                criterion=question.get("teacher_rubric", ""),
                stem="\n".join(
                    [
                        question["stem"],
                        *[
                            f"{p['label']} {p['stem']}"
                            for p in question["parts"]
                            if p["label"]
                        ],
                    ]
                ),
            )
        ]
    )["q1"]


@pytest.mark.parametrize("edition", ["v2", "alternatives"])
def test_approved_files_round_trip_including_singleton_parts_and_duplicate_question_labels(
    edition,
):
    source = fixture(edition)
    assert (
        AssignmentRubric.model_validate(source).model_dump(
            mode="json", exclude_unset=True
        )
        == source
    )
    for raw in source["questions"]:
        row = question_row(raw)
        criterion, draft = generated_rubric(row, RubricDefinition(part_rubric=raw))
        if raw.get("teacher_rubric"):
            assert criterion == raw["teacher_rubric"]
        else:
            row["ai_completion_provenance"] = {
                "criterion": {"source_kind": "ai_generated"}
            }
        row["criterion"] = criterion
        draft = retarget_draft(draft, {**row, "q_id": "q7"})
        row["rubric_draft"] = draft.model_dump(mode="json")
        assert saved_draft(row).plan.plan_id == draft.plan.plan_id
        assert draft.plan.part_rubric.id == "q7"
        assert [p.id for p in draft.plan.part_rubric.parts] == [
            p["id"] for p in raw["parts"]
        ]
        summary = validate_rubric_points(
            criterion,
            raw["total_pts"],
            row["question_structure"],
            rubric_draft=row["rubric_draft"],
        )
        assert summary.is_valid
        if edition == "alternatives":
            assert [
                p.alternative.model_dump() for p in draft.plan.part_rubric.parts
            ] == [p["alternative"] for p in raw["parts"]]


@pytest.mark.parametrize(
    "words,expected",
    [
        ("最多给一半分", 3),
        ("最贵给1/3的分", 2),
        ("最多给三分之一", 2),
        ("最多给50%", 3),
        ("最多给0.5分", 0.5),
        ("最多给半分", 0.5),
        ("最高得1/3分", 1 / 3),
    ],
)
def test_teacher_fraction_or_absolute_amount(words, expected):
    assert float(
        read_alternative(words, maximum=6).maximum.resolve(6)
    ) == pytest.approx(expected)


def test_reject_uncovered_part_wrong_step_total_and_broken_refs():
    raw = fixture()["questions"][0]
    for mutate in [
        lambda q: q["parts"].pop(),
        lambda q: q["parts"][0]["steps"][1].update(total_pts=3),
        lambda q: q["parts"][1].update(answer_refs=["ghost"]),
    ]:
        bad = deepcopy(raw)
        mutate(bad)
        with pytest.raises(ValidationError):
            PartRubric.model_validate(bad)


def decision(part, answer, **kwargs):
    return PartDecision.model_validate(
        dict(
            part_id=part.id,
            target_quote=part.stem or "说明检验过程",
            student_quote=answer,
            progress="核对作答对所求量的贡献",
            method="reference",
            **kwargs,
        )
    )


def test_nonblank_floor_is_applied_after_unrelated_or_many_real_deductions():
    question = PartRubric.model_validate(fixture()["questions"][3])
    (part,) = question.parts

    def grade(answer, status, errors=()):
        return settle_parts(
            question,
            [decision(part, answer, relevance=status, errors=errors)],
            answer,
            question.stem,
        )[0]

    assert grade("与本题无关的等式", "unrelated") == 6
    assert grade("", "unanswered") == 0
    answer = "独立验算并订正，" + "，".join(f"错算第{i}项" for i in range(7))
    errors = [
        dict(
            id=f"e{i}",
            rule_id="dA",
            evidence=f"错算第{i}项",
            origin_quote=f"错算第{i}项",
            reason="独立错误",
        )
        for i in range(7)
    ]
    assert grade(answer, "partial", errors) == 6
    with pytest.raises(ValueError, match="Duplicate"):
        grade(answer, "partial", [errors[0], errors[0]])


@pytest.mark.asyncio
async def test_real_generation_envelope_and_five_part_retry():
    from backend.agents.ingest_agent import generate_missing_question_materials
    from backend.agents.question_preparation_agent import (
        _validate_major_question_candidates,
    )
    from backend.tests.test_question_material_format_repair import Provider

    row = _rows(
        [
            dict(
                number="1.12",
                stem="判断真假。\n"
                + "\n".join(f"({i}) 陈述{i}。" for i in range(1, 6)),
                type="计算题",
                reference_answer="",
                max_score=10,
            )
        ]
    )["q1"]
    rules = dict(
        id="q1",
        label="1.12",
        stem="判断真假。",
        total_pts=10,
        parts=[
            dict(
                id=f"p{i}",
                label=f"第{i}小问",
                stem=f"陈述{i}。",
                total_pts=2,
                steps=[
                    dict(id=f"s{i}", criterion="正确得2分，错误得0分。", total_pts=2)
                ],
            )
            for i in range(1, 6)
        ],
    )
    target = dict(target="criterion", q_id="q1", target_id="q1:criterion")
    bad = deepcopy(rules)
    bad["parts"].pop(2)
    bad["parts"][0]["total_pts"] = bad["parts"][0]["steps"][0]["total_pts"] = 4
    provider = Provider(
        [
            json.dumps(
                {
                    "candidates": [
                        {
                            **target,
                            "rubric_definition": {
                                "part_rubric": value,
                                "expected_truth_by_part": {
                                    p["id"]: True for p in value["parts"]
                                },
                            },
                        }
                    ]
                }
            )
            for value in (bad, rules)
        ]
    )
    feedback = {}
    for i in range(2):
        result = await generate_missing_question_materials(
            {"q1": row},
            [target],
            1,
            provider,
            allow_partial=True,
            repair_feedback=feedback,
        )
        if i == 0:
            assert not result and "Missing subparts" in feedback["q1:criterion"]
        else:
            _validate_major_question_candidates("q1", [target], result, row)
    sent = provider.calls[-1]
    schema = json.loads(sent[0].content.split("Rubric definition schema:\n")[1])
    assert schema["required"] == ["part_rubric"]
    assert "Missing subparts" in sent[1].content
    assert [
        p["total_pts"] for p in result[0].rubric_draft["plan"]["part_rubric"]["parts"]
    ] == [2] * 5


def test_same_target_and_same_student_symbol_at_different_positions_have_distinct_addresses():
    raw = fixture()["questions"][2]
    rubric = PartRubric.model_validate(raw)
    answer = "真\n假\n真\n假"
    judgments = [
        PartDecision(
            part_id=p.id,
            relevance="complete",
            target_quote=f"part:{p.id}",
            student_quote=f"student:{i}",
            progress="判断本项",
            method="reference",
            steps=[
                dict(
                    id=p.steps[0].id,
                    outcome="earned",
                    evidence=f"student:{i}",
                    reason="判断正确",
                )
            ],
        )
        for i, p in enumerate(rubric.parts, 1)
    ]
    score, steps = settle_parts(rubric, judgments, answer, raw["stem"])
    assert score == 2 and [step.score for step in steps] == [0.5] * 4
    with pytest.raises(ValueError):
        settle_parts(
            rubric,
            [
                judgments[0].model_copy(update={"target_quote": "part:unknown"}),
                *judgments[1:],
            ],
            answer,
            raw["stem"],
        )


def test_mixed_scoring_correct_and_incorrect_alternatives_and_propagated_error():
    row = fixture()["questions"][0]
    row["parts"][0]["alternative"] = dict(
        maximum={"fraction": "1/2"},
        minimum={"points": 0},
        deduct_per_error=0.5,
        source_quote="另解最多一半分，错一点扣0.5分",
    )
    rubric = PartRubric.model_validate(row)
    first, second, third = rubric.parts
    answer = "另一种推导，顶点算错\n对称轴正确\n计算 f(0) 正确，漏求 f(3)"

    def alternate(errors):
        return PartDecision(
            part_id=first.id,
            relevance="partial" if errors else "complete",
            target_quote=f"part:{first.id}",
            student_quote="student:1",
            progress="独立建立顶点的求解方法",
            method="alternative",
            reference_method="配方",
            student_method="求导",
            errors=errors,
        )

    axis = decision(
        second,
        "student:2",
        relevance="complete",
        steps=[
            dict(
                id=second.steps[0].id,
                evidence="student:2",
                outcome="earned",
                reason="建立对称轴",
            )
        ],
    )
    values = decision(
        third,
        "student:3",
        relevance="partial",
        errors=[
            dict(
                id="miss",
                rule_id=third.deductions[0].id,
                target_id="a19",
                evidence="student:3",
                origin_quote="f(3)",
                reason="漏求第二个函数值",
            )
        ],
    )
    error = dict(
        id="error",
        rule_id="alternative",
        evidence="student:1",
        origin_quote="顶点算错",
        reason="独立算术错误",
    )
    for errors, expected in [([], 6), ([error], 5.5)]:
        score, steps = settle_parts(
            rubric, [values, axis, alternate(errors)], answer, row["stem"]
        )
        assert score == expected
        assert [s.part_id for s in steps] == [first.id, second.id, third.id]
        assert sum(s.score for s in steps) == score
    standard = PartDecision(
        part_id=first.id,
        relevance="partial",
        target_quote=f"part:{first.id}",
        student_quote="student:1",
        progress="沿用上一步的数值",
        method="minor_variant",
        steps=[
            dict(
                id=step.id,
                evidence=f"student:{i}",
                reason="核对原解",
                outcome=("earned", "missed", "follow_through")[i - 1],
                error_source="student:2" if i == 3 else "",
            )
            for i, step in enumerate(first.steps, 1)
        ],
    )
    score, steps = settle_parts(rubric, [standard, axis, values], answer, row["stem"])
    assert [s.score for s in steps] == [1, 0, 0.5, 2, 2] and score == 5.5


def test_circled_latex_judgments_match_without_inventing_a_derivation():
    from backend.services.true_false_answers import submitted_truth_values

    parts = PartRubric.model_validate(fixture()["questions"][2]).parts
    assert list(
        submitted_truth_values(
            r"① $\sqrt{\quad}$ ② $\times$ ③ $\times$ ④ $\sqrt{}$", parts
        ).values()
    ) == [True, False, False, True]
    assert (
        submitted_truth_values(r"① $\sqrt{9}$ ② $\times$ ③ $\times$ ④ $\sqrt{}$", parts)
        is None
    )
    assert submitted_truth_values("①真②假②假④真", parts) is None


def test_exact_question_header_and_math_markup_do_not_change_truth_values():
    from backend.services.true_false_answers import submitted_truth_values
    parts = PartRubric.model_validate(fixture()["questions"][2]).parts
    text = r"$T_{1.12}$ (1) $\checkmark$ (2) $\times$ (3) $\times$ (4) $\checkmark$"
    assert list(submitted_truth_values(text, parts, question_label="1.12").values()) == [True, False, False, True]
    assert submitted_truth_values(text, parts, question_label="1.2") is None


def test_alternative_edit_creates_a_new_frozen_revision_and_retains_teacher_source():
    from backend.models import ProblemInfo
    from backend.services.rubric_drafts import bind_draft, edit_alternatives

    raw = fixture()["questions"][0]
    row = question_row(raw)
    criterion, old = generated_rubric(row, RubricDefinition(part_rubric=raw))
    problem = ProblemInfo.model_validate({**row, "criterion": criterion})
    presentation = {"question_structure": row["question_structure"], "rubric_draft": old.model_dump(mode="json")}
    problem._math_rubric_source = {"presentation": presentation}
    immutable_snapshot = old.plan.model_dump_json()
    edit_alternatives(problem, presentation, {"pB": "另解最高给一半分，每个独立错误扣0.5分。"})
    new = saved_draft(problem)
    assert new and new.plan.plan_id != old.plan.plan_id
    assert old.plan.model_dump_json() == immutable_snapshot
    assert new.plan.original_criterion == raw["teacher_rubric"]
    assert new.plan.part_rubric.rubric_version == old.plan.part_rubric.rubric_version + 1
    second = new.plan.part_rubric.parts[1]
    assert second.alternative.maximum.resolve(second.total_pts) == 1
    assert second.alternative.deduct_per_error == .5
    assert new.plan.part_rubric.parts[0].steps == old.plan.part_rubric.parts[0].steps
    assert bind_draft(new, problem, grading_setup={"strictness": 100}) == bind_draft(new, problem, grading_setup={"strictness": 0})
    with pytest.raises(ValueError, match="Unknown"):
        edit_alternatives(problem, presentation, {"qGhost": "半分"})


def test_teacher_interpretation_preserves_explicit_numbers_and_marks_only_ordinary_gaps():
    q = deepcopy(fixture()["questions"][0])
    row = question_row(q)
    row["criterion"] = "第一问4分。第一问其他方法最高给本问的1/3，每错一处扣0.5分。"
    _, draft = generated_rubric(row, RubricDefinition(part_rubric=q))
    rule = draft.plan.part_rubric
    assert rule.parts[0].alternative.maximum.fraction == "1/3"
    assert rule.parts[0].alternative.deduct_per_error == .5
    assert rule.teacher_gaps and "(2)" in rule.teacher_gaps[0] and "(3)" in rule.teacher_gaps[0]
    assert rule.teacher_rubric == row["criterion"]
    q["parts"][0]["total_pts"] = 3
    q["parts"][0]["steps"][1]["total_pts"] = 1
    q["parts"][1]["total_pts"] = q["parts"][1]["steps"][0]["total_pts"] = 3
    with pytest.raises(ValueError, match="Teacher assigned"):
        generated_rubric(row, RubricDefinition(part_rubric=q))


def test_method_and_relevance_are_scoped_per_student_and_question_in_the_review_queue():
    from backend.agents.multi_expert import _apply_math_review_advisory
    from backend.models import Correction, ExpertResult
    from backend.skills.math_grading import TRACE_VERSION

    for method, relevance, expected in [
        ("alternative","complete",["math_alternative_method"]),
        ("minor_variant","complete",[]),
        ("undetermined","uncertain",["math_verification_conflict"]),
    ]:
        decision = dict(part_id="p1", target_quote="part:p1", student_quote="student:1",
                        progress="Reasoned answer", relevance=relevance, method=method)
        expert = ExpertResult(provider="fixture", score=2, max_score=2, confidence=1, comment="Checked",
            raw_output=json.dumps({"version": TRACE_VERSION, "plan_id": "frozen", "assessment":{
                "route_id":"parts","route_reason":"frozen","comment":"Checked","part_decisions":[decision]}}))
        correction = Correction(q_id="q1", number="1", type="计算题", max_score=2, score=2,
                                confidence=1, comment="Checked", steps=[], expert_results=[expert])
        assert _apply_math_review_advisory(correction).review_reasons == expected


@pytest.mark.asyncio
async def test_decision_only_generation_returns_key_reference_and_exact_budgets_in_one_call():
    from backend.agents.ingest_agent import generate_missing_question_materials
    from backend.tests.test_question_material_format_repair import Provider

    row = _rows([dict(number="1.12", type="概念题", max_score=10,
        stem="判断下列陈述正确与否。\n" + "\n".join(f"({i}) 命题{i}。" for i in range(1,6)))])["q1"]
    targets = [dict(target=name, q_id="q1", target_id=f"q1:{name}")
               for name in ("reference_answer","criterion")]
    flags = [True,True,False,True,False]
    provider = Provider([json.dumps({"judgments": [dict(label=f"({i})", correct=v, explanation=f"推导 {i}。")
                                                    for i,v in reversed(list(enumerate(flags,1)))]})])
    ref, rubric = await generate_missing_question_materials({"q1":row}, targets, 1, provider)
    assert [p["total_pts"] for p in rubric.rubric_draft["plan"]["part_rubric"]["parts"]] == [2]*5
    assert list(rubric.rubric_draft["plan"]["expected_truth_by_part"].values()) == flags
    assert ref.text_value.startswith("(1) 正确")
    existing = {**row, "reference_answer": ref.text_value}
    repeat, = await generate_missing_question_materials({"q1":existing}, [targets[1]], 1, provider)
    assert repeat.rubric_draft["plan"]["expected_truth_by_part"] == rubric.rubric_draft["plan"]["expected_truth_by_part"]
    assert len(provider.calls) == 1


def test_located_error_observation_on_zero_award_cannot_be_deducted_or_allow_credit():
    rules = PartRubric.model_validate(dict(id="q",label="3",stem="Give the count.",total_pts=2,
        parts=[dict(id="p",total_pts=2,steps=[dict(id="s",criterion="Correct count.",total_pts=2)])]))
    decision = PartDecision(part_id="p",target_quote="part:p",student_quote="student:1",progress="Incorrect count",
        method="reference",relevance="incorrect",
        errors=[dict(id="reason",rule_id="incorrect",evidence="student:1",origin_quote="student:1",reason="Boundary omitted")])
    score, lines = settle_parts(rules,[decision],"n-1","Give the count.")
    assert score == 0 and sum(row.score for row in lines) == 0
    earned = {**decision.model_dump(), "steps":[dict(id="s",outcome="earned",evidence="student:1",reason="Inconsistent")]}
    with pytest.raises(ValueError,match="zero credit"):
        settle_parts(rules,[PartDecision(**earned)],"n-1","Give the count.")
