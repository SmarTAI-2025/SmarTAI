from __future__ import annotations

import io
import json
import uuid
import zipfile
from types import SimpleNamespace

import pytest

from backend.agents.ingest_agent import (
    SubmissionSourceInput,
    parse_student_answer_sources,
)
from backend.db import assignment_repository, source_outcome_repository, workflow_repository
from backend.db.file_repository import save_file
from backend.db.models import AssignmentRecord, CourseRecord, UserRecord
from backend.db.session import session_scope
from backend.domain.errors import NotFound
from backend.services import task_facade
from backend.services.background_errors import classify_background_error
from backend.storage import get_storage
from backend.tools.structured_llm import PermanentLLMError


def _seed_task() -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:10]
    owner_id = f"teacher_{suffix}"
    course_id = f"course_{suffix}"
    task_id = f"assignment_{suffix}"
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
            id=course_id,
            name="Course",
            code=f"C-{suffix}",
            teacher_id=owner_id,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=task_id,
            course_id=course_id,
            teacher_id=owner_id,
            name="Assignment",
            status="draft",
            version=1,
        ))
    workflow_repository.ensure_workflow(assignment_id=task_id, owner_id=owner_id)
    assignment_repository.add_question(
        task_id,
        teacher_id=owner_id,
        q_id="q1",
        order_index=0,
        type="short",
        stem="Question one",
        criterion="",
        max_score=10,
    )
    return owner_id, task_id


class _Registry:
    provider = SimpleNamespace(provider_id="test-provider")

    def pick_default(self):
        return self.provider

    def get(self, provider_id):
        return self.provider if provider_id == "test-provider" else None

    def list_configs(self):
        return [{"provider_id": "test-provider", "enabled": True}]

    def pick_vision(self, provider):
        del provider
        return None


class _VisionRegistry(_Registry):
    def pick_vision(self, provider):
        return provider


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return output.getvalue()


def _queue(owner_id: str, task_id: str, content: bytes, filename: str = "answers.zip"):
    return task_facade.queue_task_submission_parsing(
        task_id=task_id,
        owner_id=owner_id,
        filename=filename,
        content=content,
        content_type="application/zip" if filename.endswith(".zip") else "image/png",
        registry=_Registry(),
        recognition_provider_id="test-provider",
    )


def test_twenty_sources_equal_eighteen_success_one_failure_one_review_and_are_owner_scoped():
    owner_id, task_id = _seed_task()
    other_owner = f"other_{uuid.uuid4().hex[:10]}"
    with session_scope() as session:
        session.add(UserRecord(
            id=other_owner,
            username=other_owner,
            password_hash="hash",
            role="teacher",
            is_active=True,
        ))
    operation, _created = workflow_repository.create_operation(
        assignment_id=task_id,
        owner_id=owner_id,
        operation_type="submission_recognition",
        input_hash=uuid.uuid4().hex,
    )
    workflow_repository.update_workflow(
        task_id,
        owner_id=owner_id,
        bump_revision=False,
        parse_job_id=operation.id,
    )

    for index in range(20):
        stored = save_file(
            storage=get_storage(),
            owner_id=owner_id,
            kind="submission_source",
            original_name=f"student-{index + 1}.txt",
            content=f"answer-{index + 1}".encode(),
            content_type="text/plain",
            assignment_id=task_id,
        )
        source, _ = source_outcome_repository.register_source(
            owner_id=owner_id,
            assignment_id=task_id,
            operation_id=operation.id,
            expected_attempt=operation.attempt,
            order_index=index,
            stored_file_id=stored.id,
        )
        if index < 18:
            status, code, phase = "parsed", None, None
        elif index == 18:
            status, code, phase = "parse_failed", "vision_provider_required", "ocr"
        else:
            status, code, phase = "identity_conflict", "identity_needs_review", "identity"
        source_outcome_repository.record_outcome(
            source_id=source.id,
            owner_id=owner_id,
            status=status,
            student_candidate=f"S{index + 1:03d}",
            matched_answer_count=1 if status != "parse_failed" else 0,
            unknown_question_ids=[],
            stable_error_code=code,
            failure_phase=phase,
            retryable=False,
        )

    summary = source_outcome_repository.summarize_sources(
        operation_id=operation.id,
        owner_id=owner_id,
        attempt=operation.attempt,
    )
    assert (
        summary.uploaded_count,
        summary.success_count,
        summary.failed_count,
        summary.conflict_count,
        summary.pending_count,
    ) == (20, 18, 1, 1, 0)
    task = task_facade.get_task(task_id=task_id, owner_id=owner_id)
    assert task["submission_source_summary"] == {
        "uploaded": 20,
        "parsed": 18,
        "failed": 1,
        "identity_needs_review": 1,
        "pending": 0,
    }
    with pytest.raises(NotFound):
        source_outcome_repository.list_source_results(
            operation_id=operation.id,
            owner_id=other_owner,
            attempt=operation.attempt,
        )


