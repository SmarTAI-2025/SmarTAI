"""Generated pixels and fake provider responses exercise the durable image pipeline.

No credentials or external model calls are used. Production decoding, recognition
planning/assembly, evidence publication, persistence and retrieval remain real.
"""
from contextlib import asynccontextmanager
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy import select

from backend.auth import create_token
from backend.db import course_library_repository, knowledge_ingestion_repository as repo
from backend.db.knowledge_repository import get_document, list_chunks, set_task_documents
from backend.db.models import KnowledgeDocumentRecord, KnowledgeEvidenceRecord
from backend.db.session import session_scope
from backend.domain.errors import DomainError
from backend.knowledge import ingestion, service
from backend.knowledge.index import KnowledgeIndexCache
from backend.knowledge.retriever import PersistentKnowledgeRetriever
from backend.main import app
from backend.services import task_facade
from backend.tests.test_knowledge_ingestion import owner, Registry as EmptyRegistry
from backend.tests.test_knowledge_retrieval_versions import assignment
from backend.tests.test_recognition_image_executor import ImageEngine


class Registry:
    def __init__(self, vision=True, name="image-model"):
        self.provider = SimpleNamespace(provider_id=name, supports_vision=vision)

    def list_configs(self):
        return [dict(provider_id=self.provider.provider_id, enabled=True, model="offline-fixture")]

    def get(self, key):
        return self.provider if key == self.provider.provider_id else None

    def pick_default(self):
        return self.provider


def pixels(format="PNG", *, multi=False):
    image = Image.new("RGB", (80, 40), "white")
    image.putpixel((0, 0), (0, 0, 0))
    output = BytesIO()
    kwargs = {"save_all": True, "append_images": [Image.new("RGB", (80, 40), "red")]} if multi else {}
    image.save(output, format, **kwargs)
    return output.getvalue()


@pytest.fixture
def offline_model(monkeypatch):
    registry = Registry()
    engines = []

    @asynccontextmanager
    async def model(*, owner_id, route, registry):
        assert route is not None
        engine = ImageEngine(result="Finite group homomorphism preserves multiplication.")
        engine.caps.route_id = route.route_id
        engine.caps.fingerprint = ingestion.stage_provider_configuration_fingerprint(owner_id=owner_id, route=route, registry=registry)
        engines.append(engine)
        from backend.tests.test_recognition_response_deadlines import AdmittedEngine
        yield AdmittedEngine(engine)

    monkeypatch.setattr(ingestion, "recognition_engine", model)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    return registry, engines


@pytest.mark.asyncio
@pytest.mark.parametrize("extension,format,mime", [
    ("jpg", "JPEG", "image/jpeg"), ("JPEG", "JPEG", "image/jpeg"),
    ("png", "PNG", "image/png"), ("bmp", "BMP", "image/bmp"),
    ("tif", "TIFF", "image/tiff"), ("TIFF", "TIFF", "image/tiff"),
    ("webp", "WEBP", "image/webp"),
])
async def test_every_static_format_becomes_searchable_persistent_knowledge(extension, format, mime, offline_model):
    registry, engines = offline_model
    who = owner()
    body = pixels(format)
    doc = await service.ingest_document(owner_id=who, original_name=f"lecture.{extension}", content=body,
                                        content_type="application/octet-stream", registry=registry)
    assert doc.content_type == mime and doc.status == "processing"
    assert not engines and list_chunks([doc.id]) == []  # Upload is not an OCR-success claim.
    job_id = doc.ingestion_summary["id"]
    assert await ingestion.KnowledgeIngestionWorker(registry_factory=lambda _: registry).run_once(job_id)
    final = get_document(doc.id, who)
    assert final.status == "ready", final.public()
    assert final.ingestion_summary["coverage_complete"] is True
    assert final.ingestion_summary["total_pages"] == final.ingestion_summary["processed_pages"] == 1
    assert len(engines) == len(engines[0].requests) == 1
    request = engines[0].requests[0]
    assert request.purpose == "knowledge" and request.input_mode == "page_image"
    assert request.page_number == 1
    chunks = list_chunks([doc.id])
    assert chunks and chunks[0].chunk_metadata["page_number"] == 1
    assert chunks[0].chunk_metadata["artifact_ids"]
    with session_scope() as session:
        assert session.scalar(select(KnowledgeEvidenceRecord.id).where(KnowledgeEvidenceRecord.document_id == doc.id))
    task = assignment(who)
    set_task_documents(assignment_id=task, owner_id=who, document_ids=[doc.id])
    # New worker/cache after publication proves persisted state, not an upload-local result.
    retriever = PersistentKnowledgeRetriever(cache=KnowledgeIndexCache())
    matches = await retriever.retrieve("group homomorphism", scope=task)
    assert matches and matches[0].citation["document_id"] == doc.id
    assert "preserves multiplication" in matches[0].content
    assert await ingestion.KnowledgeIngestionWorker(registry_factory=lambda _: registry).run_once(job_id) is False
    duplicate = await service.ingest_document(owner_id=who, original_name=f"same.{extension}", content=body, registry=registry)
    assert duplicate.id == doc.id and duplicate.status == "ready" and len(engines) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("registry,native_only,code", [
    (EmptyRegistry(), False, "no_provider_configured"),
    (Registry(vision=False), False, "provider_vision_not_supported"),
    (None, True, "knowledge_image_requires_ocr"),
])
async def test_unreadable_image_configuration_fails_before_storage(registry, native_only, code, monkeypatch):
    persist = AsyncMock(side_effect=AssertionError("must not persist unreadable images"))
    monkeypatch.setattr(service, "persist_knowledge_upload", persist)
    with pytest.raises((DomainError, HTTPException)) as caught:
        await service.ingest_document(owner_id=owner(), original_name="scan.png", content=pixels(), registry=registry, native_only=native_only)
    actual = caught.value.detail["code"] if isinstance(caught.value, HTTPException) else caught.value.code
    assert actual == code
    persist.assert_not_called()


