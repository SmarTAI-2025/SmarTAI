from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.db import assignment_repository, course_repository
from backend.db.file_repository import list_files
from backend.db.models import UserRecord
from backend.db.session import session_scope
from backend.services import assignments as assignment_service
from backend.services import submissions as submission_service
from backend.skills.ocr_ingest import OCRResult


class FakeOCRSkill:
    def __init__(self, text: str):
        self.text = text
        self.calls = []

    async def recognize_images(self, images, purpose):
        self.calls.append({"images": images, "purpose": purpose})
        return OCRResult(text=self.text, provider="fake:ocr")


def _provider():
    provider = MagicMock()
    provider.provider_id = "mock:text"
    provider.supports_vision = False
    return provider


def _seed_user(user_id: str, role: str) -> None:
    with session_scope() as session:
        session.add(
            UserRecord(
                id=user_id,
                username=user_id,
                email=f"{user_id}@test.local",
                role=role,
                password_hash="hash",
                is_active=True,
            )
        )


def _published_assignment(suffix: str) -> tuple[str, str, str]:
    teacher_id = f"teacher_{suffix}"
    student_id = f"student_{suffix}"
    _seed_user(teacher_id, "teacher")
    _seed_user(student_id, "student")
    course = course_repository.create_course(
        teacher_id=teacher_id, name=f"OCR course {suffix}"
    )
    course_repository.enroll(
        course.id, student_id=student_id, actor_id=teacher_id
    )
    assignment = assignment_repository.create_assignment(
        teacher_id=teacher_id,
        course_id=course.id,
        name=f"OCR assignment {suffix}",
    )
    assignment_repository.add_question(
        assignment.id,
        teacher_id=teacher_id,
        q_id="q1",
        order_index=0,
        number="1",
        type="short",
        stem="Question",
        criterion="rubric",
    )
    assignment_repository.publish(
        assignment.id, teacher_id=teacher_id, expected_version=1
    )
    return teacher_id, student_id, assignment.id


@pytest.mark.asyncio
async def test_problem_upload_ocr_persists_normalized_questions(monkeypatch):
    teacher_id = "teacher_ocr_questions"
    _seed_user(teacher_id, "teacher")
    course = course_repository.create_course(
        teacher_id=teacher_id, name="OCR questions"
    )
    assignment = assignment_repository.create_assignment(
        teacher_id=teacher_id,
        course_id=course.id,
        name="Imported questions",
    )
    seen = {}

    async def fake_extract(text, provider, store, reporter=None):
        seen["text"] = text
        store["q1"] = {
            "q_id": "q1",
            "number": "1",
            "type": "calculation",
            "stem": text,
            "criterion": "Show the calculation",
        }
        return store

    monkeypatch.setattr(
        "backend.services.assignments.extract_problems", fake_extract
    )
    ocr = FakeOCRSkill("OCR problem text")

    created = await assignment_service.import_questions_from_upload(
        assignment_id=assignment.id,
        teacher_id=teacher_id,
        filename="problems.png",
        content=b"fake image bytes",
        content_type="image/png",
        provider=_provider(),
        ocr_skill=ocr,
    )

    assert [question.q_id for question in created] == ["q1"]
    assert created[0].stem == "OCR problem text"
    assert created[0].source == {
        "origin": "file_upload",
        "filename": "problems.png",
    }
    assert seen["text"] == "OCR problem text"
    assert ocr.calls[0]["purpose"] == "problems"
    stored_files = list_files(
        owner_id=teacher_id, assignment_id=assignment.id
    )
    assert [item.original_name for item in stored_files] == ["problems.png"]


@pytest.mark.asyncio
async def test_student_upload_ocr_creates_answer_revision(monkeypatch):
    _teacher_id, student_id, assignment_id = _published_assignment(
        "student_upload"
    )
    seen = {}

    async def fake_parse(
        files_data, problems_data, student_store, provider, reporter=None
    ):
        seen["files_data"] = files_data
        seen["problems_data"] = problems_data
        student_store["ocr_identity_is_not_trusted"] = {
            "stu_id": "ocr_identity_is_not_trusted",
            "stu_name": "OCR Name",
            "stu_ans": [
                {
                    "q_id": "q1",
                    "number": "1",
                    "type": "short",
                    "content": "OCR answer text",
                    "flag": [],
                }
            ],
        }
        return student_store

    monkeypatch.setattr(
        "backend.services.submissions.parse_student_answers", fake_parse
    )
    ocr = FakeOCRSkill("OCR answer text")

    revision = await submission_service.submit_student_file_with_ocr(
        student_id=student_id,
        assignment_id=assignment_id,
        filename="answer.png",
        content=b"fake image bytes",
        content_type="image/png",
        provider=_provider(),
        ocr_skill=ocr,
    )

    assert revision.source == "online"
    assert revision.answers[0].q_id == "q1"
    assert revision.answers[0].content == "OCR answer text"
    assert seen["problems_data"]["q1"]["stem"] == "Question"
    assert "OCR answer text" in seen["files_data"][0]["content"]
    assert ocr.calls[0]["purpose"] == "submissions"
    stored_files = list_files(
        owner_id=student_id, submission_revision_id=revision.id
    )
    assert [item.original_name for item in stored_files] == ["answer.png"]


@pytest.mark.asyncio
async def test_teacher_upload_uses_selected_student_and_teacher_import_source(
    monkeypatch,
):
    teacher_id, student_id, assignment_id = _published_assignment(
        "teacher_upload"
    )

    async def fake_parse(
        files_data, problems_data, student_store, provider, reporter=None
    ):
        student_store["filename-derived-id"] = {
            "stu_id": "filename-derived-id",
            "stu_name": "Ignored OCR identity",
            "stu_ans": [
                {
                    "q_id": "q1",
                    "number": "1",
                    "type": "short",
                    "content": "teacher imported answer",
                    "flag": [],
                }
            ],
        }
        return student_store

    monkeypatch.setattr(
        "backend.services.submissions.parse_student_answers", fake_parse
    )

    revision = await submission_service.teacher_import_file_with_ocr(
        teacher_id=teacher_id,
        student_id=student_id,
        assignment_id=assignment_id,
        filename="answer.txt",
        content=b"teacher imported answer",
        content_type="text/plain",
        provider=_provider(),
    )

    assert revision.source == "teacher_import"
    assert revision.answers[0].content == "teacher imported answer"
    stored_files = list_files(
        owner_id=teacher_id, submission_revision_id=revision.id
    )
    assert [item.original_name for item in stored_files] == ["answer.txt"]
