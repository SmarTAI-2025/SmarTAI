"""Source-bound, one-extra-call rechecks; initial evidence is never overwritten."""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from pydantic import ValidationError

from backend.domain.errors import PdfEvidenceError, RecognitionError
from backend.recognition.budget import RecognitionBudget, validate_budget_accounting
from backend.recognition.engine import EngineRepairInputV1, RecognitionEngine
from backend.recognition.models import NormalizedRegionV1, RecognitionDocumentV1, RecognitionPatchV1
from backend.recognition.repair_records import (
    RECHECK_VERSION, RepairCallEvidenceV1, RepairExecutionV1, RepairImageEvidenceV1,
)
from backend.recognition.repair_response import REPAIR_PROMPT_VERSION, parse_repair_response
from backend.recognition.repair_selection import select_repair_targets
from backend.recognition.runtime import RecognitionCapacity, region_budget_key, run_repair_call
from backend.tools.pdf_evidence import (
    ImagePrepareRequest, ImagePreparedMetadata, ImagePreparedResult, PdfRenderRequest,
    PdfRenderResult, decode_pdf_payload, read_image_evidence, read_pdf_evidence, whole_page_render_size,
)

if TYPE_CHECKING:
    from backend.progress.tracker import ProgressReporter
    from backend.recognition.fusion import RecognitionAssemblyV1
    from backend.services.recognition_local_evidence import RecognitionLocalEvidenceReader


def _codes(values, limit=32):
    unique = list(dict.fromkeys(values))
    return unique if len(unique) <= limit else unique[:limit - 1] + ["additional_diagnostics_omitted"]


def _initial_halt(initial):
    raw = initial.raw
    if raw.schema_version == 2:
        provenance = [*raw.locator_provenance, *(raw.read_batch.provenance if raw.read_batch else [])]
        if any(record.storage_error for record in provenance):
            return "repair_initial_persistence_failed"
        if any(code.startswith("recognition_cache_") for code in raw.stop_codes):
            return "repair_initial_cache_failed"
    if raw.budget.pending_calls:
        return "repair_initial_submission_pending"
    if raw.locator_halted:
        return "repair_initial_localization_failed"
    if raw.read_batch and any(
        unit.candidate.status == "error" or unit.candidate.safe_error_code is not None
        or unit.candidate.finish_reason == "refused" or "provider_refused" in unit.candidate.warning_codes
        for unit in raw.read_batch.units
    ):
        return "repair_initial_read_failed"
    return None


def _records(raw):
    if raw.schema_version == 2:
        from backend.recognition.workflow_v2 import current_records
        return current_records(raw)
    result = [(call.result.candidate, call.submission_may_exist, call.requested_output_tokens)
              for call in raw.locator_calls]
    if raw.read_batch:
        result.extend((unit.candidate, unit.submission_may_exist, unit.requested_output_tokens)
                      for unit in raw.read_batch.units)
    return result


def _validate_image(raw, target, unit, image):
    if (image.source_sha256 != raw.request.source.input_sha256
            or image.page_number != target.context.page_number or image.region != target.context.region):
        raise ValueError("recheck image must retain its source page and exact region")
    if raw.request.source.content_type == "application/pdf":
        if image.preparation != "pdf_render_scale_2" or image.image_preparation is not None:
            raise ValueError("PDF recheck cannot invent image-source metadata")
        page = next(page for page in raw.read_batch.pages if page.page_number == image.page_number)
        if (image.width, image.height) != whole_page_render_size(page.width_points, page.height_points):
            raise ValueError("PDF recheck dimensions disagree with source geometry")
    elif image.preparation != "oriented_image" or image.image_preparation != unit.image_preparation:
        raise ValueError("recheck must preserve the inspected image transformation")
    # D2b changes the evidence prompt, not pixels. New transforms need their own
    # explicit policy/version rather than a false quality-improvement claim.
    if unit.input_mode == "page_image" and (
        image.payload_sha256 != unit.payload_sha256 or image.payload_bytes != unit.payload_bytes
    ):
        raise ValueError("same-source recheck pixels differ from the original inspected region")


