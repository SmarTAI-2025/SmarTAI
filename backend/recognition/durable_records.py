"""Small, content-free pre-submit ledger. Pending is never permission to retry."""
from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from backend.domain.errors import RecognitionError
from backend.recognition.models import EvidenceModel, RecognitionCandidateV1, RecognitionPolicyV1, RecognitionUsageV1
from backend.recognition.planner import EngineCapabilitiesV1

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=64)]
RegionKey = Annotated[str, Field(min_length=1, max_length=512)]


class RecognitionDispatchCheckpointV1(EvidenceModel):
    kind: Literal["locator", "initial", "empty_recovery", "patch"]
    identity_key: Digest
    region_keys: list[RegionKey] = Field(default_factory=list, max_length=24)
    requested_output_tokens: int = Field(strict=True, ge=1, le=32768)
    state: Literal["pending", "stored"] = "pending"
    artifact_id: Identifier | None = None
    input_tokens: int | None = Field(default=None, strict=True, ge=0)
    output_tokens: int | None = Field(default=None, strict=True, ge=0)
    submission_may_exist: bool = Field(default=True, strict=True)

    @model_validator(mode="after")
    def valid_state(self):
        if len(self.region_keys) != len(set(self.region_keys)):
            raise ValueError("duplicate region")
        if (self.kind == "locator") != (not self.region_keys):
            raise ValueError("read dispatch requires region identity")
        if self.kind in {"patch", "empty_recovery"} and len(self.region_keys) != 1:
            raise ValueError("extra call requires one region")
        if self.state == "pending":
            if not self.submission_may_exist or self.input_tokens is not None or self.output_tokens is not None:
                raise ValueError("pending usage must stay unknown")
        elif not self.artifact_id or self.submission_may_exist:
            raise ValueError("stored dispatch needs confirmed evidence")
        return self


class RecognitionRunCheckpointV1(EvidenceModel):
    schema_version: Literal[1] = 1
    operation_id: Identifier
    attempt: int = Field(strict=True, ge=1)
    identity_key: Digest
    started_at: float = Field(ge=0)
    calls: list[RecognitionDispatchCheckpointV1] = Field(default_factory=list, max_length=30)
    final_artifact_id: Identifier | None = None

    @model_validator(mode="after")
    def unique_dispatches(self):
        if len({call.identity_key for call in self.calls}) != len(self.calls):
            raise ValueError("dispatch identity must be unique")
        initials, extras = set(), set()
        for index, call in enumerate(self.calls):
            if call.state == "pending" and index != len(self.calls) - 1:
                raise ValueError("pending dispatch must halt further work")
            if call.kind == "initial":
                if initials.intersection(call.region_keys):
                    raise ValueError("initial region must not repeat")
                initials.update(call.region_keys)
            elif call.kind in {"empty_recovery", "patch"}:
                if extras.intersection(call.region_keys):
                    raise ValueError("each region has one shared extra call")
                extras.update(call.region_keys)
        return self


def _copy(checkpoint):
    try:
        return RecognitionRunCheckpointV1.model_validate(checkpoint.model_dump(warnings=False))
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise RecognitionError("recognition_plan_changed") from None


def _elapsed(checkpoint, now):
    if type(now) not in {int, float} or not math.isfinite(now) or now < checkpoint.started_at:
        raise RecognitionError("recognition_request_invalid")
    return now - checkpoint.started_at


def authorize_dispatch(checkpoint, *, kind, identity_key, region_keys, max_output_tokens,
                       policy, capabilities, now):
    checkpoint = _copy(checkpoint)
    try:
        policy = RecognitionPolicyV1.model_validate(policy.model_dump(warnings=False))
        caps = EngineCapabilitiesV1.model_validate(capabilities.model_dump(warnings=False))
        call = RecognitionDispatchCheckpointV1(kind=kind, identity_key=identity_key,
                                               region_keys=list(region_keys), requested_output_tokens=max_output_tokens)
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise RecognitionError("recognition_request_invalid") from None
    if checkpoint.final_artifact_id:
        raise RecognitionError("recognition_plan_changed")
    if any(item.state == "pending" for item in checkpoint.calls):
        raise RecognitionError("provider_submit_uncertain")
    if _elapsed(checkpoint, now) >= policy.total_seconds:
        raise RecognitionError("recognition_timeout")
    counts = {name: sum(item.kind == name for item in checkpoint.calls) + (kind == name)
              for name in ("locator", "initial", "empty_recovery", "patch")}
    if (counts["locator"] > policy.max_locator_calls or counts["initial"] > policy.max_initial_calls
            or counts["empty_recovery"] > policy.max_empty_recoveries or counts["patch"] > policy.max_patches
            or counts["empty_recovery"] + counts["patch"] > 6
            or counts["initial"] + counts["empty_recovery"] + counts["patch"] > policy.max_calls):
        raise RecognitionError("recognition_budget_exhausted")
    regions = {region for item in [*checkpoint.calls, call] if item.kind == "initial" for region in item.region_keys}
    if len(regions) > policy.max_regions:
        raise RecognitionError("recognition_budget_exhausted")
    if caps.bounded_output_tokens:
        charged = sum(item.output_tokens if item.output_tokens is not None else item.requested_output_tokens
                      for item in checkpoint.calls)
        if charged + max_output_tokens > policy.max_output_tokens:
            raise RecognitionError("recognition_budget_exhausted")
    checkpoint.calls.append(call)
    return _copy(checkpoint)


def finish_dispatch(checkpoint, *, identity_key, artifact_id, candidate, submission_may_exist):
    checkpoint = _copy(checkpoint)
    if checkpoint.final_artifact_id or not checkpoint.calls:
        raise RecognitionError("recognition_plan_changed")
    last = checkpoint.calls[-1]
    if last.state != "pending" or last.identity_key != identity_key or type(submission_may_exist) is not bool:
        raise RecognitionError("recognition_plan_changed")
    try:
        candidate = RecognitionCandidateV1.model_validate(candidate.model_dump(warnings=False))
        checkpoint.calls[-1] = RecognitionDispatchCheckpointV1(
            **{**last.model_dump(), "artifact_id": artifact_id,
               "state": "pending" if submission_may_exist else "stored",
               "submission_may_exist": submission_may_exist,
               "input_tokens": None if submission_may_exist else candidate.input_tokens,
               "output_tokens": None if submission_may_exist else candidate.output_tokens},
        )
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise RecognitionError("recognition_request_invalid") from None
    return _copy(checkpoint)


def operation_usage(checkpoint, *, now):
    checkpoint = _copy(checkpoint)
    inputs = [item.input_tokens for item in checkpoint.calls]
    outputs = [item.output_tokens for item in checkpoint.calls]
    return RecognitionUsageV1(
        **{name + "_calls": sum(item.kind == name for item in checkpoint.calls)
           for name in ("locator", "initial", "empty_recovery", "patch")},
        input_tokens=None if None in inputs else sum(inputs), output_tokens=None if None in outputs else sum(outputs),
        usage_complete=None not in inputs and None not in outputs, duration_ms=1000 * _elapsed(checkpoint, now),
    )
