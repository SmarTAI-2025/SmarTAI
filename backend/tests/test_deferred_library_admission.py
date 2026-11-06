"""Probe the optional library-retention phase of a deferred PDF source."""
import asyncio
import io
import threading
import time
from types import SimpleNamespace
import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers
from backend.api import task_preparation as api
from backend.tests.test_question_preparation_recovery import _RecoveryRegistry, _claim, _install_incremental_preparer
from backend.db import workflow_repository
from backend.tests.test_task_background_workflows import _seed_task
from backend.tests.test_recognition_agent import pdf
from backend.tools import file_processing
from backend.progress.tracker import ProgressReporter

@pytest.mark.asyncio
async def test_deferred_library_retention_waits_and_publishes_durable_success(monkeypatch):
    owner, task = _seed_task()
    registry = _RecoveryRegistry()
    from backend.services import task_facade
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    body = pdf(['Question 1. Explain this complete native paragraph in your own words.\n' * 4])
    source = await api.preflight_problem_source(task_id=task,
        file=UploadFile(io.BytesIO(body), filename='questions.pdf', headers=Headers({'content-type': 'application/pdf'})),
        library_material_id=None, stored_file_id=None, inline_text=None,
        role='problem', structure_mode='organized', extraction_hint='', save_to_library=True,
        recognition_provider_id='test-provider', recognition_options=None, defer_recognition=True,
        enable_material_ocr=False, current=SimpleNamespace(id=owner), registry=registry)
    started = await api._start_question_preparation(task_id=task,
        request=api.StartQuestionPreparationRequest(source_tokens=[source['source_token']], expected_workflow_revision=0, recognition_provider_id='test-provider'),
        current=SimpleNamespace(id=owner), registry=registry, background_tasks=None, allow_prepared_source_reuse=False)
    operation = _claim(owner, started['job_id'], 'review-retention-admission')
    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(file_processing, '_PDF_EXTRACTION_SLOTS', slots)
    real_read = api.read_question_source
    releases = []
    read_texts = []
    async def release():
        await asyncio.sleep(10.4)
        slots.release(); slots.release()
    async def read_then_other_work_admitted(**kw):
        result = await real_read(**kw)
        read_texts.append(result.text)
        assert slots.acquire(blocking=False) and slots.acquire(blocking=False)
        releases.append(asyncio.create_task(release()))
        return result
    monkeypatch.setattr(api, 'read_question_source', read_then_other_work_admitted)
    calls = []
    _install_incremental_preparer(monkeypatch, calls, question_count=1)
    start = time.monotonic()
    try:
        await api.run_durable_question_preparation(operation)
        saved = workflow_repository.get_operation(started['job_id'], owner_id=owner)
        elapsed = time.monotonic() - start
        print(f'RETENTION_DURABLE=status={saved.status} error={saved.error_code} elapsed={elapsed:.3f}')
        assert read_texts and 'native paragraph' in read_texts[0]
        assert saved.status == 'done', saved.error_code
        assert elapsed >= 10.4
        from backend.db import course_library_repository
        assert course_library_repository.list_materials(owner_id=owner)
    finally:
        await asyncio.gather(*releases)

from backend.tests.test_library_source_display_names import offline_registry
from backend.tests.test_library_image_ingestion import pixels

@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['problem', 'reference_answer', 'rubric', 'programming_tests'])
@pytest.mark.parametrize('media', ['pdf', 'image'])
async def test_all_deferred_roles_propagate_library_admission_policy(monkeypatch, offline_registry, role, media):
    from backend.knowledge import service
    registry, _ = offline_registry
    if media == 'pdf':
        registry.provider.supports_vision = False
    owner, task = _seed_task()
    body, name, mime = (pdf(['A complete native paragraph.\n' * 4]), 'source.pdf', 'application/pdf') if media == 'pdf' else (pixels(), 'source.png', 'image/png')
    source = await api.preflight_problem_source(task_id=task,
        file=UploadFile(io.BytesIO(body), filename=name, headers=Headers({'content-type':mime})),
        library_material_id=None, stored_file_id=None, inline_text=None, role=role,
        structure_mode='organized', extraction_hint='', save_to_library=True,
        recognition_provider_id='test-provider', recognition_options=None, defer_recognition=True,
        enable_material_ocr=True, current=SimpleNamespace(id=owner), registry=registry)
    # Use the actual prepared source without requiring a separate problem token
    # for auxiliary-only roles. Binding and frozen source reconstruction stay real.
    record = workflow_repository.get_operation(source['source_token'], owner_id=owner)
    from backend.models import ProblemSourceDraft
    draft = ProblemSourceDraft(source_token=record.id, task_id=task, owner_id=owner,
        role=role, source_kind='upload', structure_mode='organized', filename=name,
        content_type=mime, size_bytes=len(body), content_sha256=record.payload['sha256'],
        expires_at=time.time() + 300)
    operation = SimpleNamespace(owner_id=owner, assignment_id=task, operation_id=record.id,
        attempt=record.attempt, payload={}, update_progress=_ignore_progress)
    from backend.services.stage_provider_routing import StageProviderRoute
    seen = []
    validate = service.validate_upload_content
    async def observe(*args, **kwargs):
        seen.append(kwargs.get('media_slot_timeout_seconds', 10))
        return await validate(*args, **kwargs)
    monkeypatch.setattr(service, 'validate_upload_content', observe)
    result = await api._recognize_saved_question_sources([(draft, '', record.payload)], operation=operation,
        route=StageProviderRoute(route_id='test-provider', kind='llm', provider=registry.provider),
        registry=registry, reporter=ProgressReporter(record.id))
    assert result[0][2]['saved_material'] and seen == [None]

