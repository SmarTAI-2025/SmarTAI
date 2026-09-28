"""Persistable recheck metadata; image payloads and credentials stay ephemeral."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from backend.recognition.budget import BudgetSnapshot
from backend.recognition.models import Code, EvidenceModel, NormalizedRegionV1
from backend.recognition.repair_response import RepairResponseV1
from backend.recognition.repair_selection import RepairSelectionV1
from backend.tools.pdf_evidence import ImagePreparedMetadata

RECHECK_VERSION = "source-bound-recheck-v1"


class RepairImageEvidenceV1(EvidenceModel):
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    page_number: int = Field(strict=True, ge=1, le=10000)
    region: NormalizedRegionV1
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    payload_bytes: int = Field(strict=True, ge=1, le=10 * 1024 * 1024)
    width: int = Field(strict=True, ge=1, le=8192)
    height: int = Field(strict=True, ge=1, le=8192)
    content_type: Literal["image/png"] = "image/png"
    preparation: Literal["pdf_render_scale_2", "oriented_image"]
    image_preparation: ImagePreparedMetadata | None = None

    @model_validator(mode="after")
    def valid_preparation(self):
        if self.width * self.height > 16_000_000:
            raise ValueError("repair image exceeds pixel budget")
        metadata = self.image_preparation
        if (self.preparation == "oriented_image") != (metadata is not None):
            raise ValueError("image source requires its preparation evidence")
        if metadata is not None and (
            self.page_number != 1 or self.source_sha256 != metadata.source_sha256
            or self.region.as_tuple() != metadata.effective_region
            or (self.width, self.height) != (metadata.width, metadata.height)
        ):
            raise ValueError("repair image disagrees with preparation")
        return self


class RepairCallEvidenceV1(EvidenceModel):
    unit_id: str = Field(pattern=r"^u\d{4}$")
    image: RepairImageEvidenceV1 | None = None
    requested_output_tokens: int | None = Field(default=None, strict=True, ge=1, le=2048)
    result: RepairResponseV1 | None = Field(default=None, repr=False)
    submission_may_exist: bool = False
    safe_error_code: Code | None = None

    @model_validator(mode="after")
    def explicit_dispatch(self):
        if self.result is None:
            if self.requested_output_tokens is not None or self.submission_may_exist or self.safe_error_code is None:
                raise ValueError("undispatched repair needs an explicit failure without usage")
        elif self.image is None or self.requested_output_tokens is None or self.safe_error_code is not None:
            raise ValueError("dispatched repair must retain image, output bound and raw outcome")
        return self


class RepairExecutionV1(EvidenceModel):
    contract: Literal["smartai.recognition.recheck"] = "smartai.recognition.recheck"
    schema_version: Literal[1] = 1
    version: Literal["source-bound-recheck-v1"] = RECHECK_VERSION
    prompt_version: str = Field(min_length=1, max_length=120)
    selection: RepairSelectionV1
    calls: list[RepairCallEvidenceV1] = Field(default_factory=list, max_length=6)
    budget: BudgetSnapshot
    stop_codes: list[Code] = Field(default_factory=list, max_length=32)
