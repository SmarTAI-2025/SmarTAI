"""Editable library titles never replace the original media identity.

Decode, persistence, preflight, and deferred reads are real. Only the image
provider is replaced with an offline engine, so no external calls are made.
"""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend.api import materials, task_preparation as api
from backend.db import course_library_repository, file_repository, workflow_repository
from backend.knowledge.service import ingest_document
from backend.progress.tracker import ProgressReporter
from backend.services import question_sources, task_facade
from backend.services.stage_provider_routing import StageProviderRoute, stage_provider_configuration_fingerprint
from backend.storage import get_storage
from backend.tests.test_library_image_ingestion import pixels
from backend.tests.test_question_preparation_recovery import _RecoveryRegistry, _claim
from backend.tests.test_recognition_agent import pdf
from backend.tests.test_recognition_image_executor import ImageEngine
from backend.tests.test_task_background_workflows import _seed_task


ROLES = ("problem", "reference_answer", "rubric", "programming_tests")
TEXT = "Question 1. Explain the central thesis of this paragraph in your own words.\n" * 4


class OfflineImageEngine(ImageEngine):
    async def recognize(self, request):
        from backend.llm.request_timeouts import current_response_scope

        read = super().recognize
        scope = current_response_scope()
        if scope is not None:
            return await scope.run_response(lambda: read(request))
        return await read(request)


@pytest.fixture
def offline_registry(monkeypatch):
    registry = _RecoveryRegistry()
    registry.provider = SimpleNamespace(provider_id="test-provider", supports_vision=True)
    engines = []

    @asynccontextmanager
    async def engine(*, owner_id, route, registry):
        if route is None or not route.provider.supports_vision:
            yield None
            return
        model = OfflineImageEngine(result=TEXT)
        model.caps.route_id = route.route_id
        model.caps.fingerprint = stage_provider_configuration_fingerprint(owner_id=owner_id, route=route, registry=registry)
        engines.append(model)
        yield model

    monkeypatch.setattr(question_sources, "recognition_engine", engine)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    return registry, engines


async def library_source(owner, media, registry, title):
    filename, body, mime = ("lecture.pdf", pdf([TEXT]), "application/pdf") if media == "pdf" else ("lecture.png", pixels(), "image/png")
    doc = await ingest_document(owner_id=owner, original_name=filename, content=body,
        content_type=mime, native_only=media == "pdf", registry=registry)
    material, _ = course_library_repository.create_material(owner_id=owner, document_id=doc.id,
        filename=filename, category="lecture", labels=[], course_id=None, group_id=None)
    renamed = materials.update_course_material(material.material_id,
        materials.UpdateCourseMaterialRequest(filename=title), current=SimpleNamespace(id=owner))
    assert renamed["filename"] == title and renamed["content_type"] == mime
    return material.material_id, filename, body, mime


async def preflight(owner, task, material, role, registry, *, deferred):
    result = await api.preflight_problem_source(task_id=task, file=None, library_material_id=material,
        stored_file_id=None, inline_text=None, role=role, structure_mode="organized", extraction_hint="",
        save_to_library=False, recognition_provider_id="test-provider", recognition_options=None,
        defer_recognition=deferred, enable_material_ocr=True, current=SimpleNamespace(id=owner), registry=registry)
    assert isinstance(result, dict), getattr(result, "body", result)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("title", ["Lecture 1", "Lecture notes.docx"])
