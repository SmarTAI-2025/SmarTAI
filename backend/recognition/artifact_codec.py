"""Bounded, owner-bound evidence envelopes; schemas are not storage authorization.

Failed evidence is persistable without becoming a successful cache entry. Render
pixels, credentials and arbitrary payload dictionaries are not artifact kinds.
The byte ceilings bound serialized data, not process RSS or JSON object overhead.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal
import zlib

from pydantic import Field, ValidationError, model_validator

from backend.agents.recognition_agent import LocatorCallEvidenceV1
from backend.domain.errors import RecognitionError
from backend.recognition.cache_identity import RecognitionCacheIdentityV1, final_cache_identity
from backend.recognition.executor import ReadUnitV1
from backend.recognition.fusion import RecognitionAssemblyV1
from backend.recognition.models import EvidenceModel, RecognitionSourceRefV1
from backend.recognition.quality import assess_candidates
from backend.recognition.repair_records import RepairCallEvidenceV1
from backend.tools.pdf_evidence import PdfDetailResult, PdfIndexResult

MAX_COMPRESSED_BYTES = 16 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024
PayloadKind = Literal["native_index", "native_detail", "visual_read", "locator", "repair", "assembly"]
ArtifactPayload = PdfIndexResult | PdfDetailResult | ReadUnitV1 | LocatorCallEvidenceV1 | RepairCallEvidenceV1 | RecognitionAssemblyV1
_PAYLOAD_TYPES = {
    "native_index": PdfIndexResult, "native_detail": PdfDetailResult, "visual_read": ReadUnitV1,
    "locator": LocatorCallEvidenceV1, "repair": RepairCallEvidenceV1, "assembly": RecognitionAssemblyV1,
}
_LAYERS = {"native_index": "native", "native_detail": "native", "visual_read": "visual",
           "locator": "visual", "repair": "patch", "assembly": "final"}
_ENCODER = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _canonical_chunks(value):
    size = 0
    for chunk in _ENCODER.iterencode(value):
        encoded = chunk.encode("utf-8")
        size += len(encoded)
        if size > MAX_DECOMPRESSED_BYTES:
            raise RecognitionError("recognition_artifact_limit") from None
        yield encoded


def _payload_hash(payload):
    digest = hashlib.sha256()
    for chunk in _canonical_chunks(payload.model_dump(mode="json", warnings=False)):
        digest.update(chunk)
    return digest.hexdigest()


def _native_scope(payload):
    numbers = [page.page_number for page in payload.pages]
    if numbers != sorted(set(numbers)) or any(number > payload.total_pages for number in numbers):
        raise ValueError("native artifact page scope is inconsistent")
    if isinstance(payload, PdfIndexResult) and (
        payload.window_start > payload.window_end or payload.window_end > payload.total_pages
        or numbers != list(range(payload.window_start, payload.window_end + 1))
    ):
        raise ValueError("native index must retain its complete contiguous window")


class RecognitionArtifactV1(EvidenceModel):
    contract: Literal["smartai.recognition.artifact"] = "smartai.recognition.artifact"
    schema_version: Literal[1] = 1
    identity: RecognitionCacheIdentityV1
    source: RecognitionSourceRefV1 = Field(repr=False)
    payload_kind: PayloadKind
    payload: ArtifactPayload = Field(repr=False)
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def exact_identity_and_payload(self):
        self.identity = RecognitionCacheIdentityV1.model_validate(self.identity.model_dump(warnings=False))
        self.source = RecognitionSourceRefV1.model_validate(self.source.model_dump(warnings=False))
        expected_type = _PAYLOAD_TYPES[self.payload_kind]
        if type(self.payload) is not expected_type:
            raise ValueError("artifact kind and payload type disagree")
        self.payload = expected_type.model_validate(self.payload.model_dump(warnings=False))
        identity, source, payload = self.identity, self.source, self.payload
        if (identity.layer != _LAYERS[self.payload_kind] or identity.owner_id != source.owner_id
                or identity.source_sha256 != source.input_sha256 or identity.source_content_type != source.content_type):
            raise ValueError("artifact source and cache identity disagree")
        if self.payload_kind in {"native_index", "native_detail", "locator"} and source.content_type != "application/pdf":
            raise ValueError("PDF evidence requires its original PDF source")
        if self.payload_kind in {"native_index", "native_detail"}:
            _native_scope(payload)
        elif self.payload_kind == "visual_read":
            metadata = payload.image_preparation
            if payload.candidate.kind == "native":
                raise ValueError("visual reads must retain a model candidate")
            if metadata is not None and (metadata.source_sha256 != source.input_sha256
                                         or metadata.source_content_type != source.content_type):
                raise ValueError("visual preparation belongs to another source")
            if (source.content_type != "application/pdf") != (metadata is not None):
                raise ValueError("image-source evidence requires its preparation")
        elif self.payload_kind == "locator":
            if payload.result.candidate.kind == "native":
                raise ValueError("scan locations must retain a model candidate")
        elif self.payload_kind == "repair":
            if payload.image is not None:
                image = payload.image
                if image.source_sha256 != source.input_sha256 or (
                    (source.content_type != "application/pdf") != (image.image_preparation is not None)
                ) or (image.image_preparation is not None
                      and image.image_preparation.source_content_type != source.content_type):
                    raise ValueError("repair image belongs to another source")
            if payload.result is not None and identity.purpose != payload.result.context.purpose:
                raise ValueError("repair identity belongs to another purpose")
            if payload.result is not None and (payload.image.page_number != payload.result.context.page_number
                                                or payload.image.region != payload.result.context.region):
                raise ValueError("repair image belongs to another context region")
        elif self.payload_kind == "assembly":
            expected_identity = final_cache_identity(
                payload.raw.request, capabilities=payload.raw.engine_capabilities,
                prompt_version=payload.prompt_version, tool_version=identity.tool_version,
            )
            if payload.raw.request.source != source or identity != expected_identity:
                raise ValueError("assembly artifact belongs to another source or purpose")
        if self.payload_sha256 != _payload_hash(payload):
            raise ValueError("artifact payload digest mismatch")
        return self

    @property
    def cacheable_success(self) -> bool:
        verified = _validated(self)
        return _cacheable(verified.payload_kind, verified.payload, verified.identity.purpose)


def _candidate_success(candidate):
    return (candidate.status == "ok" and bool(candidate.text.strip()) and candidate.safe_error_code is None
            and candidate.finish_reason not in {"refused", "length"}
            and not {"provider_refused", "output_truncated"}.intersection(candidate.warning_codes))


def _cacheable(kind, payload, purpose):
    if kind in {"native_index", "native_detail"}:
        return True
    if kind == "visual_read":
        return (not payload.submission_may_exist and _candidate_success(payload.candidate)
                and assess_candidates(None, payload.candidate, purpose=purpose).confidence != "low")
    if kind == "locator":
        return (not payload.submission_may_exist and _candidate_success(payload.result.candidate)
                and not payload.result.candidate.warning_codes
                and payload.result.parse_status == "ok"
                and all(location.status == "candidate" for location in payload.result.locations))
    if kind == "repair":
        # A repair decision is an unverified proposal, including keep_*; reuse
        # must not turn the original uncertainty into a successful recognition.
        return False
    document = payload.document
    execution = payload.repair_execution
    return bool(
        document is not None and document.final_markdown.strip() and payload.safe_error_code is None
        and not payload.raw.budget.pending_calls and not payload.raw.stop_codes
        and (execution is None or not execution.budget.pending_calls and not execution.stop_codes)
        and document.coverage.complete and document.confidence != "low"
        and all(span.confidence != "low" for page in document.pages for span in page.spans)
    )


def _validated(envelope):
    try:
        if not isinstance(envelope, RecognitionArtifactV1):
            raise ValueError
        return RecognitionArtifactV1.model_validate(envelope.model_dump(warnings=False))
    except (ValidationError, ValueError, TypeError, AttributeError, RecursionError):
        raise RecognitionError("recognition_artifact_invalid") from None


def build_artifact(*, identity: RecognitionCacheIdentityV1, source: RecognitionSourceRefV1,
                   payload_kind: PayloadKind, payload: ArtifactPayload) -> RecognitionArtifactV1:
    """Construct a digest-bound envelope from an explicit, typed evidence model."""
    try:
        expected = _PAYLOAD_TYPES.get(payload_kind)
        if expected is None or type(payload) is not expected:
            raise ValueError
        payload = expected.model_validate(payload.model_dump(warnings=False))
        return RecognitionArtifactV1(identity=identity, source=source, payload_kind=payload_kind,
                                     payload=payload, payload_sha256=_payload_hash(payload))
    except (ValidationError, ValueError, TypeError, AttributeError, RecursionError):
        raise RecognitionError("recognition_artifact_invalid") from None


def encode_artifact(envelope: RecognitionArtifactV1) -> bytes:
    """Serialize deterministic single-member gzip, with bounded input and output."""
    try:
        envelope = _validated(envelope)
        compressor = zlib.compressobj(level=6, wbits=16 + zlib.MAX_WBITS)
        compressed = bytearray()
        for chunk in _canonical_chunks(envelope.model_dump(mode="json", warnings=False)):
            output = compressor.compress(chunk)
            if len(compressed) + len(output) > MAX_COMPRESSED_BYTES:
                raise RecognitionError("recognition_artifact_limit") from None
            compressed.extend(output)
        tail = compressor.flush()
        if len(compressed) + len(tail) > MAX_COMPRESSED_BYTES:
            raise RecognitionError("recognition_artifact_limit") from None
        compressed.extend(tail)
        return bytes(compressed)
    except (ValidationError, ValueError, TypeError, AttributeError, RecursionError, zlib.error):
        raise RecognitionError("recognition_artifact_invalid") from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("nonfinite JSON constant")


def decode_artifact(data: bytes) -> RecognitionArtifactV1:
    """Reject bombs, truncated streams, concatenated members and trailing bytes."""
    try:
        if type(data) is not bytes or not data:
            raise ValueError
        if len(data) > MAX_COMPRESSED_BYTES:
            raise RecognitionError("recognition_artifact_limit") from None
        decoder = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
        content = decoder.decompress(data, MAX_DECOMPRESSED_BYTES + 1)
        if len(content) > MAX_DECOMPRESSED_BYTES or decoder.unconsumed_tail:
            raise RecognitionError("recognition_artifact_limit") from None
        if not decoder.eof or decoder.unused_data:
            raise ValueError
        value = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if (not isinstance(value, dict) or value.get("contract") != "smartai.recognition.artifact"
                or type(value.get("schema_version")) is not int or value["schema_version"] != 1):
            raise ValueError
        return RecognitionArtifactV1.model_validate(value)
    except (ValidationError, ValueError, TypeError, AttributeError, RecursionError, zlib.error):
        raise RecognitionError("recognition_artifact_invalid") from None