def _validate_execution(initial, execution):
    raw, caps = initial.raw, initial.raw.engine_capabilities
    expected = select_repair_targets(initial)
    if execution.selection != expected or execution.prompt_version != REPAIR_PROMPT_VERSION:
        raise ValueError("recheck must retain its exact initial selection and prompt")
    if [call.unit_id for call in execution.calls] != [target.unit_id for target in expected.targets[:len(execution.calls)]]:
        raise ValueError("rechecks must be a prefix of the selected original units")
    if len(execution.calls) < len(expected.targets) and not execution.stop_codes:
        raise ValueError("unattempted selected rechecks require an explicit stop")
    halt = _initial_halt(initial)
    if halt and (execution.calls or halt not in execution.stop_codes):
        raise ValueError("initial failed or pending submissions must halt every recheck")
    units = {unit.unit_id: unit for unit in raw.read_batch.units} if raw.read_batch else {}
    records, counts = _records(raw), {"empty_recovery": 0, "patch": 0}
    for index, call in enumerate(execution.calls):
        target, unit = expected.targets[index], units[call.unit_id]
        image = call.image
        if image is not None:
            _validate_image(raw, target, unit, image)
        if call.result is None:
            if index != len(execution.calls) - 1 or call.safe_error_code not in execution.stop_codes:
                raise ValueError("local or budget failure must stop without a paid outcome")
            continue
        candidate = call.result.candidate
        if (caps is None or call.result.context != target.context
                or candidate.provider_route_id != caps.route_id or candidate.kind != caps.candidate_kind):
            raise ValueError("repair result belongs to another context or engine")
        if (candidate.status == "not_run" or candidate.safe_error_code is not None and candidate.status != "error"
                or candidate.safe_error_code == "provider_submit_uncertain" and not call.submission_may_exist):
            raise ValueError("repair error and uncertainty accounting is inconsistent")
        if call.submission_may_exist or call.result.parse_status != "ok":
            if index != len(execution.calls) - 1 or not execution.stop_codes:
                raise ValueError("failed or uncertain recheck must halt subsequent calls")
        records.append((candidate, call.submission_may_exist, call.requested_output_tokens))
        counts[target.kind] += 1
    budget = execution.budget
    expected_counts = {
        "locator_calls": raw.budget.locator_calls, "initial_calls": raw.budget.initial_calls,
        "empty_recovery_calls": counts["empty_recovery"], "patch_calls": counts["patch"],
        "read_calls": raw.budget.read_calls + sum(counts.values()),
        "total_calls": raw.budget.total_calls + sum(counts.values()),
    }
    if any(getattr(budget, field) != value for field, value in expected_counts.items()):
        raise ValueError("final budget must retain all initial and additional calls exactly once")
    validate_budget_accounting(budget, caps, records)
    if budget.duration_ms < raw.budget.duration_ms or budget.global_remaining_seconds > raw.budget.global_remaining_seconds:
        raise ValueError("recheck cannot restart the original deadline")
    if raw.schema_version == 2:
        from backend.recognition.workflow_v2 import validate_logical, validate_provenance
        for kind in ("empty_recovery", "patch"):
            leaves = [call for call in execution.calls if call.result is not None
                      and next(target for target in expected.targets if target.unit_id == call.unit_id).kind == kind]
            provenance = [record for record in execution.provenance if record.kind == kind]
            if any(record.origin != "dispatch" for record in provenance):
                raise ValueError("unverified repair proposals cannot be reused as successful calls")
            validate_provenance(leaves, provenance, kind=kind, source=raw.request.source,
                                purpose=raw.request.purpose, policy=raw.execution_policy, capabilities=caps)
        if len(execution.provenance) != sum(call.result is not None for call in execution.calls):
            raise ValueError("repair provenance must account for dispatched calls only")
        for record in execution.provenance:
            if record.storage_error and (record.storage_error not in execution.stop_codes
                                         or not execution.calls or execution.calls[-1] != record.original):
                raise ValueError("repair persistence failure must halt all subsequent work")
        validate_logical(raw, execution.logical_budget, execution.calls, expected)


