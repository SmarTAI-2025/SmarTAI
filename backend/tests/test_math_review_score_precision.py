"""A comment-only edit cannot resolve a score review by rounding away hundredths."""
import pytest

from backend.db import grading_repository
from backend.db.models import GradeResultRecord
from backend.db.session import session_scope
from backend.domain.errors import ResultNotReleasable
from backend.domain.grading_review import review_state
from backend.services import grading_runs, task_facade
from backend.tests.test_task_finalization_contract import _prepared_task


REASON = "math_method_review_incomplete"
STEPS = [{"step_no": 1, "part_id": "p", "item_id": "s", "desc": "Final mathematical credit.", "score": 9.37, "is_correct": False}]


@pytest.mark.parametrize("gateway", ["repository", "task"])
@pytest.mark.parametrize("resolution", ["confirm", "edit"])
def test_same_hundredths_stay_pending_across_comment_edit_report_and_release(gateway, resolution):
    seeded = _prepared_task()
    args = {"task_id": seeded["task_id"], "owner_id": seeded["owner_id"]}
    with session_scope() as session:
        row = session.get(GradeResultRecord, seeded["required_result_id"])
        row.ai_score = 9.37
        row.ai_steps = STEPS
        row.ai_confidence = .95
        row.requires_review = row.initial_requires_review = True
        row.review_reasons = row.initial_review_reasons = [REASON]

    def current():
        return next(r for r in grading_repository.list_results_for_run(seeded["run_id"]) if r.id == seeded["required_result_id"])

    def write(score, confirm):
        if gateway == "repository":
            saved = grading_repository.add_teacher_review(seeded["required_result_id"], teacher_id=seeded["owner_id"],
                new_score=score, new_comment="Comment only unless the score actually changed.", confirm=confirm)
        else:
            state = task_facade.finalization(**args)
            saved = task_facade.update_correction_review(**args, display_student_id=seeded["student_id"], q_id="q-required",
                teacher_score=score, teacher_comment="Comment only unless the score actually changed.", confirm=confirm,
                expected_revision=state["workflow_revision"])
        # Saving is allowed even when this question still needs review.
        row = current()
        assert row.effective_score == score
        assert row.teacher_review["new_comment"] == "Comment only unless the score actually changed."
        assert row.teacher_review["confirmed"] is confirm
        return saved

    def blocked():
        row = current()
        public = task_facade._serialize_correction(row)
        assert public["provisional_score"] == public["score"] == 9.37
        assert review_state(public)[0]
        state = task_facade.finalization(**args)
        assert state["remaining_review_count"] == 1 and not state["ready_for_confirmation"]
        with pytest.raises(ResultNotReleasable, match="math_review_required"):
            grading_runs.release(teacher_id=seeded["owner_id"], run_id=seeded["run_id"])
        with pytest.raises(ResultNotReleasable, match="math_review_required"):
            task_facade.generate_artifacts(**args, expected_revision=state["workflow_revision"])
        assert not task_facade.artifact_index(**args)["versions"]
        assert not grading_runs.student_results(student_id=seeded["student_id"], assignment_id=seeded["task_id"])

    blocked()
    write(9.37, False)
    blocked()
    if resolution == "edit":
        write(9.36, False)
        assert task_facade.finalization(**args)["ready_for_confirmation"]
        write(9.37, False)
        blocked()
    wanted = 9.37 if resolution == "confirm" else 9.36
    write(wanted, resolution == "confirm")
    state = task_facade.finalization(**args)
    assert state["remaining_review_count"] == 0 and state["ready_for_confirmation"]
    task_facade.generate_artifacts(**args, expected_revision=state["workflow_revision"])
    row = current()
    assert row.ai_score == 9.37 and row.effective_score == wanted
    assert grading_repository.get_run(seeded["run_id"]).released_at is not None
    shown = next(r for r in grading_runs.student_results(student_id=seeded["student_id"], assignment_id=seeded["task_id"]) if r.q_id == "q-required")
    assert shown.effective_score == wanted


def test_wire_review_state_uses_its_scoring_steps_precision():
    value = {"score": 9.37, "provisional_score": 9.37, "teacher_score": 9.37, "max_score": 10,
        "confidence": .95, "steps": STEPS, "review_reasons": [REASON], "review_status": "edited"}
    assert review_state(value) == (True, False)
    assert review_state({**value, "review_status": "confirmed"}) == (False, True)
    assert review_state({**value, "teacher_score": 9.36}) == (False, False)


@pytest.mark.parametrize("gateway", ["repository", "task"])
@pytest.mark.parametrize("edit", ["comment", "score", "both"])
def test_saved_edit_resolves_yellow_advice_without_confirming_or_changing_other_field(gateway, edit):
    seeded = _prepared_task()
    with session_scope() as session:
        row = session.get(GradeResultRecord, seeded["required_result_id"])
        row.ai_score, row.ai_steps, row.ai_confidence = 9.37, STEPS, .2
        row.ai_comment = ""
        row.requires_review = row.initial_requires_review = True
        row.review_reasons = row.initial_review_reasons = ["math_alternative_method"]
    args = {"task_id": seeded["task_id"], "owner_id": seeded["owner_id"]}
    score = 9.37 if edit == "comment" else 9.36
    comment = "Check the intermediate work." if edit != "score" else ""
    if gateway == "repository":
        grading_repository.add_teacher_review(seeded["required_result_id"], teacher_id=seeded["owner_id"],
            new_score=score, new_comment=comment, confirm=False)
    else:
        task_facade.update_correction_review(**args, display_student_id=seeded["student_id"], q_id="q-required",
            teacher_score=score, teacher_comment=comment, confirm=False,
            expected_revision=task_facade.finalization(**args)["workflow_revision"])
    row = next(r for r in grading_repository.list_results_for_run(seeded["run_id"]) if r.id == seeded["required_result_id"])
    public = task_facade._serialize_correction(row)
    assert public["teacher_score"] == score and public["teacher_comment"] == comment
    assert public["provisional_score"] == 9.37 and public["review_status"] == "edited"
    assert review_state(public) == (False, False)
    assert row.teacher_review["confirmed"] is False and not row.requires_review
