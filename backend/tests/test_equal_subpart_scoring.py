"""A repeated teacher allocation stays itemized through compilation, grading and repair."""
import json
from unittest.mock import AsyncMock

import pytest

from backend.agents.multi_expert import run_multi_expert
from backend.models import ProblemInfo, StudentAnswerInfo
from backend.services.math_grading_context import math_plans_context, prepare_math_plans
from backend.services import math_rubric_plan as rubric
from backend.services.question_structure import build_major_question_structure
from backend.services.rubric_drafts import RubricDefinition, generated_rubric, saved_draft
from backend.skills import math_grading as grading
from backend.skills.proof import ProofSkill
from backend.tests.test_math_evidence_reliability import raw_provider
from backend.tests.test_multi_expert_robustness import _FakeRegistry


def question(**updates):
    return dict({"q_id": "q3", "number": "3", "type": "推理题", "max_score": 1,
                 "criterion": "每小问0.2分", "reference_answer": "True, true, false, true, false.",
                 "stem": "Judge the statements.\n(1) 1=1\n(2) 2=2\n(3) 3=0\n(4) 4=4\n(5) 5=0"}, **updates)


def vote(scores, *, detailed):
    claims = [dict(id=f"c{i}", source_id=f"student:{i}", item_ids=[f"sp{i}"],
                   verdict="supported" if score else "refuted", reason=f"Judgment for part {i}.")
              for i, score in enumerate(scores, 1)]
    awards = [dict(item_id=f"sp{i}", score=score, claim_ids=[f"c{i}"], reason=f"Checked part {i}.")
              for i, score in enumerate(scores, 1)]
    output = dict(claims=claims, awards=list(reversed(awards)), confidence=1)
    if detailed:
        output.update(route_id="subparts", route_reason="Teacher allocation", score=0.6,
                      comment="Four correct at 0.2 each gives 0.6.")
    return json.dumps(output)


