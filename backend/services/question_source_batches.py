"""Read a complete question PDF with bounded, independently durable page batches."""
from backend.domain.errors import RecognitionError
from backend.llm.image_capability import image_quality_failure
from backend.recognition.models import RecognitionCoverageV1, RecognitionUsageV1
from backend.skills.recognition_reader import PROMPT_VERSION


def _usage(runs, attribute):
    values = [getattr(run, attribute) for run in runs]
    fields = RecognitionUsageV1.model_fields
    result = {}
    for name in fields:
        items = [getattr(value, name) for value in values]
        result[name] = all(items) if name == "usage_complete" else None if None in items else sum(items)
    return RecognitionUsageV1(**result).model_dump(mode="json")


async def read_question_pdf_batches(*, service, request, content, engine, total_pages, reporter, binding=None,
                                    allow_native_partial=False):
    requested = request.pages or list(range(1, total_pages + 1))
    # Twelve full-page reads fit the existing per-run call budget. Native text
    # still costs no model call. A budget-deferred page gets its own smaller run;
    # a failed/uncertain call is never automatically replayed.
    batches = [requested[offset:offset + 12] for offset in range(0, len(requested), 12)]
    runs, documents, pages = [], [], {}
    missing_native_pages = set()
    while batches:
        batch = batches.pop(0)
        await reporter.set_current_step("recognition_document_batch", message=(
            f"Reading PDF pages {batch[0]}–{batch[-1]}; {len(pages)}/{len(requested)} pages completed"))
        scoped = request.model_copy(update={"scope": "pages", "pages": batch})
        run = await service.run(scoped, content, engine=engine, prompt_version=PROMPT_VERSION,
                                authorized_owner_id=request.source.owner_id, binding=binding)
        if run.status == "already_running":
            raise RecognitionError("recognition_already_running")
        runs.append(run)
        document = run.assembly.document if run.assembly else None
        if document is None:
            raise RecognitionError(run.safe_error_code or "recognition_artifact_unavailable")
        documents.append(document)
        # Successful page evidence may be reused, but incomplete input must not
        # progress to question/answer generation as though the whole file read.
        native_partial = (allow_native_partial and engine is None
                          and run.safe_error_code in {None, "visual_capability_unavailable"})
        if (run.safe_error_code or document.coverage.failed_pages) and not native_partial:
            raise RecognitionError(image_quality_failure(run.safe_error_code or "question_source_incomplete", document),
                                   failed_pages=document.coverage.failed_pages,
                                   processed_pages=sorted(set(pages) | set(document.coverage.processed_pages)))
        for page in document.pages:
            if page.page_number in document.coverage.processed_pages:
                pages[page.page_number] = page
        pending = document.coverage.unprocessed_pages
        if native_partial:
            missing_native_pages.update(document.coverage.failed_pages)
            missing_native_pages.update(pending)
            continue
        if pending:
            read_batch = run.assembly.raw.read_batch
            deferred = {decision.page_number for decision in read_batch.plan.decisions
                        if decision.action == "deferred"} if read_batch else set()
            # Missing coverage alone is not proof that no paid call occurred:
            # unaligned provider output also counts as unprocessed evidence.
            if len(batch) == 1 or not set(pending).issubset(deferred):
                raise RecognitionError(image_quality_failure("question_source_incomplete", document))
            batches[0:0] = [[number] for number in pending]
    coverage = RecognitionCoverageV1(scope=request.scope, total_pages=total_pages,
                                     requested_pages=requested, processed_pages=sorted(pages),
                                     failed_pages=sorted(missing_native_pages))
    text = "\n\n".join(span.final_text for number in sorted(pages)
                        for span in pages[number].spans if span.final_text)
    if not text.strip():
        raise RecognitionError("provider_vision_not_supported" if allow_native_partial else image_quality_failure("ocr_empty_result", documents[-1] if documents else None))
    await reporter.set_current_step("recognition_document_complete", message=f"Read {len(pages)}/{len(requested)} requested PDF pages")
    unique = lambda values: list(dict.fromkeys(values))
    summary = dict(
        schema_version=1, operation_id=runs[0].operation_id,
        operation_ids=unique(run.operation_id for run in runs),
        status="already_done" if all(run.status == "already_done" for run in runs) else "completed",
        artifact_ids=unique(item for run in runs for item in run.artifact_ids),
        coverage=coverage.model_dump(mode="json"),
        confidence="low" if missing_native_pages else min((doc.confidence for doc in documents), key={"low": 0, "medium": 1, "high": 2}.get),
        confidence_reasons=unique(item for doc in documents for item in doc.confidence_reasons),
        warning_codes=unique(item for doc in documents for item in doc.warning_codes),
        error_code="visual_capability_unavailable" if missing_native_pages else None,
        usage=_usage(runs, "current_usage"), operation_usage=_usage(runs, "operation_usage"), requires_review=True,
    )
    return text, summary
