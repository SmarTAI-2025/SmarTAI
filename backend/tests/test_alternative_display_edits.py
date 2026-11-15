"""The natural-language editor must round-trip its own numeric fields without AI."""
import pytest

from backend.db import assignment_repository
from backend.services import task_facade
from backend.services.math_grading_context import problem_from_question
from backend.services.part_rubric import alternative_text
from backend.services.question_structure import build_major_question_structure
from backend.services.rubric_display import alternative_display
from backend.services.rubric_drafts import RubricDefinition, generated_rubric, saved_draft
from backend.tests.test_major_question_structure import _seed_task_with_two_part_question
from backend.tests.test_teacher_frozen_allocations import make_row, proposed


def persist(alternative):
    owner, task = "display-edit-owner", "display-edit-task"
    _seed_task_with_two_part_question(owner, task)
    old, = assignment_repository.list_questions(task, teacher_id=owner)
    row = make_row("列式3分，计算结果7分。")
    row["question_structure"] = build_major_question_structure(row, major_order=0).model_dump()
    raw = proposed()
    raw["parts"][0]["alternative"] = alternative
    _, draft = generated_rubric(row, RubricDefinition(part_rubric=raw))
    assignment_repository.update_question(task, teacher_id=owner, q_id="q1", expected_version=old.version,
        stem=row["stem"], criterion=row["criterion"], reference_answer=row["reference_answer"],
        source={"presentation": {"question_structure": row["question_structure"],
            "rubric_schema_version": 2, "rubric_draft": draft.model_dump(mode="json")}})
    return dict(owner_id=owner, task_id=task, q_id="q1")


def record(args):
    row, = assignment_repository.list_questions(args["task_id"], teacher_id=args["owner_id"])
    return row, saved_draft(problem_from_question(row))


def values(part):
    policy = part.alternative
    return {"maximum": float(policy.maximum.resolve(part.total_pts)),
            "minimum": float(policy.minimum.resolve(part.total_pts)),
            "deduct_per_error": policy.deduct_per_error, "max_deduction": policy.max_deduction}


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("policy, old_number, new_number, expected_changes", [
    ({"deduct_per_error": .5}, "0.5", "0.25", {"deduct_per_error": .25}),
    ({"maximum": {"points": 6}, "minimum": {"points": 1}, "deduct_per_error": .5, "max_deduction": 2},
     "0.5", "0.25", {"deduct_per_error": .25}),
    ({"maximum": {"points": 6}, "minimum": {"points": 1}, "deduct_per_error": .5, "max_deduction": 2},
     "6", "5", {"maximum": 5}),
    ({"maximum": {"points": 6}, "minimum": {"points": 1}, "deduct_per_error": .5, "max_deduction": 2},
     "1", "1.5", {"minimum": 1.5}),
    ({"maximum": {"points": 6}, "minimum": {"points": 1}, "deduct_per_error": .5, "max_deduction": 2},
     "2", "2.5", {"max_deduction": 2.5}),
    ({"maximum": {"points": 1.25}, "minimum": {"points": 1.25}, "deduct_per_error": .5},
     "1.25", "2", {"maximum": 2, "minimum": 2}),
])
def test_numeric_display_edit_saves_through_http_and_reloads_without_a_model(
        monkeypatch, language, policy, old_number, new_number, expected_changes):
    from fastapi.testclient import TestClient
    from backend.auth import create_token
    from backend.main import create_app

    args = persist(policy)
    before, draft = record(args)
    part = draft.plan.part_rubric.parts[0]
    original = alternative_display(part, language)
    assert original.count(old_number) == 1
    edited = original.replace(old_number, new_number)
    def forbidden(*args, **kwargs):
        pytest.fail("A numeric edit of rendered scoring fields must not require a model")
    monkeypatch.setattr(task_facade, "_compile_review_edit", forbidden)
    with TestClient(create_app(), headers={"Authorization": "Bearer " + create_token(args["owner_id"], "teacher")}) as client:
        response = client.put(f"/tasks/{args['task_id']}/problems/q1", json={"alternative_rule_edits": {part.id: edited}})
    assert response.status_code == 200, response.text
    after, updated = record(args)
    changed = updated.plan.part_rubric.parts[0]
    assert values(changed) == {**values(part), **expected_changes}
    assert changed.steps == part.steps and changed.deductions == part.deductions
    assert after.version == before.version + 1
    assert updated.plan.part_rubric.rubric_version == draft.plan.part_rubric.rubric_version + 1
    assert after.source["presentation"]["alternative_rules"][part.id]["text"] == edited