def holistic_proposal():
    return {"routes": [{"id": "holistic", "mode": "holistic", "applies_when": "always",
                        "description": "Assess the whole question."}], "follow_through": "Original rule.",
            "numerical_policy": "Original rule.", "effective_criterion": "Use the original rubric."}


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_equal_parts_use_actual_awards_through_single_expert_workflow(monkeypatch, fallback):
    parts = [0.2, 0, 0.2, 0.2, 0.2]
    outputs = ['{}', '{}'] if fallback else []
    provider, calls = raw_provider(json.dumps(holistic_proposal()), *outputs, vote(parts, detailed=not fallback))
    registry = _FakeRegistry([provider])
    row = question()
    p = ProblemInfo(**row)
    p._math_rubric_source = {"presentation": {"question_structure":
        build_major_question_structure(row, major_order=0).model_dump()}}
    answer = StudentAnswerInfo(q_id=p.q_id, number=p.number, type=p.type,
                               content="(1) T\n(2) F\n(3) F\n(4) T\n(5) F")
    monkeypatch.setattr(grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    plans = await prepare_math_plans({p.q_id: p}, registry)
    assert len(calls) == 1
    with math_plans_context(plans):
        result = await run_multi_expert(problem=p, answer=answer, student_id="fixture",
                                        registry=registry, aggregation_method="weighted_average")
    expert, = result.expert_results
    assert result.score == expert.score == 0.8
    assert [s.score for s in result.steps] == [s.score for s in expert.steps] == parts
    assert expert.comment == "0.2 + 0 + 0.2 + 0.2 + 0.2 = 0.8"
    trace = grading.read_math_trace(expert)
    assert trace["assessment"]["score"] == 0.8
    assert [w["item_id"] for w in trace["assessment"]["awards"]] == [f"sp{i}" for i in range(1, 6)]
    assert ("fallback" in trace) == fallback
    assert len(calls) == len(outputs) + 2


@pytest.mark.asyncio
async def test_multiple_experts_keep_subparts_aligned_after_recovery(monkeypatch):
    from backend.agents.multi_expert import _weighted_average_fallback

    p = ProblemInfo(**question())
    a = StudentAnswerInfo(q_id=p.q_id, number=p.number, type=p.type, content="1\n2\n3\n4\n5")
    monkeypatch.setattr(grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    scores = [0.2, 0, 0.2, 0.2, 0.2]
    first, _ = raw_provider(vote(scores, detailed=True))
    second, _ = raw_provider('{}', '{}', vote([0, *scores[1:]], detailed=False))
    plan = rubric.fallback_math_rubric_plan(p)
    with math_plans_context({p.q_id: plan}):
        experts = [await ProofSkill(provider).grade(p, a) for provider in (first, second)]
    total = _weighted_average_fallback(p, experts)
    assert [e.score for e in experts] == [0.8, 0.6]
    assert total.score == 0.7 and [s.score for s in total.steps] == [0.1, 0, 0.2, 0.2, 0.2]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["scalar", "duplicate", "missing", "excess", "disabled_partial"])
async def test_recovery_requires_one_valid_award_per_part(monkeypatch, invalid):
    p = ProblemInfo(**question())
    a = StudentAnswerInfo(q_id=p.q_id, number=p.number, type=p.type, content="1\n2\n3\n4\n5")
    plan = rubric.fallback_math_rubric_plan(p, grading_setup={"allow_partial_credit": False})
    raw = json.loads(vote([0.2, 0, 0.2, 0.2, 0.2], detailed=False))
    if invalid == "scalar":
        raw = {"score": 0.6, "rationale": "Four correct.", "comment": "0.6 total.", "confidence": 1}
    elif invalid == "duplicate":
        raw["awards"][0]["item_id"] = raw["awards"][1]["item_id"]
    elif invalid == "missing":
        raw["awards"].pop()
    else:
        raw["awards"][0]["score"] = 0.3 if invalid == "excess" else 0.1
    provider, _ = raw_provider('{}', '{}', json.dumps(raw), json.dumps(raw))
    monkeypatch.setattr(grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    with math_plans_context({p.q_id: plan}):
        failed = await ProofSkill(provider).grade(p, a)
    if invalid == "disabled_partial":
        # The retired runtime toggle cannot alter otherwise permitted item credit.
        assert failed.error_kind is None and failed.score == 0.7
    else:
        assert failed.error_kind and failed.steps == []


@pytest.mark.parametrize("criterion", ["每小问0.2分", "每个小题占 ０．２ 分。", "Each part is worth 0.2 points.",
                                      "0.2 marks per subquestion"])
def test_equal_rate_uses_named_parts_even_for_legacy_questions(criterion):
    row = question(criterion=criterion)
    plan = rubric.fallback_math_rubric_plan(row)
    assert plan.compilation_reason == rubric.SUBPART_ALLOCATION
    assert [(x.id, x.points, x.source_quote, x.source) for x in plan.routes[0].items] == [
        (f"sp{i}", 0.2, criterion, "provided") for i in range(1, 6)]


@pytest.mark.parametrize("changes,notes", [
    ({"criterion": "每错一小问，扣0.2分"}, ""), ({"criterion": "每小问0.2分，第五问除外"}, ""),
    ({"criterion": "全部正确得1分，只要有错得0分"}, ""), ({"max_score": 2}, ""),
    ({"stem": "Decide whether these statements are true."}, ""), ({"type": "编程题"}, ""),
    ({"teacher_subpart_points": {"(1)": "0.5"}}, ""),
])
def test_equal_parts_do_not_override_other_policies_or_invent_a_part_count(changes, notes):
    assert rubric.subpart_plan(rubric._question_input(question(**changes))) is None


@pytest.mark.parametrize("case,error", [
    ("per_part", None), ("shared_budget", "ambiguous_source_allocation"),
    ("duplicate_part", "ambiguous_source_allocation"), ("missing_part", "source_allocation_changed_or_omitted"),
    ("changed_amount", "source_allocation_changed_or_omitted"),
])
def test_source_validator_binds_repeated_rate_to_each_distinct_label(case, error):
    row = question(criterion="推导共0.2分") if case == "shared_budget" else question()
    items = [rubric.MathRubricItem(id=f"goal{i}", description=f"({i}) Evaluate the assertion.",
                                   points=0.2, source="provided", source_quote=row["criterion"])
             for i in range(1, 6)]
    if case == "duplicate_part":
        items[1] = items[1].model_copy(update={"description": items[0].description})
    elif case == "missing_part":
        items.pop()
    elif case == "changed_amount":
        items[0] = items[0].model_copy(update={"points": 0.3})
    proposal = rubric._Explanation(routes=[rubric.MathRubricRoute(id="candidate", applies_when="always",
        description="Award by part", mode="points", items=tuple(reversed(items)))],
        effective_criterion=row["criterion"], follow_through="Teacher policy.", numerical_policy="Exact.")
    if error:
        with pytest.raises(ValueError, match=error):
            rubric._validate_source_rules(rubric._question_input(row), proposal)
    else:
        rubric._validated_explanation(rubric._question_input(row), proposal)


def test_old_holistic_draft_is_rebound_for_new_work_without_mutating_stored_plan():
    row = question()
    # Use the actual former route and a matching input identity, as on an existing question.
    definition = RubricDefinition(routes=[{"id": "holistic", "description": "Whole answer",
        "applies_when": "always", "mode": "holistic"}], follow_through="Original rule.", numerical_policy="Original rule.")
    original, fresh = generated_rubric(row, definition)
    assert original == row["criterion"]
    old_plan = rubric._build_plan(rubric._question_input(row),
        rubric._Explanation(**definition.model_dump(), effective_criterion=original),
        status="holistic", reason="ambiguous_source_allocation")
    stored = fresh.model_copy(update={"plan": old_plan}).model_dump(mode="json")
    row["rubric_draft"] = stored
    assert saved_draft(row).plan.plan_id == fresh.plan.plan_id
    assert stored["plan"]["routes"][0]["mode"] == "holistic"


@pytest.mark.asyncio
async def test_equal_rate_allows_multiple_steps_per_part_and_preserves_their_budgets():
    row = question()
    basic = rubric.fallback_math_rubric_plan(row)
    route = basic.routes[0]
    # The teacher specified 0.2 per part; the 0.1 + 0.1 breakdown is inferred.
    parts = [item.model_dump() for item in route.items]
    details = [dict(id=k, description=f"(2) {k}", points=0.1, source="ai", source_quote="")
               for k in ("setup", "result")]
    proposal = holistic_proposal()
    proposal["routes"] = [{**route.model_dump(), "items": [*details, parts[0], *parts[2:]]}]
    provider, calls = raw_provider(json.dumps(proposal))
    plan = await rubric.prepare_math_rubric_plan(row, provider)
    assert len(calls) == 1 and plan.compilation_reason == rubric.SUBPART_ALLOCATION
    ids = ["sp1", "setup", "result", "sp3", "sp4", "sp5"]
    assert [item.id for item in plan.routes[0].items] == ids
    definition = RubricDefinition(**{k: proposal[k] for k in ("routes", "follow_through", "numerical_policy")})
    _, prepared = generated_rubric(row, definition)
    assert [item.id for item in prepared.plan.routes[0].items] == ids
    answer = "Submitted working."
    awards = [grading.Award(item_id=k, score=0 if k == "result" else item.points,
                            claim_ids=["work"], reason=k) for k, item in zip(ids, plan.routes[0].items)]
    assessment = grading.MathAssessment(route_id=route.id, route_reason="All work", comment="0.6",
        claims=[grading.Claim(id="work", quote=answer, item_ids=ids, verdict="supported", reason="Work shown.")],
        awards=awards, score=0.6)
    score, steps = grading.score_assessment(assessment, plan, answer)
    assert score == 0.9 and [s.score for s in steps] == [0.2, 0.1, 0, 0.2, 0.2, 0.2]
    # Still sums to one globally; shifting a step into a different part must fail.
    details[0]["description"] = "(1) setup"
    with pytest.raises(ValueError, match="source_allocation_changed_or_omitted"):
        rubric._validated_explanation(rubric._question_input(row), rubric._Explanation.model_validate(proposal))


def test_teacher_subtotals_and_scored_steps_use_the_existing_points_ledger():
    row = question(stem="Solve.\n(1) First\n(2) Second", max_score=10,
                   criterion="(1) 4分，其中列式1分、计算3分。\n(2) 6分，其中方法2分、结论4分。")
    plan = rubric.fallback_math_rubric_plan(row)
    route, = plan.routes
    assert [i.points for i in route.items] == [1, 3, 2, 4]
    assert sum(i.points for i in route.items) == plan.max_score
    # A parent and its children cannot both earn credit.
    extra = route.items[0].model_copy(update={"id": "subtotal", "description": "(1) 小计", "points": 4})
    proposal = rubric._Explanation.model_validate({**holistic_proposal(),
        "routes": [route.model_copy(update={"items": (*route.items, extra)})]})
    with pytest.raises(ValueError, match="source_allocation_changed_or_omitted"):
        rubric._validate_source_rules(rubric._question_input(row), proposal)
    # Keep each explicit leaf allocation even if its parent and the question still balance.
    changed = [item.model_copy(update={"points": points}) for item, points in zip(route.items, (2, 2, 2, 4))]
    proposal = proposal.model_copy(update={"routes": (route.model_copy(update={"items": tuple(changed)}),)})
    with pytest.raises(ValueError, match="source_allocation_changed_or_omitted"):
        rubric._validate_source_rules(rubric._question_input(row), proposal)


@pytest.mark.parametrize("rule,expected", [
    ("(1) 4分；(2) 6分", [4, 6]),
    ("(1) 4分，其中列式1分、计算3分；(2) 6分", [1, 3, 6]),
    ("(1) 4分，其中计算2分、验证计算2分；(2) 6分", [2, 2, 6]),
    ("(1) 4分，其中列式1分、计算4分；(2) 6分", None),
    ("(1) 4分，其中列式1分、否则得3分；(2) 6分", None),
    ("(1) 4分，其中列式1分、计算3分；(2) 5分", None),
])
def test_parent_budget_is_applied_only_to_a_complete_consistent_rule(rule, expected):
    row = question(stem="Answer each question.\n(1) First\n(2) Second", max_score=10, criterion=rule)
    plan = rubric.subpart_plan(rubric._question_input(row))
    assert ([item.points for item in plan.routes[0].items] if plan else None) == expected
