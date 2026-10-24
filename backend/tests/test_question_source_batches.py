from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.domain.errors import RecognitionError
from backend.progress.tracker import ProgressReporter
from backend.recognition.models import RecognitionUsageV1
from backend.services import question_sources
from backend.services.question_source_batches import read_question_pdf_batches
from backend.services.stage_provider_routing import StageProviderRoute
from backend.tests.test_recognition_agent import pdf, request
from backend.tests.test_recognition_v2_artifacts import setup


@pytest.mark.asyncio
async def test_native_pdf_with_selected_model_keeps_one_window_and_indexes_pages_once(tmp_path, monkeypatch):
    import hashlib
    from backend.db import file_repository
    from backend.recognition.models import RecognitionPolicyV1
    from backend.services import recognition_local_evidence as local
    from backend.services.recognition_runs import RecognitionRunService
    from backend.tools.pdf_evidence import PdfIndexRequest

    agent, original, _, results = setup(tmp_path)
    data = pdf([f"Question {n}. Explain the independent statement in words." for n in range(1, 13)])
    stored = file_repository.save_file(storage=results.store.storage, owner_id=original.source.owner_id,
        assignment_id=original.source.business_id, kind="problem_source", original_name="native.pdf",
        content=data, content_type="application/pdf")
    req = original.model_copy(update={"source": original.source.model_copy(update={
        "stored_file_id": stored.id, "content_type": "application/pdf", "input_sha256": hashlib.sha256(data).hexdigest()}),
        "policy": RecognitionPolicyV1()})
    service = RecognitionRunService(store=results.store, capacity=agent.capacity)
    indexed = []
    original_read = local.read_pdf_evidence
    async def observed(content, command, **kwargs):
        result = await original_read(content, command, **kwargs)
        if isinstance(command, PdfIndexRequest):
            indexed.extend(page.page_number for page in result.pages)
        return result
    monkeypatch.setattr(local, "read_pdf_evidence", observed)
    saved = []
    async def on_pages(pages):
        saved.extend(pages)
    _, summary = await read_question_pdf_batches(service=service, request=req, content=data,
        engine=agent.engine, total_pages=12, reporter=ProgressReporter("native-window"), on_pages=on_pages)
    assert indexed == list(range(1, 13))
    assert len(summary["operation_ids"]) == 1
    assert len(saved) == 12 and not agent.engine.reads


@pytest.mark.asyncio
async def test_real_saved_page_runs_cap_rechecks_and_reuse_without_new_calls(tmp_path):
    from backend.services.recognition_runs import RecognitionRunService

    agent, req, data, results = setup(tmp_path, pdf=True, pages=6, initial=["[unclear] + 1 = 3"] * 6)
    service = RecognitionRunService(store=results.store, capacity=agent.capacity)
    async def on_pages(pages):
        pass
    args = dict(service=service, request=req, content=data, engine=agent.engine,
        total_pages=6, reporter=ProgressReporter("actual-window"), on_pages=on_pages, allow_partial=True)
    _, first = await read_question_pdf_batches(**args)
    assert len(agent.engine.reads) == 6 and len(agent.engine.repairs) == 4
    _, second = await read_question_pdf_batches(**args)
    assert len(agent.engine.reads) == 6 and len(agent.engine.repairs) == 4
    assert sorted(second["operation_ids"]) == sorted(first["operation_ids"])
    assert second["usage"]["initial_calls"] == 0 and second["usage"]["patch_calls"] == 0


