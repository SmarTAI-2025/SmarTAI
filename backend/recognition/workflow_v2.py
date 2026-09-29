"""Versioned occurrence provenance and current spend over immutable V1 leaves.

V1 defaults, identities and wire shapes stay unchanged. Scope and document
derivation are shared; only V2 accounting excludes referenced historical calls.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from backend.agents.recognition_agent import LocatorCallEvidenceV1, RecognitionWorkflowReadV1
from backend.recognition.budget import LogicalBudgetSnapshotV2, validate_budget_accounting
from backend.recognition.cache_identity import RecognitionCacheIdentityV1, canonical_digest
from backend.recognition.executor import ReadUnitV1, RecognitionReadBatchV1
from backend.recognition.fusion import RecognitionAssemblyV1, _build_document
from backend.recognition.models import Code, EvidenceModel, RecognitionSourceRefV1
from backend.recognition.repair_records import RepairCallEvidenceV1, RepairExecutionV1
from backend.recognition.runtime import region_budget_key


class CallEvidenceV2(EvidenceModel):
    occurrence_id: str = Field(pattern=r"^(locator|initial|empty_recovery|patch):[0-9]{4}$")
    kind: Literal["locator", "initial", "empty_recovery", "patch"]
    origin: Literal["dispatch", "cache_hit"]
    artifact_id: str | None = Field(default=None, min_length=1, max_length=240)
    storage_error: Code | None = None
    source: RecognitionSourceRefV1
    identity: RecognitionCacheIdentityV1
    # The original unit ID and token counts are never rewritten to fit this run.
    original: ReadUnitV1 | LocatorCallEvidenceV1 | RepairCallEvidenceV1 = Field(repr=False)
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_original(self):
        from backend.recognition.artifact_codec import build_artifact
        from backend.services.recognition_calls import _leaf

        if self.occurrence_id.split(":")[0] != self.kind:
            raise ValueError("occurrence kind mismatch")
        kind = "visual_read" if self.kind == "initial" else "locator" if self.kind == "locator" else "repair"
        envelope = build_artifact(identity=self.identity, source=self.source, payload_kind=kind, payload=self.original)
        _leaf(envelope)
        if envelope.payload_sha256 != self.payload_sha256:
            raise ValueError("original evidence digest mismatch")
        if self.origin == "cache_hit" and (not self.artifact_id or self.storage_error or not envelope.cacheable_success):
            raise ValueError("cache reuse requires persisted successful evidence")
        if self.origin == "dispatch" and (self.artifact_id is None) != (self.storage_error is not None):
            raise ValueError("unpersisted dispatched evidence needs an explicit storage failure")
        return self


def validate_provenance(leaves, records, *, kind, source, purpose, policy, capabilities):
    if len(leaves) != len(records):
        raise ValueError("each occurrence must retain exactly one provenance record")
    for index, (leaf, record) in enumerate(zip(leaves, records)):
        record = CallEvidenceV2.model_validate(record.model_dump(warnings=False))
        if record.kind != kind or record.occurrence_id != f"{kind}:{index:04d}" or record.source != source:
            raise ValueError("occurrence scope or order mismatch")
        current, original = leaf.model_dump(), record.original.model_dump()
        if kind == "initial":
            if leaf.unit_id != f"u{index:04d}":
                raise ValueError("current read occurrence IDs must be sequential")
            current.pop("unit_id")
            original.pop("unit_id")
        if current != original:
            raise ValueError("current occurrence changed historical evidence")
        identity = record.identity
        if (capabilities is None or identity.purpose != purpose
                or identity.engine_fingerprint != capabilities.fingerprint
                or identity.capabilities_sha256 != canonical_digest(capabilities.model_dump(mode="json"))
                or identity.policy_sha256 != canonical_digest(policy.model_dump(mode="json"))):
            raise ValueError("occurrence belongs to another frozen context")


def _candidate(leaf):
    return leaf.candidate if isinstance(leaf, ReadUnitV1) else leaf.result.candidate


def current_records(raw):
    pairs = list(zip(raw.locator_calls, raw.locator_provenance))
    if raw.read_batch:
        pairs += list(zip(raw.read_batch.units, raw.read_batch.provenance))
    return [(_candidate(leaf), leaf.submission_may_exist, leaf.requested_output_tokens)
            for leaf, record in pairs if record.origin == "dispatch"]


def validate_logical(raw, logical, repair_calls=(), selection=None):
    initial = raw.read_batch.units if raw.read_batch else []
    provenance = [*raw.locator_provenance, *(raw.read_batch.provenance if raw.read_batch else [])]
    hits = [item for item in provenance if item.origin == "cache_hit"]
    extra = [call for call in repair_calls if call.result is not None]
    kinds = {target.unit_id: target.kind for target in selection.targets} if selection else {}
    empty = sum(kinds[call.unit_id] == "empty_recovery" for call in extra)
    expected = dict(locator_calls=len(raw.locator_calls), initial_calls=len(initial),
                    empty_recovery_calls=empty, patch_calls=len(extra) - empty,
                    read_calls=len(initial) + len(extra), total_calls=len(raw.locator_calls) + len(initial) + len(extra),
                    cache_hits=len(hits), locator_cache_hits=sum(item.kind == "locator" for item in hits),
                    initial_cache_hits=sum(item.kind == "initial" for item in hits),
                    region_count=len({region_budget_key(page, unit.region) for unit in initial for page in unit.page_numbers}))
    if logical.model_dump() != expected:
        raise ValueError("logical work must include cached occurrences exactly once")
    policy = raw.execution_policy
    if (logical.locator_calls > policy.max_locator_calls or logical.initial_calls > policy.max_initial_calls
            or logical.read_calls > policy.max_calls or logical.region_count > policy.max_regions
            or logical.empty_recovery_calls > policy.max_empty_recoveries or logical.patch_calls > policy.max_patches):
        raise ValueError("cached evidence cannot expand the frozen logical allowance")


class RecognitionReadBatchV2(RecognitionReadBatchV1):
    schema_version: Literal[2] = 2
    provenance: list[CallEvidenceV2] = Field(default_factory=list, max_length=24)

    def _validate_accounting(self):
        validate_provenance(self.units, self.provenance, kind="initial", source=self.source,
                            purpose=self.plan.purpose, policy=self.plan.policy, capabilities=self.plan.engine_capabilities)
        candidates = [unit.candidate for unit, record in zip(self.units, self.provenance) if record.origin == "dispatch"]
        if any(record.storage_error and record.storage_error not in self.stop_codes for record in self.provenance):
            raise ValueError("persistence failures must stop the reader explicitly")
        if any(record.storage_error for record in self.provenance[:-1]):
            raise ValueError("reader cannot continue after a persistence failure")
        inputs = None if any(c.input_tokens is None for c in candidates) else sum(c.input_tokens for c in candidates)
        outputs = None if any(c.output_tokens is None for c in candidates) else sum(c.output_tokens for c in candidates)
        if (self.usage.initial_calls != len(candidates) or self.usage.locator_calls or self.usage.patch_calls
                or self.usage.cache_hits != len(self.units) - len(candidates)
                or self.usage.empty_recovery_calls or self.usage.input_tokens != inputs or self.usage.output_tokens != outputs
                or self.usage.usage_complete != (inputs is not None and outputs is not None)):
            raise ValueError("V2 read usage must contain current dispatches only")


class RecognitionWorkflowReadV2(RecognitionWorkflowReadV1):
    schema_version: Literal[2] = 2
    read_batch: RecognitionReadBatchV2 | None = None
    locator_provenance: list[CallEvidenceV2] = Field(default_factory=list, max_length=12)
    logical_budget: LogicalBudgetSnapshotV2

    def _validate_accounting(self):
        validate_provenance(self.locator_calls, self.locator_provenance, kind="locator", source=self.request.source,
                            purpose=self.request.purpose, policy=self.execution_policy, capabilities=self.engine_capabilities)
        locator = sum(p.origin == "dispatch" for p in self.locator_provenance)
        if any(record.storage_error and record.storage_error not in self.stop_codes for record in self.locator_provenance):
            raise ValueError("persistence failures must stop localization explicitly")
        if any(record.storage_error for record in self.locator_provenance):
            if (not self.locator_halted or self.read_batch is not None
                    or any(record.storage_error for record in self.locator_provenance[:-1])):
                raise ValueError("failed localization persistence must halt the entire workflow")
        initial = self.read_batch.usage.initial_calls if self.read_batch else 0
        if (self.budget.locator_calls != locator or self.budget.initial_calls != initial
                or self.budget.empty_recovery_calls or self.budget.patch_calls or self.budget.read_calls != initial
                or self.budget.total_calls != locator + initial):
            raise ValueError("V2 workflow spend must exclude historical calls")
        validate_budget_accounting(self.budget, self.engine_capabilities, current_records(self))
        validate_logical(self, self.logical_budget)


class RepairExecutionV2(RepairExecutionV1):
    schema_version: Literal[2] = 2
    provenance: list[CallEvidenceV2] = Field(default_factory=list, max_length=6)
    logical_budget: LogicalBudgetSnapshotV2


class RecognitionAssemblyV2(RecognitionAssemblyV1):
    schema_version: Literal[2] = 2
    assembly_version: Literal["faithful-assembly-v2"] = "faithful-assembly-v2"
    raw: RecognitionWorkflowReadV2 = Field(repr=False)
    repair_execution: RepairExecutionV2 | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def exact_derivation(self):
        self.raw = RecognitionWorkflowReadV2.model_validate(self.raw.model_dump(warnings=False))
        expected, error = _build_document(self.raw, self.prompt_version)
        if self.repair_execution is not None:
            from backend.recognition.recheck import build_rechecked_document
            initial = RecognitionAssemblyV2(raw=self.raw, prompt_version=self.prompt_version,
                                            document=expected, safe_error_code=error)
            expected, error = build_rechecked_document(initial, self.repair_execution)
        if self.document != expected or self.safe_error_code != error:
            raise ValueError("assembled output must derive from its complete V2 evidence")
        return self


def assemble_recognition_v2(raw, *, prompt_version):
    raw = RecognitionWorkflowReadV2.model_validate(raw.model_dump(warnings=False))
    document, error = _build_document(raw, prompt_version)
    return RecognitionAssemblyV2(raw=raw, prompt_version=prompt_version, document=document, safe_error_code=error)
