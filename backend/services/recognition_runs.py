"""Durable assignment-bound recognition, without wiring a business endpoint.

One stable operation owns the pre-submit checkpoint and every derived write.
Restart can reuse confirmed successful leaves; ambiguous submits never repeat.
No API key, source text or pixels belong in the operation checkpoint.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import math
import time
from typing import Literal
import uuid

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from backend.agents.recognition_agent import RecognitionAgent, RecognitionReadRequestV1
from backend.db import workflow_repository
from backend.domain.errors import DomainError, LeaseLost, RecognitionError, RECOGNITION_ERROR_CODES
from backend.recognition.cache_identity import final_cache_identity, model_cache_identity
from backend.recognition.durable_records import (
    RecognitionRunCheckpointV1, authorize_dispatch, finish_dispatch, operation_usage,
)
from backend.recognition.invocation import evidence_usage
from backend.recognition.local_cache import RecognitionByteCache
from backend.recognition.models import RecognitionUsageV1
from backend.recognition.planner import EngineCapabilitiesV1
from backend.recognition.workflow_v2 import RecognitionAssemblyV2
from backend.services.recognition_artifacts import RecognitionArtifactBindingV1, RecognitionArtifactFenceV1, artifact_assignment_id
from backend.services.recognition_calls import RecognitionCallService, _leaf
from backend.services.recognition_local_evidence import RecognitionLocalEvidenceReader
from backend.services.recognition_results import RecognitionResultService
from backend.tools.pdf_evidence import MAX_IMAGE_INPUT_BYTES, MAX_INPUT_BYTES

OPERATION_TYPE = "recognition_harness_v2"


@dataclass(frozen=True)
class RecognitionRunResultV1:
    status: Literal["completed", "already_done", "already_running", "needs_review"]
    operation_id: str
    assembly: RecognitionAssemblyV2 | None
    current_usage: RecognitionUsageV1
    operation_usage: RecognitionUsageV1
    artifact_ids: tuple[str, ...] = ()
    safe_error_code: str | None = None


def _zero():
    return RecognitionUsageV1(input_tokens=0, output_tokens=0)


class _RunContext:
    def __init__(self, row, *, identity, request, capabilities, store, progress, clock, binding, repository):
        self.row, self.identity, self.request, self.capabilities = row, identity, request, capabilities
        self.store, self.progress, self.clock = store, progress, clock
        self.binding = binding
        self.repository = repository
        self.policy = request.policy.model_copy(deep=True)
        if request.scope == "targets":
            self.policy.max_detail_pages = min(self.policy.max_detail_pages, 2 * len(request.targets) + 4)
        self.checkpoint = None

    @property
    def fence(self):
        return RecognitionArtifactFenceV1(operation_id=self.row.id, attempt=self.row.attempt,
                                          lease_token=self.row.lease_token)

    async def write(self, checkpoint, *, status=None, error=None):
        checkpoint = RecognitionRunCheckpointV1.model_validate(checkpoint.model_dump())
        refs = list(dict.fromkeys([*(call.artifact_id for call in checkpoint.calls if call.artifact_id),
                                   *([checkpoint.final_artifact_id] if checkpoint.final_artifact_id else [])]))
        row = await run_in_threadpool(
            self.repository.save_operation_checkpoint, self.row.id, owner_id=self.row.owner_id,
            expected_attempt=self.row.attempt, expected_checkpoint_revision=self.row.checkpoint_revision,
            expected_lease_token=self.row.lease_token, stage="recognition_" + (status or "checkpoint"),
            checkpoint=checkpoint.model_dump(mode="json"), artifact_refs=refs,
            terminal_status=status,
            terminal_summary=None if status is None else dict(status=status, error_code=error,
                                                              final_artifact_id=checkpoint.final_artifact_id),
        )
        self.row, self.checkpoint = row, checkpoint

    async def active(self):
        return await self.store.active_source(source=self.request.source, identity=self.identity,
                                             binding=self.binding, authorized_owner_id=self.row.owner_id)


class _GuardedCalls(RecognitionCallService):
    def __init__(self, context):
        self.context = context
        super().__init__(store=context.store, binding=context.binding, authorized_owner_id=context.row.owner_id,
                         fence=context.fence)

    async def before_dispatch(self, source, request, *, region_keys, **options):
        ctx = self.context
        if source != ctx.request.source or options["policy"] != ctx.policy or options["capabilities"] != ctx.capabilities:
            raise RecognitionError("recognition_plan_changed")
        if not await ctx.active():
            raise RecognitionError("recognition_source_unavailable")
        identity = model_cache_identity(source, request, capabilities=ctx.capabilities, policy=ctx.policy,
                                        prompt_version=options["prompt_version"])
        checkpoint = authorize_dispatch(
            ctx.checkpoint, kind=options["kind"], identity_key=identity.key, region_keys=region_keys,
            max_output_tokens=request.max_output_tokens, policy=ctx.policy,
            capabilities=ctx.capabilities, now=ctx.clock(),
        )
        # A failed or cancelled checkpoint write never reaches provider dispatch.
        await ctx.write(checkpoint)
        if ctx.progress:
            await ctx.progress.increment_stage_metrics(recognition_submits_checkpointed=1)

    async def record(self, *args, **kwargs):
        receipt = await super().record(*args, **kwargs)
        candidate, uncertain = _leaf(receipt.envelope)
        checkpoint = finish_dispatch(
            self.context.checkpoint, identity_key=receipt.envelope.identity.key, artifact_id=receipt.artifact_id,
            candidate=candidate, submission_may_exist=uncertain,
        )
        await self.context.write(checkpoint)
        return receipt


class RecognitionRunService:
    """Internal source-authorized service; returns execution state, not acceptance.

    Existing business operations may call this boundary after their own source
    association checks. The stable child operation never resets error attempts.
    A new policy/route/source is a different explicitly requested run, not fallback.
    """

    def __init__(self, *, store, capacity, cache=None, progress=None, clock=time.time,
                 operation_repository=workflow_repository, binding_resolver=artifact_assignment_id):
        self.store, self.capacity = store, capacity
        self.cache = cache if cache is not None else RecognitionByteCache()
        self.progress, self.clock = progress, clock
        self.repository, self.binding_resolver = operation_repository, binding_resolver

    async def _restored(self, row, request, identity, *, status):
        try:
            checkpoint = RecognitionRunCheckpointV1.model_validate(row.checkpoint)
            if (checkpoint.identity_key != identity.key or checkpoint.operation_id != row.id
                    or checkpoint.attempt != row.attempt):
                raise ValueError
            assembly = None
            if checkpoint.final_artifact_id:
                envelope = await self.store.load(
                    checkpoint.final_artifact_id, source=request.source, identity=identity,
                    binding=RecognitionArtifactBindingV1.model_validate((row.payload or {}).get("binding") or
                        dict(link="assignment", business_id=request.source.business_id)),
                    authorized_owner_id=row.owner_id,
                )
                if envelope.payload_kind != "assembly_v2":
                    raise ValueError
                assembly = envelope.payload
            return RecognitionRunResultV1(
                status, row.id, assembly, _zero(), operation_usage(checkpoint, now=row.completed_at or self.clock()),
                tuple(row.artifact_refs or []), (row.terminal_summary or {}).get("error_code"),
            )
        except (ValidationError, ValueError, TypeError, AttributeError, RecognitionError):
            return RecognitionRunResultV1("needs_review", row.id, None, _zero(),
                                          RecognitionUsageV1(input_tokens=None, output_tokens=None, usage_complete=False),
                                          safe_error_code="recognition_artifact_invalid")

    async def run(self, request, source_bytes, *, engine, prompt_version, authorized_owner_id, binding=None):
        try:
            request = RecognitionReadRequestV1.model_validate(request.model_dump(warnings=False))
            caps = None if engine is None else EngineCapabilitiesV1.model_validate(engine.capabilities.model_dump(warnings=False))
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise RecognitionError("recognition_request_invalid") from None
        source = request.source
        if source.owner_id != authorized_owner_id:
            raise RecognitionError("recognition_source_mismatch")
        identity = final_cache_identity(request, capabilities=caps, prompt_version=prompt_version, workflow_version=2)
        binding = binding or RecognitionArtifactBindingV1(link="assignment", business_id=source.business_id)
        binding = RecognitionArtifactBindingV1.model_validate(binding.model_dump())
        if binding.business_id != source.business_id:
            raise RecognitionError("recognition_source_mismatch")
        assignment_id = await run_in_threadpool(self.binding_resolver, binding, authorized_owner_id)
        # create_operation checks live assignment ownership. Crucially, this
        # internal operation never uses the legacy error/expiry retry reset.
        row, _ = await run_in_threadpool(
            self.repository.create_operation, assignment_id=assignment_id, owner_id=authorized_owner_id,
            operation_type=OPERATION_TYPE, input_hash=identity.key, retry_existing=False,
            payload=dict(identity_key=identity.key, binding=binding.model_dump()),
        )
        if row.terminal_summary is not None:
            return await self._restored(row, request, identity,
                                        status="already_done" if row.status == "completed" else "needs_review")
        if row.status not in {"pending", "running"}:
            return await self._restored(row, request, identity, status="needs_review")
        maximum = MAX_INPUT_BYTES if source.content_type == "application/pdf" else MAX_IMAGE_INPUT_BYTES
        if (type(source_bytes) is not bytes or not source_bytes or len(source_bytes) > maximum
                or hashlib.sha256(source_bytes).hexdigest() != source.input_sha256):
            raise RecognitionError("recognition_source_mismatch")
        if not await self.store.active_source(source=source, identity=identity, binding=binding, authorized_owner_id=authorized_owner_id):
            raise RecognitionError("recognition_source_unavailable")
        worker = "recognition-" + uuid.uuid4().hex
        try:
            row = await run_in_threadpool(self.repository.claim_operation, row.id, owner_id=authorized_owner_id,
                                          worker_id=worker, lease_seconds=math.ceil(request.policy.total_seconds) + 60)
        except LeaseLost:
            latest = await run_in_threadpool(self.repository.get_operation, row.id, owner_id=authorized_owner_id)
            if latest.terminal_summary is not None:
                return await self._restored(latest, request, identity,
                                            status="already_done" if latest.status == "completed" else "needs_review")
            if latest.checkpoint:
                return await self._restored(latest, request, identity, status="already_running")
            return RecognitionRunResultV1("already_running", row.id, None, _zero(), _zero())
        context = _RunContext(row, identity=identity, request=request, capabilities=caps,
                              store=self.store, progress=self.progress, clock=self.clock, binding=binding,
                              repository=self.repository)
        assembly = None
        try:
            if row.checkpoint:
                checkpoint = RecognitionRunCheckpointV1.model_validate(row.checkpoint)
                if (checkpoint.identity_key != identity.key or checkpoint.operation_id != row.id
                        or checkpoint.attempt != row.attempt):
                    raise RecognitionError("recognition_plan_changed")
            else:
                checkpoint = RecognitionRunCheckpointV1(operation_id=row.id, attempt=row.attempt,
                                                         identity_key=identity.key, started_at=self.clock())
            context.checkpoint = checkpoint
            if any(call.state == "pending" for call in checkpoint.calls):
                await context.write(checkpoint, status="needs_review", error="provider_submit_uncertain")
                return await self._restored(context.row, request, identity, status="needs_review")
            if checkpoint.final_artifact_id:
                await context.write(checkpoint, status="completed")
                return await self._restored(context.row, request, identity, status="already_done")
            remaining = min(request.policy.total_seconds, checkpoint.started_at + request.policy.total_seconds - self.clock())
            if remaining <= 0:
                await context.write(checkpoint, status="needs_review", error="recognition_timeout")
                return await self._restored(context.row, request, identity, status="needs_review")
            await context.write(checkpoint)
            if self.progress:
                await self.progress.set_current_step("recognition_resume", message="Checking durable recognition progress")
            local = RecognitionLocalEvidenceReader(store=self.store, cache=self.cache, binding=binding,
                                                    source=source, authorized_owner_id=authorized_owner_id, fence=context.fence,
                                                    progress=self.progress)
            calls = _GuardedCalls(context)
            results = RecognitionResultService(store=self.store, binding=binding, authorized_owner_id=authorized_owner_id,
                                                fence=context.fence)
            async with asyncio.timeout(remaining):
                assembly = await RecognitionAgent(engine, capacity=self.capacity, progress=self.progress,
                                                   local_reader=local, call_service=calls,
                                                   elapsed_seconds=max(0, self.clock() - checkpoint.started_at)).recognize(
                    request, source_bytes, authorized_owner_id=authorized_owner_id, prompt_version=prompt_version,
                )
                receipt = await results.record(assembly, timeout_seconds=min(10, remaining))
                checkpoint = context.checkpoint.model_copy(deep=True)
                checkpoint.final_artifact_id = receipt.artifact_id
                pending = any(call.state == "pending" for call in checkpoint.calls)
                codes = [*assembly.raw.stop_codes,
                         *(assembly.raw.read_batch.stop_codes if assembly.raw.read_batch else [])]
                error = "provider_submit_uncertain" if pending else assembly.safe_error_code or (codes[0] if codes else None)
                complete = assembly.document is not None and assembly.document.coverage.complete
                status = "needs_review" if error or not complete else "completed"
                await context.write(checkpoint, status=status, error=error)
                return RecognitionRunResultV1(status, row.id, assembly, receipt.current_usage,
                                              operation_usage(checkpoint, now=self.clock()), tuple(context.row.artifact_refs), error)
        except asyncio.CancelledError:
            # Keep the durable pending marker. A new worker may inspect it but
            # cannot infer that cancellation prevented the remote submission.
            raise
        except (DomainError, ValidationError, TimeoutError) as exc:
            code = "recognition_timeout" if isinstance(exc, TimeoutError) else (
                exc.code if isinstance(exc, DomainError) and exc.code in RECOGNITION_ERROR_CODES else "recognition_artifact_unavailable")
            if context.checkpoint is not None:
                try:
                    await context.write(context.checkpoint, status="needs_review", error=code)
                except DomainError:
                    pass
            usage = operation_usage(context.checkpoint, now=self.clock()) if context.checkpoint else _zero()
            return RecognitionRunResultV1("needs_review", row.id, assembly,
                                          evidence_usage(assembly) if assembly is not None else
                                          RecognitionUsageV1(input_tokens=None, output_tokens=None, usage_complete=False),
                                          usage, tuple(context.row.artifact_refs or []), code)
        finally:
            try:
                await asyncio.shield(run_in_threadpool(self.repository.release_operation, row.id,
                                                      owner_id=authorized_owner_id, worker_id=worker,
                                                      lease_token=row.lease_token))
            except DomainError:
                pass
