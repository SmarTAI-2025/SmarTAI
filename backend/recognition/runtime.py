"""One frozen-route dispatch boundary for recognition workflow stages."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from typing import Literal, TYPE_CHECKING

from backend.domain.errors import RecognitionError
from backend.recognition.budget import RecognitionBudget
from backend.recognition.engine import EngineLocateInputV1, EngineReadInputV1, RecognitionEngine, freeze_engine_input
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
        self._global = asyncio.Semaphore(global_limit)
        self._owner_limit = owner_limit
        self._owners: dict[str, tuple[asyncio.Semaphore, int]] = {}
        self._loop = None

    @asynccontextmanager
    async def lease(self, owner_id: str):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RecognitionError("recognition_request_invalid")
        self._loop = loop
        semaphore, users = self._owners.get(owner_id, (asyncio.Semaphore(self._owner_limit), 0))
        self._owners[owner_id] = semaphore, users + 1
        try:
            async with semaphore, self._global:
                yield
        finally:
            _, users = self._owners[owner_id]
            if users == 1:
                del self._owners[owner_id]
            else:
                self._owners[owner_id] = semaphore, users - 1


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
) -> RecognitionCallResultV1:
    return await _dispatch(engine, request, kind="initial", source=source, policy=policy,
                           capabilities=capabilities, region_keys=region_keys, budget=budget,
                           capacity=capacity, progress=progress)


async def run_locator_call(
    engine: RecognitionEngine, request: EngineLocateInputV1, *, source: RecognitionSourceRefV1,
    policy: RecognitionPolicyV1, capabilities: EngineCapabilitiesV1, budget: RecognitionBudget,
    capacity: RecognitionCapacity, progress: ProgressReporter | None = None,
) -> RecognitionCallResultV1:
    return await _dispatch(engine, request, kind="locator", source=source, policy=policy,
                           capabilities=capabilities, region_keys=(), budget=budget,
                           capacity=capacity, progress=progress)


async def _dispatch(
    engine: RecognitionEngine, request: EngineReadInputV1 | EngineLocateInputV1, *,
    kind: Literal["initial", "locator"], source: RecognitionSourceRefV1, policy: RecognitionPolicyV1,
    capabilities: EngineCapabilitiesV1, region_keys: tuple[str, ...], budget: RecognitionBudget,
    capacity: RecognitionCapacity, progress: ProgressReporter | None,
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
    if engine.capabilities != capabilities:
        raise RecognitionError("recognition_route_changed")
    if request.input_mode not in capabilities.visual_inputs:
        raise RecognitionError("recognition_input_unsupported")
    if kind == "locator" and (not capabilities.target_location or len(request.images) > capabilities.max_locator_images):
        raise RecognitionError("recognition_input_unsupported")
    dispatch = getattr(engine, "locate" if kind == "locator" else "recognize", None)
    if not callable(dispatch):
        raise RecognitionError("recognition_input_unsupported")
    phase = "locator" if kind == "locator" else "read"
    ticket = None
    uncertain = False
    try:
        async with asyncio.timeout(budget.remaining(phase)), capacity.lease(source.owner_id):
            budget.assert_context(source, policy, engine.capabilities)
            seconds = min(policy.per_call_seconds, budget.remaining(phase))
            ticket = budget.reserve(kind, max_output_tokens=request.max_output_tokens, region_keys=region_keys)
            async with asyncio.timeout(seconds):
                candidate = await dispatch(request)
            candidate = RecognitionCandidateV1.model_validate(candidate.model_dump(warnings=False))
            if engine.capabilities != capabilities:
                raise RecognitionError("recognition_route_changed", submission_may_exist=True)
            if candidate.status == "not_run" or candidate.provider_route_id != capabilities.route_id or candidate.kind != capabilities.candidate_kind:
                raise RecognitionError("recognition_response_invalid", submission_may_exist=True)
    except TimeoutError:
        if ticket is None:
            raise RecognitionError("recognition_timeout") from None
        uncertain = True
        candidate = RecognitionCandidateV1(
            kind=capabilities.candidate_kind, status="error", provider_route_id=capabilities.route_id,
            safe_error_code="provider_submit_uncertain",
        )
    except RecognitionError as exc:
        if ticket is None:
            raise
        uncertain = exc.submission_may_exist
        candidate = RecognitionCandidateV1(
            kind=capabilities.candidate_kind, status="error", provider_route_id=capabilities.route_id,
            safe_error_code=exc.code,
        )
    except Exception:
        if ticket is None:
            raise RecognitionError("recognition_response_invalid") from None
        uncertain = True
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
