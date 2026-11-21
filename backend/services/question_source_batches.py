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


def _truncated_submission_page(run, request):
    """Only a confirmed, complete provider response can authorize a second paid read."""
    if request.purpose != "submissions" or len(request.pages) != 1 or run.safe_error_code or not run.assembly:
        return False
    document = run.assembly.document
    batch = getattr(getattr(run.assembly, "raw", None), "read_batch", None)
    units = getattr(batch, "units", ())
    if not document or document.coverage.failed_pages != request.pages or len(units) != 1:
        return False
    unit = units[0]
    return (unit.page_numbers == request.pages and not unit.submission_may_exist
            and unit.candidate.status in {"ok", "empty"} and not unit.candidate.safe_error_code
            and unit.candidate.finish_reason == "length")


def _longer_submission_read(request):
    return request.model_copy(update={"policy": request.policy.model_copy(update={
        "version": "submission-truncated-page-v1", "enable_repair": False})})


async def read_question_pdf_batches(*, service, request, content, engine, total_pages, reporter, binding=None,
                                    allow_native_partial=False, retry_key=None, acknowledge_pending_retry=False,
                                    timeout_profile=None, allow_partial=False, on_pages=None):
    requested = request.pages or list(range(1, total_pages + 1))
    incremental = on_pages is not None and engine is not None
    batches = [requested[offset:offset + 12] for offset in range(0, len(requested), 12)]
    runs, documents, pages = [], [], {}
    failed_pages, unprocessed_pages, page_errors, failure_codes = set(), set(), [], []

    panel_pages = set()
    def scoped_request(batch):
        scoped = request.model_copy(update={"scope": "pages", "pages": batch})
        if request.purpose == "problems" and len(batch) == 1 and batch[0] in panel_pages:
            scoped = scoped.model_copy(update={"policy": scoped.policy.model_copy(update={"version": "printed-formula-panels-v2"})})
        return apply_source_timeout_profile(scoped, engine=engine, timeout_profile=timeout_profile,
                                             total_pages=total_pages)

    async def process(batch, repair_admission=None, saved_rereads=()):
        scoped = scoped_request(batch)
        progress = reporter
        if scoped.policy.timeout_mode == "response_only":
            progress = bounded_recognition_progress(reporter)
        await progress.set_current_step("recognition_document_batch", message=(
            f"Reading PDF pages {batch[0]}–{batch[-1]}; {len(pages)}/{len(requested)} pages completed"))
        async def read(page_request):
            return await service.run(page_request, content, engine=engine, prompt_version=PROMPT_VERSION,
                                     authorized_owner_id=request.source.owner_id, binding=binding, retry_key=retry_key,
                                     acknowledge_pending_retry=acknowledge_pending_retry,
                                     **({"repair_admission": repair_admission} if repair_admission is not None else {}))
        try:
            run = await read(scoped)
            if (_truncated_submission_page(run, scoped) and repair_admission is not None
                    and (batch[0] in saved_rereads or await repair_admission("empty_recovery"))):
                # Count the initial billable read and retain its evidence. The second
                # policy has a separate durable/cache identity and cannot recurse.
                runs.append(run)
                await progress.set_current_step("recognition_recheck", message=f"Retrying truncated submission page {batch[0]}")
                run = await read(_longer_submission_read(scoped))

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
                    **({"formula_conflicts": page.formula_conflicts} if getattr(page, "formula_conflicts", None) else {}),
                    text="\n".join(span.final_text for span in page.spans if span.final_text)))
        if ready and on_pages is not None:
            # service.run has already persisted and verified these page results.
            # A page completion is deliberately not a question-boundary claim.
            await on_pages(ready)
        failed_pages.update(document.coverage.failed_pages)
        code = image_quality_failure(run.safe_error_code or "question_source_incomplete", document)
        if _truncated_submission_page(run, scoped):
            code = "submission_page_output_truncated"
        if run.safe_error_code or document.coverage.failed_pages:
            failure_codes.append(code)
        read_batch = getattr(getattr(run.assembly, "raw", None), "read_batch", None)
        unassigned_failures = set(document.coverage.failed_pages)
        for unit in getattr(read_batch, "units", []):
            candidate = unit.candidate
            refused = (getattr(candidate, "finish_reason", None) == "refused"
                       or "provider_refused" in getattr(candidate, "warning_codes", ()))
            if code == "submission_page_output_truncated":
                page_errors.extend(dict(page=number, error_code=code, status_code=None) for number in unit.page_numbers)
                unassigned_failures.difference_update(unit.page_numbers)
            elif candidate.status == "error" or refused:
                cause = candidate.safe_error_code or ("provider_content_blocked" if refused else "recognition_response_invalid")
                page_errors.extend(dict(page=number, error_code=cause, status_code=candidate.upstream_status_code)
                                   for number in unit.page_numbers if number in document.coverage.failed_pages)
                unassigned_failures.difference_update(unit.page_numbers)
        # Local rendering stops before a model read unit exists. Earlier failed
        # reads in this batch retain their own page diagnostics.
        if "pdf_render_limit_exceeded" in getattr(read_batch, "stop_codes", ()):
            page_errors.extend(dict(page=number, error_code="pdf_render_limit_exceeded", status_code=None)
                               for number in sorted(unassigned_failures))
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
        if incremental and hasattr(service, "inspect_page_window"):
            native, panel_numbers = await service.inspect_page_window(scoped_request(window), content, engine=engine,
                authorized_owner_id=request.source.owner_id, binding=binding)
            panel_pages.update(panel_numbers)
        if not incremental or native:
            await process(window)
            continue
        scoped = [scoped_request([page]) for page in window]
        ledger_requests = scoped + [_longer_submission_read(page) for page in scoped] if request.purpose == "submissions" else scoped
        used = (await service.saved_page_extras(ledger_requests, engine=engine, prompt_version=PROMPT_VERSION,
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
        tasks = [asyncio.create_task(process([page], admit, used.get("truncated_pages", ()))) for page in window]
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
        **({"formula_conflicts": sorted({label for page in pages.values() for label in getattr(page,"formula_conflicts",[])})}
           if any(getattr(page,"formula_conflicts",None) for page in pages.values()) else {}),
        usage=_usage(runs, "current_usage"), operation_usage=_usage(runs, "operation_usage"), requires_review=True,
    )
    return text, summary
