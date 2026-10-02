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
async def test_uncertain_batch_stops_without_replaying_or_starting_next_batch():
    run = AsyncMock(return_value=fake_result(list(range(1, 13)), error="provider_submit_uncertain"))
    with pytest.raises(RecognitionError) as caught:
        await read_question_pdf_batches(service=SimpleNamespace(run=run), request=request(b"pdf"),
            content=b"pdf", engine=None, total_pages=200, reporter=ProgressReporter("batches"))
    assert caught.value.code == "provider_submit_uncertain"
    assert run.await_count == 1


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