def build_rechecked_document(initial: RecognitionAssemblyV1, execution: RepairExecutionV1):
    """Re-derive final output from original evidence and explicit bounded changes."""
    execution_type = RepairExecutionV1
    if initial.schema_version == 2:
        from backend.recognition.workflow_v2 import RepairExecutionV2
        execution_type = RepairExecutionV2
    execution = execution_type.model_validate(execution.model_dump(warnings=False))
    _validate_execution(initial, execution)
    if initial.document is None:
        return None, initial.safe_error_code
    document = initial.document.model_copy(deep=True)
    pages = {page.page_number: page for page in document.pages}
    recovered = set()
    for call in execution.calls:
        if call.result is None:
            continue
        result = call.result
        span = next(span for span in pages[result.context.page_number].spans if span.span_id == result.context.span_id)
        applied = result.parse_status == "ok" and result.decision != "still_unknown" and not call.submission_may_exist
        span.patch = RecognitionPatchV1(
            status="applied" if applied else "still_unknown" if result.parse_status == "ok" else "rejected",
            attempt_count=1, decision=result.decision if not call.submission_may_exist else "still_unknown",
            before_text=result.context.before_text, proposed_text=result.proposed_text if not call.submission_may_exist else "",
            final_text=result.final_text if applied else result.context.before_text,
            unresolved_codes=_codes([*span.issues, *result.reason_codes]),
        )
        if applied:
            span.final_text = result.final_text
            span.adopted_from = ("native" if result.decision == "keep_native" else span.visual.kind
                                 if result.decision == "keep_visual" else "repair")
            recovered.add(result.context.page_number)
        span.confidence = "low"
        span.confidence_reasons = _codes([*span.confidence_reasons, *result.reason_codes, "repair_fidelity_unverified"])
        span.issues = _codes([*span.issues, "repair_fidelity_unverified"])
    # A complete-region recheck can recover an empty read, not missing targets,
    # unaligned page groups or unprocessed regions (selection excludes them).
    if recovered:
        coverage = document.coverage
        coverage.failed_pages = sorted(set(coverage.failed_pages) - recovered)
        coverage.unprocessed_pages = sorted(set(coverage.unprocessed_pages) - recovered)
        coverage.processed_pages = sorted(set(coverage.processed_pages) | recovered)
    final_text = "\n\n".join(span.final_text for page in document.pages for span in page.spans if span.final_text)
    if len(final_text) > 400_000:
        return None, "recognition_assembly_limit"
    document.final_markdown = final_text
    document.warning_codes = _codes([*document.warning_codes, *execution.stop_codes], 64)
    if execution.calls:
        document.confidence = "low"
        document.confidence_reasons = _codes([*document.confidence_reasons, "repair_fidelity_unverified"])
    ledger = execution.budget
    for field in ("locator_calls", "initial_calls", "empty_recovery_calls", "patch_calls", "input_tokens",
                  "output_tokens", "duration_ms", "usage_complete"):
        setattr(document.usage, field, getattr(ledger, field))
    identity = {"initial": document.result_cache_key, "recheck": RECHECK_VERSION, "prompt": execution.prompt_version}
    document.result_cache_key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return RecognitionDocumentV1.model_validate(document.model_dump(warnings=False)), None


async def _prepare_image(source_bytes, source, target, budget, progress, *, total_pages, local_reader=None):
    context = target.context
    if source.content_type == "application/pdf":
        command = PdfRenderRequest(page_number=context.page_number, region=context.region.as_tuple(), scale=2)
        if local_reader is not None:
            result = (await local_reader.read(source_bytes, command, timeout_seconds=min(10, budget.remaining("read")))).evidence
        else:
            result = await read_pdf_evidence(
                source_bytes, command, timeout_seconds=min(10, budget.remaining("read")), progress=progress,
            )
        if (not isinstance(result, PdfRenderResult) or result.total_pages != total_pages
                or result.page_number != context.page_number or result.region != context.region.as_tuple()):
            raise RecognitionError("recognition_response_invalid")
        preparation, metadata, region = "pdf_render_scale_2", None, result.region
    else:
        command = ImagePrepareRequest(content_type=source.content_type, region=context.region.as_tuple())
        if local_reader is not None:
            result = (await local_reader.read(source_bytes, command, timeout_seconds=min(10, budget.remaining("read")))).evidence
        else:
            result = await read_image_evidence(
                source_bytes, command, timeout_seconds=min(10, budget.remaining("read")), progress=progress,
            )
        if not isinstance(result, ImagePreparedResult):
            raise RecognitionError("recognition_response_invalid")
        metadata = ImagePreparedMetadata.model_validate({name: getattr(result, name) for name in ImagePreparedMetadata.model_fields})
        preparation, region = "oriented_image", metadata.effective_region
    payload = decode_pdf_payload(result)
    image = RepairImageEvidenceV1(
        source_sha256=source.input_sha256, page_number=context.page_number,
        region=NormalizedRegionV1(**dict(zip(("x0", "y0", "x1", "y1"), region))),
        payload_sha256=hashlib.sha256(payload).hexdigest(), payload_bytes=len(payload),
        width=result.width, height=result.height, preparation=preparation, image_preparation=metadata,
    )
    return payload, image


