from __future__ import annotations

import hashlib
import io
import json
import os
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from backend.api import task_preparation, tasks as tasks_api
from backend.auth import require_teacher
from backend.db import (
    course_library_repository,
    file_repository,
    source_outcome_repository,
    workflow_repository,
)
from backend.db.models import (
    AssignmentRecord,
    CourseRecord,
    KnowledgeDocumentRecord,
    StoredFileRecord,
    UserRecord,
)
from backend.db.session import configure_database, session_scope
from backend.domain.errors import DomainError, SourceStorageQuotaExceeded
from backend.models import User
from backend.services import source_files, task_facade
from backend.storage import get_storage
from backend.storage.base import (
    StorageBackend,
    StorageObjectNotFound,
    StorageUnavailable,
)
from backend.storage.local import LocalStorage


PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF"
PDF_TWO = b"%PDF-1.5\n2 0 obj<</Type/Catalog>>endobj\n%%EOF"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00preview\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\npreview"
WEBP = b"RIFF\x08\x00\x00\x00WEBPVP8 preview"


class _Registry:
    provider = SimpleNamespace(
        provider_id="test-provider",
        supports_vision=False,
    )

    def pick_default(self):
        return self.provider

    def pick_default_id(self):
        return self.provider.provider_id

    def pick_vision(self, _preferred=None):
        return None

    def get(self, provider_id):
        return self.provider if provider_id == self.provider.provider_id else None

    def uses_shared_pool(self):
        return True

    def list_configs(self):
        return [{
            "provider_id": self.provider.provider_id,
            "provider_type": "openai",
            "model": "test-model",
            "enabled": True,
            "is_default": True,
            "is_shared": False,
            "scope": "owner",
        }]


class _VisionRegistry(_Registry):
    vision = SimpleNamespace(
        provider_id="vision-provider", supports_vision=True
    )
    provider = vision

    def pick_vision(self, _preferred=None):
        return self.vision


def _seed_task(owner_id: str, task_id: str) -> None:
    course_id = f"course-{task_id}"
    with session_scope() as session:
        session.merge(UserRecord(
            id=owner_id,
            username=owner_id,
            password_hash="hash",
            role="teacher",
            is_active=True,
        ))
        session.flush()
        session.merge(CourseRecord(
            id=course_id,
            name="Course",
            code=course_id,
            teacher_id=owner_id,
        ))
        session.flush()
        session.merge(AssignmentRecord(
            id=task_id,
            course_id=course_id,
            teacher_id=owner_id,
            name="Assignment",
            status="draft",
            version=1,
        ))
    workflow_repository.ensure_workflow(
        assignment_id=task_id, owner_id=owner_id
    )


def _create_problem_selection(
    *,
    owner_id: str,
    task_id: str,
    storage: StorageBackend,
    content: bytes = PDF,
    filename: str = "题目.pdf",
):
    source_operation, _ = workflow_repository.create_operation(
        assignment_id=task_id,
        owner_id=owner_id,
        operation_type="problem_source",
        input_hash=uuid.uuid4().hex,
        payload={},
        initial_status="preparing",
        expires_at=time.time() + 3600,
    )
    stored = file_repository.save_file(
        storage=storage,
        owner_id=owner_id,
        kind="problem_source",
        original_name=filename,
        content=content,
        content_type="application/pdf",
        assignment_id=task_id,
    )
    source, _ = source_outcome_repository.register_source(
        owner_id=owner_id,
        assignment_id=task_id,
        operation_id=source_operation.id,
        expected_attempt=source_operation.attempt,
        order_index=0,
        stored_file_id=stored.id,
    )
    ref = {
        "role": "problem",
        "source_kind": "upload",
        "display_name": filename,
        "source_id": source.id,
        "stored_file_id": stored.id,
        "source_operation_id": source_operation.id,
        "source_attempt": source_operation.attempt,
        "library_material_id": None,
        "knowledge_document_id": None,
    }
    source_operation = workflow_repository.save_operation_checkpoint(
        source_operation.id,
        owner_id=owner_id,
        expected_attempt=source_operation.attempt,
        expected_checkpoint_revision=source_operation.checkpoint_revision,
        stage="problem_source_saved",
        checkpoint={"source_refs": [ref]},
        artifact_refs=[stored.id],
    )
    workflow_repository.update_operation(
        source_operation.id,
        owner_id=owner_id,
        expected_attempt=source_operation.attempt,
        status="ready",
        payload={"source_ref": ref},
    )
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    job, _ = workflow_repository.create_operation(
        assignment_id=task_id,
        owner_id=owner_id,
        operation_type="question_preparation",
        input_hash=uuid.uuid4().hex,
        payload={"source_refs": [ref]},
        expires_at=time.time() + 3600,
    )
    job = workflow_repository.save_operation_checkpoint(
        job.id,
        owner_id=owner_id,
        expected_attempt=job.attempt,
        expected_checkpoint_revision=job.checkpoint_revision,
        stage="problem_sources_selected",
        checkpoint={"source_refs": [ref]},
        artifact_refs=[stored.id],
    )
    claimed_revision = task_facade.claim_workflow_operation_atomic(
        task_id=task_id,
        owner_id=owner_id,
        operation_id=job.id,
        expected_operation_attempt=job.attempt,
        expected_workflow_revision=workflow.workflow_revision,
        workflow_changes={
            "presentation_status": "extracting_problems",
            "active_operation": "question_preparation",
            "active_job_id": job.id,
            "extract_job_id": job.id,
            "problem_file_name": filename,
        },
    )
    return SimpleNamespace(
        source_operation=source_operation,
        job=job,
        stored=stored,
        source=source,
        ref=ref,
        claimed_revision=claimed_revision,
    )