def test_source_persistence_failure_survives_safe_error_projection():
    error = RuntimeError("submission_source_persistence_failed")
    assert classify_background_error(error, "submission_parse_failed") == (
        "submission_source_persistence_failed"
    )


@pytest.mark.asyncio
async def test_partial_batch_keeps_success_and_exact_per_file_failure(monkeypatch):
    owner_id, task_id = _seed_task()
    content = _zip_bytes({"good.txt": b"answer", "bad.txt": b"answer"})
    queued = _queue(owner_id, task_id, content)

    async def fake_invoke(_provider, messages):
        prompt = messages[-1].content
        if "bad.txt" in prompt:
            return SimpleNamespace(content="not structured JSON")
        return SimpleNamespace(content=json.dumps({
            "stu_id": "S001",
            "stu_name": "Student One",
            "stu_ans": [{
                "q_id": "q1",
                "number": "1",
                "type": "short",
                "content": "answer",
            }],
        }))

    monkeypatch.setattr("backend.agents.ingest_agent.ainvoke_with_retry", fake_invoke)
    await task_facade.run_task_submission_parsing(
        task_id=task_id,
        owner_id=owner_id,
        job_id=queued["job_id"],
        filename="answers.zip",
        content=content,
        content_type="application/zip",
        registry=_Registry(),
        job_attempt=queued["_job_attempt"],
        identity_mode="filename",
        roster_entries=None,
        recognition_provider_id="test-provider",
        replace_confirmed=False,
        claimed_workflow_revision=queued["workflow_revision"],
    )

    task = task_facade.get_task(task_id=task_id, owner_id=owner_id)
    assert task["status"] == "submissions_ready"
    assert task["student_count"] == 1
    assert task["submission_source_summary"] == {
        "uploaded": 2,
        "parsed": 1,
        "failed": 1,
        "identity_needs_review": 0,
        "pending": 0,
    }
    by_name = {item["file_name"]: item for item in task["submission_sources"]}
    assert by_name["good.txt"]["status"] == "parsed"
    assert by_name["bad.txt"]["status"] == "failed"
    assert by_name["bad.txt"]["internal_status"] == "parse_failed"
    assert by_name["bad.txt"]["reason_code"] == "submission_parse_invalid"
    assert by_name["bad.txt"]["failure_phase"] == "structured_parse"
    assert by_name["bad.txt"]["retryable"] is False


@pytest.mark.asyncio
async def test_teacher_identity_confirmation_resolves_current_attention_without_rewriting_outcome(monkeypatch):
    owner_id, task_id = _seed_task()
    content = _zip_bytes({"uncertain.txt": b"answer"})
    queued = _queue(owner_id, task_id, content)

    async def fake_invoke(_provider, _messages):
        return SimpleNamespace(content=json.dumps({
            "stu_id": "S010",
            "stu_name": "Candidate",
            "stu_ans": [{
                "q_id": "q1", "number": "1", "type": "short", "content": "answer"
            }],
        }))

    monkeypatch.setattr("backend.agents.ingest_agent.ainvoke_with_retry", fake_invoke)
    await task_facade.run_task_submission_parsing(
        task_id=task_id,
        owner_id=owner_id,
        job_id=queued["job_id"],
        filename="answers.zip",
        content=content,
        content_type="application/zip",
        registry=_Registry(),
        job_attempt=queued["_job_attempt"],
        identity_mode="manual_review",
        roster_entries=None,
        recognition_provider_id="test-provider",
        replace_confirmed=False,
        claimed_workflow_revision=queued["workflow_revision"],
    )
    before = task_facade.get_task(task_id=task_id, owner_id=owner_id)
    assert before["submission_source_summary"]["identity_needs_review"] == 1
    assert before["submission_sources"][0]["reason_code"] == "identity_needs_review"

    task_facade.update_student_identity(
        task_id=task_id,
        owner_id=owner_id,
        current_display_id="S010",
        new_display_id="S010-confirmed",
        new_display_name="Confirmed Student",
        expected_revision=before["workflow_revision"],
    )
    after = task_facade.get_task(task_id=task_id, owner_id=owner_id)
    assert after["submission_source_summary"] == {
        "uploaded": 1,
        "parsed": 1,
        "failed": 0,
        "identity_needs_review": 0,
        "pending": 0,
    }
    source = after["submission_sources"][0]
    assert source["status"] == "parsed"
    assert source["internal_status"] == "identity_conflict"
    assert source["reason_code"] is None
    assert source["recognition_reason_code"] == "identity_needs_review"
    assert source["resolution_status"] == "identity_resolved"