@pytest.mark.asyncio
async def test_image_limit_reuses_preprocessor_limit_before_storage(monkeypatch):
    monkeypatch.setattr(service, "MAX_IMAGE_INPUT_BYTES", 10)
    with pytest.raises(HTTPException) as caught:
        await service.ingest_document(owner_id=owner(), original_name="scan.png", content=pixels(), registry=Registry())
    assert caught.value.status_code == 413 and caught.value.detail["max_bytes"] == 10
    with session_scope() as session:
        assert session.scalar(select(KnowledgeDocumentRecord.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("name,body", [("scan.jpg", pixels("PNG")), ("scan.png", b"text"), ("scan.txt", pixels())])
async def test_image_extension_must_agree_with_real_media(name, body):
    with pytest.raises(HTTPException) as caught:
        await service.ingest_document(owner_id=owner(), original_name=name, content=body, registry=Registry())
    assert caught.value.status_code == 415 and caught.value.detail["code"] == "source_content_type_not_allowed"


def test_course_library_http_upload_keeps_model_selection_and_original_bytes(offline_model):
    registry, engines = offline_model
    who = owner()
    headers = {"Authorization": "Bearer " + create_token(who, "teacher")}
    body = pixels()
    with TestClient(app) as client:
        response = client.post("/course-materials/", headers=headers,
                               data={"recognition_route_id": registry.provider.provider_id},
                               files={"file": ("lecture.png", body, "image/png")})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["parse_status"] == "processing" and not engines
        material = course_library_repository.get_material(payload["material_id"], who)
        doc = get_document(material.document_id, who)
        with session_scope() as session:
            from backend.db.models import KnowledgeIngestionRecord
            job = session.get(KnowledgeIngestionRecord, doc.ingestion_summary["id"])
            assert job.configuration["route_id"] == registry.provider.provider_id
        download = client.get(f"/knowledge/documents/{doc.id}/download", headers=headers)
        assert download.status_code == 200 and download.content == body
        assert download.headers["content-type"].startswith("image/png")
        assert client.get("/course-materials/", headers=headers).json()["capabilities"]["accepted_types"] == sorted(ext[1:] for ext in service.KNOWLEDGE_UPLOAD_EXTENSIONS)


@pytest.mark.asyncio
async def test_task_source_library_save_reuses_explicit_recognition_route(monkeypatch):
    from backend.api import task_preparation
    registry = Registry(name="chosen-not-default")
    doc = SimpleNamespace(id="document", status="processing")
    ingest = AsyncMock(return_value=doc)
    monkeypatch.setattr(task_preparation, "ingest_document", ingest)
    monkeypatch.setattr(task_facade, "get_task", lambda **_: {})
    monkeypatch.setattr(course_library_repository, "create_material", lambda **_: (SimpleNamespace(public=lambda: {"material_id":"material"}), True))
    await task_preparation._save_source_to_library(save=True, descriptor={"_body":pixels(), "filename":"scan.png", "content_type":"image/png"},
        owner_id="owner", task_id="task", role="problem", existing_material_id=None, registry=registry,
        recognition_route_id=registry.provider.provider_id)
    assert ingest.await_args.kwargs["registry"] is registry
    assert ingest.await_args.kwargs["recognition_route_id"] == "chosen-not-default"


@pytest.mark.asyncio
@pytest.mark.parametrize("name,body,code", [
    ("broken.png", b"\x89PNG\r\n\x1a\n" + b"bad", "image_invalid"),
    ("animated.png", pixels("PNG", multi=True), "image_multiframe_unsupported"),
    ("pages.tiff", pixels("TIFF", multi=True), "image_multiframe_unsupported"),
    ("animated.webp", pixels("WEBP", multi=True), "image_multiframe_unsupported"),
])
async def test_corrupt_and_multiframe_images_are_rejected_without_persistence(name, body, code):
    with pytest.raises(HTTPException) as caught:
        await service.ingest_document(owner_id=owner(), original_name=name, content=body, registry=Registry())
    assert caught.value.status_code == 400 and caught.value.detail["code"] == code
    with session_scope() as session:
        assert session.scalar(select(KnowledgeDocumentRecord.id)) is None


@pytest.mark.asyncio
async def test_changed_image_route_pauses_without_call_and_retry_can_recover(offline_model):
    registry, engines = offline_model
    who = owner()
    doc = await service.ingest_document(owner_id=who, original_name="scan.png", content=pixels(), registry=registry)
    registry.list_configs = lambda: [dict(provider_id=registry.provider.provider_id, enabled=True, model="changed-offline-model")]
    worker = ingestion.KnowledgeIngestionWorker(registry_factory=lambda _: registry)
    await worker.run_once(doc.ingestion_summary["id"])
    paused = get_document(doc.id, who)
    assert paused.ingestion_summary["status"] == "paused"
    assert paused.ingestion_summary["error_code"] == "recognition_route_changed"
    assert not engines and list_chunks([doc.id]) == []
    configuration = ingestion.frozen_configuration(who, registry, require_visual=True)
    retry = repo.queue_document(doc.id, who, configuration, new_version=True, retry_failed_only=True)
    await worker.run_once(retry)
    recovered = get_document(doc.id, who)
    assert recovered.status == "ready" and len(engines) == 1


def test_task_knowledge_image_upload_and_library_reuse_remain_durable(offline_model):
    _, engines = offline_model
    who = owner()
    task = task_facade.create_task(owner_id=who, name="Image knowledge", semester_id=None, course_id=None,
                                  idempotency_key="image-task")
    headers = {"Authorization": "Bearer " + create_token(who, "teacher")}
    with TestClient(app) as client:
        response = client.post(f"/tasks/{task['task_id']}/kb", headers=headers, data={"save_to_library":"true"},
                               files={"file":("task.png", pixels(), "image/png")})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["saved_to_library"] and payload["chunk_count"] == 0 and not engines
        again = client.post(f"/tasks/{task['task_id']}/kb", headers=headers,
                            data={"library_material_id":payload["library_material_id"]})
        assert again.status_code == 200 and again.json()["doc_id"] == payload["doc_id"]
        rows = client.get(f"/tasks/{task['task_id']}/kb", headers=headers).json()["docs"]
        assert len(rows) == 1 and rows[0]["status"] == "processing"
        assert not engines


def test_native_only_configuration_never_silently_accepts_pixel_input():
    with pytest.raises(DomainError) as caught:
        ingestion.frozen_configuration("owner", None, native_only=True, require_visual=True)
    assert caught.value.code == "knowledge_image_requires_ocr"


def test_image_retry_requires_a_usable_model_before_replacing_the_job(offline_model, monkeypatch):
    who = owner()
    headers = {"Authorization":"Bearer " + create_token(who, "teacher")}
    with TestClient(app) as client:
        uploaded = client.post("/course-materials/", headers=headers,
                               files={"file":("scan.png", pixels(), "image/png")})
        assert uploaded.status_code == 200
        doc_id = uploaded.json()["document_id"]
        original_job = get_document(doc_id, who).ingestion_summary["id"]
        monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: EmptyRegistry())
        retry = client.post(f"/knowledge/documents/{doc_id}/retry-failed", headers=headers)
        assert retry.status_code == 422 and retry.json()["error"]["code"] == "no_provider_configured"
        assert get_document(doc_id, who).ingestion_summary["id"] == original_job
