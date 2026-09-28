"""Owner-scoped evidence persistence, independent of successful cache reuse.

Callers authorize the business operation before calling this internal service.
A cache key alone grants no access. Saving also requires an available, matching
owner-scoped source row. Reading historical evidence does not reopen an original
that may have been cleaned up. No provider dispatch or retry happens here.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal, TYPE_CHECKING

from pydantic import Field, ValidationError
from starlette.concurrency import run_in_threadpool

from backend.db import assignment_repository, file_repository
from backend.db.file_repository import StoredFile
from backend.domain.errors import DomainError, NotFound, RecognitionError
from backend.recognition.artifact_codec import MAX_COMPRESSED_BYTES, PayloadKind, RecognitionArtifactV1, decode_artifact, encode_artifact
from backend.recognition.cache_identity import RecognitionCacheIdentityV1, canonical_digest
from backend.recognition.models import EvidenceModel, RecognitionSourceRefV1
from backend.storage.base import StorageBackend, StorageObjectNotFound

if TYPE_CHECKING:
    from backend.progress.tracker import ProgressReporter

ARTIFACT_KIND = "recognition_artifact_v1"
ARTIFACT_CONTENT_TYPE = "application/vnd.smartai.recognition-artifact+json+gzip"


@dataclass(frozen=True)
class RecognitionCacheLookup:
    status: Literal["hit", "miss", "not_success", "corrupt", "unavailable", "source_unavailable"]
    artifact_id: str | None = None
    envelope: RecognitionArtifactV1 | None = None


class RecognitionArtifactBindingV1(EvidenceModel):
    link: Literal["assignment", "submission_revision", "knowledge_document"]
    business_id: str = Field(strict=True, min_length=1, max_length=240)

    @property
    def repository_links(self):
        return {field: self.business_id if field == f"{self.link}_id" else None for field in (
            "assignment_id", "submission_revision_id", "knowledge_document_id")}


class RecognitionArtifactFenceV1(EvidenceModel):
    operation_id: str = Field(strict=True, min_length=1, max_length=240)
    attempt: int = Field(strict=True, ge=1)
    lease_token: str | None = Field(default=None, min_length=1, max_length=240, repr=False)


def _context(source, identity, binding, authorized_owner_id):
    try:
        source = RecognitionSourceRefV1.model_validate(source.model_dump(warnings=False))
        identity = RecognitionCacheIdentityV1.model_validate(identity.model_dump(warnings=False))
        binding = RecognitionArtifactBindingV1.model_validate(binding.model_dump(warnings=False))
        # Only assignment artifacts have restart-safe intents and deletion
        # fencing. G/H must enroll other scopes in their actual lifecycles.
        if binding.link != "assignment":
            raise RecognitionError("recognition_artifact_scope_unsupported")
        if (source.owner_id != authorized_owner_id or identity.owner_id != authorized_owner_id
                or not source.stored_file_id or source.input_sha256 != identity.source_sha256
                or source.content_type != identity.source_content_type or source.business_id != binding.business_id
                or source.scope not in {"assignment_source", "submission_source"}):
            raise ValueError
        return source, identity, binding
    except (ValidationError, ValueError, AttributeError, TypeError):
        raise RecognitionError("recognition_artifact_invalid") from None


def _matches_binding(row, binding):
    return all(getattr(row, key) == value for key, value in binding.repository_links.items())


def _legacy_artifact_name(envelope: RecognitionArtifactV1) -> str:
    return f"ocr-{envelope.identity.layer}-{envelope.identity.key}-{envelope.payload_sha256}.json.gz"


def _source_prefix(source, identity):
    return f"ocr-v2-{identity.layer}-{identity.key}-{canonical_digest(source.model_dump(mode='json'))}-"


def artifact_name(envelope: RecognitionArtifactV1) -> str:
    # Namespace the storage lookup, not the persisted V1 identity or payload.
    return f"{_source_prefix(envelope.source, envelope.identity)}{envelope.payload_sha256}.json.gz"


class _LegacyOtherOriginal(Exception):
    """A valid old row for another original is not a corrupted cache object."""


def _get_file(file_id, owner_id):
    try:
        return file_repository.get_file(file_id=file_id, owner_id=owner_id)
    except DomainError:
        raise
    except Exception:
        raise RecognitionError("recognition_artifact_unavailable") from None


def _active_source(source, identity, binding, owner_id):
    source, identity, binding = _context(source, identity, binding, owner_id)
    try:
        assignment_repository.get_assignment(binding.business_id, actor_id=owner_id)
    except NotFound:
        return False
    except Exception:
        raise RecognitionError("recognition_artifact_unavailable") from None
    original = _get_file(source.stored_file_id, owner_id)
    if original is None:
        return False
    if (original.owner_id != owner_id or not _matches_binding(original, binding)
            or original.sha256 != source.input_sha256 or original.content_type != source.content_type):
        raise RecognitionError("recognition_source_mismatch") from None
    return original.availability_status == "available"


def _find_success(storage, source, identity, binding, owner_id, payload_kind):
    source, identity, binding = _context(source, identity, binding, owner_id)
    layers = {"native_index": "native", "native_detail": "native", "visual_read": "visual",
              "locator": "visual", "repair": "patch", "assembly": "final", "assembly_v2": "final"}
    if type(payload_kind) is not str or payload_kind not in layers or identity.layer != layers[payload_kind]:
        raise RecognitionError("recognition_artifact_invalid") from None
    try:
        if not _active_source(source, identity, binding, owner_id):
            return RecognitionCacheLookup("source_unavailable")
        row = file_repository.find_latest_assignment_file(
            owner_id=owner_id, assignment_id=binding.business_id, kind=ARTIFACT_KIND,
            original_name_prefix=_source_prefix(source, identity),
        )
        legacy = row is None
        if legacy:
            row = file_repository.find_latest_assignment_file(
                owner_id=owner_id, assignment_id=binding.business_id, kind=ARTIFACT_KIND,
                original_name_prefix=f"ocr-{identity.layer}-{identity.key}-",
            )
    except RecognitionError as exc:
        if exc.code != "recognition_artifact_unavailable":
            raise
        return RecognitionCacheLookup("unavailable")
    except Exception:
        return RecognitionCacheLookup("unavailable")
    if row is None:
        return RecognitionCacheLookup("miss")
    try:
        envelope = _load(storage, row.id, source, identity, binding, owner_id, legacy_source_miss=legacy)
        if envelope.payload_kind != payload_kind:
            return RecognitionCacheLookup("corrupt", row.id)
        if not envelope.cacheable_success:
            return RecognitionCacheLookup("not_success", row.id)
        # Recheck after object I/O; no cache hit authorizes later provider work.
        if not _active_source(source, identity, binding, owner_id):
            return RecognitionCacheLookup("source_unavailable")
        return RecognitionCacheLookup("hit", row.id, envelope)
    except _LegacyOtherOriginal:
        return RecognitionCacheLookup("miss")
    except RecognitionError as exc:
        if exc.code == "recognition_artifact_unavailable":
            return RecognitionCacheLookup("unavailable", row.id)
        if exc.code in {"recognition_artifact_invalid", "recognition_artifact_limit"}:
            return RecognitionCacheLookup("corrupt", row.id)
        raise


def _save(storage, envelope, binding, owner_id, fence):
    try:
        envelope = RecognitionArtifactV1.model_validate(envelope.model_dump(warnings=False))
    except (ValidationError, ValueError, AttributeError, TypeError):
        raise RecognitionError("recognition_artifact_invalid") from None
    source, _, binding = _context(envelope.source, envelope.identity, binding, owner_id)
    try:
        if fence is not None:
            fence = RecognitionArtifactFenceV1.model_validate(fence.model_dump(warnings=False))
            if binding.link != "assignment":
                raise ValueError
    except (ValidationError, ValueError, AttributeError, TypeError):
        raise RecognitionError("recognition_artifact_invalid") from None
    # Verify the persisted original before writing any derived object.
    original = _get_file(source.stored_file_id, owner_id)
    if (original is None or original.owner_id != owner_id or not _matches_binding(original, binding)
            or original.sha256 != source.input_sha256 or original.content_type != source.content_type
            or original.availability_status != "available"):
        raise RecognitionError("recognition_artifact_invalid") from None
    content = encode_artifact(envelope)
    try:
        return file_repository.save_file(
            storage=storage, owner_id=owner_id, kind=ARTIFACT_KIND, original_name=artifact_name(envelope),
            content=content, content_type=ARTIFACT_CONTENT_TYPE, **binding.repository_links,
            fence_operation_id=fence.operation_id if fence else None,
            fence_operation_attempt=fence.attempt if fence else None,
            fence_lease_token=fence.lease_token if fence else None,
        )
    except DomainError:
        # Preserve transaction fences; an uncertain persistence result is not permission to resubmit.
        raise
    except Exception:
        raise RecognitionError("recognition_artifact_unavailable") from None


def _load(storage, file_id, source, identity, binding, owner_id, *, legacy_source_miss=False):
    source, identity, binding = _context(source, identity, binding, owner_id)
    if not isinstance(file_id, str) or not file_id or len(file_id) > 240:
        raise RecognitionError("recognition_artifact_invalid") from None
    artifact = _get_file(file_id, owner_id)
    if (artifact is None or artifact.owner_id != owner_id or not _matches_binding(artifact, binding)
            or artifact.kind != ARTIFACT_KIND or artifact.content_type != ARTIFACT_CONTENT_TYPE
            or artifact.availability_status != "available" or artifact.storage_backend != getattr(storage, "name", "unknown")
            or not 0 < artifact.size_bytes <= MAX_COMPRESSED_BYTES):
        raise RecognitionError("recognition_artifact_invalid") from None
    try:
        with storage.open(artifact.storage_key) as stream:
            content = stream.read(MAX_COMPRESSED_BYTES + 1)
    except (StorageObjectNotFound, FileNotFoundError):
        raise RecognitionError("recognition_artifact_invalid") from None
    except Exception:
        raise RecognitionError("recognition_artifact_unavailable") from None
    if (len(content) != artifact.size_bytes or len(content) > MAX_COMPRESSED_BYTES
            or hashlib.sha256(content).hexdigest() != artifact.sha256):
        raise RecognitionError("recognition_artifact_invalid") from None
    envelope = decode_artifact(content)
    if envelope.identity != identity or artifact.original_name not in {artifact_name(envelope), _legacy_artifact_name(envelope)}:
        raise RecognitionError("recognition_artifact_invalid") from None
    if envelope.source != source:
        if (legacy_source_miss and artifact.original_name == _legacy_artifact_name(envelope)
                and envelope.source.stored_file_id != source.stored_file_id
                and envelope.source.model_dump(exclude={"stored_file_id", "original_name"})
                == source.model_dump(exclude={"stored_file_id", "original_name"})):
            raise _LegacyOtherOriginal from None
        raise RecognitionError("recognition_artifact_invalid") from None
    return envelope


class RecognitionArtifactStore:
    """Assignment persistence only; other scopes need their deletion fencing."""

    def __init__(self, storage: StorageBackend, *, progress: ProgressReporter | None = None):
        self.storage, self.progress = storage, progress

    async def active_source(self, *, source: RecognitionSourceRefV1, identity: RecognitionCacheIdentityV1,
                            binding: RecognitionArtifactBindingV1, authorized_owner_id: str) -> bool:
        """A fresh availability check, not a reusable authorization token or job lease."""
        source, identity, binding = _context(source, identity, binding, authorized_owner_id)
        return await run_in_threadpool(_active_source, source, identity, binding, authorized_owner_id)

    async def find_success(self, *, source: RecognitionSourceRefV1, identity: RecognitionCacheIdentityV1,
                           binding: RecognitionArtifactBindingV1, authorized_owner_id: str,
                           payload_kind: PayloadKind) -> RecognitionCacheLookup:
        source, identity, binding = _context(source, identity, binding, authorized_owner_id)
        if self.progress:
            await self.progress.set_current_step("recognition_cache_lookup", message="Checking reusable recognition evidence")
        result = await run_in_threadpool(_find_success, self.storage, source, identity, binding, authorized_owner_id, payload_kind)
        if self.progress:
            await self.progress.increment_stage_metrics(**{f"recognition_cache_{result.status}": 1})
        return result

    async def save(self, envelope: RecognitionArtifactV1, *, binding: RecognitionArtifactBindingV1,
                   authorized_owner_id: str, fence: RecognitionArtifactFenceV1 | None = None) -> StoredFile:
        try:
            envelope = envelope.model_copy(deep=True)
            _, _, binding = _context(envelope.source, envelope.identity, binding, authorized_owner_id)
            fence = None if fence is None else RecognitionArtifactFenceV1.model_validate(fence.model_dump(warnings=False))
        except (ValidationError, ValueError, AttributeError, TypeError):
            raise RecognitionError("recognition_artifact_invalid") from None
        if self.progress:
            await self.progress.set_current_step("recognition_persist", message="Saving recognition evidence")
        result = await run_in_threadpool(_save, self.storage, envelope, binding, authorized_owner_id, fence)
        if self.progress:
            await self.progress.increment_stage_metrics(recognition_artifacts_saved=1, recognition_artifact_bytes=result.size_bytes)
        return result

    async def load(self, file_id: str, *, source: RecognitionSourceRefV1, identity: RecognitionCacheIdentityV1,
                   binding: RecognitionArtifactBindingV1, authorized_owner_id: str) -> RecognitionArtifactV1:
        source, identity, binding = _context(source, identity, binding, authorized_owner_id)
        if self.progress:
            await self.progress.set_current_step("recognition_restore", message="Checking stored recognition evidence")
        result = await run_in_threadpool(_load, self.storage, file_id, source, identity, binding, authorized_owner_id)
        if self.progress:
            await self.progress.increment_stage_metrics(recognition_artifacts_loaded=1)
        return result
