"""One frozen-route dispatch boundary for recognition workflow stages."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from typing import TYPE_CHECKING

from pydantic import ValidationError

from backend.domain.errors import RecognitionError
from backend.recognition.budget import RecognitionBudget
from backend.recognition.engine import EngineReadInputV1, RecognitionEngine
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
    """Dispatch once; an ambiguous completion remains charged and pending.

    Durable ownership and cancellation recovery belong to the application job.
    This process-local ledger does not authorize source access or persist a lock.
    """
    budget.assert_context(source, policy, capabilities)
    source, policy, capabilities = (value.model_copy(deep=True) for value in (source, policy, capabilities))
    try:
        request = EngineReadInputV1.model_validate({**request.model_dump(warnings=False), "payload": request.payload})
    except (ValidationError, AttributeError, TypeError):
        raise RecognitionError("recognition_request_invalid") from None
    if engine.capabilities != capabilities:
        raise RecognitionError("recognition_route_changed")
    if request.input_mode not in capabilities.visual_inputs:
        raise RecognitionError("recognition_input_unsupported")
    ticket = None
    uncertain = False
    try:
        async with asyncio.timeout(budget.remaining("read")), capacity.lease(source.owner_id):
            budget.assert_context(source, policy, engine.capabilities)
            seconds = min(policy.per_call_seconds, budget.remaining("read"))
            ticket = budget.reserve("initial", max_output_tokens=request.max_output_tokens, region_keys=region_keys)
            async with asyncio.timeout(seconds):
                candidate = await engine.recognize(request)
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
        await progress.increment_stage_metrics(recognition_initial_calls=1)
    return RecognitionCallResultV1(candidate=candidate, submission_may_exist=uncertain)
