"""Read a complete question PDF with bounded, independently durable page batches."""
import asyncio
from backend.domain.errors import RecognitionError
from backend.llm.image_capability import image_quality_failure
from backend.recognition.local_limits import bounded_recognition_progress
from backend.recognition.models import RecognitionCoverageV1, RecognitionUsageV1
from backend.services.recognition_timeouts import apply_source_timeout_profile
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
                                    allow_native_partial=False, retry_key=None, acknowledge_pending_retry=False,
                                    timeout_profile=None, allow_partial=False, on_pages=None):
    requested = request.pages or list(range(1, total_pages + 1))
    incremental = on_pages is not None and engine is not None
    batches = [requested[offset:offset + 12] for offset in range(0, len(requested), 12)]
    runs, documents, pages = [], [], {}
    failed_pages, unprocessed_pages, page_errors, failure_codes = set(), set(), [], []

    def scoped_request(batch):
        scoped = request.model_copy(update={"scope": "pages", "pages": batch})
        return apply_source_timeout_profile(scoped, engine=engine, timeout_profile=timeout_profile,
                                             total_pages=total_pages)

    async def process(batch, repair_admission=None):
        scoped = scoped_request(batch)
        progress = reporter
        if scoped.policy.timeout_mode == "response_only":
            progress = bounded_recognition_progress(reporter)
        await progress.set_current_step("recognition_document_batch", message=(
            f"Reading PDF pages {batch[0]}–{batch[-1]}; {len(pages)}/{len(requested)} pages completed"))
        try:
            run = await service.run(scoped, content, engine=engine, prompt_version=PROMPT_VERSION,
                                    authorized_owner_id=request.source.owner_id, binding=binding, retry_key=retry_key,
                                    acknowledge_pending_retry=acknowledge_pending_retry,
                                    **({"repair_admission": repair_admission} if repair_admission is not None else {}))
        except RecognitionError as exc:
            # A failed independent batch must not suppress later pages. These
            # pages are recorded as failed, never automatically resubmitted.
            failed_pages.update(batch)
            failure_codes.append(exc.code)
            page_errors.extend(dict(page=number, error_code=exc.code, status_code=None) for number in batch)
            return
        if run.status == "already_running":
            raise RecognitionError("recognition_already_running")
        runs.append(run)
        document = run.assembly.document if run.assembly else None
        if document is None:
            code = run.safe_error_code or "recognition_artifact_unavailable"
            failed_pages.update(batch)
            failure_codes.append(code)
            page_errors.extend(dict(page=number, error_code=code, status_code=None) for number in batch)
            return
        documents.append(document)
        # Successful page evidence may be reused, but incomplete input must not
        # progress to question/answer generation as though the whole file read.
        native_partial = (allow_native_partial and engine is None
                          and run.safe_error_code in {None, "visual_capability_unavailable"})
        ready = []
        for page in document.pages:
            if page.page_number in document.coverage.processed_pages:
                pages[page.page_number] = page
                ready.append(dict(page_number=page.page_number,
                    text="\n".join(span.final_text for span in page.spans if span.final_text)))
        if ready and on_pages is not None:
            # service.run has already persisted and verified these page results.
            # A page completion is deliberately not a question-boundary claim.
            await on_pages(ready)
        failed_pages.update(document.coverage.failed_pages)
        code = image_quality_failure(run.safe_error_code or "question_source_incomplete", document)
        if run.safe_error_code or document.coverage.failed_pages:
            failure_codes.append(code)
        read_batch = getattr(getattr(run.assembly, "raw", None), "read_batch", None)
        for unit in getattr(read_batch, "units", []):
            if unit.candidate.status == "error":
                page_errors.extend(dict(page=number, error_code=unit.candidate.safe_error_code or "recognition_response_invalid",
                    status_code=unit.candidate.upstream_status_code) for number in unit.page_numbers
                    if number in document.coverage.failed_pages)
        pending = document.coverage.unprocessed_pages
        if native_partial:
            failed_pages.update(pending)
            return
        if pending:
            deferred = {decision.page_number for decision in read_batch.plan.decisions
                        if decision.action == "deferred"} if read_batch else set()
            # The executor can stop a batch after one failed request. Its
            # persisted unit ledger proves later untouched pages were never
            # sent. They may proceed independently; attempted/unaligned output
            # is never treated as safe deferred work.
            untouched = (set(pending) - {number for unit in read_batch.units for number in unit.page_numbers}
                         if read_batch is not None and hasattr(read_batch, "units") else set())
            safe_to_start = (set(pending) & (deferred | untouched)) if len(batch) > 1 else set()
            unresolved = set(pending) - safe_to_start
            if unresolved:
                unprocessed_pages.update(unresolved)
                failure_codes.append(code)
            batches[0:0] = [[number] for number in sorted(safe_to_start)]

    while batches:
        window = batches.pop(0)
        native = False
        if incremental and hasattr(service, "native_page_window"):
            native = await service.native_page_window(scoped_request(window), content, engine=engine,
                authorized_owner_id=request.source.owner_id, binding=binding)
        if not incremental or native:
            await process(window)
            continue
        scoped = [scoped_request([page]) for page in window]
        used = (await service.saved_page_extras(scoped, engine=engine, prompt_version=PROMPT_VERSION,
            authorized_owner_id=request.source.owner_id, binding=binding, retry_key=retry_key)
            if hasattr(service, "saved_page_extras") else {"patch": 0, "empty_recovery": 0})
        limits = {"patch": request.policy.max_patches, "empty_recovery": request.policy.max_empty_recoveries}
        allowance_lock = asyncio.Lock()

        async def admit(kind):
            async with allowance_lock:
                if used[kind] >= limits[kind]:
                    return False
                # Reserve before dispatch; a cancelled or uncertain submission
                # never refunds this window. The child also checkpoints it.
                used[kind] += 1
                return True

        # Bound prepared page work by the existing window. Actual model starts
        # share grading's credential/model RPM and adaptive resource scheduler.
        tasks = [asyncio.create_task(process([page], admit)) for page in window]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    failed_pages.difference_update(pages)
    unprocessed_pages.difference_update(pages)
    unprocessed_pages.update(set(requested) - set(pages) - failed_pages)
    coverage = RecognitionCoverageV1(scope=request.scope, total_pages=total_pages,
                                     requested_pages=requested, processed_pages=sorted(pages),
                                     failed_pages=sorted(failed_pages), unprocessed_pages=sorted(unprocessed_pages))
    text = "\n\n".join(span.final_text for number in sorted(pages)
                        for span in pages[number].spans if span.final_text)
    incomplete = bool(failed_pages or unprocessed_pages or failure_codes)
    error_code = ("provider_submit_uncertain" if "provider_submit_uncertain" in failure_codes
                  else failure_codes[0] if failure_codes else "question_source_incomplete") if incomplete else None
    diagnostics = dict(total_pages=total_pages, processed_pages=sorted(pages), failed_pages=sorted(failed_pages),
                       unprocessed_pages=sorted(unprocessed_pages), page_errors=page_errors)
    if not text.strip() or (incomplete and not (allow_partial or allow_native_partial)):
        raise RecognitionError(("provider_vision_not_supported" if allow_native_partial and not text.strip() else error_code) or (
            image_quality_failure("ocr_empty_result", documents[-1] if documents else None)),
            failed_pages=sorted(failed_pages | unprocessed_pages), processed_pages=sorted(pages),
            page_diagnostics=diagnostics)
    await reporter.set_current_step("recognition_document_complete", message=f"Read {len(pages)}/{len(requested)} requested PDF pages")
    unique = lambda values: list(dict.fromkeys(values))
    summary = dict(
        schema_version=1, operation_id=runs[0].operation_id,
        operation_ids=unique(run.operation_id for run in runs),
        status="failed" if incomplete else "already_done" if all(run.status == "already_done" for run in runs) else "completed",
        artifact_ids=unique(item for run in runs for item in run.artifact_ids),
        coverage=coverage.model_dump(mode="json"),
        confidence="low" if incomplete else min((doc.confidence for doc in documents), key={"low": 0, "medium": 1, "high": 2}.get),
        confidence_reasons=unique(item for doc in documents for item in doc.confidence_reasons),
        warning_codes=unique(item for doc in documents for item in doc.warning_codes),
        error_code=error_code, page_diagnostics=diagnostics,
        usage=_usage(runs, "current_usage"), operation_usage=_usage(runs, "operation_usage"), requires_review=True,
    )
    return text, summary
