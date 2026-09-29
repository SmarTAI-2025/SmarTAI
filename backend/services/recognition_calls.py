"""Exact per-call evidence reuse, without provider dispatch or budget authority.

The caller supplies input produced by the authorized source tools. This boundary
checks the actual submitted bytes against retained leaf evidence; it does not
prove that arbitrary pixels came from the source, reconstruct a job lease, or
authorize a new call after a miss. V1 evidence remains byte-for-byte unchanged.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import math
import re
import time
from typing import Literal

from pydantic import Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

from backend.domain.errors import RecognitionError
from backend.recognition.artifact_codec import ArtifactPayload, RecognitionArtifactV1, build_artifact
from backend.recognition.cache_identity import model_cache_identity
from backend.recognition.engine import EngineLocateInputV1, EngineReadInputV1, EngineRepairInputV1, freeze_engine_input
from backend.recognition.models import EvidenceModel, RecognitionPolicyV1, RecognitionSourceRefV1
from backend.recognition.planner import EngineCapabilitiesV1
from backend.services.recognition_artifacts import (
    RecognitionArtifactBindingV1, RecognitionArtifactFenceV1, RecognitionArtifactStore,
)

CallKind = Literal["locator", "initial", "empty_recovery", "patch"]
_KINDS = {"locator": "locator", "initial": "visual_read", "empty_recovery": "repair", "patch": "repair"}
_OCCURRENCE = re.compile(r"(locator|initial|empty_recovery|patch):[0-9]{4}")


class RecognitionCallUsageV2(EvidenceModel):
    """One occurrence's model spend; unknown historical usage remains unknown."""

    dispatches: int = Field(strict=True, ge=0, le=1)
    input_tokens: int | None = Field(default=None, strict=True, ge=0)
    output_tokens: int | None = Field(default=None, strict=True, ge=0)
    duration_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    submission_may_exist: bool = Field(default=False, strict=True)
    usage_complete: bool = Field(strict=True)

    @model_validator(mode="after")
    def exact_accounting(self):
        if self.usage_complete != (not self.submission_may_exist and self.input_tokens is not None and self.output_tokens is not None):
            raise ValueError("unknown usage cannot be reported as complete")
        if not self.dispatches and (self.input_tokens != 0 or self.output_tokens != 0 or self.duration_ms != 0
                                    or self.submission_may_exist):
            raise ValueError("a cache hit has no new model dispatch or spend")
        if self.submission_may_exist and (self.input_tokens is not None or self.output_tokens is not None):
            raise ValueError("uncertain submissions cannot settle spend")
        return self


def _leaf(envelope):
    payload = envelope.payload
    if envelope.payload_kind == "visual_read":
        candidate = payload.candidate
    elif envelope.payload_kind in {"locator", "repair"} and payload.result is not None:
        candidate = payload.result.candidate
    else:
        raise ValueError("call receipts require dispatched model evidence")
    if candidate.kind == "native" or candidate.status == "not_run":
        raise ValueError("a model call cannot contain undispatched or native evidence")
    if candidate.status != "error" and candidate.safe_error_code is not None:
        raise ValueError("a non-error outcome cannot contain a provider error")
    uncertain = payload.submission_may_exist
    if candidate.safe_error_code == "provider_submit_uncertain" and not uncertain:
        raise ValueError("uncertain provider evidence cannot be settled")
    return candidate, uncertain


def _evidence_usage(envelope):
    candidate, uncertain = _leaf(envelope)
    return RecognitionCallUsageV2(
        dispatches=1, input_tokens=None if uncertain else candidate.input_tokens,
        output_tokens=None if uncertain else candidate.output_tokens, duration_ms=candidate.duration_ms,
        submission_may_exist=uncertain,
        usage_complete=not uncertain and candidate.input_tokens is not None and candidate.output_tokens is not None,
    )


def _hit_usage():
    return RecognitionCallUsageV2(dispatches=0, input_tokens=0, output_tokens=0, duration_ms=0, usage_complete=True)


def _occurrence(kind, occurrence_id):
    if (type(kind) is not str or kind not in _KINDS or type(occurrence_id) is not str
            or not _OCCURRENCE.fullmatch(occurrence_id) or occurrence_id.split(":", 1)[0] != kind):
        raise RecognitionError("recognition_request_invalid") from None