@pytest.mark.parametrize("media", ["pdf", "image"])
async def test_renamed_library_source_reads_and_preflights_with_original_identity(role, title, media, offline_registry):
    registry, engines = offline_registry
    # Native PDF reads should not make any provider calls.
    if media == "pdf":
        registry.provider.supports_vision = False
    owner, task = _seed_task()
    material, filename, body, mime = await library_source(owner, media, registry, title)
    route = StageProviderRoute(route_id="test-provider", kind="llm", provider=registry.provider)
    text, descriptor = await api._read_source(file=None, library_material_id=material, inline_text=None,
        owner_id=owner, task_id=task, registry=registry, role=role, route=route, enable_material_ocr=True)
    assert "central thesis" in text
    assert descriptor["filename"] == title
    stored = file_repository.get_file(file_id=descriptor["recognition_stored_file_id"], owner_id=owner)
    assert stored.original_name == filename and stored.content_type == mime

    result = await preflight(owner, task, material, role, registry, deferred=False)
    assert result["source"]["filename"] == title
    operation = workflow_repository.get_operation(result["source_token"], owner_id=owner)
    assert "central thesis" in operation.payload["text"]
    assert operation.payload["filename"] == operation.payload["source_ref"]["display_name"] == title
    clone = file_repository.get_file(file_id=result["source"]["stored_file_id"], owner_id=owner)
    assert clone.original_name == filename and clone.content_type == mime
    with get_storage().open(clone.storage_key) as stream:
        assert stream.read() == body
    assert not engines if media == "pdf" else engines


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("title", ["Lecture 1", "Lecture notes.docx"])
@pytest.mark.parametrize("media", ["pdf", "image"])
async def test_renamed_library_source_survives_deferred_worker_rehydration(monkeypatch, role, title, media, offline_registry):
    registry, engines = offline_registry
    if media == "pdf":
        registry.provider.supports_vision = False
    owner, task = _seed_task()
    material, filename, body, mime = await library_source(owner, media, registry, title)
    # A previous release could persist the editable title as a source name
    # before failing. Matching bytes alone must not reuse that legacy clone.
    legacy = file_repository.save_file(storage=get_storage(), owner_id=owner, assignment_id=task,
        kind="problem_source", original_name=title, content=body, content_type=mime)
    result = await preflight(owner, task, material, role, registry, deferred=True)
    assert result["source"]["filename"] == title and not engines
    stored = file_repository.get_file(file_id=result["source"]["stored_file_id"], owner_id=owner)
    assert stored.original_name == filename and stored.content_type == mime
    assert stored.id != legacy.id
    started = await api._start_question_preparation(task_id=task,
        request=api.StartQuestionPreparationRequest(source_tokens=[result["source_token"]],
            expected_workflow_revision=0, recognition_provider_id="test-provider"),
        current=SimpleNamespace(id=owner), registry=registry, background_tasks=None, allow_prepared_source_reuse=False)
    assert isinstance(started, dict), getattr(started, "body", started)
    operation = _claim(owner, started["job_id"], "library-rename")
    inputs = api._rehydrate_question_preparation_inputs(operation)
    assert inputs["sources"][0][0].filename == title
    # Once rehydrated, a new task copy must not add a dependency on the live
    # library while decoding. Its canonical name is part of the frozen input.
    monkeypatch.setattr(course_library_repository, "get_material",
        lambda *_args, **_kwargs: pytest.fail("New source copies use frozen metadata"))
    resolved = await api._recognize_saved_question_sources(inputs["sources"], operation=operation,
        route=inputs["route"], registry=registry, reporter=ProgressReporter(operation.operation_id))
    assert len(resolved) == 1 and "central thesis" in resolved[0][1]
    assert resolved[0][0].filename == title
    assert not engines if media == "pdf" else engines


