"""Exercise shared detection through upload readers without any model calls."""
import io

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from backend.api import task_preparation
from backend.services.stage_provider_routing import StageProviderRoute
from backend.tests.test_archive_content_detection import PDF, archive_bytes
from backend.tests.test_task_background_workflows import _seed_task
from backend.tests.test_question_preparation_recovery import _RecoveryRegistry


ROLES = ("problem", "reference_answer", "rubric", "programming_tests")
TEXT_HEADERS = [f"BZh{level}" for level in range(1, 10)] + ["BZh91AY&SY"]


def upload(body, name, mime):
    return UploadFile(file=io.BytesIO(body), filename=name, headers=Headers({"content-type": mime}))


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("name", ["material.txt", "material.md", "material.markdown"])
async def test_all_teacher_material_readers_preserve_text_quoting_pdf(role, name):
    body = b"Question 1: Explain the %PDF-1.7 file header.\n"
    owner, task = _seed_task()
    registry = _RecoveryRegistry()
    route = StageProviderRoute(route_id="test-provider", kind="llm", provider=registry.provider)
    text, descriptor = await task_preparation._read_source(
        file=upload(body, name, "text/plain"), library_material_id=None, inline_text=None,
        owner_id=owner, task_id=task, registry=registry, role=role, route=route,
    )
    assert text == body.decode()
    assert descriptor["content_type"].startswith("text/")
    assert descriptor["recognition"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("header", TEXT_HEADERS)
async def test_teacher_material_readers_preserve_bzip_header_text(role, header):
    body = f"{header} is an example header, not compressed data.\n".encode()
    owner, task = _seed_task()
    registry = _RecoveryRegistry()
    text, descriptor = await task_preparation._read_source(
        file=upload(body, "material.txt", "text/plain"), library_material_id=None, inline_text=None,
        owner_id=owner, task_id=task, registry=registry, role=role,
        route=StageProviderRoute(route_id="test-provider", kind="llm", provider=registry.provider),
    )
    assert text == body.decode()
    assert descriptor["content_type"] == "text/plain"
    assert descriptor["recognition"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mime", ["application/json", "text/json", "text/plain", "application/octet-stream"])
async def test_programming_tests_json_reaches_actual_text_reader(mime):
    body = b'{"input": "%PDF-1.7", "expected_output": "PDF"}'
    owner, task = _seed_task()
    registry = _RecoveryRegistry()
    text, descriptor = await task_preparation._read_source(
        file=upload(body, "cases.json", mime), library_material_id=None, inline_text=None,
        owner_id=owner, task_id=task, registry=registry, role="programming_tests",
        route=StageProviderRoute(route_id="test-provider", kind="llm", provider=registry.provider),
    )
    assert text == body.decode() and descriptor["content_type"] == "application/json"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ROLES)
async def test_teacher_uploads_reject_zip_renamed_pdf_before_recognition(role):
    body, _ = archive_bytes("zip-deflate", {"source.pdf": PDF})
    assert b"%PDF-" in body[:1024]
    with pytest.raises(HTTPException) as caught:
        await task_preparation._select_source(
            file=upload(body, "source.pdf", "application/pdf"), library_material_id=None,
            inline_text=None, owner_id="synthetic-owner", registry=_RecoveryRegistry(), role=role,
        )
    assert caught.value.detail["code"] == "source_content_type_not_allowed"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ROLES)
async def test_png_metadata_cannot_route_teacher_images_to_pdf(role):
    from PIL import Image, PngImagePlugin
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("description", "Example PDF header: %PDF-1.7")
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(buffer, format="PNG", pnginfo=metadata)
    descriptor = await task_preparation._select_source(
        file=upload(buffer.getvalue(), "source.png", "image/png"), library_material_id=None,
        inline_text=None, owner_id="synthetic-owner", registry=_RecoveryRegistry(), role=role,
        document_ocr_available=True,
    )
    assert descriptor["content_type"] == "image/png"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["book.txt", "book.md", "book.markdown"])
@pytest.mark.parametrize("header", ["%PDF-1.7", *TEXT_HEADERS])
async def test_knowledge_text_is_stored_and_chunked_without_pdf_recognition(name, header):
    from backend.db.knowledge_repository import list_chunks
    from backend.knowledge.service import ingest_document
    from backend.tests.test_knowledge_ingestion import owner, Registry
    text = f"{'File format notes: ' if header.startswith('%PDF') else ''}{header} is an example file header.\n"
    document = await ingest_document(owner_id=owner(), original_name=name, content=text.encode(),
                                     content_type="text/plain", registry=Registry())
    assert document.status == "ready"
    assert document.content_type.startswith("text/")
    assert list_chunks([document.id])[0].content == text
    assert document.ingestion_summary["usage"].get("initial_calls", 0) == 0


