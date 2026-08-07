from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, UploadFile
from pydantic import ValidationError as PydanticValidationError
from starlette.datastructures import Headers

from backend.api.task_preparation import (
    StartQuestionPreparationRequest,
    _read_source,
    _source_role_ocr_purpose,
    _validate_source_upload,
    preflight_problem_source,
    question_preparation_capabilities,
)
from backend.models import QuestionScorePolicy
from backend.skills.question_score import (
    InterpretedQuestionScore,
    InterpretedQuestionScorePlan,
    resolve_question_score_policy,
)


def _problems():
    return {
        "q1": {"q_id": "q1", "number": "1", "stem": "First question"},
        "q2": {"q_id": "q2", "number": "2", "stem": "Second question"},
    }


def test_question_preparation_request_defaults_to_reviewable_ten_points():
    request = StartQuestionPreparationRequest(
        source_tokens=["source-1"],
        expected_workflow_revision=0,
    )

    assert request.score_policy == QuestionScorePolicy()
    assert request.score_policy.mode == "default_10"


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "uniform"},
        {"mode": "uniform", "uniform_max_score": 0},
        {"mode": "per_question"},
        {"mode": "default_10", "uniform_max_score": 5},
        {"mode": "per_question", "per_question_text": "   "},
    ],
)
def test_question_score_policy_rejects_ambiguous_or_invalid_config(payload):
    with pytest.raises(PydanticValidationError):
        QuestionScorePolicy.model_validate(payload)


@pytest.mark.asyncio
async def test_default_and_uniform_score_policies_do_not_call_provider():
    provider = MagicMock()

    default = await resolve_question_score_policy(
        _problems(), QuestionScorePolicy(), provider
    )
    uniform = await resolve_question_score_policy(
        _problems(),
        QuestionScorePolicy(mode="uniform", uniform_max_score=5),
        provider,
    )

    assert {row.max_score for row in default.values()} == {10}
    assert {row.source for row in default.values()} == {"default_10"}
    assert {row.review_status for row in default.values()} == {"needs_review"}
    assert {row.issue_code for row in default.values()} == {
        "default_max_score_requires_review"
    }
    assert {row.max_score for row in uniform.values()} == {5}
    assert {row.source for row in uniform.values()} == {"uniform"}
    assert {row.review_status for row in uniform.values()} == {"confirmed"}
    provider.assert_not_called()


@pytest.mark.asyncio
async def test_per_question_policy_keeps_matches_and_flags_unmatched(monkeypatch):
    async def structured_call(*_args, **_kwargs):
        return (
            InterpretedQuestionScorePlan(scores=[
                InterpretedQuestionScore(q_id="q1", max_score=5),
                InterpretedQuestionScore(q_id="unknown", max_score=99),
            ]),
            SimpleNamespace(content="{}"),
        )

    monkeypatch.setattr(
        "backend.skills.question_score.structured_llm_call", structured_call
    )

    resolved = await resolve_question_score_policy(
        _problems(),
        QuestionScorePolicy(
            mode="per_question",
            per_question_text="第一题 5 分，第二题未说明。",
        ),
        MagicMock(),
    )

    assert resolved["q1"].max_score == 5
    assert resolved["q1"].source == "per_question_text"
    assert resolved["q1"].review_status == "needs_review"
    assert resolved["q2"].max_score == 10
    assert resolved["q2"].source == "default_10"
    assert resolved["q2"].issue_code == "max_score_not_found"


def _seed_question_task(owner_id: str, task_id: str):
    from backend.db import assignment_repository, workflow_repository
    from backend.db.models import AssignmentRecord, CourseRecord, UserRecord
    from backend.db.session import session_scope

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
        type="概念题",
        stem="Question",
        criterion="Reasoning 100%",
        max_score=10,
        source={
            "presentation": {
                "review_status": "needs_review",
                "max_score_source": "default_10",
                "max_score_review_status": "needs_review",
                "preparation_issues": [{
                    "field": "max_score",
                    "code": "default_max_score_requires_review",
                }],
            }
        },
    )


