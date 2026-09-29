"""A new use of immutable evidence, distinct from the evidence's historical cost.

This separate contract does not add fields to any persisted V1 assembly/candidate
or change its canonical digest. It is an accounting projection, not a bill, job
lease, authorization token, or proof that a caller actually made the calls.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from backend.recognition.cache_identity import RecognitionCacheIdentityV1, final_cache_identity
from backend.recognition.fusion import RecognitionAssemblyV1
from backend.recognition.models import EvidenceModel, RecognitionUsageV1


def evidence_usage(assembly: RecognitionAssemblyV1) -> RecognitionUsageV1:
    budget = assembly.repair_execution.budget if assembly.repair_execution else assembly.raw.budget
    return RecognitionUsageV1(**{name: getattr(budget, name) for name in (
        "locator_calls", "initial_calls", "empty_recovery_calls", "patch_calls",
        "input_tokens", "output_tokens", "duration_ms", "usage_complete",
    )})


def hit_usage() -> RecognitionUsageV1:
    # Zero is justified by no dispatch in this invocation, not by absent usage
    # from a historical OCR engine. Historical unknowns remain unknown below.
    return RecognitionUsageV1(input_tokens=0, output_tokens=0, cache_hits=1)


class RecognitionInvocationV1(EvidenceModel):
    contract: Literal["smartai.recognition.invocation"] = "smartai.recognition.invocation"
    schema_version: Literal[1] = 1
    identity: RecognitionCacheIdentityV1
    artifact_id: str = Field(strict=True, min_length=1, max_length=240)
    reused: bool = Field(strict=True)
    assembly: RecognitionAssemblyV1 = Field(repr=False)
    current_usage: RecognitionUsageV1
    historical_usage: RecognitionUsageV1 | None = None
    artifact_io_duration_ms: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def separate_current_and_historical_usage(self):
        self.assembly = RecognitionAssemblyV1.model_validate(self.assembly.model_dump(warnings=False))
        expected = final_cache_identity(self.assembly.raw.request, capabilities=self.assembly.raw.engine_capabilities,
                                        prompt_version=self.assembly.prompt_version, tool_version=self.identity.tool_version)
        if self.identity != expected:
            raise ValueError("invocation and evidence identity disagree")
        usage = evidence_usage(self.assembly)
        if self.current_usage != (hit_usage() if self.reused else usage):
            raise ValueError("current invocation cannot charge or erase historical usage")
        if self.historical_usage != (usage if self.reused else None):
            raise ValueError("reused evidence must preserve its historical usage")
        if self.reused:
            from backend.recognition.artifact_codec import build_artifact

            artifact = build_artifact(identity=self.identity, source=self.assembly.raw.request.source,
                                      payload_kind="assembly", payload=self.assembly)
            if not artifact.cacheable_success:
                raise ValueError("unsuccessful evidence cannot be a terminal reuse receipt")
        return self


def invocation_receipt(*, assembly: RecognitionAssemblyV1, identity: RecognitionCacheIdentityV1,
                       artifact_id: str, reused: bool, artifact_io_duration_ms: float) -> RecognitionInvocationV1:
    return RecognitionInvocationV1(
        identity=identity, artifact_id=artifact_id, reused=reused, assembly=assembly,
        current_usage=hit_usage() if reused else evidence_usage(assembly),
        historical_usage=evidence_usage(assembly) if reused else None,
        artifact_io_duration_ms=artifact_io_duration_ms,
    )