@pytest.mark.asyncio
@pytest.mark.parametrize("violation", ["owner", "document", "bytes"])
async def test_library_title_fix_preserves_original_owner_document_and_hash_checks(monkeypatch, violation, offline_registry):
    from dataclasses import replace
    from fastapi import HTTPException
    from backend.domain.errors import NotFound

    registry, engines = offline_registry
    owner, task = _seed_task()
    material_id, _, body, _ = await library_source(owner, "image", registry, "Lecture 1")
    material = course_library_repository.get_material(material_id, owner)
    original = file_repository.get_file(file_id=material.stored_file_id, owner_id=owner)
    if violation == "owner":
        owner, task = _seed_task()
    elif violation == "document":
        monkeypatch.setattr(file_repository, "get_file", lambda **_: replace(original, knowledge_document_id="unrelated"))
    else:
        get_storage().save(original.storage_key, body + b"changed")
    with pytest.raises(HTTPException if violation == "bytes" else NotFound) as caught:
        await api._read_source(file=None, library_material_id=material_id, inline_text=None,
            owner_id=owner, task_id=task, registry=registry, role="problem",
            route=StageProviderRoute(route_id="test-provider", kind="llm", provider=registry.provider))
    if violation == "bytes":
        assert caught.value.detail["code"] == "recognition_source_mismatch"
    assert not engines


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["rubric", "programming_tests"])
async def test_renamed_library_images_still_require_material_ocr_confirmation(role, offline_registry):
    from backend.domain.errors import RecognitionError

    registry, engines = offline_registry
    owner, task = _seed_task()
    material, _, _, _ = await library_source(owner, "image", registry, "Lecture 1")
    with pytest.raises(RecognitionError) as caught:
        await api._read_source(file=None, library_material_id=material, inline_text=None,
            owner_id=owner, task_id=task, registry=registry, role=role, enable_material_ocr=False,
            route=StageProviderRoute(route_id="test-provider", kind="llm", provider=registry.provider))
    assert caught.value.code == "material_ocr_confirmation_required" and not engines


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["Lecture 1", "Lecture notes.docx"])
@pytest.mark.parametrize("media", ["pdf", "image"])
async def test_legacy_failed_library_preflight_does_not_poison_corrected_request(monkeypatch, title, media, offline_registry):
    from fastapi import HTTPException

    registry, _ = offline_registry
    if media == "pdf":
        registry.provider.supports_vision = False
    owner, task = _seed_task()
    material, filename, _, _ = await library_source(owner, media, registry, title)
    select_source = api._select_source

    async def legacy_selection(**kwargs):
        descriptor = await select_source(**kwargs)
        descriptor.pop("_original_filename")
        return descriptor

    with monkeypatch.context() as old:
        old.setattr(api, "_select_source", legacy_selection)
        with pytest.raises(HTTPException) as caught:
            await preflight(owner, task, material, "problem", registry, deferred=False)
        assert caught.value.status_code == 415
    legacy_files = file_repository.list_files(owner_id=owner, assignment_id=task)
    assert any(row.original_name == title for row in legacy_files)
    result = await preflight(owner, task, material, "problem", registry, deferred=False)
    assert result["source"]["filename"] == title
    stored = file_repository.get_file(file_id=result["source"]["stored_file_id"], owner_id=owner)
    assert stored.original_name == filename
    assert stored.id not in {row.id for row in legacy_files}
    operation = workflow_repository.get_operation(result["source_token"], owner_id=owner)
    assert operation.payload["original_filename"] == filename and "central thesis" in operation.payload["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("media", ["pdf", "image"])
async def test_already_queued_legacy_library_source_keeps_frozen_hash_and_can_read(monkeypatch, media, offline_registry):
    registry, _ = offline_registry
    if media == "pdf":
        registry.provider.supports_vision = False
    owner, task = _seed_task()
    material, _, _, _ = await library_source(owner, media, registry, "Lecture 1")
    select_source = api._select_source

    async def legacy_selection(**kwargs):
        descriptor = await select_source(**kwargs)
        descriptor.pop("_original_filename")
        return descriptor

    with monkeypatch.context() as old:
        old.setattr(api, "_select_source", legacy_selection)
        result = await preflight(owner, task, material, "problem", registry, deferred=True)
    source = workflow_repository.get_operation(result["source_token"], owner_id=owner)
    assert "original_filename" not in source.payload
    stored = file_repository.get_file(file_id=result["source"]["stored_file_id"], owner_id=owner)
    assert stored.original_name == "Lecture 1"
    started = await api._start_question_preparation(task_id=task,
        request=api.StartQuestionPreparationRequest(source_tokens=[result["source_token"]],
            expected_workflow_revision=0, recognition_provider_id="test-provider"),
        current=SimpleNamespace(id=owner), registry=registry, background_tasks=None, allow_prepared_source_reuse=False)
    assert isinstance(started, dict), getattr(started, "body", started)
    operation = _claim(owner, started["job_id"], "library-legacy")
    inputs = api._rehydrate_question_preparation_inputs(operation)
    resolved = await api._recognize_saved_question_sources(inputs["sources"], operation=operation,
        route=inputs["route"], registry=registry, reporter=ProgressReporter(operation.operation_id))
    assert "central thesis" in resolved[0][1]
    unchanged = workflow_repository.get_operation(source.id, owner_id=owner)
    assert unchanged.input_hash == source.input_hash and unchanged.payload == source.payload