def test_existing_english_editor_text_still_supports_direct_rate_edits(monkeypatch):
    args = persist({"deduct_per_error": .5})
    _, draft = record(args)
    part = draft.plan.part_rubric.parts[0]
    text = alternative_text(part, zh=False).replace("0.5", "0.25")
    monkeypatch.setattr(task_facade, "_compile_review_edit", lambda *a, **k: pytest.fail("Unexpected model call"))
    task_facade.update_problem(**args, patch={"alternative_rule_edits": {part.id: text}})
    assert record(args)[1].plan.part_rubric.parts[0].alternative.deduct_per_error == .25


@pytest.mark.parametrize("text", [
    "Other correct methods cannot earn all 10 points; deduct 0.25 points per independent error.",
    "Other correct methods can earn all 10 points; deduct 0.25 points per independent error only if the final answer is wrong.",
])
def test_changed_english_conditions_do_not_match_the_numeric_display_reader(text):
    from backend.services.part_rubric import UninterpretedAlternative, read_alternative
    with pytest.raises(UninterpretedAlternative):
        read_alternative(text, maximum=10)


@pytest.mark.parametrize("text", [
    "Other correct methods can earn all 11 points; deduct 0.25 points per independent error.",
    "Alternative methods earn up to 6 points; deduct 0.25 points per independent error; retain at least 7 points.",
    "Other correct methods can earn all 10 points; deduct 0 points per independent error.",
    "Alternative methods receive 11 points.",
])
def test_numeric_display_reader_keeps_budget_and_positive_deduction_validation(text):
    from backend.services.part_rubric import read_alternative
    with pytest.raises(ValueError):
        read_alternative(text, maximum=10)


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("editor", ["complete", "concise"])
@pytest.mark.parametrize("case", ["hidden_cap", "fraction", "precision"])
def test_numeric_save_preserves_unedited_rules_through_reload_second_save_and_rebinding(
        monkeypatch, language, editor, case):
    from fastapi.testclient import TestClient
    from backend.auth import create_token
    from backend.main import create_app
    from backend.services.math_rubric_plan import _question_input
    from backend.services.part_rubric import Alternative, bind_parts
    from backend.skills.part_scoring import settle_parts
    from backend.tests.test_alternative_route import alternate, error

    policies = {
        "hidden_cap": {"maximum": {"points": 5}, "deduct_per_error": 1, "max_deduction": 5},
        "fraction": {"maximum": {"fraction": "1/3"}, "deduct_per_error": .5},
        "precision": {"maximum": {"points": 7.123456789012345}, "minimum": {"fraction": "1/7"},
                      "deduct_per_error": .5, "max_deduction": 3.123456789012345},
    }
    args = persist(policies[case])
    _, before = record(args)
    part = before.plan.part_rubric.parts[0]
    render = (lambda p: alternative_text(p, zh=language == "zh")) if editor == "complete" else (
        lambda p: alternative_display(p, language))
    original = render(part)
    old, new, field = ("5", "8", "maximum") if case == "hidden_cap" else ("0.5", "0.25", "deduct_per_error")
    edited = original.replace(old, new, 1)
    expected = part.alternative.model_dump()
    expected[field] = {"points": 8} if field == "maximum" else .25
    # The existing settlement engine must see the retained cumulative cap.
    def check_policy(actual):
        for name in ("maximum", "minimum", "deduct_per_error", "max_deduction"):
            assert actual.model_dump().get(name) == expected.get(name)

    monkeypatch.setattr(task_facade, "_compile_review_edit", lambda *a, **k: pytest.fail("Unexpected model call"))
    with TestClient(create_app(), headers={"Authorization": "Bearer " + create_token(args["owner_id"], "teacher")}) as client:
        response = client.put(f"/tasks/{args['task_id']}/problems/q1", json={"alternative_rule_edits": {part.id: edited}})
        assert response.status_code == 200, response.text
        row, saved = record(args)
        check_policy(saved.plan.part_rubric.parts[0].alternative)
        if case == "hidden_cap":
            answer = "\n".join(f"第{i}处独立计算错误" for i in range(1, 7))
            score, _ = settle_parts(saved.plan.part_rubric,
                [alternate(errors=[error(line=i) for i in range(1, 7)])], answer, row.stem)
            assert score == 3
        # Rebuild against a model proposal with different defaults. Persisted
        # source binding must keep all exact merged fields, not the UI summary.
        context = _question_input(problem_from_question(row))
        candidate = saved.plan.part_rubric.model_copy(update={"parts": (
            saved.plan.part_rubric.parts[0].model_copy(update={"alternative": Alternative(deduct_per_error=2)}),)})
        rebuilt = bind_parts(candidate, context)
        check_policy(rebuilt.parts[0].alternative)
        # A subsequent edit after reloading must not depend on transient state.
        current = saved.plan.part_rubric.parts[0]
        second = render(current).replace("8", "9", 1) if case == "hidden_cap" else render(current).replace("0.25", "0.125", 1)
        expected[field] = {"points": 9} if field == "maximum" else .125
        response = client.put(f"/tasks/{args['task_id']}/problems/q1", json={"alternative_rule_edits": {part.id: second}})
        assert response.status_code == 200, response.text
        check_policy(record(args)[1].plan.part_rubric.parts[0].alternative)


