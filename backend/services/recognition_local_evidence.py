"""Authorized local evidence reuse; no model, retry or billing decisions."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
import time
from typing import Literal, TYPE_CHECKING

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from backend.domain.errors import PdfEvidenceError, RecognitionError
from backend.recognition.artifact_codec import build_artifact
from backend.recognition.cache_identity import native_cache_identity, render_cache_identity
from backend.recognition.local_cache import RecognitionByteCache
from backend.recognition.models import RecognitionSourceRefV1
from backend.services.recognition_artifacts import RecognitionArtifactBindingV1, RecognitionArtifactFenceV1, RecognitionArtifactStore
from backend.tools.pdf_evidence import (
    MAX_IMAGE_INPUT_BYTES, MAX_INPUT_BYTES, ImagePreparedResult, ImagePrepareRequest,
    PdfContactSheetRequest, PdfIndexRequest, PdfPagesRequest, PdfRenderRequest, PdfResult,
    read_image_evidence, read_pdf_evidence, validate_evidence_result,
)

if TYPE_CHECKING:
    from backend.progress.tracker import ProgressReporter


@dataclass(frozen=True)
class LocalEvidenceRead:
    evidence: PdfResult | ImagePreparedResult
    reused: Literal["none", "memory", "durable"]
    artifact_id: str | None = None


def _decode(data, command, source):
    try:
        result = validate_evidence_result(json.loads(data) if isinstance(data, bytes) else data, command)
        if isinstance(result, ImagePreparedResult) and result.source_sha256 != source.input_sha256:
            raise ValueError
        return result
    except (ValueError, TypeError, ValidationError, PdfEvidenceError):
        raise RecognitionError("recognition_artifact_invalid") from None


class RecognitionLocalEvidenceReader:
    """An assignment-bound facade; shared process cache is explicitly injected.

    Each call rechecks the current original and parent task. These checks do not
    replace the eventual durable workflow claim/fence before provider dispatch.
    Native artifacts persist; render pixels only enter the bounded RAM cache.
    """

    def __init__(self, *, store: RecognitionArtifactStore, cache: RecognitionByteCache,
                 source: RecognitionSourceRefV1, binding: RecognitionArtifactBindingV1,
                 authorized_owner_id: str, fence: RecognitionArtifactFenceV1 | None = None,
                 progress: ProgressReporter | None = None):
        try:
            self._source = RecognitionSourceRefV1.model_validate(source.model_dump(warnings=False))
            self._binding = RecognitionArtifactBindingV1.model_validate(binding.model_dump(warnings=False))
            self._fence = None if fence is None else RecognitionArtifactFenceV1.model_validate(fence.model_dump(warnings=False))
            if self._source.owner_id != authorized_owner_id:
                raise ValueError
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise RecognitionError("recognition_request_invalid") from None
        self.store, self.cache, self.owner, self.progress = store, cache, authorized_owner_id, progress

    def _context(self, identity):
        return dict(source=self._source, identity=identity, binding=self._binding, authorized_owner_id=self.owner)

    def _cache_context(self, identity):
        return dict(source=self._source, identity=identity, authorized_owner_id=self.owner)

    async def _require_active(self, identity):
        if not await self.store.active_source(**self._context(identity)):
            self.cache.invalidate_source(source=self._source, authorized_owner_id=self.owner)
            raise RecognitionError("recognition_source_unavailable")

    async def read(self, source_bytes: bytes, command: PdfIndexRequest | PdfPagesRequest | PdfRenderRequest | PdfContactSheetRequest | ImagePrepareRequest,
                   *, timeout_seconds: float = 10) -> LocalEvidenceRead:
        if (type(source_bytes) is not bytes or not source_bytes or len(source_bytes) > MAX_INPUT_BYTES
                or type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30):
            raise RecognitionError("recognition_request_invalid")
        try:
            if not isinstance(command, (PdfIndexRequest, PdfPagesRequest, PdfRenderRequest, PdfContactSheetRequest, ImagePrepareRequest)):
                raise ValueError
            command = type(command).model_validate(command.model_dump(warnings=False))
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise RecognitionError("recognition_request_invalid") from None
        if isinstance(command, ImagePrepareRequest) and len(source_bytes) > MAX_IMAGE_INPUT_BYTES:
            raise RecognitionError("recognition_request_invalid")
        is_native = isinstance(command, PdfIndexRequest) or isinstance(command, PdfPagesRequest) and command.operation == "detail"
        is_export = isinstance(command, PdfPagesRequest) and command.operation == "export_pages"
        identity = (native_cache_identity(self._source, PdfPagesRequest(pages=command.pages)) if is_export else
                    native_cache_identity(self._source, command) if is_native else render_cache_identity(self._source, command))
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout_seconds):
                digest = await run_in_threadpool(lambda: hashlib.sha256(source_bytes).hexdigest())
                if digest != self._source.input_sha256:
                    raise RecognitionError("recognition_source_mismatch")
                await self._require_active(identity)
                payload_kind = "native_index" if isinstance(command, PdfIndexRequest) else "native_detail"
                if is_native:
                    lookup = await self.store.find_success(**self._context(identity), payload_kind=payload_kind)
                    if lookup.status == "hit":
                        evidence = await run_in_threadpool(lambda: _decode(lookup.envelope.payload.model_dump(warnings=False), command, self._source))
                        await self._require_active(identity)
                        return LocalEvidenceRead(evidence, "durable", lookup.artifact_id)
                    if lookup.status != "miss":
                        code = ("recognition_artifact_unavailable" if lookup.status == "unavailable" else
                                "recognition_source_unavailable" if lookup.status == "source_unavailable" else "recognition_artifact_invalid")
                        raise RecognitionError(code)
                elif not is_export:
                    content = self.cache.get(**self._cache_context(identity))
                    if content is not None:
                        try:
                            evidence = await run_in_threadpool(_decode, content, command, self._source)
                        except RecognitionError:
                            self.cache.invalidate_source(source=self._source, authorized_owner_id=self.owner)
                            raise
                        await self._require_active(identity)
                        if self.progress:
                            await self.progress.increment_stage_metrics(recognition_render_cache_hits=1)
                        return LocalEvidenceRead(evidence, "memory")
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise RecognitionError("recognition_timeout")
                reader = read_image_evidence if isinstance(command, ImagePrepareRequest) else read_pdf_evidence
                result = await reader(source_bytes, command, timeout_seconds=remaining, progress=self.progress)
                result = await run_in_threadpool(lambda: validate_evidence_result(result.model_dump(warnings=False), command))
                if isinstance(result, ImagePreparedResult) and result.source_sha256 != self._source.input_sha256:
                    raise RecognitionError("recognition_source_mismatch")
                await self._require_active(identity)
                if is_native:
                    envelope = await run_in_threadpool(build_artifact, identity=identity, source=self._source,
                                                       payload_kind=payload_kind, payload=result)
                    row = await self.store.save(envelope, binding=self._binding, authorized_owner_id=self.owner, fence=self._fence)
                    return LocalEvidenceRead(result, "none", row.id)
                if not is_export:
                    content = await run_in_threadpool(lambda: result.model_dump_json().encode("utf-8"))
                    await self._require_active(identity)
                    self.cache.put(**self._cache_context(identity), value=content)
                return LocalEvidenceRead(result, "none")
        except TimeoutError:
            raise RecognitionError("recognition_timeout") from None
