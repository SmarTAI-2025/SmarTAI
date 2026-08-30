from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.domain.errors import ValidationError
from backend.services.question_structure import (
    QuestionRubricValidationError,
    annotate_major_question_structures,
    build_major_question_structure,
    summarize_rubric_points,
    validate_rubric_points,
)
from backend.models import QuestionScorePolicy
from backend.skills.question_score import (
    InterpretedQuestionScore,
    InterpretedQuestionScorePlan,
    resolve_question_score_policy,
)


def _problem(q_id: str, number: str, stem: str, criterion: str = "") -> dict:
    return {
        "q_id": q_id,
        "number": number,
        "type": "计算题",
        "stem": stem,
        "criterion": criterion,
        "max_score": 10,
        "review_status": "needs_review",
    }


def test_dot_numbers_remain_independent_major_questions():
    original = {
        "q1": _problem("q1", "1.1", "求群 G 的中心。"),
        "q2": _problem("q2", "1.2", "证明 H 是正规子群。"),
    }

    result = annotate_major_question_structures(original)

    assert list(result) == ["q1", "q2"]
    assert [row["number"] for row in result.values()] == ["1.1", "1.2"]
    assert all(
        row["question_structure"]["scoring_unit"] == "major_question"
        for row in result.values()
    )
    assert original == {
        "q1": _problem("q1", "1.1", "求群 G 的中心。"),
        "q2": _problem("q2", "1.2", "证明 H 是正规子群。"),
    }


def test_subparts_inside_stem_are_metadata_not_scored_rows():
    result = annotate_major_question_structures({
        "q1": _problem(
            "q1",
            "1",
            "已知函数 f。(a) 求导数。(b) 证明极值唯一。",
        ),
        "q2": _problem("q2", "2", "计算积分。"),
    })

    assert list(result) == ["q1", "q2"]
    structure = result["q1"]["question_structure"]
    assert structure["major_number"] == "1"
    assert [part["label"] for part in structure["subparts"]] == ["(a)", "(b)"]
    assert [part["subpart_id"] for part in structure["subparts"]] == ["sp1", "sp2"]
    assert result["q2"]["question_structure"]["subparts"] == []


def test_model_emitted_composite_subpart_rows_collapse_before_scoring():
    result = annotate_major_question_structures({
        "q1": _problem("q1", "1(a)", "求导数。"),
        "q2": _problem("q2", "1(b)", "证明极值唯一。"),
        "q3": _problem("q3", "2", "计算积分。"),
    })

    assert list(result) == ["q1", "q2"]
    assert [row["number"] for row in result.values()] == ["1", "2"]
    assert "(a)" in result["q1"]["stem"]
    assert "(b)" in result["q1"]["stem"]
    assert [
        part["label"] for part in result["q1"]["question_structure"]["subparts"]
    ] == ["(a)", "(b)"]


def test_parenthesized_top_level_numbers_remain_distinct_without_parent_evidence():
    result = annotate_major_question_structures({
        "q1": _problem("q1", "(1)", "求导数。"),
        "q2": _problem("q2", "(2)", "证明极值唯一。"),
        "q3": _problem("q3", "(3)", "计算积分。"),
    })

    assert list(result) == ["q1", "q2", "q3"]
    assert [row["number"] for row in result.values()] == ["(1)", "(2)", "(3)"]


def test_leading_letter_subparts_before_question_two_recover_question_one():
    result = annotate_major_question_structures({
        "q1": _problem("q1", "(a)", "求导数。"),
        "q2": _problem("q2", "(b)", "证明极值唯一。"),
        "q3": _problem("q3", "2", "计算积分。"),
    })

    assert list(result) == ["q1", "q2"]
    assert [row["number"] for row in result.values()] == ["1", "2"]
    assert result["q1"]["question_structure"]["review_status"] == "needs_review"
    assert [
        part["label"] for part in result["q1"]["question_structure"]["subparts"]
    ] == ["(a)", "(b)"]


def test_unanchored_second_subpart_is_not_silently_attached():
    result = annotate_major_question_structures({
        "q1": _problem("q1", "1", "第一题。"),
        "q2": _problem("q2", "(2)", "可能是独立第二题。"),
    })

    assert list(result) == ["q1", "q2"]
    assert [row["number"] for row in result.values()] == ["1", "(2)"]