class RecognitionCallReceiptV2(EvidenceModel):
    """An occurrence references a leaf; it never renames a historical unit_id.

    Historical usage describes referenced evidence, not a deduplicated invoice.
    A fresh receipt is a projection, not proof of a durable pre-submit checkpoint.
    """

    contract: Literal["smartai.recognition.call_receipt"] = "smartai.recognition.call_receipt"
    schema_version: Literal[2] = 2
    occurrence_id: str = Field(pattern=r"^(locator|initial|empty_recovery|patch):[0-9]{4}$")
    kind: CallKind
    origin: Literal["dispatch", "cache_hit"]
    artifact_id: str = Field(strict=True, min_length=1, max_length=240)
    envelope: RecognitionArtifactV1 = Field(repr=False)
    current_usage: RecognitionCallUsageV2
    historical_usage: RecognitionCallUsageV2 | None = None
    artifact_io_duration_ms: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def evidence_and_projection(self):
        self.envelope = RecognitionArtifactV1.model_validate(self.envelope.model_dump(warnings=False))
        if self.envelope.payload_kind != _KINDS[self.kind] or self.occurrence_id.split(":", 1)[0] != self.kind:
            raise ValueError("occurrence stage and evidence kind disagree")
        history = _evidence_usage(self.envelope)
        reused = self.origin == "cache_hit"
        if reused and not self.envelope.cacheable_success:
            raise ValueError("unsuccessful evidence cannot become a cache hit")
        if self.current_usage != (_hit_usage() if reused else history) or self.historical_usage != (history if reused else None):
            raise ValueError("current and historical usage must match raw evidence")
        return self


@dataclass(frozen=True)
class RecognitionCallLookup:
    status: Literal["hit", "miss", "not_success", "corrupt", "unavailable", "source_unavailable"]
    artifact_id: str | None = None
    receipt: RecognitionCallReceiptV2 | None = None


def _pair(request, capabilities, envelope):
    """Check facts recoverable from the leaf, in addition to its exact cache key."""
    payload = envelope.payload
    candidate, _ = _leaf(envelope)
    if candidate.kind != capabilities.candidate_kind or candidate.provider_route_id != capabilities.route_id:
        raise ValueError("call evidence belongs to a different route")
    if payload.requested_output_tokens != request.max_output_tokens:
        raise ValueError("call evidence changed its submitted output bound")
    if isinstance(request, EngineLocateInputV1):
        if (envelope.payload_kind != "locator" or payload.result.requested_targets != request.targets
                or len(payload.sheets) != len(request.images)):
            raise ValueError("locator evidence changed its scope")
        for sheet, image in zip(payload.sheets, request.images):
            if sheet.metadata.page_numbers != image.page_numbers or sheet.payload_sha256 != hashlib.sha256(image.payload).hexdigest():
                raise ValueError("locator evidence changed its actual submitted sheets")
    elif isinstance(request, EngineRepairInputV1):
        if envelope.payload_kind != "repair" or payload.result.context != request.repair_context:
            raise ValueError("repair evidence changed its context")
        image = payload.image
        if (image.page_number != request.page_number or image.region != request.region
                or image.content_type != request.content_type or image.payload_bytes != len(request.payload)
                or image.payload_sha256 != hashlib.sha256(request.payload).hexdigest()):
            raise ValueError("repair evidence changed its actual submitted image")
    else:
        submitted_region = (payload.image_preparation.effective_region if payload.image_preparation is not None
                            else payload.region.as_tuple())
        if (envelope.payload_kind != "visual_read" or payload.page_numbers != (request.document_pages or [request.page_number])
                or submitted_region != request.region.as_tuple() or payload.input_mode != request.input_mode
                or payload.payload_bytes != len(request.payload)
                or payload.payload_sha256 != hashlib.sha256(request.payload).hexdigest()
                or payload.output_mapping != ("document_only" if len(payload.page_numbers) > 1 else "single_region")
                or (payload.image_preparation is not None and payload.image_preparation.content_type != request.content_type)):
            raise ValueError("reader evidence changed its actual submitted input")


def _receipt(envelope, artifact_id, kind, occurrence_id, reused, started):
    history = _evidence_usage(envelope)
    return RecognitionCallReceiptV2(
        occurrence_id=occurrence_id, kind=kind, origin="cache_hit" if reused else "dispatch",
        artifact_id=artifact_id, envelope=envelope, current_usage=_hit_usage() if reused else history,
        historical_usage=history if reused else None, artifact_io_duration_ms=(time.monotonic() - started) * 1000,
    )


