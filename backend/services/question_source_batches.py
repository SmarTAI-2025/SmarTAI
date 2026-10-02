"""Read a complete question PDF with bounded, independently durable page batches."""
from backend.domain.errors import RecognitionError
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


async def read_question_pdf_batches(*, service, request, content, engine, total_pages, reporter, binding=None):
    requested = request.pages or list(range(1, total_pages + 1))
    # Twelve full-page reads fit the existing per-run call budget. Native text
    # still costs no model call. A budget-deferred page gets its own smaller run;
    # a failed/uncertain call is never automatically replayed.
    batches = [requested[offset:offset + 12] for offset in range(0, len(requested), 12)]
    runs, documents, pages = [], [], {}
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
        if run.safe_error_code or document.coverage.failed_pages:
            raise RecognitionError(run.safe_error_code or "question_source_incomplete")
        for page in document.pages:
            if page.page_number in document.coverage.processed_pages:
                pages[page.page_number] = page
        pending = document.coverage.unprocessed_pages
        if pending:
            read_batch = run.assembly.raw.read_batch
            deferred = {decision.page_number for decision in read_batch.plan.decisions
                        if decision.action == "deferred"} if read_batch else set()
            # Missing coverage alone is not proof that no paid call occurred:
            # unaligned provider output also counts as unprocessed evidence.
            if len(batch) == 1 or not set(pending).issubset(deferred):
                raise RecognitionError("question_source_incomplete")
            batches[0:0] = [[number] for number in pending]
    coverage = RecognitionCoverageV1(scope=request.scope, total_pages=total_pages,
                                     requested_pages=requested, processed_pages=sorted(pages))
    text = "\n\n".join(span.final_text for number in sorted(pages)
                        for span in pages[number].spans if span.final_text)
    if not text.strip():
        raise RecognitionError("ocr_empty_result")
    await reporter.set_current_step("recognition_document_complete", message=f"Read all {len(requested)} requested PDF pages")
    unique = lambda values: list(dict.fromkeys(values))
    summary = dict(
        schema_version=1, operation_id=runs[0].operation_id,
        operation_ids=unique(run.operation_id for run in runs),
        status="already_done" if all(run.status == "already_done" for run in runs) else "completed",
        artifact_ids=unique(item for run in runs for item in run.artifact_ids),
        coverage=coverage.model_dump(mode="json"),
        confidence=min((doc.confidence for doc in documents), key={"low": 0, "medium": 1, "high": 2}.get),
        confidence_reasons=unique(item for doc in documents for item in doc.confidence_reasons),
        warning_codes=unique(item for doc in documents for item in doc.warning_codes), error_code=None,
        usage=_usage(runs, "current_usage"), operation_usage=_usage(runs, "operation_usage"), requires_review=True,
    )
    return text, summary