@pytest.mark.asyncio
async def test_teacher_10_and_6_scores_bind_once_to_two_major_questions(monkeypatch):
    normalized = annotate_major_question_structures({
        "q1": _problem("q1", "1(a)", "求导数。"),
        "q2": _problem("q2", "1(b)", "证明极值唯一。"),
        "q3": _problem("q3", "2", "计算积分。"),
    })

    async def structured_call(*_args, **_kwargs):
        return (
            InterpretedQuestionScorePlan(scores=[
                InterpretedQuestionScore(q_id="q1", max_score=10),
                InterpretedQuestionScore(q_id="q2", max_score=6),
            ]),
            SimpleNamespace(content="{}"),
        )

    monkeypatch.setattr(
        "backend.skills.question_score.structured_llm_call", structured_call
    )
    resolved = await resolve_question_score_policy(
        normalized,
        QuestionScorePolicy(
            mode="per_question",
            per_question_text="第一大题 10 分，第二大题 6 分",
        ),
        MagicMock(),
    )

    assert list(resolved) == ["q1", "q2"]
    assert [row.max_score for row in resolved.values()] == [10, 6]


def _two_part_structure():
    return build_major_question_structure(
        {
            "number": "1",
            "stem": "第一大题。(a) 完成计算。(b) 给出证明。",
        },
        major_order=0,
    )


def test_explicit_subpart_points_must_exactly_equal_major_maximum():
    structure = _two_part_structure()

    valid = validate_rubric_points("(a) 4 分\n(b) 6 分", 10, structure)
    assert valid.is_valid is True
    assert valid.total_points == "10"

    with pytest.raises(QuestionRubricValidationError) as exc:
        validate_rubric_points("(a) 5 分\n(b) 6 分", 10, structure)
    assert exc.value.summary.total_points == "11"
    assert exc.value.summary.major_max_score == "10"
    assert exc.value.summary.issue_code == "rubric_subpart_points_mismatch"


def test_partial_or_duplicate_explicit_allocations_are_rejected():
    structure = _two_part_structure()

    partial = summarize_rubric_points("(a) 10 分", 10, structure)
    duplicate = summarize_rubric_points("(a) 4 分；(a) 4 分；(b) 6 分", 10, structure)

    assert partial.issue_code == "rubric_subpart_points_incomplete"
    assert duplicate.issue_code == "rubric_subpart_points_duplicate"


@pytest.mark.parametrize(
    "criterion",
    [
        "Part (a): 4 points\nPart (b): 6 points",
        "小问(a)：4分\n小问(b)：6分",
        "(a) 方法正确，4分\n(b) 证明完整，6分",
    ],
)
def test_common_english_and_chinese_allocation_formats_parse_once(criterion):
    summary = validate_rubric_points(criterion, 10, _two_part_structure())
    assert summary.is_valid is True
    assert summary.total_points == "10"
    assert [item.points for item in summary.items] == ["4", "6"]


def test_components_sum_within_subpart_and_trailing_total_is_not_double_counted():
    summary = validate_rubric_points(
        "(a) 方法 2 分、结果 2 分；(b) 证明 6 分；总分 10 分",
        10,
        _two_part_structure(),
    )

    assert summary.is_valid is True
    assert [item.points for item in summary.items] == ["4", "6"]
    assert summary.total_points == "10"


@pytest.mark.parametrize(
    "criterion",
    [
        "步骤一 40%；步骤二 60%。",
        "答案唯一: 答对满分, 答错 0 分",
        "结合方法与结论综合评分。",
    ],
)
def test_percentage_and_free_text_rubrics_remain_compatible(criterion):
    summary = validate_rubric_points(criterion, 10, _two_part_structure())
    assert summary.has_explicit_subpart_points is False
    assert summary.is_valid is True


def _seed_task_with_two_part_question(owner_id: str, task_id: str) -> None:
    from backend.db import assignment_repository, workflow_repository
    from backend.db.models import AssignmentRecord, CourseRecord, UserRecord
    from backend.db.session import session_scope

    structure = _two_part_structure().model_dump()
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id,
            username=owner_id,
            password_hash="hash",
            role="teacher",
            is_active=True,
        ))
        session.flush()
        session.add(CourseRecord(
            id=f"{task_id}-course",
            name="Course",
            teacher_id=owner_id,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=task_id,
            course_id=f"{task_id}-course",
            teacher_id=owner_id,
            name="Assignment",
            status="draft",
            version=1,
        ))
    workflow_repository.ensure_workflow(assignment_id=task_id, owner_id=owner_id)
    assignment_repository.add_question(
        assignment_id=task_id,
        teacher_id=owner_id,
        q_id="q1",
        order_index=0,
        type="计算题",
        number="1",
        stem="第一大题。(a) 完成计算。(b) 给出证明。",
        criterion="(a) 4 分\n(b) 6 分",
        max_score=10,
        source={
            "presentation": {
                "review_status": "needs_review",
                "question_structure": structure,
            }
        },
    )