def test_task_problem_contract_serializes_and_edits_authoritative_max_score():
    from backend.services import task_facade

    owner_id = "score-owner"
    task_id = "score-task"
    _seed_question_task(owner_id, task_id)

    before = task_facade.get_task(task_id=task_id, owner_id=owner_id, full=True)
    assert before["problem_data"]["q1"]["max_score"] == 10
    assert before["problem_data"]["q1"]["max_score_source"] == "default_10"
    assert (
        before["problem_data"]["q1"]["max_score_review_status"]
        == "needs_review"
    )

    response = task_facade.update_problem(
        task_id=task_id,
        owner_id=owner_id,
        q_id="q1",
        patch={"max_score": 5},
    )

    problem = response["problem"]
    assert problem["max_score"] == 5
    assert problem["max_score_source"] == "teacher_edited"
    assert problem["max_score_review_status"] == "confirmed"
    assert not [
        issue
        for issue in problem["preparation_issues"]
        if issue.get("field") == "max_score"
    ]


def test_question_preparation_capabilities_expose_ocr_images_but_not_test_images():
    owner_id = "score-capability-owner"
    task_id = "score-capability-task"
    _seed_question_task(owner_id, task_id)
    registry = MagicMock()
    registry.pick_vision.return_value = MagicMock()

    capabilities = question_preparation_capabilities(
        task_id,
        current=SimpleNamespace(id=owner_id),
        registry=registry,
    )

    assert ".jpg" in capabilities["source_roles"]["problem"]["accepted_extensions"]
    assert ".webp" in capabilities["source_roles"]["rubric"]["accepted_extensions"]
    assert ".jpg" not in capabilities["source_roles"]["programming_tests"]["accepted_extensions"]
    assert capabilities["reader"]["images"] is True


def test_question_preparation_capabilities_hide_images_without_vision():
    owner_id = "score-no-vision-owner"
    task_id = "score-no-vision-task"
    _seed_question_task(owner_id, task_id)
    registry = MagicMock()
    registry.pick_vision.return_value = None

    capabilities = question_preparation_capabilities(
        task_id,
        current=SimpleNamespace(id=owner_id),
        registry=registry,
    )

    for role in ("problem", "reference_answer", "rubric"):
        assert ".png" not in capabilities["source_roles"][role]["accepted_extensions"]
    assert capabilities["source_roles"]["programming_tests"]["accepted_extensions"] == [
        ".pdf", ".txt", ".md", ".markdown", ".json"
    ]
    assert capabilities["reader"]["ocr"] is False
    assert capabilities["reader"]["images"] is False
    assert capabilities["reader"]["scanned_pdf"] is False


@pytest.mark.parametrize(
    ("role", "filename", "content_type", "has_vision", "error_code"),
    [
        ("problem", "questions.png", "image/png", True, None),
        ("reference_answer", "answers.webp", "image/webp", True, None),
        ("rubric", "rubric.jpg", "image/jpeg", True, None),
        ("problem", "questions.png", "image/png", False, "vision_provider_required"),
        ("programming_tests", "cases.png", "image/png", True, "source_type_not_allowed"),
        ("programming_tests", "cases.json", "application/json", False, None),
        ("rubric", "rubric.json", "application/json", True, "source_type_not_allowed"),
    ],
)
def test_source_upload_role_extension_matrix(
    role, filename, content_type, has_vision, error_code
):
    upload = UploadFile(
        file=io.BytesIO(b"payload"),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )

    if error_code is None:
        assert _validate_source_upload(
            upload, role=role, has_vision=has_vision
        ) == f".{filename.rsplit('.', 1)[1]}"
        return

    with pytest.raises(HTTPException) as exc:
        _validate_source_upload(upload, role=role, has_vision=has_vision)
    assert exc.value.detail["code"] == error_code


def test_source_upload_rejects_declared_mime_mismatch():
    upload = UploadFile(
        file=io.BytesIO(b"payload"),
        filename="questions.png",
        headers=Headers({"content-type": "text/plain"}),
    )

    with pytest.raises(HTTPException) as exc:
        _validate_source_upload(upload, role="problem", has_vision=True)

    assert exc.value.status_code == 415
    assert exc.value.detail["code"] == "source_mime_type_not_allowed"


class _FakeVisionProvider:
    provider_id = "vision-provider"
    provider_type = "test"
    supports_vision = True
    model = "vision-model"

    def __init__(self):
        self.calls = []

    async def ainvoke_vision(self, prompt, images):
        self.calls.append({"prompt": prompt, "images": images})
        return SimpleNamespace(
            content="Question 1: OCR result",
            provider=self.provider_id,
            model=self.model,
            duration_ms=1,
            input_tokens=5,
            output_tokens=5,
        )