async def _ignore_progress(_):
    pass

@pytest.mark.asyncio
async def test_http_library_ingestion_keeps_default_admission_limit(monkeypatch):
    from backend.knowledge import service
    seen = []
    validate = service.validate_upload_content
    async def observe(*args, **kwargs):
        seen.append(kwargs.get('media_slot_timeout_seconds', 10))
        return await validate(*args, **kwargs)
    monkeypatch.setattr(service, 'validate_upload_content', observe)
    owner, _ = _seed_task()
    await service.ingest_document(owner_id=owner, original_name='source.pdf',
        content=pdf(['A complete native paragraph.\n' * 4]), content_type='application/pdf', native_only=True)
    assert seen == [10]

@pytest.mark.asyncio
@pytest.mark.parametrize('policy', [10, None])
@pytest.mark.parametrize('existing', [False, True])
async def test_retention_short_circuits_do_not_validate_or_acquire(monkeypatch, policy, existing):
    from backend.db import course_library_repository
    async def forbidden(**kwargs):
        pytest.fail('No retention or an existing material must not ingest again')
    monkeypatch.setattr(api, 'ingest_document', forbidden)
    material = SimpleNamespace(public=lambda: {'material_id': 'existing'})
    monkeypatch.setattr(course_library_repository, 'get_material', lambda *args: material)
    result = await api._save_source_to_library(save=existing, descriptor={}, owner_id='owner',
        task_id='task', role='problem', existing_material_id='existing' if existing else None,
        media_slot_timeout_seconds=policy)
    assert result == ({'material_id': 'existing', 'created': False} if existing else None)

@pytest.mark.asyncio
@pytest.mark.parametrize('stop', ['cancel', 'lease_loss', 'budget', 'expired_budget'])
async def test_background_library_queue_keeps_cancellation_and_budget(monkeypatch, stop):
    from contextvars import ContextVar
    from backend.knowledge import service
    from backend.services import submission_work_budget as clocks
    from backend.domain.errors import RecognitionError
    owner, _ = _seed_task()
    from backend.db import knowledge_repository, file_repository
    before_files = file_repository.list_files(owner_id=owner)
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(file_processing, '_PDF_EXTRACTION_SLOTS', slots)
    async def forbidden(*args, **kwargs):
        pytest.fail('Queued validation must not launch decoder')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    budget = clocks.SubmissionWorkBudget('retention-test')
    monkeypatch.setattr(clocks, '_current', ContextVar('retention-budget', default=budget))
    if stop == 'expired_budget':
        budget.expire()
    if stop == 'budget':
        budget.start()
        asyncio.get_running_loop().call_later(0.02, budget.expire)
    work = asyncio.create_task(service.ingest_document(owner_id=owner, original_name='source.pdf',
        content=pdf(['A complete native paragraph.\n' * 4]), content_type='application/pdf',
        native_only=True, media_slot_timeout_seconds=None))
    try:
        if stop in {'cancel', 'lease_loss'}:
            await asyncio.sleep(0.04)
            if stop == 'lease_loss':
                from backend.services.workflow_worker import LeasedOperation
                lease = LeasedOperation(SimpleNamespace(lease_expires_at=time.time() + 60),
                    worker_id='retention-lease', lease_seconds=60, on_lease_lost=work.cancel)
                lease.lose_lease()
            else:
                work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
            assert budget.active == 0
        else:
            with pytest.raises(RecognitionError, match=clocks.CODE):
                await asyncio.wait_for(work, 1)
        assert not slots.acquire(blocking=False)
        assert not knowledge_repository.list_documents(owner)
        assert file_repository.list_files(owner_id=owner) == before_files
    finally:
        if stop == 'budget':
            budget.finish()
        budget.expire()
        slots.release()