def _create_submission_selection(
    *,
    owner_id: str,
    task_id: str,
    storage: StorageBackend,
    files: list[tuple[str, bytes, str]],
    input_hash: str | None = None,
    operation=None,
):
    if operation is None:
        operation, _ = workflow_repository.create_operation(
            assignment_id=task_id,
            owner_id=owner_id,
            operation_type="submission_recognition",
            input_hash=input_hash or uuid.uuid4().hex,
            payload={},
            expires_at=time.time() + 3600,
        )
    sources = []
    stored_files = []
    for index, (filename, content, kind) in enumerate(files):
        stored = file_repository.save_file(
            storage=storage,
            owner_id=owner_id,
            kind=kind,
            original_name=filename,
            content=content,
            content_type=(
                "application/vnd.smartai.archive-member-reference+json"
                if kind == "submission_source_reference"
                else "application/pdf"
            ),
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
        stored_files.append(stored)
        sources.append(source)
    operation = workflow_repository.save_operation_checkpoint(
        operation.id,
        owner_id=owner_id,
        expected_attempt=operation.attempt,
        expected_checkpoint_revision=operation.checkpoint_revision,
        stage="submission_sources_saved",
        checkpoint={"source_ids": [source.id for source in sources]},
        artifact_refs=[stored.id for stored in stored_files],
    )
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    claimed_revision = task_facade.claim_workflow_operation_atomic(
        task_id=task_id,
        owner_id=owner_id,
        operation_id=operation.id,
        expected_operation_attempt=operation.attempt,
        expected_workflow_revision=workflow.workflow_revision,
        workflow_changes={
            "presentation_status": "parsing_submissions",
            "active_operation": "submission_recognition",
            "active_job_id": operation.id,
            "parse_job_id": operation.id,
        },
    )
    return SimpleNamespace(
        operation=operation,
        sources=sources,
        stored_files=stored_files,
        claimed_revision=claimed_revision,
    )


def _client(monkeypatch, *, owner_id: str, storage: StorageBackend) -> TestClient:
    api = FastAPI()
    api.include_router(tasks_api.router)
    api.dependency_overrides[require_teacher] = lambda: User(
        id=owner_id, username=owner_id, role="teacher"
    )
    monkeypatch.setattr(tasks_api, "get_storage", lambda: storage)
    return TestClient(api)


@pytest.mark.asyncio
async def test_formal_preflight_persists_pdf_and_source_before_extraction_failure(
    monkeypatch,
):
    owner_id = "preflight-owner"
    task_id = "preflight-task"
    _seed_task(owner_id, task_id)
    storage = get_storage()
    observed: dict[str, str] = {}

    async def fail_after_asserting_persistence(*_args, **_kwargs):
        files = file_repository.list_files(
            owner_id=owner_id, assignment_id=task_id
        )
        assert len(files) == 1
        with session_scope() as session:
            operation = session.query(
                workflow_repository.WorkflowOperationRecord
            ).filter_by(operation_type="problem_source").one()
        sources = source_outcome_repository.list_sources(
            operation_id=operation.id,
            owner_id=owner_id,
            attempt=operation.attempt,
        )
        assert sources[0].stored_file_id == files[0].id
        assert operation.artifact_refs == [files[0].id]
        with storage.open(files[0].storage_key) as stream:
            assert stream.read() == PDF
        observed["file_id"] = files[0].id
        raise RuntimeError("injected OCR detail /private/secret")

    monkeypatch.setattr(
        task_preparation, "extract_text_from_upload", fail_after_asserting_persistence
    )
    upload = UploadFile(
        file=io.BytesIO(PDF),
        filename="questions.pdf",
        headers=Headers({"content-type": "application/pdf"}),
    )

    with pytest.raises(HTTPException) as failure:
        await task_preparation.preflight_problem_source(
            task_id=task_id,
            file=upload,
            library_material_id=None,
            inline_text=None,
            structure_mode="organized",
            role="problem",
            extraction_hint="",
            save_to_library=False,
            current=SimpleNamespace(id=owner_id),
            registry=_Registry(),
        )

    assert failure.value.status_code == 503
    assert observed["file_id"]
    operation = workflow_repository.get_operation(
        next(
            row.id
            for row in _operations(owner_id)
            if row.operation_type == "problem_source"
        ),
        owner_id=owner_id,
    )
    assert operation.status == "error"
    assert operation.artifact_refs == [observed["file_id"]]
    assert "/private/secret" not in json.dumps(failure.value.detail)


def _operations(owner_id: str):
    with session_scope() as session:
        rows = session.query(workflow_repository.WorkflowOperationRecord).filter_by(
            owner_id=owner_id
        ).all()
        return [
            SimpleNamespace(
                id=row.id,
                operation_type=row.operation_type,
                attempt=row.attempt,
            )
            for row in rows
        ]


@pytest.mark.asyncio
async def test_question_job_copies_source_refs_and_survives_failure_and_restart(
    monkeypatch,
):
    owner_id = "question-owner"
    task_id = "question-task"
    _seed_task(owner_id, task_id)

    async def extracted(*_args, **_kwargs):
        return "1. What is 1 + 1?"

    monkeypatch.setattr(task_preparation, "extract_text_from_upload", extracted)
    result = await task_preparation.preflight_problem_source(
        task_id=task_id,
        file=UploadFile(
            file=io.BytesIO(PDF),
            filename="原题.pdf",
            headers=Headers({"content-type": "application/pdf"}),
        ),
        library_material_id=None,
        inline_text=None,
        structure_mode="organized",
        role="problem",
        extraction_hint="",
        save_to_library=False,
        current=SimpleNamespace(id=owner_id),
        registry=_Registry(),
    )
    started = await task_preparation.start_question_preparation(
        task_id=task_id,
        request=task_preparation.StartQuestionPreparationRequest(
            source_tokens=[result["source_token"]],
            expected_workflow_revision=0,
        ),
        background_tasks=BackgroundTasks(),
        current=SimpleNamespace(id=owner_id),
        registry=_Registry(),
    )

    job = workflow_repository.get_operation(started["job_id"], owner_id=owner_id)
    assert job.payload["source_refs"] == job.checkpoint["source_refs"]
    assert job.checkpoint["source_ids"]
    assert job.checkpoint["stored_file_ids"] == job.artifact_refs
    descriptor = source_files.describe_source_files(
        task_id=task_id, owner_id=owner_id, storage=get_storage()
    )
    assert descriptor["problem_source"]["status"] == "available"
    assert descriptor["problem_source"]["file_id"] == job.artifact_refs[0]

    assert task_facade._fail_operation(
        task_id,
        owner_id,
        job.id,
        job.attempt,
        "problem_extraction_failed",
    )
    configure_database(os.environ["SMARTAI_DATABASE_URL"])
    recreated_storage = LocalStorage(get_storage().root)
    after_restart = source_files.describe_source_files(
        task_id=task_id, owner_id=owner_id, storage=recreated_storage
    )
    assert after_restart["problem_source"] == descriptor["problem_source"]
    content = source_files.read_source_file_content(
        task_id=task_id,
        file_id=job.artifact_refs[0],
        owner_id=owner_id,
        storage=recreated_storage,
    )
    assert content.content == PDF


@pytest.mark.asyncio
async def test_formal_image_preflight_persists_original_before_ocr(monkeypatch):
    owner_id = "image-owner"
    task_id = "image-task"
    _seed_task(owner_id, task_id)
    observed = False

    async def extracted(*_args, **_kwargs):
        nonlocal observed
        files = file_repository.list_files(
            owner_id=owner_id, assignment_id=task_id
        )
        assert len(files) == 1
        with get_storage().open(files[0].storage_key) as stream:
            assert stream.read() == PNG
        observed = True
        return "1. Image question"

    monkeypatch.setattr(task_preparation, "extract_text_from_upload", extracted)
    result = await task_preparation.preflight_problem_source(
        task_id=task_id,
        file=UploadFile(
            file=io.BytesIO(PNG),
            filename="questions.png",
            headers=Headers({"content-type": "image/png"}),
        ),
        library_material_id=None,
        inline_text=None,
        structure_mode="organized",
        role="problem",
        extraction_hint="",
        save_to_library=False,
        current=SimpleNamespace(id=owner_id),
        registry=_VisionRegistry(),
    )

    assert observed is True
    operation = workflow_repository.get_operation(
        result["source_token"], owner_id=owner_id
    )
    assert operation.status == "ready"
    assert operation.artifact_refs


@pytest.mark.asyncio
async def test_preflight_storage_failure_never_reports_ready(monkeypatch):
    owner_id = "save-failure-owner"
    task_id = "save-failure-task"
    _seed_task(owner_id, task_id)

    def fail_save(**_kwargs):
        raise RuntimeError("bucket=/secret-bucket")

    async def must_not_extract(*_args, **_kwargs):
        raise AssertionError("extraction must not run after persistence failure")

    monkeypatch.setattr(source_files, "persist_problem_source", fail_save)
    monkeypatch.setattr(
        task_preparation, "extract_text_from_upload", must_not_extract
    )
    with pytest.raises(HTTPException) as failure:
        await task_preparation.preflight_problem_source(
            task_id=task_id,
            file=UploadFile(
                file=io.BytesIO(PDF),
                filename="questions.pdf",
                headers=Headers({"content-type": "application/pdf"}),
            ),
            library_material_id=None,
            inline_text=None,
            structure_mode="organized",
            role="problem",
            extraction_hint="",
            save_to_library=False,
            current=SimpleNamespace(id=owner_id),
            registry=_Registry(),
        )

    assert failure.value.status_code == 503
    assert failure.value.detail == {"code": "source_persistence_failed"}
    assert not file_repository.list_files(
        owner_id=owner_id, assignment_id=task_id
    )
    source_operation = workflow_repository.get_operation(
        _operations(owner_id)[0].id, owner_id=owner_id
    )
    assert source_operation.status == "error"


@pytest.mark.asyncio
async def test_preflight_quota_error_keeps_its_stable_contract(monkeypatch):
    owner_id = "quota-preflight-owner"
    task_id = "quota-preflight-task"
    _seed_task(owner_id, task_id)

    def reject_quota(**_kwargs):
        raise SourceStorageQuotaExceeded(
            "The task-original storage allocation is full.",
            details={
                "used_bytes": 10,
                "limit_bytes": 10,
                "requested_bytes": len(PDF),
            },
        )

    monkeypatch.setattr(source_files, "persist_problem_source", reject_quota)
    response = await task_preparation.preflight_problem_source(
        task_id=task_id,
        file=UploadFile(
            file=io.BytesIO(PDF),
            filename="questions.pdf",
            headers=Headers({"content-type": "application/pdf"}),
        ),
        library_material_id=None,
        inline_text=None,
        structure_mode="organized",
        role="problem",
        extraction_hint="",
        save_to_library=False,
        current=SimpleNamespace(id=owner_id),
        registry=_Registry(),
    )

    assert response.status_code == 413
    payload = json.loads(response.body)
    assert payload["error"]["code"] == "source_storage_quota_exceeded"
    assert payload["error"]["details"] == {
        "used_bytes": 10,
        "limit_bytes": 10,
        "requested_bytes": len(PDF),
    }
    source_operation = workflow_repository.get_operation(
        _operations(owner_id)[0].id, owner_id=owner_id
    )
    assert source_operation.status == "error"
    assert source_operation.error_code == "source_storage_quota_exceeded"


@pytest.mark.asyncio
async def test_formal_preflight_rejects_disguised_pdf_before_persistence():
    owner_id = "signature-owner"
    task_id = "signature-task"
    _seed_task(owner_id, task_id)
    disguised = b"<!doctype html><script>alert(1)</script>%PDF-1.4"

    with pytest.raises(HTTPException) as failure:
        await task_preparation.preflight_problem_source(
            task_id=task_id,
            file=UploadFile(
                file=io.BytesIO(disguised),
                filename="questions.pdf",
                headers=Headers({"content-type": "application/pdf"}),
            ),
            library_material_id=None,
            inline_text=None,
            structure_mode="organized",
            role="problem",
            extraction_hint="",
            save_to_library=False,
            current=SimpleNamespace(id=owner_id),
            registry=_Registry(),
        )

    assert failure.value.status_code == 415
    assert failure.value.detail["code"] == "source_content_type_not_allowed"
    assert not _operations(owner_id)


@pytest.mark.parametrize(
    ("content", "filename", "mime_type", "preview_kind"),
    [
        (PDF, "试题.pdf", "application/pdf", "pdf"),
        (JPEG, "answer.jpg", "image/jpeg", "image"),
        (PNG, "answer.png", "image/png", "image"),
        (WEBP, "answer.webp", "image/webp", "image"),
    ],
)
def test_content_route_returns_true_mime_and_frozen_security_headers(
    tmp_path, monkeypatch, content, filename, mime_type, preview_kind
):
    owner_id = "header-owner"
    task_id = "header-task"
    _seed_task(owner_id, task_id)
    storage = LocalStorage(tmp_path / "storage")
    current = _create_problem_selection(
        owner_id=owner_id,
        task_id=task_id,
        storage=storage,
        content=content,
        filename=filename,
    )
    client = _client(monkeypatch, owner_id=owner_id, storage=storage)

    described = client.get(f"/tasks/{task_id}/source-files")
    assert described.status_code == 200
    descriptor = described.json()["problem_source"]
    assert descriptor == {
        "source_id": current.source.id,
        "file_id": current.stored.id,
        "display_name": filename,
        "mime_type": mime_type,
        "size_bytes": len(content),
        "status": "available",
        "preview_kind": preview_kind,
        "unavailable_reason": None,
    }
    assert all(
        secret not in described.text
        for secret in ("storage_key", "storage_backend", "sha256", str(tmp_path))
    )

    response = client.get(
        f"/tasks/{task_id}/source-files/{current.stored.id}/content"
    )
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"] == mime_type
    assert response.headers["content-disposition"].startswith(
        "inline; filename*=UTF-8''"
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-length"] == str(len(content))


@pytest.mark.parametrize(
    ("content", "filename"),
    [
        (b"<!doctype html><html><script>x</script></html>", "fake.pdf"),
        (b"<?xml version='1.0'?><svg><script>x</script></svg>", "fake.png"),
        (b"PK\x03\x04archive", "fake.pdf"),
        (b"plain text pretending to be pdf", "fake.pdf"),
        (b"plain text pretending to be png", "fake.png"),
        (b"<html>x</html>%PDF-1.4", "polyglot.pdf"),
    ],
)
def test_active_or_disguised_current_sources_are_never_inlined(
    tmp_path, monkeypatch, content, filename
):
    owner_id = "unsupported-owner"
    task_id = "unsupported-task"
    _seed_task(owner_id, task_id)
    storage = LocalStorage(tmp_path / "storage")
    current = _create_problem_selection(
        owner_id=owner_id,
        task_id=task_id,
        storage=storage,
        content=content,
        filename=filename,
    )
    client = _client(monkeypatch, owner_id=owner_id, storage=storage)

    descriptor = client.get(f"/tasks/{task_id}/source-files").json()[
        "problem_source"
    ]
    assert descriptor["status"] == "unavailable"
    assert descriptor["preview_kind"] == "unsupported"
    assert descriptor["unavailable_reason"] == "unsupported_type"
    response = client.get(
        f"/tasks/{task_id}/source-files/{current.stored.id}/content"
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "source_preview_unsupported_type"
    assert response.headers.get("content-type", "").startswith("application/json")


def test_owner_task_current_source_and_non_source_boundaries_are_uniform_404(
    tmp_path, monkeypatch
):
    storage = LocalStorage(tmp_path / "storage")
    _seed_task("teacher-a", "task-a")
    _seed_task("teacher-a", "task-a2")
    _seed_task("teacher-b", "task-b")
    current_a = _create_problem_selection(
        owner_id="teacher-a", task_id="task-a", storage=storage
    )
    current_a2 = _create_problem_selection(
        owner_id="teacher-a", task_id="task-a2", storage=storage, content=PDF_TWO
    )

    artifact = file_repository.save_file(
        storage=storage,
        owner_id="teacher-a",
        kind="ocr_artifact",
        original_name="ocr.json",
        content=b"{}",
        content_type="application/json",
        assignment_id="task-a",
    )
    document_id = "doc-private"
    with session_scope() as session:
        session.add(KnowledgeDocumentRecord(
            id=document_id,
            owner_id="teacher-a",
            stored_file_id=None,
            title="Private",
            original_name="private.pdf",
            content_type="application/pdf",
            size_bytes=len(PDF),
            sha256=hashlib.sha256(PDF).hexdigest(),
            status="ready",
            parser_version="v1",
            chunk_count=1,
        ))
    library_file = file_repository.save_file(
        storage=storage,
        owner_id="teacher-a",
        kind="personal_knowledge",
        original_name="private.pdf",
        content=PDF,
        content_type="application/pdf",
        knowledge_document_id=document_id,
    )
    with session_scope() as session:
        document = session.get(KnowledgeDocumentRecord, document_id)
        assert document is not None
        document.stored_file_id = library_file.id

    owner_client = _client(
        monkeypatch, owner_id="teacher-a", storage=storage
    )
    ok = owner_client.get(
        f"/tasks/task-a/source-files/{current_a.stored.id}/content"
    )
    assert ok.status_code == 200
    expected = owner_client.get(
        "/tasks/task-a/source-files/not-a-file/content"
    ).json()
    for file_id in (
        current_a2.stored.id,
        artifact.id,
        library_file.id,
    ):
        response = owner_client.get(
            f"/tasks/task-a/source-files/{file_id}/content"
        )
        assert response.status_code == 404
        assert response.json() == expected

    other_client = _client(
        monkeypatch, owner_id="teacher-b", storage=storage
    )
    wrong_owner = other_client.get(
        f"/tasks/task-a/source-files/{current_a.stored.id}/content"
    )
    wrong_descriptor = other_client.get("/tasks/task-a/source-files")
    assert wrong_owner.status_code == wrong_descriptor.status_code == 404
    assert wrong_owner.json() == wrong_descriptor.json() == expected


def test_missing_object_is_unavailable_and_content_is_not_empty_200(
    tmp_path, monkeypatch
):
    owner_id = "missing-owner"
    task_id = "missing-task"
    _seed_task(owner_id, task_id)
    storage = LocalStorage(tmp_path / "storage")
    current = _create_problem_selection(
        owner_id=owner_id, task_id=task_id, storage=storage
    )
    storage.delete(current.stored.storage_key)
    client = _client(monkeypatch, owner_id=owner_id, storage=storage)

    descriptor = client.get(f"/tasks/{task_id}/source-files").json()[
        "problem_source"
    ]
    assert descriptor["status"] == "unavailable"
    assert descriptor["unavailable_reason"] == "missing"
    response = client.get(
        f"/tasks/{task_id}/source-files/{current.stored.id}/content"
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "source_unavailable_missing"


@pytest.mark.parametrize(
    ("lifecycle_status", "reason", "expected_status", "expected_reason"),
    [
        ("cleanup_pending", "task_finalized", "cleanup_pending", "cleanup_pending"),
        ("unavailable", "task_finalized", "unavailable", "task_finalized"),
    ],
)
def test_descriptor_storage_race_projects_current_lifecycle_state(
    tmp_path,
    monkeypatch,
    lifecycle_status,
    reason,
    expected_status,
    expected_reason,
):
    owner_id = f"descriptor-race-{lifecycle_status}"
    task_id = f"descriptor-race-task-{lifecycle_status}"
    _seed_task(owner_id, task_id)
    storage = LocalStorage(tmp_path / lifecycle_status)
    current = _create_problem_selection(
        owner_id=owner_id, task_id=task_id, storage=storage
    )

    def finalize_during_open(_key):
        with session_scope() as session:
            row = session.get(StoredFileRecord, current.stored.id)
            assert row is not None
            row.availability_status = lifecycle_status
            row.availability_reason = reason
            if lifecycle_status == "unavailable":
                row.unavailable_at = time.time()
        raise StorageObjectNotFound("cleanup won the race")

    monkeypatch.setattr(storage, "open", finalize_during_open)
    client = _client(monkeypatch, owner_id=owner_id, storage=storage)

    response = client.get(f"/tasks/{task_id}/source-files")

    assert response.status_code == 200
    descriptor = response.json()["problem_source"]
    assert descriptor["status"] == expected_status
    assert descriptor["unavailable_reason"] == expected_reason


@pytest.mark.parametrize(
    ("lifecycle_status", "expected_http", "expected_code"),
    [
        ("cleanup_pending", 409, "source_cleanup_pending"),
        ("unavailable", 410, "source_unavailable_task_finalized"),
    ],
)
def test_content_storage_race_projects_current_lifecycle_error(
    tmp_path, monkeypatch, lifecycle_status, expected_http, expected_code,
):
    owner_id = f"content-race-{lifecycle_status}"
    task_id = f"content-race-task-{lifecycle_status}"
    _seed_task(owner_id, task_id)
    storage = LocalStorage(tmp_path / lifecycle_status)
    current = _create_problem_selection(
        owner_id=owner_id, task_id=task_id, storage=storage
    )
    original_open = storage.open
    open_count = 0

    def finalize_during_content_open(key):
        nonlocal open_count
        open_count += 1
        if open_count == 1:
            return original_open(key)
        with session_scope() as session:
            row = session.get(StoredFileRecord, current.stored.id)
            assert row is not None
            row.availability_status = lifecycle_status
            row.availability_reason = "task_finalized"
            if lifecycle_status == "unavailable":
                row.unavailable_at = time.time()
        raise StorageObjectNotFound("cleanup won the content race")

    monkeypatch.setattr(storage, "open", finalize_during_content_open)
    client = _client(monkeypatch, owner_id=owner_id, storage=storage)

    response = client.get(
        f"/tasks/{task_id}/source-files/{current.stored.id}/content"
    )

    assert response.status_code == expected_http
    assert response.json()["error"]["code"] == expected_code


def test_content_disposition_strips_paths_and_header_controls(
    tmp_path, monkeypatch
):
    owner_id = "filename-owner"
    task_id = "filename-task"
    _seed_task(owner_id, task_id)
    storage = LocalStorage(tmp_path / "storage")
    current = _create_problem_selection(
        owner_id=owner_id,
        task_id=task_id,
        storage=storage,
        filename="../folder\\unsafe\r\nInjected.pdf",
    )
    client = _client(monkeypatch, owner_id=owner_id, storage=storage)

    descriptor = client.get(f"/tasks/{task_id}/source-files").json()[
        "problem_source"
    ]
    assert descriptor["display_name"] == "unsafeInjected.pdf"
    response = client.get(
        f"/tasks/{task_id}/source-files/{current.stored.id}/content"
    )
    disposition = response.headers["content-disposition"]
    assert response.status_code == 200
    assert "\r" not in disposition and "\n" not in disposition
    assert ".." not in disposition and "folder" not in disposition


class _UnavailableReadStorage(StorageBackend):
    def save(self, key: str, content: bytes) -> None:
        del key, content

    def open(self, key: str):
        del key
        raise StorageUnavailable("sdk endpoint /private/storage secret-bucket")

    def delete(self, key: str) -> None:
        del key

    def exists(self, key: str) -> bool:
        del key
        return False


def test_storage_read_failure_is_safe_retryable_503(
    tmp_path, monkeypatch, caplog
):
    owner_id = "unavailable-owner"
    task_id = "unavailable-task"
    _seed_task(owner_id, task_id)
    local = LocalStorage(tmp_path / "storage")
    current = _create_problem_selection(
        owner_id=owner_id, task_id=task_id, storage=local
    )
    client = _client(
        monkeypatch, owner_id=owner_id, storage=_UnavailableReadStorage()
    )

    descriptor = client.get(f"/tasks/{task_id}/source-files")
    content = client.get(
        f"/tasks/{task_id}/source-files/{current.stored.id}/content"
    )
    for response in (descriptor, content):
        assert response.status_code == 503
        assert response.json()["error"]["code"] == (
            "source_preview_storage_unavailable"
        )
        assert "/private" not in response.text
        assert "secret-bucket" not in response.text
    assert "/private" not in caplog.text
    assert "secret-bucket" not in caplog.text


def test_submission_single_zip_members_partial_failure_and_duplicate_identity_stay_independent(
    tmp_path,
):
    owner_id = "submission-owner"
    task_id = "submission-task"
    _seed_task(owner_id, task_id)
    storage = LocalStorage(tmp_path / "storage")
    current = _create_submission_selection(
        owner_id=owner_id,
        task_id=task_id,
        storage=storage,
        files=[
            ("PB0001.pdf", PDF, "submission_source"),
            ("folder/PB0001.pdf", PDF_TWO, "submission_source"),
            (
                "answers.zip :: unreadable.pdf",
                b'{"schema":"smartai.archive-member-reference.v1"}',
                "submission_source_reference",
            ),
        ],
    )
    source_outcome_repository.record_outcome(
        source_id=current.sources[0].id,
        owner_id=owner_id,
        status="parsed",
        student_candidate="PB0001",
        matched_answer_count=1,
        unknown_question_ids=[],
        stable_error_code=None,
        failure_phase=None,
        retryable=False,
    )
    source_outcome_repository.record_outcome(
        source_id=current.sources[1].id,
        owner_id=owner_id,
        status="parse_failed",
        student_candidate="PB0001",
        matched_answer_count=0,
        unknown_question_ids=[],
        stable_error_code="submission_parse_invalid",
        failure_phase="structured_parse",
        retryable=True,
    )

    payload = source_files.describe_source_files(
        task_id=task_id, owner_id=owner_id, storage=storage
    )
    descriptors = payload["submission_sources"]
    assert list(descriptors) == [source.id for source in current.sources]
    assert descriptors[current.sources[0].id]["status"] == "available"
    assert descriptors[current.sources[1].id]["status"] == "available"
    assert descriptors[current.sources[0].id]["file_id"] != descriptors[
        current.sources[1].id
    ]["file_id"]
    reference = descriptors[current.sources[2].id]
    assert reference["source_id"] == current.sources[2].id
    assert reference["file_id"] is None
    assert reference["status"] == "unavailable"
    assert reference["unavailable_reason"] == "not_persisted"


def test_submission_retry_uses_only_current_attempt(tmp_path):
    owner_id = "retry-owner"
    task_id = "retry-task"
    _seed_task(owner_id, task_id)
    storage = LocalStorage(tmp_path / "storage")
    input_hash = "same-submission-input"
    first = _create_submission_selection(
        owner_id=owner_id,
        task_id=task_id,
        storage=storage,
        files=[("old.pdf", PDF, "submission_source")],
        input_hash=input_hash,
    )
    assert task_facade._fail_operation(
        task_id,
        owner_id,
        first.operation.id,
        first.operation.attempt,
        "submission_parse_failed",
    )
    retried, created = workflow_repository.create_operation(
        assignment_id=task_id,
        owner_id=owner_id,
        operation_type="submission_recognition",
        input_hash=input_hash,
        payload={},
        expires_at=time.time() + 3600,
    )
    assert created is True
    assert retried.id == first.operation.id
    assert retried.attempt == first.operation.attempt + 1
    second = _create_submission_selection(
        owner_id=owner_id,
        task_id=task_id,
        storage=storage,
        files=[("current.pdf", PDF_TWO, "submission_source")],
        operation=retried,
    )

    payload = source_files.describe_source_files(
        task_id=task_id, owner_id=owner_id, storage=storage
    )
    assert list(payload["submission_sources"]) == [second.sources[0].id]
    assert first.sources[0].id not in payload["submission_sources"]


def test_current_library_file_is_revalidated_and_inline_source_is_not_persisted(
    tmp_path,
):
    owner_id = "library-owner"
    library_task_id = "library-task"
    inline_task_id = "inline-task"
    _seed_task(owner_id, library_task_id)
    _seed_task(owner_id, inline_task_id)
    storage = LocalStorage(tmp_path / "storage")
    from backend.db.knowledge_repository import update_document
    from backend.services.knowledge_storage import persist_knowledge_upload

    upload = persist_knowledge_upload(
        storage=storage,
        owner_id=owner_id,
        original_name="library.pdf",
        content=PDF,
        content_type="application/pdf",
        title="Library problem",
    )
    document_id = upload.document_id
    document = update_document(
        document_id,
        owner_id,
        status="ready",
        chunk_count=1,
    )
    assert document is not None
    stored = file_repository.get_file(file_id=upload.file_id, owner_id=owner_id)
    assert stored is not None
    material, _ = course_library_repository.create_material(
        owner_id=owner_id,
        document_id=document_id,
        filename="library.pdf",
        category="other",
        labels=[],
        course_id=f"course-{library_task_id}",
        group_id=None,
    )
    library_ref = {
        "role": "problem",
        "source_kind": "library",
        "display_name": material.filename,
        "source_id": None,
        "stored_file_id": stored.id,
        "source_operation_id": "source-library",
        "source_attempt": 1,
        "library_material_id": material.material_id,
        "knowledge_document_id": document_id,
    }
    workflow = workflow_repository.get_workflow(
        library_task_id, owner_id=owner_id
    )
    job, _ = workflow_repository.create_operation(
        assignment_id=library_task_id,
        owner_id=owner_id,
        operation_type="question_preparation",
        input_hash=uuid.uuid4().hex,
        payload={"source_refs": [library_ref]},
    )
    job = workflow_repository.save_operation_checkpoint(
        job.id,
        owner_id=owner_id,
        expected_attempt=job.attempt,
        expected_checkpoint_revision=job.checkpoint_revision,
        stage="problem_sources_selected",
        checkpoint={"source_refs": [library_ref]},
        artifact_refs=[],
    )
    task_facade.claim_workflow_operation_atomic(
        task_id=library_task_id,
        owner_id=owner_id,
        operation_id=job.id,
        expected_operation_attempt=job.attempt,
        expected_workflow_revision=workflow.workflow_revision,
        workflow_changes={
            "active_operation": "question_preparation",
            "active_job_id": job.id,
            "extract_job_id": job.id,
        },
    )

    library_descriptor = source_files.describe_source_files(
        task_id=library_task_id, owner_id=owner_id, storage=storage
    )["problem_source"]
    assert library_descriptor["source_id"] is None
    assert library_descriptor["file_id"] == stored.id
    assert library_descriptor["status"] == "available"
    assert source_files.read_source_file_content(
        task_id=library_task_id,
        file_id=stored.id,
        owner_id=owner_id,
        storage=storage,
    ).content == PDF

    inline_ref = {
        "role": "problem",
        "source_kind": "inline_text",
        "display_name": "inline-text.txt",
        "source_id": None,
        "stored_file_id": None,
        "source_operation_id": "source-inline",
        "source_attempt": 1,
        "library_material_id": None,
        "knowledge_document_id": None,
    }
    workflow = workflow_repository.get_workflow(inline_task_id, owner_id=owner_id)
    inline_job, _ = workflow_repository.create_operation(
        assignment_id=inline_task_id,
        owner_id=owner_id,
        operation_type="question_preparation",
        input_hash=uuid.uuid4().hex,
        payload={"source_refs": [inline_ref]},
    )
    task_facade.claim_workflow_operation_atomic(
        task_id=inline_task_id,
        owner_id=owner_id,
        operation_id=inline_job.id,
        expected_operation_attempt=inline_job.attempt,
        expected_workflow_revision=workflow.workflow_revision,
        workflow_changes={
            "active_operation": "question_preparation",
            "active_job_id": inline_job.id,
            "extract_job_id": inline_job.id,
        },
    )
    inline_descriptor = source_files.describe_source_files(
        task_id=inline_task_id, owner_id=owner_id, storage=storage
    )["problem_source"]
    assert inline_descriptor == {
        "source_id": None,
        "file_id": None,
        "display_name": "inline-text.txt",
        "mime_type": None,
        "size_bytes": None,
        "status": "unavailable",
        "preview_kind": "unsupported",
        "unavailable_reason": "not_persisted",
    }


def test_second_problem_selection_and_late_old_worker_cannot_reexpose_old_file(
    tmp_path,
):
    owner_id = "rerun-owner"
    task_id = "rerun-task"
    _seed_task(owner_id, task_id)
    storage = LocalStorage(tmp_path / "storage")
    first = _create_problem_selection(
        owner_id=owner_id,
        task_id=task_id,
        storage=storage,
        content=PDF,
        filename="old.pdf",
    )
    assert task_facade._fail_operation(
        task_id,
        owner_id,
        first.job.id,
        first.job.attempt,
        "problem_extraction_failed",
    )
    second = _create_problem_selection(
        owner_id=owner_id,
        task_id=task_id,
        storage=storage,
        content=PDF_TWO,
        filename="current.pdf",
    )

    with pytest.raises(DomainError):
        task_facade._replace_draft_questions(
            task_id,
            owner_id,
            {"q1": {"stem": "late old worker"}},
            "old.pdf",
            expected_workflow_revision=first.claimed_revision,
            replace_confirmed=False,
            operation_id=first.job.id,
            expected_operation_attempt=first.job.attempt,
            operation_artifact_refs=[first.stored.id],
        )
    payload = source_files.describe_source_files(
        task_id=task_id, owner_id=owner_id, storage=storage
    )
    assert payload["problem_source"]["file_id"] == second.stored.id
    assert payload["problem_source"]["display_name"] == "current.pdf"
    with pytest.raises(source_files.SourcePreviewNotFound):
        source_files.read_source_file_content(
            task_id=task_id,
            file_id=first.stored.id,
            owner_id=owner_id,
            storage=storage,
        )


def test_source_file_routes_require_authentication():
    api = FastAPI()
    api.include_router(tasks_api.router)
    response = TestClient(api).get("/tasks/unknown/source-files")
    assert response.status_code == 401
