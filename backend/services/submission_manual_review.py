"""Explicit teacher handoff of saved submission sources, without model calls."""
import uuid
import asyncio
import time
from copy import deepcopy

from backend.agents.ingest_agent import _ocr_identity_candidate
from backend.db import assignment_repository, source_outcome_repository, workflow_repository
from backend.domain.errors import InvalidTransition, NotFound, VersionConflict
from backend.domain.source_outcomes import safe_source_diagnostic
from backend.services import task_facade
from backend.services.submission_source_pipeline import prepare_submission_sources


async def continue_manually(*, task_id: str, owner_id: str, job_id: str, expected_revision: int):
    workflow = workflow_repository.get_live_workflow(task_id, owner_id=owner_id)
    origin = workflow_repository.get_operation(job_id, owner_id=owner_id)
    if origin.assignment_id != task_id or origin.operation_type != "submission_recognition":
        raise NotFound("submission_recognition")
    if workflow.active_job_id or origin.status != "error" or workflow.last_failed_job_id != origin.id:
        raise InvalidTransition("Only the latest stopped or failed recognition can be reviewed manually.")
    if workflow.workflow_revision != expected_revision:
        raise VersionConflict("The task changed before manual review.", code="stale_revision")
    questions = assignment_repository.list_questions(task_id, teacher_id=owner_id)
    if (origin.payload or {}).get("question_snapshot") != [[q.id, q.version] for q in questions]:
        raise VersionConflict("Recognition inputs changed.", code="recognition_plan_changed")
    upload = task_facade.load_submission_retry_upload(task_id=task_id, owner_id=owner_id, job_id=job_id)
    reusable = task_facade._reusable_submission_results(
        owner_id=owner_id, task_id=task_id, payload={"retry_from": upload["retry_from"]}, questions=questions, include_incomplete=True)
    # A separate local operation keeps the stopped attempt and source outcomes
    # immutable. The model worker does not dispatch this operation type.
    row, _ = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id, operation_type="submission_manual_review",
        input_hash=task_facade._hash_json([job_id, origin.attempt, expected_revision]), retry_existing=True,
        payload={**(origin.payload or {}), "retry_from": upload["retry_from"], "manual_origin_id": job_id,
                 "source_page_diagnostics": (origin.checkpoint or {}).get("source_page_diagnostics", {})})
    worker = "manual-review-" + uuid.uuid4().hex
    row = workflow_repository.claim_operation(row.id, owner_id=owner_id, worker_id=worker, lease_seconds=300)
    try:
        # Reuse stored source identities; enumerate any untouched archive members
        # through the same bounded local extraction path used by recognition.
        for source in source_outcome_repository.list_sources(operation_id=origin.id, owner_id=owner_id, attempt=origin.attempt):
            source_outcome_repository.register_source(owner_id=owner_id, assignment_id=task_id,
                operation_id=row.id, expected_attempt=row.attempt, order_index=source.order_index,
                stored_file_id=source.stored_file_id,
                expected_lease_token=row.lease_token)
        workflow_repository.save_operation_checkpoint(row.id, owner_id=owner_id,
            expected_attempt=row.attempt, expected_checkpoint_revision=row.checkpoint_revision,
            expected_lease_token=row.lease_token, stage="manual_sources",
            checkpoint={"source_page_diagnostics": row.payload["source_page_diagnostics"]}, artifact_refs=origin.artifact_refs)
        sources = await prepare_submission_sources(
            content=upload["content"], filename=upload["filename"], content_type=upload["content_type"],
            owner_id=owner_id, task_id=task_id, job_id=row.id, job_attempt=row.attempt,
            operation_lease_token=row.lease_token, ocr_skill=None,
            reuse_source=lambda *_args: True)
        originals = {source.id: source for source in source_outcome_repository.list_sources(
            operation_id=row.id, owner_id=owner_id, attempt=row.attempt)}
        students, used_ids = [], set()
        # Unrecognized filename candidates must not displace saved identities.
        saved_ids = {result.student["stu_id"] for result in reusable.values()}
        for source in sources:
            saved = reusable.get((source.filename, originals[source.source_id].sha256))
            if saved is not None:
                student = deepcopy(saved.student)
                outcome = dict(status=saved.status, stable_error_code=saved.stable_error_code,
                    failure_phase=saved.failure_phase, matched_answer_count=saved.matched_answer_count)
                if task_facade._submission_result_incomplete(saved):
                    reason, phase = safe_source_diagnostic("parse_failed",
                        (student.get("recognition") or {}).get("error_code")
                        or (saved.stable_error_code if saved.failure_phase != "identity" else None)
                        or origin.error_code, "recognition")
                    outcome.update(status="parse_failed", stable_error_code=reason, failure_phase=phase)
            else:
                candidate_id, candidate_name, _ = _ocr_identity_candidate("", source.filename)
                student = dict(stu_id=candidate_id, stu_name=candidate_name, stu_ans=[],
                    identity_status="needs_review", identity_match_method="manual_review")
                reason, phase = safe_source_diagnostic("parse_failed", origin.error_code, "recognition")
                outcome = dict(status="parse_failed", stable_error_code=reason,
                    failure_phase=phase, matched_answer_count=0)
            if student["stu_id"] in used_ids or (saved is None and student["stu_id"] in saved_ids):
                student["stu_id"] = f"{student['stu_id']}-{source.source_id[-8:]}"
                student["identity_status"] = "needs_review"
            used_ids.add(student["stu_id"])
            answers = {answer["q_id"]: answer for answer in student.get("stu_ans", [])}
            for question in questions:
                if question.q_id not in answers:
                    answers[question.q_id] = dict(q_id=question.q_id, number=question.number,
                        type=question.type, content="", flag=["recognition_manual_pending"])
                answer = answers[question.q_id]
                if task_facade._answer_recognition_failures(answer.get("flag")):
                    answer["flag"] = list(dict.fromkeys([*answer.get("flag", []), "recognition_manual_pending",
                        "recognition_paused" if origin.error_code == "operation_cancelled" else "recognition_incomplete"]))
            student.update(source_id=source.source_id, stored_file_id=source.stored_file_id,
                source_filename=source.filename, stu_ans=list(answers.values()))
            students.append(student)
            source_outcome_repository.record_outcome(source_id=source.source_id, owner_id=owner_id,
                student_candidate=student["stu_id"], unknown_question_ids=[], retryable=False,
                expected_attempt=row.attempt, expected_lease_token=row.lease_token, **outcome)
        assignment = assignment_repository.get_assignment(task_id, actor_id=owner_id)
        task_facade._commit_imported_submissions(
            task_id=task_id, owner_id=owner_id, course_id=assignment.course_id, students=students,
            replace_existing=bool(origin.payload.get("replace_confirmed")), expected_workflow_revision=expected_revision,
            operation_id=row.id, expected_operation_attempt=row.attempt, expected_lease_token=row.lease_token,
            submission_file_name=upload["filename"], manual_recovery=True)
        updated = workflow_repository.get_live_workflow(task_id, owner_id=owner_id)
        return {"status": "ready_for_review", "task_id": task_id, "workflow_revision": updated.workflow_revision}
    except (Exception, asyncio.CancelledError):
        current = workflow_repository.get_operation(row.id, owner_id=owner_id)
        if current.lease_token == row.lease_token:
            workflow_repository.update_operation(row.id, owner_id=owner_id, expected_attempt=row.attempt,
                expected_lease_token=row.lease_token, status="error", error_code="submission_manual_review_failed", completed_at=time.time())
        raise
    finally:
        current = workflow_repository.get_operation(row.id, owner_id=owner_id)
        if current.lease_token == row.lease_token:
            workflow_repository.release_operation(row.id, owner_id=owner_id, worker_id=worker, lease_token=row.lease_token)