@pytest.mark.asyncio
async def test_real_200_page_pdf_covers_every_page_and_reuses_saved_batches(tmp_path, monkeypatch):
    from backend.db import file_repository

    _agent, original, _data, results = setup(tmp_path)
    # Plain-text native evidence needs no OCR. Exercise the actual PDF worker,
    # authorization, operation IDs, artifacts and resume path for all 200 pages.
    data = pdf([f"Exercise {page}. Explain the stated property in words." for page in range(1, 201)])
    stored = file_repository.save_file(
        storage=results.store.storage, owner_id=original.source.owner_id,
        assignment_id=original.source.business_id, kind="problem_source",
        original_name="200-pages.pdf", content=data, content_type="application/pdf",
    )
    monkeypatch.setattr(question_sources, "get_storage", lambda: results.store.storage)
    args = dict(owner_id=original.source.owner_id, task_id=original.source.business_id, content=data,
                filename=stored.original_name, stored_file_id=stored.id, registry=None,
                route=StageProviderRoute(route_id="no-model-needed", kind="llm", provider=None))
    first = await question_sources.read_question_source(**args)
    second = await question_sources.read_question_source(**args)
    assert first.text == second.text
    assert "Exercise 1." in first.text and "Exercise 100." in first.text and "Exercise 200." in first.text
    assert first.recognition["coverage"]["processed_pages"] == list(range(1, 201))
    assert first.recognition["coverage"]["scope"] == "document"
    assert not first.recognition["coverage"]["unprocessed_pages"]
    assert len(first.recognition["operation_ids"]) == 17
    assert second.recognition["operation_ids"] == first.recognition["operation_ids"]
    assert second.recognition["status"] == "already_done"
    assert second.recognition["usage"]["initial_calls"] == 0
    selected = await question_sources.read_question_source(**args, options={"pages": [100, 200]})
    assert selected.recognition["coverage"]["scope"] == "pages"
    assert selected.recognition["coverage"]["processed_pages"] == [100, 200]
    assert "Exercise 100." in selected.text and "Exercise 200." in selected.text
    assert "Exercise 1." not in selected.text


def fake_result(pages, *, deferred=(), error=None):
    processed = [page for page in pages if page not in deferred]
    document = SimpleNamespace(
        pages=[SimpleNamespace(page_number=page, spans=[SimpleNamespace(final_text=f"Question {page}")]) for page in processed],
        coverage=SimpleNamespace(processed_pages=processed, unprocessed_pages=list(deferred), failed_pages=[]),
        confidence="high", confidence_reasons=[], warning_codes=[],
    )
    return SimpleNamespace(status="completed", operation_id=f"op-{pages}",
                           assembly=SimpleNamespace(document=document, raw=SimpleNamespace(read_batch=SimpleNamespace(
                               plan=SimpleNamespace(decisions=[SimpleNamespace(page_number=p, action="deferred") for p in deferred])))),
                           safe_error_code=error,
                           artifact_ids=[f"artifact-{pages}"], current_usage=RecognitionUsageV1(),
                           operation_usage=RecognitionUsageV1())


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["reference", "rubric", "test_cases", "submissions"])
async def test_non_question_pdf_sources_enumerate_every_page_and_emit_saved_pages(tmp_path, monkeypatch, purpose):
    from backend.db import file_repository

    _agent, original, _data, results = setup(tmp_path)
    data = pdf([f"Material page {page}. Describe the independent source." for page in range(1, 26)])
    stored = file_repository.save_file(storage=results.store.storage, owner_id=original.source.owner_id,
        assignment_id=original.source.business_id, kind="problem_source", original_name=f"{purpose}.pdf",
        content=data, content_type="application/pdf")
    monkeypatch.setattr(question_sources, "get_storage", lambda: results.store.storage)
    emitted = []
    async def on_pages(pages):
        emitted.extend(pages)
    read = await question_sources.read_question_source(owner_id=original.source.owner_id,
        task_id=original.source.business_id, content=data, filename=stored.original_name,
        stored_file_id=stored.id, registry=None, purpose=purpose, on_pages=on_pages,
        route=StageProviderRoute(route_id="no-model-needed", kind="llm", provider=None))
    assert read.recognition["coverage"]["processed_pages"] == list(range(1, 26))
    assert len(read.recognition["operation_ids"]) == 3
    assert [page["page_number"] for page in emitted] == list(range(1, 26))
    assert "Material page 25." in emitted[-1]["text"]


