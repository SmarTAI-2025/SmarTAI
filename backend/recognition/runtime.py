"""One frozen-route dispatch boundary for recognition workflow stages."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext
import hashlib
import json
from typing import Literal, TYPE_CHECKING

from backend.domain.errors import RecognitionError
from backend.llm.request_timeouts import response_timeout_scope
from backend.recognition.budget import RecognitionBudget
from backend.recognition.engine import EngineLocateInputV1, EngineReadInputV1, EngineRepairInputV1, RecognitionEngine, freeze_engine_input
from backend.recognition.local_limits import LOCAL_STEP_SECONDS, bounded_recognition_progress
from backend.recognition.models import (
    EvidenceModel, NormalizedRegionV1, RecognitionCandidateV1,
    RecognitionPolicyV1, RecognitionSourceRefV1,
)
from backend.recognition.planner import EngineCapabilitiesV1

if TYPE_CHECKING:
    from backend.progress.tracker import ProgressReporter


class RecognitionCapacity:
    """Process-local limits shared across purposes, not a distributed quota."""

    def __init__(self, *, global_limit: int = 2, owner_limit: int = 2):
        if type(global_limit) is not int or not 1 <= global_limit <= 64 or type(owner_limit) is not int or not 1 <= owner_limit <= 2:
            raise RecognitionError("recognition_request_invalid")
        self._condition = asyncio.Condition()
        self._global_limit, self._active, self._background = global_limit, 0, 0
        self._interactive_waiters = 0
        self._owner_limit = owner_limit
        self._owners: dict[str, int] = {}
        self._loop = None

    @asynccontextmanager
    async def lease(self, owner_id: str, *, background: bool = False):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RecognitionError("recognition_request_invalid")
        self._loop = loop
        acquired = False
        async with self._condition:
            self._interactive_waiters += int(not background)
            try:
                await self._condition.wait_for(lambda: self._active < self._global_limit
                    and self._owners.get(owner_id, 0) < self._owner_limit
                    and (not background or (self._background == 0 and not self._interactive_waiters)))
                self._active += 1
                self._background += int(background)
                self._owners[owner_id] = self._owners.get(owner_id, 0) + 1
                acquired = True
            finally:
                self._interactive_waiters -= int(not background)
                self._condition.notify_all()
        try:
            yield
        finally:
            if acquired:
                async with self._condition:
                    self._active -= 1
                    self._background -= int(background)
                    self._owners[owner_id] -= 1
                    if not self._owners[owner_id]:
                        del self._owners[owner_id]
                    self._condition.notify_all()


class RecognitionCallResultV1(EvidenceModel):
    candidate: RecognitionCandidateV1
    submission_may_exist: bool = False


def region_budget_key(page_number: int, region: NormalizedRegionV1) -> str:
    coordinates = [float(value) + 0.0 for value in region.as_tuple()]
    digest = hashlib.sha256(json.dumps(coordinates, separators=(",", ":")).encode()).hexdigest()
    return f"p{page_number}:{digest}"


async def run_initial_read(
    engine: RecognitionEngine,
    request: EngineReadInputV1,
    *,
    source: RecognitionSourceRefV1,
    policy: RecognitionPolicyV1,
    capabilities: EngineCapabilitiesV1,
    region_keys: tuple[str, ...],
    budget: RecognitionBudget,
    capacity: RecognitionCapacity,
    progress: ProgressReporter | None = None,
    call_session=None,
) -> RecognitionCallResultV1:
    return await _dispatch(engine, request, kind="initial", source=source, policy=policy,
                           capabilities=capabilities, region_keys=region_keys, budget=budget,
                           capacity=capacity, progress=progress, call_session=call_session)


async def run_locator_call(
    engine: RecognitionEngine, request: EngineLocateInputV1, *, source: RecognitionSourceRefV1,
    policy: RecognitionPolicyV1, capabilities: EngineCapabilitiesV1, budget: RecognitionBudget,
    capacity: RecognitionCapacity, progress: ProgressReporter | None = None,
    call_session=None,
) -> RecognitionCallResultV1:
    return await _dispatch(engine, request, kind="locator", source=source, policy=policy,
                           capabilities=capabilities, region_keys=(), budget=budget,
                           capacity=capacity, progress=progress, call_session=call_session)


async def run_repair_call(
    engine: RecognitionEngine, request: EngineRepairInputV1, *, kind: Literal["empty_recovery", "patch"],
    source: RecognitionSourceRefV1, policy: RecognitionPolicyV1, capabilities: EngineCapabilitiesV1,
    initial_region_key: str, budget: RecognitionBudget, capacity: RecognitionCapacity,
    progress: ProgressReporter | None = None,
    call_session=None,
) -> RecognitionCallResultV1:
    """One extra dispatch charged to the original region, never to a new span ID.

    The caller proves the repair's relationship to an initial unit and prepares
    the authorized image. The live budget enforces its settled outcome and shared
    extra-call allowance. No snapshot can reconstruct that reservation authority.
    """
    if kind not in {"empty_recovery", "patch"}:
        raise RecognitionError("recognition_request_invalid")
    return await _dispatch(engine, request, kind=kind, source=source, policy=policy,
                           capabilities=capabilities, region_keys=(initial_region_key,), budget=budget,
                           capacity=capacity, progress=progress, call_session=call_session)


async def _dispatch(
    engine: RecognitionEngine, request: EngineReadInputV1 | EngineLocateInputV1 | EngineRepairInputV1, *,
    kind: Literal["initial", "locator", "empty_recovery", "patch"], source: RecognitionSourceRefV1, policy: RecognitionPolicyV1,
    capabilities: EngineCapabilitiesV1, region_keys: tuple[str, ...], budget: RecognitionBudget,
    capacity: RecognitionCapacity, progress: ProgressReporter | None,
    call_session=None, before_send=None,
) -> RecognitionCallResultV1:
    """Dispatch once; an ambiguous completion remains charged and pending.

    Durable ownership and cancellation recovery belong to the application job.
    This process-local ledger does not authorize source access or persist a lock.
    """
    budget.assert_context(source, policy, capabilities)
    source, policy, capabilities = (value.model_copy(deep=True) for value in (source, policy, capabilities))
    request = freeze_engine_input(request)
    if (kind == "locator") != isinstance(request, EngineLocateInputV1):
        raise RecognitionError("recognition_request_invalid")
    if (kind in {"empty_recovery", "patch"}) != isinstance(request, EngineRepairInputV1):
        raise RecognitionError("recognition_request_invalid")
    if engine.capabilities != capabilities:
        raise RecognitionError("recognition_route_changed")
    if request.input_mode not in capabilities.visual_inputs:
        raise RecognitionError("recognition_input_unsupported")
    if kind == "locator" and (not capabilities.target_location or len(request.images) > capabilities.max_locator_images):
        raise RecognitionError("recognition_input_unsupported")
    if kind in {"empty_recovery", "patch"} and (
        not capabilities.response_recheck or not capabilities.semantic_repair or not policy.enable_repair
    ):
        raise RecognitionError("recognition_input_unsupported")
    method = "locate" if kind == "locator" else "repair" if kind in {"empty_recovery", "patch"} else "recognize"
    dispatch = getattr(engine, method, None)
    if not callable(dispatch):
        raise RecognitionError("recognition_input_unsupported")
    phase = "locator" if kind == "locator" else "read"
    if call_session is not None:
        return await call_session.dispatch(
            kind, request, source=source, policy=policy, capabilities=capabilities,
            region_keys=region_keys, budget=budget,
            dispatch=lambda before_send=None: _dispatch(engine, request, kind=kind, source=source, policy=policy,
                                      capabilities=capabilities, region_keys=region_keys, budget=budget,
                                      capacity=capacity, progress=progress, before_send=before_send),
        )
    if policy.timeout_mode == "response_only":
        return await _dispatch_response_only(
            engine, request, dispatch=dispatch, kind=kind, source=source, policy=policy,
            capabilities=capabilities, region_keys=region_keys, budget=budget,
            capacity=capacity, progress=bounded_recognition_progress(progress), before_send=before_send,
        )
    ticket = None
    uncertain = False
    failure_pages = (
        sorted(number for image in request.images for number in image.page_numbers)
        if isinstance(request, EngineLocateInputV1) else request.document_pages or [request.page_number]
    ) if source.content_type == "application/pdf" else []
    try:
        lease = capacity.lease(source.owner_id, background=True) if source.scope == "knowledge_document" else capacity.lease(source.owner_id)
        async with asyncio.timeout(budget.remaining(phase)), lease:
            budget.assert_context(source, policy, engine.capabilities)
            seconds = min(policy.per_call_seconds, budget.remaining(phase))
            ticket = budget.reserve(kind, max_output_tokens=request.max_output_tokens, region_keys=region_keys)
            async with asyncio.timeout(seconds):
                # Only extended task OCR policies opt in. Legacy policies and
                # knowledge reads retain their configured relay I/O limits.
                scope = response_timeout_scope(seconds) if (
                    policy.per_call_seconds > 120 and capabilities.candidate_kind == "vision"
                    and source.scope != "knowledge_document" and request.purpose != "knowledge"
                ) else nullcontext()
                with scope:
                    candidate = await dispatch(request)
            candidate = RecognitionCandidateV1.model_validate(candidate.model_dump(warnings=False))
            if engine.capabilities != capabilities:
                raise RecognitionError("recognition_route_changed", submission_may_exist=True)
            if candidate.status == "not_run" or candidate.provider_route_id != capabilities.route_id or candidate.kind != capabilities.candidate_kind:
                raise RecognitionError("recognition_response_invalid", submission_may_exist=True)
            if candidate.status != "error" and candidate.safe_error_code is not None:
                raise RecognitionError("recognition_response_invalid", submission_may_exist=True)
            if candidate.status == "error" and progress:
                await progress.record_provider_failure(
                    RecognitionError(candidate.safe_error_code or "recognition_response_invalid"),
                    pages=failure_pages,
                )
            if candidate.safe_error_code == "provider_submit_uncertain":
                uncertain = True
    except TimeoutError as exc:
        if ticket is None:
            raise RecognitionError("recognition_timeout") from None
        from backend.services.execution_control import current_execution, provider_key
        control = current_execution()
        provider = getattr(engine, "provider", None)
        wait = control.waits.get(provider_key(provider)) if control is not None and provider is not None else None
        # An outer OCR deadline can expire during backoff after an explicit
        # rejection. No request is in flight then; do not turn it into an
        # uncertain paid submission or give the next phase a fresh budget.
        import time
        rejected_wait = bool(wait and wait["retry_at"] > time.time())
        uncertain = not rejected_wait
        if rejected_wait:
            control.block(provider, "provider_rate_limited")
        elif progress:
            await progress.record_provider_failure(exc, pages=failure_pages)
        candidate = RecognitionCandidateV1(
            kind=capabilities.candidate_kind, status="error", provider_route_id=capabilities.route_id,
            safe_error_code="provider_rate_limited" if rejected_wait else "provider_submit_uncertain",
        )
    except RecognitionError as exc:
        if ticket is None:
            raise
        if progress:
            await progress.record_provider_failure(exc, pages=failure_pages)
        uncertain = exc.submission_may_exist or exc.code == "provider_submit_uncertain"
        candidate = RecognitionCandidateV1(
            kind=capabilities.candidate_kind, status="error", provider_route_id=capabilities.route_id,
            safe_error_code=exc.code,
            upstream_status_code=exc.upstream_status_code,
        )
    except Exception:
        if ticket is None:
            raise RecognitionError("recognition_response_invalid") from None
        uncertain = True
        if progress:
            await progress.record_provider_failure(
                RecognitionError("recognition_response_invalid", submission_may_exist=True), pages=failure_pages,
            )
        candidate = RecognitionCandidateV1(
            kind=capabilities.candidate_kind, status="error", provider_route_id=capabilities.route_id,
            safe_error_code="recognition_response_invalid",
        )
    if not uncertain:
        failed = candidate.status == "error" or candidate.finish_reason == "refused" or "provider_refused" in candidate.warning_codes
        budget.settle(ticket, outcome="failed" if failed else candidate.status,
                      input_tokens=candidate.input_tokens, output_tokens=candidate.output_tokens)
    if progress:
        await progress.increment_stage_metrics(**{f"recognition_{kind}_calls": 1})
    return RecognitionCallResultV1(candidate=candidate, submission_may_exist=uncertain)


async def _dispatch_response_only(
    engine, request, *, dispatch, kind, source, policy, capabilities, region_keys,
    budget, capacity, progress, before_send,
):
    """Charge only admitted submissions; local queues have a separate allowance."""
    from backend.config import settings
    from backend.llm.providers import ProviderRequestError

    if capabilities.candidate_kind != "vision" or source.scope == "knowledge_document" or request.purpose == "knowledge":
        raise RecognitionError("recognition_request_invalid")
    ticket = None
    state = None
    authorization_error = None
    failure_pages = (
        sorted(number for image in request.images for number in image.page_numbers)
        if isinstance(request, EngineLocateInputV1) else request.document_pages or [request.page_number]
    ) if source.content_type == "application/pdf" else []

    async def authorize():
        nonlocal authorization_error
        try:
            budget.assert_context(source, policy, engine.capabilities)
            budget.preflight(kind, request.max_output_tokens, region_keys=region_keys)
            if before_send is not None:
                async with asyncio.timeout(LOCAL_STEP_SECONDS):
                    await before_send()
        except TimeoutError:
            # A late DB write may already hold a pending marker. The run
            # retains that evidence; no provider dispatch follows this error.
            authorization_error = RecognitionError("recognition_artifact_unavailable")
            raise authorization_error from None
        except Exception as exc:
            authorization_error = exc
            raise

    def reserve():
        nonlocal ticket, authorization_error
        try:
            budget.assert_context(source, policy, engine.capabilities)
            ticket = budget.reserve(kind, request.max_output_tokens, region_keys=region_keys)
        except Exception as exc:
            authorization_error = exc
            raise

    try:
        with response_timeout_scope(
            policy.per_call_seconds, response_only=True, local_wait_seconds=settings.llm_timeout,
            before_send=authorize, on_send=reserve,
        ) as state:
            async with state.local_admission(capacity.lease(source.owner_id)):
                budget.assert_context(source, policy, engine.capabilities)
                candidate = await dispatch(request)
            if ticket is None or not state.ever_started:
                # A provider adapter must identify its actual request boundary;
                # returning a candidate is not proof that this contract was met.
                raise RecognitionError("recognition_response_invalid")
            candidate = RecognitionCandidateV1.model_validate(candidate.model_dump(warnings=False))
            if engine.capabilities != capabilities:
                raise RecognitionError("recognition_route_changed", submission_may_exist=True)
            if (candidate.status == "not_run" or candidate.provider_route_id != capabilities.route_id
                    or candidate.kind != capabilities.candidate_kind
                    or candidate.status != "error" and candidate.safe_error_code is not None):
                raise RecognitionError("recognition_response_invalid", submission_may_exist=True)
            uncertain = state.submission_may_exist or candidate.safe_error_code == "provider_submit_uncertain"
    except (RecognitionError, ProviderRequestError) as exc:
        if authorization_error is not None:
            # Provider adapters normalize model errors. A trusted local fence
            # failure must pass through that wrapper unchanged to the worker.
            raise authorization_error
        upstream_status = exc.upstream_status_code if isinstance(exc, RecognitionError) else exc.status_code
        if ticket is None:
            raise RecognitionError(exc.code, submission_may_exist=False,
                                   upstream_status_code=upstream_status) from None
        # Only proved rejection/no connection can override the adapter's
        # conservative marker. An unverifiable completed response still keeps
        # the existing duplicate-submission protection.
        uncertain = state.submission_may_exist or (not state.last_attempt_rejected and (
            getattr(exc, "submission_may_exist", False) or exc.code == "provider_submit_uncertain"
        ))
        candidate = RecognitionCandidateV1(
            kind=capabilities.candidate_kind, status="error", provider_route_id=capabilities.route_id,
            safe_error_code=exc.code, upstream_status_code=upstream_status,
        )
    except TimeoutError:
        if authorization_error is not None:
            raise authorization_error
        if ticket is None:
            raise RecognitionError("recognition_artifact_unavailable") from None
        uncertain = state.submission_may_exist or not state.last_attempt_rejected
        candidate = RecognitionCandidateV1(
            kind=capabilities.candidate_kind, status="error", provider_route_id=capabilities.route_id,
            safe_error_code="provider_submit_uncertain" if uncertain else "recognition_response_invalid",
        )
    except Exception:
        if authorization_error is not None:
            raise authorization_error
        if ticket is None:
            raise RecognitionError("recognition_response_invalid") from None
        uncertain = state.submission_may_exist or not state.last_attempt_rejected
        candidate = RecognitionCandidateV1(
            kind=capabilities.candidate_kind, status="error", provider_route_id=capabilities.route_id,
            safe_error_code="recognition_response_invalid",
        )
    if not uncertain:
        failed = candidate.status == "error" or candidate.finish_reason == "refused" or "provider_refused" in candidate.warning_codes
        budget.settle(ticket, outcome="failed" if failed else candidate.status,
                      input_tokens=candidate.input_tokens, output_tokens=candidate.output_tokens)
    if progress:
        if candidate.status == "error":
            await progress.record_provider_failure(
                RecognitionError(candidate.safe_error_code or "recognition_response_invalid",
                                 submission_may_exist=uncertain), pages=failure_pages,
            )
        await progress.increment_stage_metrics(**{f"recognition_{kind}_calls": 1})
    return RecognitionCallResultV1(candidate=candidate, submission_may_exist=uncertain)