@pytest.mark.parametrize("language", ["zh", "en"])
def test_complete_editor_preserves_fractions_and_full_decimal_precision(language):
    from backend.services.part_rubric import _alternative_fields

    args = persist({"maximum": {"fraction": "1/3"}, "minimum": {"fraction": "1/7"},
                    "deduct_per_error": .123456789012345, "max_deduction": 1.123456789012345})
    part = record(args)[1].plan.part_rubric.parts[0]
    fields = _alternative_fields(alternative_text(part, zh=language == "zh"))
    assert fields == {name: getattr(part.alternative, name)
                      for name in ("maximum", "minimum", "deduct_per_error", "max_deduction")}


@pytest.mark.parametrize("language", ["zh", "en"])
def test_explicit_replacement_can_remove_a_previously_preserved_cap(monkeypatch, language):
    args = persist({"maximum": {"points": 5}, "deduct_per_error": 1, "max_deduction": 5})
    part = record(args)[1].plan.part_rubric.parts[0]
    monkeypatch.setattr(task_facade, "_compile_review_edit", lambda *a, **k: pytest.fail("Unexpected model call"))
    first = alternative_display(part, language).replace("5", "8")
    task_facade.update_problem(**args, patch={"alternative_rule_edits": {part.id: first}})
    assert record(args)[1].plan.part_rubric.parts[0].alternative.max_deduction == 5
    replacement = ("其他解法最高得8分，最低0分。每个独立错误扣2分。" if language == "zh" else
                   "Alternative methods: maximum 8 points, minimum 0 points. Deduct 2 points per independent error.")
    task_facade.update_problem(**args, patch={"alternative_rule_edits": {part.id: replacement}})
    row, saved = record(args)
    assert saved.plan.part_rubric.parts[0].alternative.max_deduction is None
    assert saved.plan.part_rubric.parts[0].alternative.deduct_per_error == 2
    assert "binding_text" not in row.source["presentation"]["alternative_rules"][part.id]


@pytest.mark.parametrize("language", ["zh", "en"])
def test_numeric_format_only_does_not_change_frozen_policy(monkeypatch, language):
    args = persist({"maximum": {"fraction": "1/3"}, "deduct_per_error": .5, "max_deduction": 5})
    _, before = record(args)
    part = before.plan.part_rubric.parts[0]
    text = alternative_display(part, language).replace("0.5", "0.50")
    monkeypatch.setattr(task_facade, "_compile_review_edit", lambda *a, **k: pytest.fail("Unexpected model call"))
    task_facade.update_problem(**args, patch={"alternative_rule_edits": {part.id: text}})
    assert record(args)[1].plan == before.plan