class _VisionRegistry:
    def __init__(self):
        self.provider = _FakeVisionProvider()

    def pick_default(self):
        return self.provider

    def pick_vision(self, _preferred=None):
        return self.provider


@pytest.mark.parametrize(
    ("role", "filename", "prompt_fragment"),
    [
        ("problem", "questions.png", "数理题目 OCR"),
        ("reference_answer", "answers.png", "数理参考答案 OCR"),
    ],
)
@pytest.mark.asyncio
async def test_source_image_uses_role_specific_normalized_vision_ocr_path(
    role, filename, prompt_fragment
):
    registry = _VisionRegistry()
    upload = UploadFile(
        file=io.BytesIO(b"fake image bytes"),
        filename=filename,
        headers=Headers({"content-type": "image/png"}),
    )

    text, descriptor = await _read_source(
        file=upload,
        library_material_id=None,
        inline_text=None,
        owner_id="ocr-owner",
        registry=registry,
        role=role,
    )

    assert text == "Question 1: OCR result"
    assert descriptor["filename"] == filename
    assert len(registry.provider.calls) == 1
    assert registry.provider.calls[0]["images"][0].media_type == "image/png"
    assert prompt_fragment in registry.provider.calls[0]["prompt"]


def test_scanned_programming_test_document_uses_test_case_ocr_prompt():
    assert _source_role_ocr_purpose("programming_tests") == "test_cases"


@pytest.mark.asyncio
async def test_vision_off_image_fails_before_reading_or_creating_operation(monkeypatch):
    owner_id = "preflight-no-vision-owner"
    task_id = "preflight-no-vision-task"
    _seed_question_task(owner_id, task_id)
    stream = io.BytesIO(b"fake image bytes")
    upload = UploadFile(
        file=stream,
        filename="questions.png",
        headers=Headers({"content-type": "image/png"}),
    )
    registry = MagicMock()
    registry.pick_default.return_value = None
    registry.pick_vision.return_value = None
    create_operation = MagicMock(side_effect=AssertionError("must not enqueue"))
    monkeypatch.setattr(
        "backend.api.task_preparation.workflow_repository.create_operation",
        create_operation,
    )

    with pytest.raises(HTTPException) as exc:
        await preflight_problem_source(
            task_id=task_id,
            file=upload,
            library_material_id=None,
            inline_text=None,
            structure_mode="organized",
            role="problem",
            extraction_hint="",
            save_to_library=False,
            current=SimpleNamespace(id=owner_id),
            registry=registry,
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "vision_provider_required"
    assert stream.tell() == 0
    create_operation.assert_not_called()


@pytest.mark.asyncio
async def test_vision_off_scanned_pdf_returns_stable_preflight_error():
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    document.new_page(width=300, height=200)
    upload = UploadFile(
        file=io.BytesIO(document.tobytes()),
        filename="scanned.pdf",
        headers=Headers({"content-type": "application/pdf"}),
    )
    document.close()
    registry = MagicMock()
    registry.pick_default.return_value = None
    registry.pick_vision.return_value = None

    with pytest.raises(HTTPException) as exc:
        await _read_source(
            file=upload,
            library_material_id=None,
            inline_text=None,
            owner_id="scan-owner",
            registry=registry,
            role="problem",
        )

    assert exc.value.status_code == 422
    assert exc.value.detail == {
        "code": "vision_provider_required",
        "role": "problem",
        "filename": "scanned.pdf",
        "recovery": "configure_vision_provider",
    }


def test_confirming_question_also_confirms_default_max_score():
    from backend.services import task_facade

    owner_id = "confirm-score-owner"
    task_id = "confirm-score-task"
    _seed_question_task(owner_id, task_id)

    response = task_facade.update_problem(
        task_id=task_id,
        owner_id=owner_id,
        q_id="q1",
        patch={"review_status": "confirmed"},
    )

    assert response["problem"]["max_score"] == 10
    assert response["problem"]["max_score_source"] == "default_10"
    assert response["problem"]["max_score_review_status"] == "confirmed"
