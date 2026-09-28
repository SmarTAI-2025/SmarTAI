"""Terminal final-result reuse, never an automatic get-or-dispatch wrapper.

Only a confirmed hit returns historical evidence with a zero-dispatch receipt.
Miss/error/unsuccessful outcomes do not authorize provider work. The durable job
must own the pre-submit checkpoint and decide whether execution is permissible.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import math
import time
from typing import Literal

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from backend.agents.recognition_agent import RecognitionReadRequestV1
from backend.domain.errors import RecognitionError
from backend.recognition.artifact_codec import build_artifact
from backend.recognition.cache_identity import final_cache_identity
from backend.recognition.fusion import RecognitionAssemblyV1
from backend.recognition.workflow_v2 import RecognitionAssemblyV2
from backend.recognition.invocation import RecognitionInvocationV1, invocation_receipt
from backend.recognition.planner import EngineCapabilitiesV1
from backend.services.recognition_artifacts import (
    RecognitionArtifactBindingV1, RecognitionArtifactFenceV1, RecognitionArtifactStore,
)
from backend.tools.pdf_evidence import MAX_IMAGE_INPUT_BYTES, MAX_INPUT_BYTES


@dataclass(frozen=True)
class RecognitionFinalLookup:
    status: Literal["hit", "miss", "not_success", "corrupt", "unavailable", "source_unavailable"]
    artifact_id: str | None = None
    invocation: RecognitionInvocationV1 | None = None


def _timeout(value):
    if type(value) not in {int, float} or not math.isfinite(value) or not 0 < value <= 30:
        raise RecognitionError("recognition_request_invalid") from None
    return value


class RecognitionResultService:
    """Internal assignment-bound lookup/record API; no engine or credentials."""

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

    def _binding(self, request):
        if request.source.owner_id != self.owner or request.source.business_id != self.binding.business_id:
            raise RecognitionError("recognition_source_mismatch") from None

    async def lookup(self, request: RecognitionReadRequestV1, source_bytes: bytes, *,
                     capabilities: EngineCapabilitiesV1 | None, prompt_version: str,
                     timeout_seconds: float = 10, workflow_version: Literal[1, 2] = 1) -> RecognitionFinalLookup:
        timeout_seconds = _timeout(timeout_seconds)
        try:
            request = RecognitionReadRequestV1.model_validate(request.model_dump(warnings=False))
            capabilities = None if capabilities is None else EngineCapabilitiesV1.model_validate(capabilities.model_dump(warnings=False))
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise RecognitionError("recognition_request_invalid") from None
        self._binding(request)
        maximum = MAX_INPUT_BYTES if request.source.content_type == "application/pdf" else MAX_IMAGE_INPUT_BYTES
        if type(source_bytes) is not bytes or not source_bytes or len(source_bytes) > maximum:
            raise RecognitionError("recognition_request_invalid") from None
        identity = final_cache_identity(request, capabilities=capabilities, prompt_version=prompt_version,
                                        workflow_version=workflow_version)
        context = dict(source=request.source, identity=identity, binding=self.binding.model_copy(deep=True), authorized_owner_id=self.owner)
        started = time.monotonic()
        try:
            async with asyncio.timeout(min(timeout_seconds, request.policy.total_seconds)):
                digest = await run_in_threadpool(lambda: hashlib.sha256(source_bytes).hexdigest())
                if digest != request.source.input_sha256:
                    raise RecognitionError("recognition_source_mismatch")
                found = await self.store.find_success(**context, payload_kind="assembly_v2" if workflow_version == 2 else "assembly")
                if found.status != "hit":
                    return RecognitionFinalLookup(found.status, found.artifact_id)
                receipt = await run_in_threadpool(
                    invocation_receipt, assembly=found.envelope.payload, identity=identity,
                    artifact_id=found.artifact_id, reused=True,
                    artifact_io_duration_ms=(time.monotonic() - started) * 1000,
                )
                if not await self.store.active_source(**context):
                    return RecognitionFinalLookup("source_unavailable")
                receipt.artifact_io_duration_ms = (time.monotonic() - started) * 1000
                return RecognitionFinalLookup("hit", found.artifact_id, receipt)
        except TimeoutError:
            raise RecognitionError("recognition_timeout") from None

    async def record(self, assembly: RecognitionAssemblyV1, *, timeout_seconds: float = 10) -> RecognitionInvocationV1:
        """Record one already-executed result, including failed/incomplete evidence.

        The caller must not feed a historical assembly to this method or interpret
        a persistence error as permission to rerun. D3c supplies that durable guard.
        """
        timeout_seconds = _timeout(timeout_seconds)
        try:
            assembly = assembly.model_copy(deep=True)
            binding = self.binding.model_copy(deep=True)
            fence = None if self.fence is None else self.fence.model_copy(deep=True)
        except (TypeError, AttributeError):
            raise RecognitionError("recognition_request_invalid") from None
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout_seconds):
                assembly_type = RecognitionAssemblyV2 if assembly.schema_version == 2 else RecognitionAssemblyV1
                assembly = await run_in_threadpool(lambda: assembly_type.model_validate(assembly.model_dump(warnings=False)))
                self._binding(assembly.raw.request)
                identity = final_cache_identity(assembly.raw.request, capabilities=assembly.raw.engine_capabilities,
                                                prompt_version=assembly.prompt_version, workflow_version=assembly.schema_version)
                envelope = await run_in_threadpool(build_artifact, identity=identity, source=assembly.raw.request.source,
                                                   payload_kind="assembly_v2" if assembly.schema_version == 2 else "assembly", payload=assembly)
                row = await self.store.save(envelope, binding=binding, authorized_owner_id=self.owner, fence=fence)
                receipt = await run_in_threadpool(invocation_receipt, assembly=assembly, identity=identity,
                                                artifact_id=row.id, reused=False,
                                                artifact_io_duration_ms=(time.monotonic() - started) * 1000)
                receipt.artifact_io_duration_ms = (time.monotonic() - started) * 1000
                return receipt
        except TimeoutError:
            raise RecognitionError("recognition_timeout") from None
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise RecognitionError("recognition_artifact_invalid") from None