class RecognitionCallService:
    """Assignment-bound record/lookup only; no engine, credentials or auto retry."""

    def __init__(self, *, store: RecognitionArtifactStore, binding: RecognitionArtifactBindingV1,
                 authorized_owner_id: str, fence: RecognitionArtifactFenceV1 | None = None):
        try:
            if type(authorized_owner_id) is not str or not authorized_owner_id.strip() or len(authorized_owner_id) > 240:
                raise ValueError
            self.binding = RecognitionArtifactBindingV1.model_validate(binding.model_dump(warnings=False))
            self.fence = None if fence is None else RecognitionArtifactFenceV1.model_validate(fence.model_dump(warnings=False))
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise RecognitionError("recognition_request_invalid") from None
        self.store, self.owner = store, authorized_owner_id

    def _snapshot(self, source, request, capabilities, policy, kind, occurrence_id, timeout_seconds):
        _occurrence(kind, occurrence_id)
        if type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise RecognitionError("recognition_request_invalid") from None
        try:
            source = RecognitionSourceRefV1.model_validate(source.model_dump(warnings=False))
            request = freeze_engine_input(request)
            capabilities = EngineCapabilitiesV1.model_validate(capabilities.model_dump(warnings=False))
            policy = RecognitionPolicyV1.model_validate(policy.model_dump(warnings=False))
            binding = RecognitionArtifactBindingV1.model_validate(self.binding.model_dump(warnings=False))
            fence = None if self.fence is None else RecognitionArtifactFenceV1.model_validate(self.fence.model_dump(warnings=False))
            if (kind == "locator") != isinstance(request, EngineLocateInputV1) or (
                kind in {"empty_recovery", "patch"}) != isinstance(request, EngineRepairInputV1):
                raise ValueError
            if kind in {"empty_recovery", "patch"} and not policy.enable_repair:
                raise ValueError
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise RecognitionError("recognition_request_invalid") from None
        owner = self.owner
        if source.owner_id != owner or source.business_id != binding.business_id:
            raise RecognitionError("recognition_source_mismatch") from None
        return source, request, capabilities, policy, binding, fence, owner

    async def lookup(self, source: RecognitionSourceRefV1, request: EngineReadInputV1 | EngineLocateInputV1 | EngineRepairInputV1,
                     *, capabilities: EngineCapabilitiesV1, policy: RecognitionPolicyV1, prompt_version: str,
                     kind: CallKind, occurrence_id: str, timeout_seconds: float = 10) -> RecognitionCallLookup:
        source, request, capabilities, policy, binding, _, owner = self._snapshot(
            source, request, capabilities, policy, kind, occurrence_id, timeout_seconds,
        )
        started = time.monotonic()
        try:
            async with asyncio.timeout(min(timeout_seconds, policy.total_seconds)):
                identity = await run_in_threadpool(model_cache_identity, source, request, capabilities=capabilities,
                                                  policy=policy, prompt_version=prompt_version)
                context = dict(source=source, identity=identity, binding=binding, authorized_owner_id=owner)
                found = await self.store.find_success(**context, payload_kind=_KINDS[kind])
                if found.status != "hit":
                    return RecognitionCallLookup(found.status, found.artifact_id)
                try:
                    def validate_hit():
                        envelope = RecognitionArtifactV1.model_validate(found.envelope.model_dump(warnings=False))
                        if envelope.source != source or envelope.identity != identity:
                            raise ValueError("cached evidence changed its source or identity")
                        _pair(request, capabilities, envelope)
                        return _receipt(envelope, found.artifact_id, kind, occurrence_id, True, started)
                    receipt = await run_in_threadpool(validate_hit)
                except (ValidationError, ValueError, TypeError, AttributeError):
                    return RecognitionCallLookup("corrupt", found.artifact_id)
                if not await self.store.active_source(**context):
                    return RecognitionCallLookup("source_unavailable")
                receipt.artifact_io_duration_ms = (time.monotonic() - started) * 1000
                return RecognitionCallLookup("hit", found.artifact_id, receipt)
        except TimeoutError:
            raise RecognitionError("recognition_timeout") from None

    async def record(self, source: RecognitionSourceRefV1, request: EngineReadInputV1 | EngineLocateInputV1 | EngineRepairInputV1,
                     payload: ArtifactPayload, *, capabilities: EngineCapabilitiesV1, policy: RecognitionPolicyV1,
                     prompt_version: str, kind: CallKind, occurrence_id: str,
                     timeout_seconds: float = 10) -> RecognitionCallReceiptV2:
        """Persist this run's dispatched outcome, including failures and proposals.

        The job must supply its own fresh call evidence. Persistence failure or
        cancellation never authorizes resubmission. Undispatched failures belong
        in the workflow, not in a per-call usage receipt.
        """
        source, request, capabilities, policy, binding, fence, owner = self._snapshot(
            source, request, capabilities, policy, kind, occurrence_id, timeout_seconds,
        )
        try:
            payload = payload.model_copy(deep=True)
        except (TypeError, AttributeError):
            raise RecognitionError("recognition_artifact_invalid") from None
        started = time.monotonic()
        try:
            async with asyncio.timeout(min(timeout_seconds, policy.total_seconds)):
                def prepare():
                    identity = model_cache_identity(source, request, capabilities=capabilities, policy=policy, prompt_version=prompt_version)
                    envelope = build_artifact(identity=identity, source=source, payload_kind=_KINDS[kind], payload=payload)
                    _pair(request, capabilities, envelope)
                    # Validate projections before any object is persisted.
                    _evidence_usage(envelope)
                    return envelope
                envelope = await run_in_threadpool(prepare)
                row = await self.store.save(envelope, binding=binding, authorized_owner_id=owner, fence=fence)
                receipt = await run_in_threadpool(_receipt, envelope, row.id, kind, occurrence_id, False, started)
                receipt.artifact_io_duration_ms = (time.monotonic() - started) * 1000
                return receipt
        except TimeoutError:
            raise RecognitionError("recognition_timeout") from None
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise RecognitionError("recognition_artifact_invalid") from None