@pytest.mark.asyncio
async def test_model_without_image_support_reaches_teacher_as_vision_reason(monkeypatch):
    owner_id, task_id = _seed_task()
    content = b"not-a-real-image-but-ocr-provider-sees-bytes"
    queued = _queue(owner_id, task_id, content, filename="scan.png")

    class UnsupportedVisionSkill:
        async def recognize_images(self, _images, _purpose):
            raise PermanentLLMError("This model does not support image input")

    monkeypatch.setattr(
        task_facade,
        "LLMVisionOCRSkill",
        lambda _provider: UnsupportedVisionSkill(),
    )
    await task_facade.run_task_submission_parsing(
        task_id=task_id,
        owner_id=owner_id,
        job_id=queued["job_id"],
        filename="scan.png",
        content=content,
        content_type="image/png",
        registry=_VisionRegistry(),
        job_attempt=queued["_job_attempt"],
        identity_mode="filename",
        roster_entries=None,
        recognition_provider_id="test-provider",
        replace_confirmed=False,
        claimed_workflow_revision=queued["workflow_revision"],
    )

    task = task_facade.get_task(task_id=task_id, owner_id=owner_id)
    assert task["status"] == "error"
    assert task["error"] == "vision_provider_required"
    assert task["submission_source_summary"] == {
        "uploaded": 1,
        "parsed": 0,
        "failed": 1,
        "identity_needs_review": 0,
        "pending": 0,
    }
    source = task["submission_sources"][0]
    assert source["status"] == "failed"
    assert source["internal_status"] == "parse_failed"
    assert source["reason_code"] == "vision_provider_required"
    assert source["failure_phase"] == "ocr"
    operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner_id)
    assert operation.error_code == "vision_provider_required"
    assert operation.progress["error_detail"] == "vision_provider_required"


@pytest.mark.asyncio
async def test_no_matching_answer_is_not_student_answer_wrong(monkeypatch):
    async def fake_invoke(_provider, _messages):
        return SimpleNamespace(content=json.dumps({
            "stu_id": "S002",
            "stu_name": "Student Two",
            "stu_ans": [{
                "q_id": "q99",
                "number": "99",
                "type": "short",
                "content": "an answer for another assignment",
            }],
        }))

    monkeypatch.setattr("backend.agents.ingest_agent.ainvoke_with_retry", fake_invoke)
    results = await parse_student_answer_sources(
        [SubmissionSourceInput(
            source_id="src-one",
            stored_file_id="file-one",
            filename="student.txt",
            content_type="text/plain",
            text="answer",
        )],
        {"q1": {"q_id": "q1", "number": "1", "type": "short", "stem": "Q1"}},
        SimpleNamespace(provider_id="test"),
    )
    result = results[0]
    assert result.status == "no_matching_answer"
    assert result.stable_error_code == "no_matching_answer"
    assert result.failure_phase == "question_matching"
    assert result.unknown_question_ids == ("q99",)
    assert result.student is None


@pytest.mark.asyncio
async def test_empty_answer_list_has_a_distinct_teacher_reason(monkeypatch):
    async def fake_invoke(_provider, _messages):
        return SimpleNamespace(content=json.dumps({
            "stu_id": "S004",
            "stu_name": "Student Four",
            "stu_ans": [],
        }))

    monkeypatch.setattr("backend.agents.ingest_agent.ainvoke_with_retry", fake_invoke)
    results = await parse_student_answer_sources(
        [SubmissionSourceInput(
            source_id="src-empty-answer",
            stored_file_id="file-empty-answer",
            filename="cover-page.txt",
            content_type="text/plain",
            text="student name but no answer region",
        )],
        {"q1": {"q_id": "q1", "number": "1", "type": "short", "stem": "Q1"}},
        SimpleNamespace(provider_id="test"),
    )
    result = results[0]
    assert result.status == "no_matching_answer"
    assert result.stable_error_code == "no_answer_content_detected"
    assert result.failure_phase == "answer_detection"
    assert result.unknown_question_ids == ()
    assert result.student is None


@pytest.mark.asyncio
async def test_duplicate_student_ids_preserve_both_sources_for_review(monkeypatch):
    async def fake_invoke(_provider, _messages):
        return SimpleNamespace(content=json.dumps({
            "stu_id": "S003",
            "stu_name": "Student Three",
            "stu_ans": [{
                "q_id": "q1", "number": "1", "type": "short", "content": "A"
            }],
        }))

    monkeypatch.setattr("backend.agents.ingest_agent.ainvoke_with_retry", fake_invoke)
    sources = [
        SubmissionSourceInput(
            source_id=f"src-{index}",
            stored_file_id=f"file-{index}",
            filename=f"student-{index}.txt",
            content_type="text/plain",
            text="answer",
        )
        for index in (1, 2)
    ]
    results = await parse_student_answer_sources(
        sources,
        {"q1": {"q_id": "q1", "number": "1", "type": "short", "stem": "Q1"}},
        SimpleNamespace(provider_id="test"),
    )
    assert [result.status for result in results] == ["identity_conflict", "identity_conflict"]
    assert [result.stable_error_code for result in results] == [
        "duplicate_student_identity", "duplicate_student_identity"
    ]
    ids = [result.student["stu_id"] for result in results if result.student]
    assert ids == ["S003#duplicate-1", "S003#duplicate-2"]
