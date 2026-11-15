import pytest
from backend.services.part_rubric import PartRubric
from backend.services.rubric_display import rubric_display, alternative_display, simple_decisions, accept_display_plan, show_alternative


def decisions():
    return PartRubric(id="q1", label="1.12", stem="判断以下断言正确与否。", total_pts=10,
        parts=[dict(id=f"p{i}", label=f"({i})", stem=f"陈述{i}", total_pts=2,
            steps=[dict(id=f"s{i}", criterion="判断与本问标答一致。", total_pts=2, follow_through_pts=1.6)],
            alternative=dict(deduct_per_error=2)) for i in range(1, 6)])


def test_five_independent_judgments_have_one_clear_rule_without_changing_scoring_json():
    rules = decisions()
    original = rules.model_dump(mode="json")
    key = {part.id: True for part in rules.parts}
    assert rubric_display(rules, rules.stem, expected_truth=key) == "共 5 小问，每问 2 分。回答正确得 2 分，错误或未答得 0 分。"
    assert simple_decisions(rules, rules.stem, expected_truth=key)
    assert not any(show_alternative(rules, part, expected_truth=key) for part in rules.parts)
    assert "follow-through" not in rubric_display(rules, rules.stem, "en")
    assert rules.model_dump(mode="json") == original


def test_reasoning_requirements_and_teacher_rules_are_not_hidden_by_binary_summary():
    rules = decisions()
    assert not simple_decisions(rules, rules.stem + "请说明理由。")
    assert not simple_decisions(rules.model_copy(update={"teacher_rubric": "答对得2分。"}), rules.stem)
    rendered = rubric_display(rules, rules.stem + "请说明理由。")
    assert rendered.count("(1)") == 1
    assert "配分单位" not in rendered and "沿用错值" not in rendered and "保底" not in rendered
    assert "\n\n- (2)" in rendered


@pytest.mark.parametrize("language", ["zh", "en"])
def test_alternative_summary_uses_points_and_retains_special_limits(language):
    part = decisions().parts[0]
    assert "1/1" not in alternative_display(part, language)
    assert "2" in alternative_display(part, language)
    # Revalidate nested policy so the display always reads a typed frozen rule.
    from backend.services.part_rubric import Alternative
    policy = Alternative.model_validate({**part.alternative.model_dump(), "maximum": {"points": 1},
        "minimum": {"points": .25}, "max_deduction": .5})
    rendered = alternative_display(part.model_copy(update={"alternative": policy}), language)
    for amount in ("1", "0.25", "0.5"):
        assert amount in rendered


def test_unexplained_fractional_alternative_cap_is_reviewed_without_changing_rules():
    from backend.services.part_rubric import Alternative
    from backend.services.rubric_display import alternative_cap_issue
    from backend.services.question_preparation_diagnostics import preparation_review, public_preparation_review
    rules = decisions()
    part = rules.parts[0].model_copy(update={"alternative": Alternative(maximum={"fraction": "1/8"}, deduct_per_error=.31)})
    rules = rules.model_copy(update={"parts": (part, *rules.parts[1:])})
    before = rules.model_dump_json()
    issues = alternative_cap_issue("q1", rules)
    assert issues[0]["severity"] == "warning"
    assert issues[0]["details"]["alternative_caps"] == [{"label": "(1)", "maximum": .25, "total": 2}]
    projected = public_preparation_review(preparation_review({"q1": {"number": "1", "preparation_issues": issues}}))
    assert projected[0]["issues"][0]["details"] == issues[0]["details"]
    assert rules.model_dump_json() == before
    explicit = part.model_copy(update={"alternative": part.alternative.model_copy(update={
        "source_quote": "本问其他解法最多四分之一分",
        "field_sources": {"maximum": "本问其他解法最多四分之一分"}})})
    assert not alternative_cap_issue("q1", rules.model_copy(update={"parts": (explicit,)}))


