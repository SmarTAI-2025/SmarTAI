"""A subpart total and its included components are not separate allocations."""
import pytest

from backend.services.question_structure import build_major_question_structure, validate_rubric_points
from backend.services.question_structure import QuestionRubricValidationError


@pytest.mark.parametrize("criterion", [
    "(a) 4分，其中方法2分、结果2分；(b) 6分",
    "(a) 4 分（包括方法 2 分、结果 2 分）；(b) 6 分",
    "(a) 共4分，包含方法2分、结果2分；(b) 6分",
    "(a) 总分4分，其中方法2分、结果2分；(b) 6分",
    "(a) 4 points, including method 2 points and result 2 points; (b) 6 points",
    "(a) 4 pts (of which method 2 pts, result 2 pts); (b) 6 pts",
])
def test_included_breakdown_counts_total_once(criterion):
    structure = build_major_question_structure(
        {"number": "1", "stem": "(a) Calculate.\n(b) Prove."}, major_order=0,
    )
    summary = validate_rubric_points(criterion, 10, structure)
    assert [item.points for item in summary.items] == ["4", "6"]
    assert summary.total_points == "10"


def test_breakdown_does_not_hide_wrong_major_total():
    structure = build_major_question_structure(
        {"number": "1", "stem": "(a) Calculate.\n(b) Prove."}, major_order=0,
    )
    with pytest.raises(QuestionRubricValidationError):
        validate_rubric_points("(a) 5分，其中方法2分、结果3分；(b) 6分", 10, structure)


def test_teacher_can_save_included_breakdown_without_rewriting_rubric():
    from backend.tests.test_major_question_structure import _seed_task_with_two_part_question
    from backend.services import task_facade

    owner, task = "included-rubric-owner", "included-rubric-task"
    _seed_task_with_two_part_question(owner, task)
    before = task_facade.get_task(task_id=task, owner_id=owner, full=True)
    criterion = "(a) 4分，其中方法2分、结果2分；(b) 6分"
    response = task_facade.update_problem(
        task_id=task, owner_id=owner, q_id="q1", patch={"criterion": criterion},
        expected_revision=before["workflow_revision"],
    )
    assert response["problem"]["criterion"] == criterion
    assert response["problem"]["rubric_point_summary"]["total_points"] == "10"