@pytest.mark.asyncio
async def test_knowledge_zip_renamed_pdf_rejected_before_storage(monkeypatch):
    from backend.knowledge import service
    def forbidden(**kwargs):
        pytest.fail("A disguised archive must not be persisted")
    monkeypatch.setattr(service, "persist_knowledge_upload", forbidden)
    body, _ = archive_bytes("zip", {"source.pdf": PDF})
    with pytest.raises(HTTPException) as caught:
        await service.ingest_document(owner_id="synthetic-owner", original_name="source.pdf", content=body,
                                      content_type="application/pdf", native_only=True)
    assert caught.value.status_code == 415
    assert caught.value.detail == {"code": "source_content_type_not_allowed"}


def test_roster_csv_is_parsed_as_csv_even_with_pdf_marker_in_a_field():
    from backend.api.tasks import _parse_roster
    assert _parse_roster(b"student_id,student_name\nS1,Synthetic %PDF- example\n") == [
        {"stu_id": "S1", "stu_name": "Synthetic %PDF- example"}]


@pytest.mark.asyncio
async def test_pdf_worker_rejects_archive_before_launch(monkeypatch):
    from unittest.mock import AsyncMock
    from backend.domain.errors import PdfEvidenceError
    from backend.tools import pdf_evidence
    launch = AsyncMock(side_effect=AssertionError("Invalid PDF must not launch a worker"))
    monkeypatch.setattr(pdf_evidence.asyncio, "create_subprocess_exec", launch)
    body, _ = archive_bytes("zip", {"source.pdf": PDF})
    with pytest.raises(PdfEvidenceError, match="pdf_invalid"):
        await pdf_evidence.read_pdf_evidence(body, pdf_evidence.PdfIndexRequest())
    launch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("name,mime", [("renamed.txt", "text/plain"), ("source.pdf", "image/png")])
async def test_compatibility_question_reader_checks_filename_and_mime_before_visual_work(name, mime, monkeypatch):
    from backend.services import question_sources
    async def forbidden(*args, **kwargs):
        pytest.fail("Inconsistent file hints must be rejected before PDF/model work")
    monkeypatch.setattr(question_sources, "read_pdf_evidence", forbidden)
    registry = _RecoveryRegistry()
    import fitz
    document = fitz.open()
    document.new_page()
    body = document.tobytes()
    document.close()
    with pytest.raises(HTTPException) as caught:
        await question_sources.read_question_source(owner_id="synthetic-owner", task_id="synthetic-task",
            content=body, filename=name, content_type=mime, registry=registry,
            route=StageProviderRoute(route_id="test-provider", kind="llm", provider=registry.provider))
    assert caught.value.status_code == 415
    assert caught.value.detail == {"code": "source_content_type_not_allowed"}


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_archive_diagnostics_survive_durable_submission_processing(monkeypatch, nested):
    import json
    from types import SimpleNamespace
    from backend.api import tasks
    from backend.db import source_outcome_repository, workflow_repository
    from backend.services import task_facade
    from backend.services.workflow_worker import LeasedOperation
    from backend.tests.test_task_background_workflows import _BackgroundTasks, _Registry, _seed_task
    owner, task = _seed_task(with_question=True)
    inner, _ = archive_bytes("zip", {"answer.txt": b"synthetic"})
    entries = {"folder/inner.zip": inner} if nested else {"folder/": b""}
    body, name = archive_bytes("zip", entries)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner, filename=name,
        content=body, content_type="application/zip", registry=_Registry())
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: _Registry())
    claimed = workflow_repository.claim_operation(queued["job_id"], owner_id=owner,
        worker_id="archive-worker", lease_seconds=60)
    await task_facade.run_durable_submission_recognition(
        LeasedOperation(claimed, worker_id="archive-worker", lease_seconds=60))
    source = source_outcome_repository.list_sources(operation_id=queued["job_id"], owner_id=owner, attempt=1)[0]
    outcome = source_outcome_repository.get_outcome(source.id, owner_id=owner)
    operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    expected = "submission_archive_nested_unsupported" if nested else "submission_archive_directories_only"
    assert operation.status == "error"
    assert outcome.stable_error_code == expected
    assert outcome.failure_phase == "archive" and outcome.retryable is False
    before = task_facade.get_task(task_id=task, owner_id=owner)
    def forbidden(**kwargs):
        pytest.fail("An unchanged non-retryable archive must not queue another operation")
    monkeypatch.setattr(task_facade, "queue_task_submission_parsing", forbidden)
    response = await tasks.retry_submission_recognition_endpoint(
        task_id=task, job_id=operation.id,
        request=tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=before["workflow_revision"]),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=_Registry(),
    )
    assert response.status_code == 409
    assert json.loads(response.body)["error"]["code"] == "submission_retry_requires_reupload"
    after = task_facade.get_task(task_id=task, owner_id=owner)
    assert after["workflow_revision"] == before["workflow_revision"]
    assert after["parse_job_id"] == before["parse_job_id"]
    assert after["submission_sources"] == before["submission_sources"]