def test_cap_notice_clears_after_confirmation_or_correcting_the_actual_policy():
    from types import SimpleNamespace
    from backend.services.part_rubric import Alternative
    from backend.services.task_facade import _serialize_problem
    rules = decisions()
    limited = rules.model_copy(update={"parts": (rules.parts[0].model_copy(update={
        "alternative": Alternative(maximum={"fraction": "1/8"}, deduct_per_error=.2)}), *rules.parts[1:])})
    plan = SimpleNamespace(part_rubric=limited, expected_truth_by_part={}, advisories=[])
    presentation = {"review_status": "needs_review"}
    question = SimpleNamespace(q_id="q1", number="1", type="concept", stem=rules.stem, criterion="判断正确得分。",
        max_score=10, reference_answer="", order_index=0, test_cases=None, source={"presentation": presentation})
    issue, = _serialize_problem(question, math_plan=plan)["preparation_issues"]
    presentation["review_status"] = "confirmed"
    assert _serialize_problem(question, math_plan=plan)["preparation_issues"] == []
    presentation.update(review_status="edited", preparation_issues=[issue])
    plan.part_rubric = rules
    assert _serialize_problem(question, math_plan=plan)["preparation_issues"] == []


@pytest.mark.parametrize("teacher, expected_warning", [
    ("其他解法每错扣0.5分。", True),
    ("Alternative methods: deduct 0.5 points per independent error.", True),
    ("其他解法最低0分，每错扣0.5分，累计最多扣1分。", True),
    ("其他解法最高得1.25分，每错扣0.5分。", False),
    ("Alternative methods earn up to 1.25 points; deduct 0.5 points per independent error.", False),
])
def test_cap_warning_binds_to_the_teacher_maximum_not_other_alternative_fields(teacher, expected_warning):
    from types import SimpleNamespace
    from backend.tests.test_teacher_frozen_allocations import make_row, proposed
    from backend.services.rubric_drafts import generated_rubric, RubricDefinition
    from backend.services.task_facade import _serialize_problem
    row, raw = make_row(teacher), proposed()
    raw["parts"][0]["alternative"]["maximum"] = {"fraction": "1/8"}
    _, draft = generated_rubric(row, RubricDefinition(part_rubric=raw))
    before = draft.plan.model_dump_json()
    question = SimpleNamespace(**row, order_index=0, test_cases=None,
        source={"presentation": {"review_status": "needs_review"}})
    issues = _serialize_problem(question, math_plan=draft.plan)["preparation_issues"]
    caps = [issue for issue in issues if issue.get("details", {}).get("reason") == "alternative_cap_review"]
    assert bool(caps) == expected_warning
    if expected_warning:
        assert caps[0]["severity"] == "warning"
        assert caps[0]["details"]["alternative_caps"] == [{"label": "", "maximum": 1.25, "total": 10}]
    assert draft.plan.model_dump_json() == before


def test_optional_model_display_plan_is_separate_from_frozen_scoring_and_invalidates_on_edit():
    from backend.services.rubric_drafts import RubricDefinition, generated_rubric
    row = dict(q_id="q1", number="1", type="计算题", stem="只写最终答案：计算 1+1。",
               max_score=2, criterion="", reference_answer="2")
    raw = dict(id="q1", label="1", stem=row["stem"], total_pts=2,
               parts=[dict(id="p1", total_pts=2, steps=[dict(id="s1", criterion="结果正确", total_pts=2)],
                           alternative=dict(deduct_per_error=2))])
    proposal = dict(parts=[dict(part_id="p1", response="answer_only", alternative_relevant=False,
                               evidence=["只写最终答案"])])
    text, original = generated_rubric(row, RubricDefinition(part_rubric=raw))
    second_text, displayed = generated_rubric(row, RubricDefinition(part_rubric=raw, display_plan=proposal))
    assert text == second_text and original.plan == displayed.plan
    rules = displayed.plan.part_rubric
    assert displayed.display_plan and not show_alternative(rules, rules.parts[0], displayed.display_plan)
    assert show_alternative(rules.model_copy(update={"stem": "写出详细推导过程"}), rules.parts[0], displayed.display_plan)
    malformed = generated_rubric(row, RubricDefinition(part_rubric=raw, display_plan="invalid display only"))[1]
    assert malformed.plan == original.plan and malformed.display_plan is None


