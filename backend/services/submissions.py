"""Submission use cases: online answers, OCR file uploads, teacher import,
immutable revisions, and answer correction.

Both ingestion paths flow through one pipeline: ``create_submission`` (the
single current submission row, enrollment + open-state gated) then
``add_revision`` (a new immutable revision with normalized answer rows). The
only difference is the ``source`` enum and who is the actor.

Teacher batch import resolves each student id explicitly and returns a
per-student success/failure record; a partial import never reports full success
("partial failure cannot silently report full success"). Answer correction is a
new revision, never an in-place mutation.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from backend.agents.ingest_agent import parse_student_answers
from backend.db import assignment_repository, course_repository, submission_repository
from backend.domain import education
from backend.domain.errors import AssignmentClosed, NotFound, ValidationError
from backend.tools.file_processing import extract_files_from_archive

if TYPE_CHECKING:
    from backend.llm.providers import BaseProvider
    from backend.skills.ocr_ingest import OCRIngestSkill


def _resolve_question_map(assignment_id: str) -> dict[str, education.QuestionDTO]:
    questions = assignment_repository.get_questions_by_assignment(assignment_id=assignment_id)
    return {q.q_id: q for q in questions}


def _build_answers(question_map: dict[str, education.QuestionDTO],
                   raw_answers: list[dict]) -> list[dict]:
    out: list[dict] = []
    for ans in raw_answers:
        q_id = ans.get("q_id") or ans.get("question_q_id")
        q = question_map.get(q_id)
        if q is None:
            # Unknown q_id for this assignment: refuse rather than persist a
            # dangling answer that could never be graded.
            raise ValidationError(f"Unknown question id: {q_id}")
        out.append({
            "question_id": q.id,
            "q_id": q.q_id,
            "type": q.type,
            "number": ans.get("number", q.number),
            "content": ans.get("content", ""),
            "flag": ans.get("flag", []),
        })
    return out


def _problem_store(
    questions: list[education.QuestionDTO],
) -> dict[str, dict]:
    return {
        question.q_id: {
            "q_id": question.q_id,
            "number": question.number,
            "type": question.type,
            "stem": question.stem,
            "criterion": question.criterion,
        }
        for question in questions
    }


async def _parse_upload_answers(
    *,
    assignment_id: str,
    filename: str,
    content: bytes,
    provider: "BaseProvider",
    ocr_skill: "OCRIngestSkill | None",
) -> list[dict]:
    """OCR/text-extract one authenticated student's upload and segment answers."""
    questions = assignment_repository.get_questions_by_assignment(
        assignment_id=assignment_id
    )
    if not questions:
        raise ValidationError("Assignment has no questions")

    extracted_files = await extract_files_from_archive(
        content,
        filename,
        ocr_skill=ocr_skill,
        purpose="submissions",
    )
    if not extracted_files:
        raise ValidationError("No supported files found in upload")

    # A student upload may be an archive of several pages. Treat every member
    # as one authenticated student's document rather than trusting OCR-derived
    # identity or creating one submission per archive member.
    combined = "\n\n".join(
        f"[Source file: {item['filename']}]\n{item['content']}"
        for item in extracted_files
        if item.get("content", "").strip()
    )
    if not combined.strip():
        raise ValidationError("Upload did not contain readable text")

    parsed_students: dict[str, dict] = {}
    await parse_student_answers(
        files_data=[{"filename": filename, "content": combined}],
        problems_data=_problem_store(questions),
        student_store=parsed_students,
        provider=provider,
    )
    parsed = next(iter(parsed_students.values()), None)
    if parsed is None:
        raise ValidationError("Unable to parse answers from upload")
    answers = parsed.get("stu_ans")
    if not isinstance(answers, list):
        raise ValidationError("Parsed upload did not contain an answer list")
    return answers


def submit_online(*, student_id: str, assignment_id: str,
                  answers: list[dict]) -> education.SubmissionRevisionDTO:
    """Student online submission: structured answers arrive already parsed."""
    sub = submission_repository.create_submission(
        assignment_id=assignment_id, student_id=student_id
    )
    question_map = _resolve_question_map(assignment_id)
    built = _build_answers(question_map, answers)
    return submission_repository.add_revision(
        submission_id=sub.id, student_id=student_id,
        source=education.SubmissionRevisionSource.ONLINE.value, answers=built,
    )


def teacher_import(*, teacher_id: str, assignment_id: str,
                   items: list[dict]) -> dict:
    """Batch import submissions for one assignment.

    Each item is ``{"student_id": ..., "answers": [{"q_id": ..., "content": ...}]}``.
    Returns ``{"succeeded": [...], "failed": [{"student_id": ..., "error": ...}]}``;
    a non-empty ``failed`` list means the response is not a full success even
    though the accepted students were persisted.
    """
    # Ownership: the assignment must belong to the importing teacher.
    assignment_repository.get_assignment(assignment_id=assignment_id, actor_id=teacher_id)
    question_map = _resolve_question_map(assignment_id)

    succeeded: list[str] = []
    failed: list[dict] = []
    for item in items:
        student_id = item.get("student_id")
        if not student_id:
            failed.append({"student_id": student_id, "error": "missing student_id"})
            continue
        try:
            # Enforce enrollment: an imported student must be enrolled in the
            # assignment's course, otherwise the import would create a submission
            # for someone who cannot see the assignment.
            assignment = assignment_repository.get_assignment_unscoped(assignment_id)
            if not course_repository.is_enrolled(
                course_id=assignment.course_id, student_id=student_id
            ):
                raise ValidationError("student not enrolled")
            sub = submission_repository.create_submission(
                assignment_id=assignment_id, student_id=student_id
            )
            built = _build_answers(question_map, item.get("answers", []))
            submission_repository.add_revision(
                submission_id=sub.id, student_id=student_id,
                source=education.SubmissionRevisionSource.TEACHER_IMPORT.value,
                file_name=item.get("file_name", ""), answers=built,
            )
            succeeded.append(student_id)
        except Exception as exc:  # noqa: BLE001 — surface per-student failure
            failed.append({"student_id": student_id, "error": str(exc) or type(exc).__name__})
    return {"succeeded": succeeded, "failed": failed}


