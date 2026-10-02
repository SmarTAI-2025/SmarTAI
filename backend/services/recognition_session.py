"""One live, sequential recognition execution with exact per-call reuse.

This is not durable job ownership. D3c's dispatch guard must wrap new submits;
no cache miss, process-local budget or receipt is itself permission to replay.
"""
from __future__ import annotations

from backend.domain.errors import DomainError, RecognitionError
from backend.recognition.artifact_codec import build_artifact
from backend.recognition.cache_identity import model_cache_identity
from backend.recognition.engine import freeze_engine_input
from backend.recognition.models import RecognitionSourceRefV1
from backend.recognition.runtime import RecognitionCallResultV1
from backend.recognition.workflow_v2 import CallEvidenceV2


class RecognitionCallSessionV2:
    def __init__(self, service, *, source, authorized_owner_id):
        self.source = RecognitionSourceRefV1.model_validate(source.model_dump(warnings=False))
        if service.owner != authorized_owner_id or self.source.owner_id != authorized_owner_id:
            raise RecognitionError("recognition_source_mismatch")
        if service.binding.business_id != self.source.business_id:
            raise RecognitionError("recognition_source_mismatch")
        self.service, self._records, self._pending = service, [], None

    def records(self, kind):
        return [record.model_copy(deep=True) for record in self._records if record.kind == kind]

    async def dispatch(self, kind, request, *, source, policy, capabilities, region_keys, budget, dispatch):
        from backend.skills.recognition_reader import PROMPT_VERSION, LOCATOR_PROMPT_VERSION
        from backend.recognition.repair_response import REPAIR_PROMPT_VERSION

        if self._pending is not None or source != self.source:
            raise RecognitionError("recognition_plan_changed")
        budget.assert_context(source, policy, capabilities)
        request = freeze_engine_input(request)
        policy, capabilities = policy.model_copy(deep=True), capabilities.model_copy(deep=True)
        prompt = LOCATOR_PROMPT_VERSION if kind == "locator" else PROMPT_VERSION if kind == "initial" else REPAIR_PROMPT_VERSION
        options = dict(capabilities=capabilities, policy=policy, prompt_version=prompt, kind=kind,
                       occurrence_id=f"{kind}:{len(self.records(kind)):04d}")
        phase = "locator" if kind == "locator" else "read"
        # Neither corrupt evidence nor a previous failed/pending submit authorizes a retry.
        found = await self.service.lookup(source, request, timeout_seconds=min(10, budget.remaining(phase)), **options)
        if found.status == "hit":
            receipt = found.receipt
            leaf = receipt.envelope.payload
            candidate = leaf.candidate if kind == "initial" else leaf.result.candidate
            budget.register_cached_success(kind, region_keys=region_keys, candidate=candidate)
            self._pending = request, options, receipt, budget
            return RecognitionCallResultV1(candidate=candidate.model_copy(deep=True))
        if found.status != "miss":
            raise RecognitionError("recognition_cache_" + found.status)
        guard = getattr(self.service, "before_dispatch", None)
        if guard is not None:
            await guard(source, request, region_keys=region_keys, **options)
        outcome = await dispatch()
        self._pending = request, options, None, budget
        return outcome

    async def record(self, kind, leaf):
        if self._pending is None or self._pending[1]["kind"] != kind:
            raise RecognitionError("recognition_plan_changed")
        request, options, receipt, budget = self._pending
        error = None
        if receipt is None:
            try:
                receipt = await self.service.record(
                    self.source, request, leaf,
                    timeout_seconds=min(10, budget.remaining("locator" if kind == "locator" else "read")), **options,
                )
            except RecognitionError as exc:
                error = exc.code
            except DomainError:
                error = "recognition_artifact_unavailable"
        if receipt is not None:
            envelope = receipt.envelope
            record = CallEvidenceV2(
                occurrence_id=receipt.occurrence_id, kind=kind, origin=receipt.origin, artifact_id=receipt.artifact_id,
                source=envelope.source, identity=envelope.identity, original=envelope.payload, payload_sha256=envelope.payload_sha256,
            )
        else:
            # A failed durable write must not erase paid evidence or reset its budget.
            identity = model_cache_identity(self.source, request, capabilities=options["capabilities"],
                                             policy=options["policy"], prompt_version=options["prompt_version"])
            envelope = build_artifact(identity=identity, source=self.source, payload=leaf,
                                     payload_kind="visual_read" if kind == "initial" else "locator" if kind == "locator" else "repair")
            record = CallEvidenceV2(
                occurrence_id=options["occurrence_id"], kind=kind, origin="dispatch", storage_error=error,
                source=envelope.source, identity=identity, original=envelope.payload, payload_sha256=envelope.payload_sha256,
            )
        self._records.append(record)
        self._pending = None
        return error