def test_model_cannot_hide_teacher_constraints_or_compact_partial_credit_items():
    rules = decisions()
    proposal = dict(parts=[dict(part_id=part.id, response="answer_only", alternative_relevant=False,
                               evidence=[rules.stem]) for part in rules.parts])
    view = accept_display_plan(proposal, rules)
    assert view and simple_decisions(rules, display_plan=view)
    # Several independently credited outputs are not an all-or-nothing item.
    from backend.services.part_rubric import PointStep
    first = rules.parts[0].model_copy(update={"steps": (
        PointStep(id="a", criterion="第一个空正确", total_pts=1),
        PointStep(id="b", criterion="第二个空正确", total_pts=1))})
    updated = rules.model_copy(update={"parts": (first, *rules.parts[1:])})
    new_view = accept_display_plan(proposal, updated)
    assert not simple_decisions(updated, display_plan=new_view)
    rendered = rubric_display(updated, updated.stem, display_plan=new_view)
    assert "第一个空正确（1 分）" in rendered and "第二个空正确（1 分）" in rendered
    restricted = rules.parts[0].model_copy(update={"alternative": rules.parts[0].alternative.model_copy(update={"source_quote": "本问其他解法最多1分。"})})
    special = rules.model_copy(update={"parts": (restricted, *rules.parts[1:])})
    assert show_alternative(special, restricted, accept_display_plan(proposal, special))
    assert accept_display_plan({"parts": [dict(part_id="unknown", response="answer_only", evidence=[rules.stem])]}, rules) is None
    for item in proposal["parts"]:
        item["evidence"] = ["不存在的题干要求"]
    assert accept_display_plan(proposal, rules) is None


def test_explicit_teacher_follow_through_survives_display_without_default_parameters():
    from backend.tests.test_teacher_frozen_allocations import make_row, proposed
    from backend.services.rubric_drafts import generated_rubric, RubricDefinition
    raw = proposed(names=("列式", "计算结果"), points=(2, 8))
    raw["parts"][0]["steps"][1]["follow_through_keep"] = .5
    row = make_row("列式2分，计算结果8分。计算结果沿用错值保留一半分")
    rules = generated_rubric(row, RubricDefinition(part_rubric=raw))[1].plan.part_rubric
    original = rules.model_dump()
    text = rubric_display(rules, rules.stem)
    assert text.count("本步方法正确时得 4 分") == 1
    assert "配分单位" not in text and "教师补充要求" not in text
    assert rules.model_dump() == original


def test_generation_schema_requires_the_same_alternative_fields_as_validation():
    from backend.services.rubric_drafts import _WrittenProposal
    schema = _WrittenProposal.model_json_schema()["$defs"]
    assert "alternative" in schema["RubricPart"]["required"]
    assert "deduct_per_error" in schema["Alternative"]["required"]


def test_valid_frozen_rubric_without_reference_is_still_allowed_to_proceed():
    from types import SimpleNamespace
    from backend.tests.test_teacher_frozen_allocations import make_row, proposed
    from backend.services.rubric_drafts import generated_rubric, RubricDefinition, saved_draft
    from backend.services.math_grading_context import problem_from_question
    from backend.services.task_facade import _question_review_blockers, _serialize_problem
    row = {**make_row("列式3分，计算结果7分"), "reference_answer": ""}
    _, draft = generated_rubric(row, RubricDefinition(part_rubric=proposed()))
    question = SimpleNamespace(**row, id="optional-reference", version=1, order_index=0, test_cases=None,
        source={"presentation": {"rubric_draft": draft.model_dump(mode="json"), "review_status": "needs_review"}})
    assert saved_draft(problem_from_question(question)) is not None
    assert _question_review_blockers([question]) == []
    assert _serialize_problem(question)["reference_answer"] == ""