@pytest.mark.asyncio
async def test_deferred_pages_get_smaller_batches_without_repeating_processed_pages():
    calls = []
    async def run(scoped, *_args, **_kwargs):
        calls.append(scoped.pages)
        return fake_result(scoped.pages, deferred=[2] if len(scoped.pages) > 1 else [])
    text, summary = await read_question_pdf_batches(
        service=SimpleNamespace(run=run), request=request(b"pdf"), content=b"pdf", engine=None,
        total_pages=3, reporter=ProgressReporter("batches"),
    )
    assert calls == [[1, 2, 3], [2]]
    assert text == "Question 1\n\nQuestion 2\n\nQuestion 3"
    assert summary["coverage"]["processed_pages"] == [1, 2, 3]


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_partial", [False, True])
async def test_uncertain_page_does_not_replay_but_later_untouched_pages_and_batches_continue(allow_partial):
    calls, callbacks = [], []
    async def run(scoped, *_args, **_kwargs):
        calls.append(scoped.pages)
        if len(calls) == 1:
            result = fake_result(scoped.pages, deferred=list(range(2, 13)), error="provider_submit_uncertain")
            result.assembly.document.coverage.failed_pages = [2]
            result.assembly.document.coverage.unprocessed_pages = list(range(3, 13))
            result.assembly.raw.read_batch.plan.decisions = []
            result.assembly.raw.read_batch.units = [SimpleNamespace(page_numbers=[1], candidate=SimpleNamespace(status="ok")),
                SimpleNamespace(page_numbers=[2], candidate=SimpleNamespace(status="error",
                    safe_error_code="provider_submit_uncertain", upstream_status_code=503))]
            return result
        return fake_result(scoped.pages)
    async def on_pages(pages):
        callbacks.extend(page["page_number"] for page in pages)
    args = dict(service=SimpleNamespace(run=run), request=request(b"pdf"), content=b"pdf", engine=None,
        total_pages=25, reporter=ProgressReporter("batches"), allow_partial=allow_partial, on_pages=on_pages)
    if allow_partial:
        text, summary = await read_question_pdf_batches(**args)
        assert "Question 25" in text and "Question 2\n" not in text
        assert summary["status"] == "failed" and summary["error_code"] == "provider_submit_uncertain"
        assert summary["coverage"]["failed_pages"] == [2]
        assert summary["page_diagnostics"]["page_errors"] == [dict(page=2, error_code="provider_submit_uncertain", status_code=503)]
    else:
        with pytest.raises(RecognitionError) as caught:
            await read_question_pdf_batches(**args)
        assert caught.value.code == "provider_submit_uncertain"
        assert caught.value.details["failed_pages"] == [2]
        assert 25 in caught.value.details["processed_pages"]
    assert calls == [list(range(1, 13)), *[[page] for page in range(3, 13)], list(range(13, 25)), [25]]
    assert sorted(callbacks) == [page for page in range(1, 26) if page != 2]


@pytest.mark.asyncio
async def test_failed_batch_without_any_document_still_attempts_later_batch():
    calls = []
    async def run(scoped, *_args, **_kwargs):
        calls.append(scoped.pages)
        if len(calls) == 1:
            raise RecognitionError("provider_submit_uncertain")
        return fake_result(scoped.pages)
    with pytest.raises(RecognitionError) as caught:
        await read_question_pdf_batches(service=SimpleNamespace(run=run), request=request(b"pdf"),
            content=b"pdf", engine=None, total_pages=13, reporter=ProgressReporter("batches"))
    assert caught.value.code == "provider_submit_uncertain"
    assert calls == [list(range(1, 13)), [13]]
    assert caught.value.details["processed_pages"] == [13]


@pytest.mark.asyncio
async def test_active_page_eight_failure_keeps_pages_nine_and_ten_independent():
    calls, saved = [], []
    async def run(scoped, *_args, **_kwargs):
        calls.append(scoped.pages)
        if scoped.pages == [8]:
            raise RecognitionError("provider_submit_uncertain")
        return fake_result(scoped.pages)
    async def on_pages(pages):
        saved.extend(page["page_number"] for page in pages)
    text, summary = await read_question_pdf_batches(service=SimpleNamespace(run=run), request=request(b"pdf"),
        content=b"pdf", engine=SimpleNamespace(), total_pages=10, reporter=ProgressReporter("page-eight"),
        allow_partial=True, on_pages=on_pages)
    assert calls == [[page] for page in range(1, 11)]
    assert saved == [1, 2, 3, 4, 5, 6, 7, 9, 10]
    assert summary["status"] == "failed" and summary["coverage"]["failed_pages"] == [8]
    assert "Question 9" in text and "Question 10" in text


@pytest.mark.asyncio
async def test_unaligned_paid_output_is_not_treated_as_safe_deferred_work():
    result = fake_result([1, 2], deferred=[2])
    result.assembly.raw.read_batch.plan.decisions = []
    run = AsyncMock(return_value=result)
    with pytest.raises(RecognitionError) as caught:
        await read_question_pdf_batches(service=SimpleNamespace(run=run), request=request(b"pdf"),
            content=b"pdf", engine=None, total_pages=2, reporter=ProgressReporter("batches"))
    assert caught.value.code == "question_source_incomplete"
    assert run.await_count == 1