async def recheck_recognition(
    initial: RecognitionAssemblyV1, source_bytes: bytes, *, authorized_owner_id: str,
    engine: RecognitionEngine | None, budget: RecognitionBudget, capacity: RecognitionCapacity,
    progress: ProgressReporter | None = None,
    local_reader: RecognitionLocalEvidenceReader | None = None,
    call_session=None,
) -> RecognitionAssemblyV1:
    """Use the original live reservation authority, never a reconstituted ledger."""
    from backend.recognition.fusion import RecognitionAssemblyV1

    assembly_type, execution_type = RecognitionAssemblyV1, RepairExecutionV1
    if call_session is not None:
        from backend.recognition.workflow_v2 import RecognitionAssemblyV2, RepairExecutionV2
        assembly_type, execution_type = RecognitionAssemblyV2, RepairExecutionV2
    try:
        initial = assembly_type.model_validate(initial.model_dump(warnings=False))
    except (ValidationError, AttributeError, TypeError):
        raise RecognitionError("recognition_request_invalid") from None
    if initial.repair_execution is not None:
        raise RecognitionError("recognition_request_invalid")
    raw, source = initial.raw, initial.raw.request.source
    if (authorized_owner_id != source.owner_id or not isinstance(source_bytes, bytes)
            or hashlib.sha256(source_bytes).hexdigest() != source.input_sha256):
        raise RecognitionError("recognition_source_mismatch")
    if local_reader is not None:
        local_reader.assert_context(source, authorized_owner_id=authorized_owner_id)
    budget.assert_context(source, raw.execution_policy, raw.engine_capabilities)
    if call_session is not None and budget.logical_snapshot() != raw.logical_budget:
        raise RecognitionError("recognition_plan_changed")
    # Reading consumed these calls on this exact live ledger. A fresh same-context
    # budget is not authority to replay them or to obtain additional credits.
    snapshot = budget.snapshot()
    for field in ("total_calls", "locator_calls", "initial_calls", "patch_calls", "empty_recovery_calls",
                  "settled_calls", "pending_calls", "known_input_tokens", "known_output_tokens",
                  "reserved_output_tokens", "charged_output_tokens"):
        if getattr(snapshot, field) != getattr(raw.budget, field):
            raise RecognitionError("recognition_plan_changed")
    selection = select_repair_targets(initial)
    calls, stops = [], []
    halt = _initial_halt(initial)
    if halt:
        stops.append(halt)
    else:
        units = {unit.unit_id: unit for unit in raw.read_batch.units} if raw.read_batch else {}
        for target in selection.targets:
            image = None
            try:
                if engine is None or engine.capabilities != raw.engine_capabilities:
                    raise RecognitionError("recognition_route_changed")
                if progress:
                    await progress.set_current_step("recognition_recheck", message="Rechecking uncertain source evidence")
                token_limit = budget.output_limit(2048)
                payload, image = await _prepare_image(source_bytes, source, target, budget, progress,
                                                      total_pages=raw.total_pages, local_reader=local_reader)
                unit = units[target.unit_id]
                try:
                    _validate_image(raw, target, unit, image)
                except ValueError:
                    raise RecognitionError("recognition_source_mismatch") from None
                result = await run_repair_call(
                    engine, EngineRepairInputV1(
                        purpose=raw.request.purpose, page_number=target.context.page_number, region=image.region,
                        content_type="image/png", payload=payload, max_output_tokens=token_limit, repair_context=target.context,
                    ), kind=target.kind, source=source, policy=raw.execution_policy, capabilities=raw.engine_capabilities,
                    initial_region_key=region_budget_key(unit.page_numbers[0], unit.region), budget=budget,
                    capacity=capacity, progress=progress,
                    call_session=call_session,
                )
                parsed = parse_repair_response(result.candidate, target.context)
                calls.append(RepairCallEvidenceV1(unit_id=unit.unit_id, image=image, requested_output_tokens=token_limit,
                                                  result=parsed, submission_may_exist=result.submission_may_exist))
                if call_session is not None:
                    error = await call_session.record(target.kind, calls[-1])
                    if error:
                        stops.append(error)
                        break
                if result.submission_may_exist or parsed.parse_status != "ok":
                    stops.append("repair_submission_pending" if result.submission_may_exist else parsed.reason_codes[0])
                    break
            except (RecognitionError, PdfEvidenceError) as exc:
                calls.append(RepairCallEvidenceV1(unit_id=target.unit_id, safe_error_code=exc.code))
                stops.append(exc.code)
                break
    extra = {} if call_session is None else dict(logical_budget=budget.logical_snapshot(),
                                                provenance=call_session.records("empty_recovery") + call_session.records("patch"))
    execution = execution_type(**extra, prompt_version=REPAIR_PROMPT_VERSION, selection=selection, calls=calls,
                                  stop_codes=stops, budget=budget.snapshot())
    document, error = build_rechecked_document(initial, execution)
    if progress:
        await progress.increment_stage_metrics(recognition_rechecks_finished=1)
    return assembly_type(raw=raw, prompt_version=initial.prompt_version, document=document,
                                safe_error_code=error, repair_execution=execution)
