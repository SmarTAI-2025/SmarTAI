"""Secret-free, owner-scoped identities; a key is never permission to read data."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from backend.domain.errors import RecognitionError
from backend.recognition.engine import EngineLocateInputV1, EngineReadInputV1, EngineRepairInputV1, freeze_engine_input
from backend.recognition.models import EvidenceModel, Purpose, RecognitionPolicyV1, RecognitionSourceRefV1
from backend.recognition.planner import EngineCapabilitiesV1
from backend.tools.pdf_evidence import ImagePrepareRequest, PdfContactSheetRequest, PdfIndexRequest, PdfPagesRequest, PdfRenderRequest

TOOL_VERSION = "bounded-pdf-image-v1"


def canonical_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=True, allow_nan=False).encode("utf-8")).hexdigest()


class RecognitionCacheIdentityV1(EvidenceModel):
    layer: Literal["native", "render", "visual", "patch", "final"]
    owner_id: str = Field(strict=True, min_length=1, max_length=240)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_content_type: Literal["application/pdf", "image/png", "image/jpeg", "image/webp", "image/bmp", "image/tiff"]
    parameters_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    tool_version: str = Field(strict=True, min_length=1, max_length=120)
    purpose: Purpose | None = None
    engine_fingerprint: str | None = Field(default=None, min_length=1, max_length=256)
    capabilities_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    policy_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    prompt_version: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def layer_contract(self):
        model_fields = (self.purpose, self.engine_fingerprint, self.capabilities_sha256, self.policy_sha256, self.prompt_version)
        if self.layer in {"native", "render"}:
            if any(value is not None for value in model_fields):
                raise ValueError("local evidence cannot depend on a model purpose or credential")
        elif any(value is None for value in model_fields):
            raise ValueError("model and final evidence require their complete frozen context")
        if self.layer == "native" and self.source_content_type != "application/pdf":
            raise ValueError("image pixels do not have native PDF text")
        return self

    @property
    def key(self) -> str:
        return canonical_digest({"contract": "smartai.recognition.cache_identity", "schema_version": 1,
                                 **self.model_dump(mode="json")})


def _source(source):
    return RecognitionSourceRefV1.model_validate(source.model_dump(warnings=False))


def _local_identity(source, request, layer, tool_version):
    source = _source(source)
    request = type(request).model_validate(request.model_dump(warnings=False))
    return RecognitionCacheIdentityV1(
        layer=layer, owner_id=source.owner_id, source_sha256=source.input_sha256,
        source_content_type=source.content_type, tool_version=tool_version,
        parameters_sha256=canonical_digest(request.model_dump(mode="json")),
    )


def native_cache_identity(source: RecognitionSourceRefV1, request: PdfIndexRequest | PdfPagesRequest,
                          *, tool_version: str = TOOL_VERSION) -> RecognitionCacheIdentityV1:
    try:
        if not isinstance(request, (PdfIndexRequest, PdfPagesRequest)) or source.content_type != "application/pdf":
            raise ValueError
        if isinstance(request, PdfPagesRequest) and request.operation != "detail":
            raise ValueError
        return _local_identity(source, request, "native", tool_version)
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise RecognitionError("recognition_request_invalid") from None


def render_cache_identity(source: RecognitionSourceRefV1, request: PdfRenderRequest | PdfContactSheetRequest | ImagePrepareRequest,
                          *, tool_version: str = TOOL_VERSION) -> RecognitionCacheIdentityV1:
    try:
        if isinstance(request, (PdfRenderRequest, PdfContactSheetRequest)):
            if source.content_type != "application/pdf":
                raise ValueError
        elif isinstance(request, ImagePrepareRequest):
            if source.content_type != request.content_type:
                raise ValueError
        else:
            raise ValueError
        return _local_identity(source, request, "render", tool_version)
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise RecognitionError("recognition_request_invalid") from None


def model_cache_identity(
    source: RecognitionSourceRefV1, request: EngineReadInputV1 | EngineLocateInputV1 | EngineRepairInputV1,
    *, capabilities: EngineCapabilitiesV1, policy: RecognitionPolicyV1, prompt_version: str,
    tool_version: str = TOOL_VERSION,
) -> RecognitionCacheIdentityV1:
    try:
        source, request = _source(source), freeze_engine_input(request)
        caps = EngineCapabilitiesV1.model_validate(capabilities.model_dump(warnings=False))
        policy = RecognitionPolicyV1.model_validate(policy.model_dump(warnings=False))
        if request.input_mode not in caps.visual_inputs:
            raise ValueError
        layer = "patch" if isinstance(request, EngineRepairInputV1) else "visual"
        if isinstance(request, EngineLocateInputV1):
            if not caps.target_location or len(request.images) > caps.max_locator_images:
                raise ValueError
            payloads = [hashlib.sha256(image.payload).hexdigest() for image in request.images]
            stage = "locator"
        else:
            if isinstance(request, EngineRepairInputV1) and (not caps.semantic_repair or not caps.response_recheck):
                raise ValueError
            payloads, stage = [hashlib.sha256(request.payload).hexdigest()], "repair" if layer == "patch" else "reader"
        return RecognitionCacheIdentityV1(
            layer=layer, owner_id=source.owner_id, source_sha256=source.input_sha256,
            source_content_type=source.content_type, purpose=request.purpose, tool_version=tool_version,
            engine_fingerprint=caps.fingerprint, capabilities_sha256=canonical_digest(caps.model_dump(mode="json")),
            policy_sha256=canonical_digest(policy.model_dump(mode="json")), prompt_version=prompt_version,
            parameters_sha256=canonical_digest({"stage": stage, "request": request.model_dump(mode="json"), "payloads": payloads}),
        )
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise RecognitionError("recognition_request_invalid") from None


def final_cache_identity(request, *, capabilities: EngineCapabilitiesV1 | None, prompt_version: str,
                         tool_version: str = TOOL_VERSION, workflow_version: Literal[1, 2] = 1,
                         repair_prompt_version: str | None = None) -> RecognitionCacheIdentityV1:
    from backend.agents.recognition_agent import RecognitionReadRequestV1
    from backend.recognition.fusion import ASSEMBLY_VERSION
    from backend.recognition.repair_records import RECHECK_VERSION
    from backend.recognition.repair_response import REPAIR_PROMPT_VERSION

    try:
        if type(workflow_version) is not int or workflow_version not in {1, 2}:
            raise ValueError
        repair_prompt_version = REPAIR_PROMPT_VERSION if repair_prompt_version is None else repair_prompt_version
        if repair_prompt_version not in {"faithful-region-recheck-v1", REPAIR_PROMPT_VERSION}:
            raise ValueError
        request = RecognitionReadRequestV1.model_validate(request.model_dump(warnings=False))
        caps = None if capabilities is None else EngineCapabilitiesV1.model_validate(capabilities.model_dump(warnings=False))
        return RecognitionCacheIdentityV1(
            layer="final", owner_id=request.source.owner_id, source_sha256=request.source.input_sha256,
            source_content_type=request.source.content_type, purpose=request.purpose, tool_version=tool_version,
            engine_fingerprint=caps.fingerprint if caps else "native-only",
            capabilities_sha256=canonical_digest(caps.model_dump(mode="json") if caps else None),
            policy_sha256=canonical_digest(request.policy.model_dump(mode="json")), prompt_version=prompt_version,
            parameters_sha256=canonical_digest({"request": request.model_dump(mode="json"),
                                                 "assembly": "faithful-assembly-v2" if workflow_version == 2 else ASSEMBLY_VERSION,
                                                 "recheck": RECHECK_VERSION, "repair_prompt": repair_prompt_version}),
        )
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise RecognitionError("recognition_request_invalid") from None