def test_teacher_edit_rejects_11_of_10_without_partial_write():
    from backend.services import task_facade

    owner_id = "major-rubric-owner"
    task_id = "major-rubric-task"
    _seed_task_with_two_part_question(owner_id, task_id)
    before = task_facade.get_task(task_id=task_id, owner_id=owner_id, full=True)

    with pytest.raises(ValidationError) as exc:
        task_facade.update_problem(
            task_id=task_id,
            owner_id=owner_id,
            q_id="q1",
            patch={"criterion": "(a) 5 分\n(b) 6 分"},
            expected_revision=before["workflow_revision"],
        )

    assert exc.value.code == "rubric_subpart_points_mismatch"
    after = task_facade.get_task(task_id=task_id, owner_id=owner_id, full=True)
    assert after["workflow_revision"] == before["workflow_revision"]
    assert after["problem_data"]["q1"]["criterion"] == "(a) 4 分\n(b) 6 分"
    assert after["problem_data"]["q1"]["rubric_point_summary"]["total_points"] == "10"


def test_teacher_can_atomically_change_major_maximum_and_matching_rubric():
    from backend.services import task_facade

    owner_id = "major-atomic-owner"
    task_id = "major-atomic-task"
    _seed_task_with_two_part_question(owner_id, task_id)
    before = task_facade.get_task(task_id=task_id, owner_id=owner_id, full=True)

    response = task_facade.update_problem(
        task_id=task_id,
        owner_id=owner_id,
        q_id="q1",
        patch={"max_score": 11, "criterion": "(a) 5 分\n(b) 6 分"},
        expected_revision=before["workflow_revision"],
    )

    assert response["workflow_revision"] == before["workflow_revision"] + 1
    assert response["problem"]["max_score"] == 11
    assert response["problem"]["rubric_point_summary"]["total_points"] == "11"


def test_legacy_question_without_metadata_derives_subparts_before_edit():
    from backend.db.models import AssignmentQuestionRecord
    from backend.db.session import session_scope
    from backend.services import task_facade

    owner_id = "major-legacy-owner"
    task_id = "major-legacy-task"
    _seed_task_with_two_part_question(owner_id, task_id)
    with session_scope() as session:
        row = session.query(AssignmentQuestionRecord).filter_by(
            assignment_id=task_id, q_id="q1"
        ).one()
        row.source = {"origin": "legacy"}

    before = task_facade.get_task(task_id=task_id, owner_id=owner_id, full=True)
    assert [
        part["label"]
        for part in before["problem_data"]["q1"]["question_structure"]["subparts"]
    ] == ["(a)", "(b)"]

    with pytest.raises(ValidationError) as exc:
        task_facade.update_problem(
            task_id=task_id,
            owner_id=owner_id,
            q_id="q1",
            patch={"criterion": "(a) 5 分\n(b) 6 分"},
            expected_revision=before["workflow_revision"],
        )
    assert exc.value.code == "rubric_subpart_points_mismatch"


def test_assignment_repository_creation_persists_structure_and_rejects_bad_rubric():
    from backend.db import assignment_repository, workflow_repository
    from backend.db.models import AssignmentRecord, CourseRecord, UserRecord
    from backend.db.session import session_scope

    owner_id = "major-direct-owner"
    task_id = "major-direct-task"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id, username=owner_id, password_hash="hash",
            role="teacher", is_active=True,
        ))
        session.flush()
        session.add(CourseRecord(
            id=f"{task_id}-course", name="Course", teacher_id=owner_id,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=task_id, course_id=f"{task_id}-course", teacher_id=owner_id,
            name="Assignment", status="draft", version=1,
        ))
    workflow_repository.ensure_workflow(assignment_id=task_id, owner_id=owner_id)

    created = assignment_repository.add_question(
        assignment_id=task_id,
        teacher_id=owner_id,
        q_id="q1",
        order_index=0,
        type="计算题",
        number="1",
        stem="第一大题。(a) 计算。(b) 证明。",
        criterion="(a) 4 分\n(b) 6 分",
        max_score=10,
    )
    assert [
        part["label"]
        for part in created.source["presentation"]["question_structure"]["subparts"]
    ] == ["(a)", "(b)"]

    with pytest.raises(ValidationError) as exc:
        assignment_repository.add_question(
            assignment_id=task_id,
            teacher_id=owner_id,
            q_id="q2",
            order_index=1,
            type="计算题",
            number="2",
            stem="第二大题。(a) 计算。(b) 证明。",
            criterion="(a) 5 分\n(b) 6 分",
            max_score=10,
        )
    assert exc.value.code == "rubric_subpart_points_mismatch"