def correct_answer(*, student_id: str, submission_id: str,
                   answers: list[dict]) -> education.SubmissionRevisionDTO:
    """Answer correction creates a new immutable revision (never mutates)."""
    sub = submission_repository.get_submission(submission_id=submission_id, actor_id=student_id)
    question_map = _resolve_question_map(sub.assignment_id)
    built = _build_answers(question_map, answers)
    return submission_repository.add_revision(
        submission_id=submission_id, student_id=student_id,
        source=education.SubmissionRevisionSource.ONLINE.value, answers=built,
    )


def submit_student_file(*, student_id: str, assignment_id: str, filename: str,
                        content: bytes, content_type: str | None,
                        answers: list[dict] | None = None) -> education.SubmissionRevisionDTO:
    """Student file upload: persist the original file and write its parsed
    answers (if any) as a new revision. The original file is linked to the
    revision so an audit can always retrieve exactly what was submitted."""
    return _persist_file_revision(
        student_id=student_id,
        assignment_id=assignment_id,
        filename=filename,
        content=content,
        content_type=content_type,
        answers=answers or [],
        source=education.SubmissionRevisionSource.ONLINE.value,
        file_owner_id=student_id,
    )


def _persist_file_revision(
    *,
    student_id: str,
    assignment_id: str,
    filename: str,
    content: bytes,
    content_type: str | None,
    answers: list[dict],
    source: str,
    file_owner_id: str,
) -> education.SubmissionRevisionDTO:
    from backend.db.file_repository import save_file
    from backend.storage import get_storage

    sub = submission_repository.create_submission(
        assignment_id=assignment_id, student_id=student_id
    )
    question_map = _resolve_question_map(assignment_id)
    built = _build_answers(question_map, answers)
    revision = submission_repository.add_revision(
        submission_id=sub.id,
        student_id=student_id,
        source=source,
        file_name=filename,
        answers=built,
    )
    save_file(
        storage=get_storage(),
        owner_id=file_owner_id,
        kind="submission",
        original_name=filename,
        content=content,
        content_type=content_type,
        submission_revision_id=revision.id,
    )
    return revision


async def submit_student_file_with_ocr(
    *,
    student_id: str,
    assignment_id: str,
    filename: str,
    content: bytes,
    content_type: str | None,
    provider: "BaseProvider",
    ocr_skill: "OCRIngestSkill | None" = None,
) -> education.SubmissionRevisionDTO:
    """Create an immutable online revision populated from OCR/LLM parsing."""
    submission_repository.validate_submission_access(
        assignment_id=assignment_id, student_id=student_id
    )
    answers = await _parse_upload_answers(
        assignment_id=assignment_id,
        filename=filename,
        content=content,
        provider=provider,
        ocr_skill=ocr_skill,
    )
    return _persist_file_revision(
        student_id=student_id,
        assignment_id=assignment_id,
        filename=filename,
        content=content,
        content_type=content_type,
        answers=answers,
        source=education.SubmissionRevisionSource.ONLINE.value,
        file_owner_id=student_id,
    )


async def teacher_import_file_with_ocr(
    *,
    teacher_id: str,
    student_id: str,
    assignment_id: str,
    filename: str,
    content: bytes,
    content_type: str | None,
    provider: "BaseProvider",
    ocr_skill: "OCRIngestSkill | None" = None,
) -> education.SubmissionRevisionDTO:
    """Teacher upload for one enrolled student, persisted as teacher_import."""
    assignment = assignment_repository.get_assignment(
        assignment_id=assignment_id, actor_id=teacher_id
    )
    if assignment.status != education.AssignmentStatus.PUBLISHED.value:
        raise AssignmentClosed("assignment_closed")
    if not course_repository.is_enrolled(
        assignment.course_id, student_id=student_id
    ):
        raise ValidationError("student not enrolled")

    answers = await _parse_upload_answers(
        assignment_id=assignment_id,
        filename=filename,
        content=content,
        provider=provider,
        ocr_skill=ocr_skill,
    )
    return _persist_file_revision(
        student_id=student_id,
        assignment_id=assignment_id,
        filename=filename,
        content=content,
        content_type=content_type,
        answers=answers,
        source=education.SubmissionRevisionSource.TEACHER_IMPORT.value,
        file_owner_id=teacher_id,
    )


def get_revision(*, actor_id: str, revision_id: str) -> education.SubmissionRevisionDTO:
    return submission_repository.get_revision(revision_id=revision_id, actor_id=actor_id)


def get_submission(*, actor_id: str, submission_id: str, role: str) -> education.SubmissionDTO:
    return submission_repository.get_submission(submission_id=submission_id, actor_id=actor_id)


def list_submissions(*, actor_id: str, assignment_id: str, role: str) -> list[education.SubmissionDTO]:
    return submission_repository.list_submissions(assignment_id=assignment_id, actor_id=actor_id)
