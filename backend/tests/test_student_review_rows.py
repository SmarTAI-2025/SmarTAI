import pytest
from sqlalchemy import select

from backend.db import file_repository, workflow_repository
from backend.db.models import SubmissionRecord, SubmissionRevisionRecord
from backend.db.session import session_scope
from backend.domain.errors import DomainError
from backend.services import task_facade
from backend.services.student_review_rows import add_student, delete_student
from backend.services.submission_manual_review import continue_manually
from backend.services.task_execution import stop_task_run
from backend.tests.test_task_background_workflows import _Registry, _seed_task


async def manual_task():
    owner, task = _seed_task(with_question=True)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1.txt", content=b"Original student answer", content_type="text/plain", registry=_Registry())
    stop_task_run(task_id=task, owner_id=owner, job_id=queued["job_id"],
        expected_revision=workflow_repository.get_workflow(task, owner_id=owner).workflow_revision)
    paused = task_facade.get_task(task_id=task, owner_id=owner)
    await continue_manually(task_id=task, owner_id=owner, job_id=queued["job_id"],
        expected_revision=paused["workflow_revision"])
    return owner, task


@pytest.mark.asyncio
async def test_add_edit_delete_student_rows_preserves_other_students_originals_and_revisions():
    owner, task = await manual_task()
    before = task_facade.get_task(task_id=task, owner_id=owner)
    original_student = next(iter(before["student_data"].values()))
    original_files = {row.id for row in file_repository.list_files(owner_id=owner, assignment_id=task)}
    with session_scope() as session:
        old_submissions = list(session.scalars(select(SubmissionRecord).where(SubmissionRecord.assignment_id == task)))
        original_submission_id = old_submissions[0].id
        original_revision_id = old_submissions[0].current_revision_id
    created = add_student(task_id=task, owner_id=owner, expected_revision=before["workflow_revision"])
    after = task_facade.get_task(task_id=task, owner_id=owner)
    assert after["student_data"][original_student["stu_id"]] == original_student
    new_student = after["student_data"][created["student_id"]]
    assert new_student["identity_status"] == "needs_review" and new_student["stu_name"] == "[Unknown Student]"
    assert new_student["stu_ans"][0]["content"] == "" and "recognition_manual_pending" in new_student["stu_ans"][0]["flag"]
    assert "submission_recognition_blocked" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
    task_facade.update_student_answer(task_id=task, owner_id=owner, display_student_id=created["student_id"],
        q_id="q1", patch={"content": "Teacher transcribed answer", "review_status": "confirmed"}, expected_revision=after["workflow_revision"])
    after = task_facade.get_task(task_id=task, owner_id=owner)
    task_facade.update_student_identity(task_id=task, owner_id=owner, current_display_id=created["student_id"],
        new_display_id="S2", new_display_name="Synthetic learner", expected_revision=after["workflow_revision"])
    after = task_facade.get_task(task_id=task, owner_id=owner)
    deleted = delete_student(task_id=task, owner_id=owner, student_id=original_student["stu_id"], expected_revision=after["workflow_revision"])
    after = task_facade.get_task(task_id=task, owner_id=owner)
    assert set(after["student_data"]) == {"S2"}
    assert not {"submission_recognition_blocked", "submission_identities_unresolved", "submission_sources_failed"}.intersection(
        task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"])
    assert {row.id for row in file_repository.list_files(owner_id=owner, assignment_id=task)} == original_files
    with session_scope() as session:
        assert session.get(SubmissionRecord, original_submission_id) is not None
        assert session.get(SubmissionRevisionRecord, original_revision_id) is not None
    delete_student(task_id=task, owner_id=owner, student_id="S2", expected_revision=deleted["workflow_revision"])
    empty = task_facade.get_task(task_id=task, owner_id=owner)
    assert empty["status"] == "submissions_ready" and not empty["student_data"]
    assert "submissions_required" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
    # Emptying the original matrix must leave the same add-row flow available.
    restored = add_student(task_id=task, owner_id=owner, expected_revision=empty["workflow_revision"])
    assert restored["student_id"] in task_facade.get_task(task_id=task, owner_id=owner)["student_data"]


@pytest.mark.asyncio
async def test_student_row_mutations_reject_other_owners_stale_revisions_and_grading():
    owner, task = await manual_task()
    before = task_facade.get_task(task_id=task, owner_id=owner)
    student_id = next(iter(before["student_data"]))
    other_owner, _ = _seed_task(with_question=True)
    for mutation in (
        lambda user, revision: add_student(task_id=task, owner_id=user, expected_revision=revision),
        lambda user, revision: delete_student(task_id=task, owner_id=user, student_id=student_id, expected_revision=revision),
    ):
        with pytest.raises(DomainError):
            mutation(other_owner, before["workflow_revision"])
        with pytest.raises(DomainError) as stale:
            mutation(owner, before["workflow_revision"] - 1)
        assert stale.value.code == "stale_revision"
    for status in ("parsing_submissions", "grading", "graded", "finalized", "error"):
        updated = workflow_repository.update_workflow(task, owner_id=owner, presentation_status=status)
        with pytest.raises(DomainError):
            add_student(task_id=task, owner_id=owner, expected_revision=updated.workflow_revision)
        with pytest.raises(DomainError):
            delete_student(task_id=task, owner_id=owner, student_id=student_id, expected_revision=updated.workflow_revision)
    assert task_facade.get_task(task_id=task, owner_id=owner)["student_data"] == before["student_data"]


@pytest.mark.asyncio
@pytest.mark.parametrize("same_display_id", [False, True])
async def test_deleted_student_cannot_be_edited_through_legacy_internal_id_fallback(same_display_id):
    owner, task = await manual_task()
    with session_scope() as session:
        presentation = session.scalar(select(workflow_repository.AssignmentStudentPresentationRecord).where(
            workflow_repository.AssignmentStudentPresentationRecord.assignment_id == task))
        if same_display_id:
            presentation.display_student_id = presentation.student_id
        display_id = presentation.display_student_id
        internal_id = presentation.student_id
    before = task_facade.get_task(task_id=task, owner_id=owner)
    removed = delete_student(task_id=task, owner_id=owner, student_id=display_id, expected_revision=before["workflow_revision"])
    for legacy_id in {internal_id, display_id}:
        with pytest.raises(DomainError):
            task_facade.update_student_answer(task_id=task, owner_id=owner, display_student_id=legacy_id,
                q_id="q1", patch={"content": "Must not resurrect deleted row"}, expected_revision=removed["workflow_revision"])
    current = task_facade.get_task(task_id=task, owner_id=owner)
    assert current["student_data"] == {} and current["workflow_revision"] == removed["workflow_revision"]
