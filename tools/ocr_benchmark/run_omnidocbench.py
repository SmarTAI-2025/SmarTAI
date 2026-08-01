#!/usr/bin/env python3
"""Plan and execute an approved OmniDocBench OCR Smoke run.

The dry-run validates the approved sample list, local dataset snapshot,
manifests, image hashes, product input limits, OCR purposes, prompt hashes,
cache keys, and output paths. OCR calls require the explicit ``--execute`` flag.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import settings
from backend.skills.ocr_ingest import (
    LLMVisionOCRSkill,
    OCRImage,
    OCRResult,
    build_ocr_prompt,
)
from backend.tools.file_processing import IMAGE_MEDIA_TYPES, extract_text_from_upload
from fastapi import HTTPException

try:
    import fitz
except ImportError:  # pragma: no cover - project OCR dependencies include PyMuPDF
    fitz = None


RUNNER_SCHEMA_VERSION = 1
CACHE_KEY_SCHEMA_VERSION = 1
DEFAULT_CONFIG = Path(
    "tools/ocr_benchmark/config/omnidocbench_smoke_20260723.approved.json"
)
DEFAULT_DATASET_ROOT = Path("data/benchmarks/OmniDocBench")
DEFAULT_OUTPUT_DIR = Path(
    "artifacts/ocr_benchmark/omnidocbench_smoke_20260723"
)
DRY_RUN_FILENAME = "dry_run_plan.json"
RESULTS_FILENAME = "results.jsonl"
RUN_SUMMARY_FILENAME = "run_summary.json"
NORMALIZATION_MANIFEST_FILENAME = "normalization_manifest.jsonl"
NORMALIZATION_VERSION = "pymupdf-jpeg-v1"
NORMALIZATION_MAX_LONG_EDGE = 4096
NORMALIZATION_JPEG_QUALITY = 90
DETECTED_MEDIA_TYPES = {
    "jpeg": "image/jpeg",
    "png": "image/png",
}
VALID_PURPOSES = {"problems", "submissions", "reference", "test_cases"}


class DryRunError(ValueError):
    """Raised when an approved run cannot be planned safely."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise DryRunError(f"{label} not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DryRunError(f"Could not read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DryRunError(f"{label} root must be an object: {path}")
    return value


def _read_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise DryRunError(
            f"{label} not found: {path}. Run prepare_omnidocbench.py first."
        )
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise DryRunError(
                    f"{label} line {line_number} must be an object: {path}"
                )
            rows.append(value)
    except json.JSONDecodeError as exc:
        raise DryRunError(f"Could not parse {label} {path}: {exc}") from exc
    except OSError as exc:
        raise DryRunError(f"Could not read {label} {path}: {exc}") from exc
    return rows


def _safe_relative_path(raw_path: Any, *, label: str) -> PurePosixPath:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise DryRunError(f"{label} must be a non-empty relative path")
    path = PurePosixPath(raw_path.replace("\\", "/").strip())
    if path.is_absolute() or ".." in path.parts or path.as_posix() in {"", "."}:
        raise DryRunError(f"Unsafe {label}: {raw_path!r}")
    return path


def _resolve_under(root: Path, raw_path: Any, *, label: str) -> Path:
    relative = _safe_relative_path(raw_path, label=label)
    root = root.resolve()
    target = (root / Path(*relative.parts)).resolve()
    if not target.is_relative_to(root):
        raise DryRunError(f"{label} escapes its root: {raw_path!r}")
    return target


def _resolve_project_path(
    project_root: Path,
    raw_path: str | Path,
    *,
    label: str,
) -> Path:
    project_root = project_root.resolve()
    path = Path(raw_path)
    target = (path if path.is_absolute() else project_root / path).resolve()
    if not target.is_relative_to(project_root):
        raise DryRunError(f"{label} must stay inside the project root: {raw_path}")
    return target


def _display_project_path(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _index_unique(
    rows: Sequence[dict[str, Any]],
    *,
    label: str,
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row_number, row in enumerate(rows, start=1):
        sample_id = row.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id:
            raise DryRunError(f"{label} row {row_number} has no sample_id")
        if sample_id in indexed:
            raise DryRunError(f"Duplicate sample_id in {label}: {sample_id}")
        indexed[sample_id] = row
    return indexed


def _resolve_provider(
    provider_id: str | None,
    model: str | None,
) -> tuple[str, str]:
    if bool(provider_id) != bool(model):
        raise DryRunError("--provider-id and --model must be supplied together")
    if provider_id and model:
        return provider_id.strip(), model.strip()

    from backend.llm.registry import ExpertRegistry

    provider = ExpertRegistry().pick_vision()
    if provider is None:
        raise DryRunError(
            "No vision provider is configured. Supply --provider-id and --model "
            "for offline planning, or configure a vision provider."
        )
    return provider.provider_id, provider.model


def _resolve_registered_vision_provider(
    provider_id: str | None,
    model: str | None,
) -> Any:
    if bool(provider_id) != bool(model):
        raise DryRunError("--provider-id and --model must be supplied together")

    from backend.llm.registry import ExpertRegistry

    registry = ExpertRegistry()
    if provider_id and model:
        target_provider_id = provider_id.strip()
        target_model = model.strip()
        provider = registry.get(target_provider_id)
        if (
            provider is None
            or provider.model != target_model
            or not getattr(provider, "supports_vision", False)
        ):
            raise DryRunError(
                "Execute mode requires the requested vision provider to be "
                f"registered: {target_provider_id}/{target_model}"
            )
        return provider

    provider = registry.pick_vision()
    if provider is None:
        raise DryRunError("No registered vision provider is available for execution")
    return provider


def _read_download_revision(
    dataset_root: Path,
    annotation_file: PurePosixPath,
) -> str | None:
    metadata_path = (
        dataset_root
        / ".cache"
        / "huggingface"
        / "download"
        / Path(*annotation_file.parts)
    )
    metadata_path = metadata_path.with_name(metadata_path.name + ".metadata")
    if not metadata_path.is_file():
        return None
    try:
        first_line = metadata_path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError) as exc:
        raise DryRunError(
            f"Could not read Hugging Face revision metadata: {metadata_path}"
        ) from exc
    return first_line or None


def _validate_sample_id(sample_id: Any) -> str:
    if not isinstance(sample_id, str) or not sample_id.strip():
        raise DryRunError("Approved sample_id must be a non-empty string")
    if "/" in sample_id or "\\" in sample_id or sample_id in {".", ".."}:
        raise DryRunError(f"Unsafe approved sample_id: {sample_id!r}")
    return sample_id


def _cache_key(
    *,
    track: str = "raw_product",
    image_sha256: str,
    provider_id: str,
    model: str,
    purpose: str,
    prompt_sha256: str,
    media_type: str,
) -> str:
    return _canonical_sha256(
        {
            "schema_version": CACHE_KEY_SCHEMA_VERSION,
            "track": track,
            "ocr_skill": "LLMVisionOCRSkill",
            "image_sha256": image_sha256,
            "provider_id": provider_id,
            "model": model,
            "purpose": purpose,
            "prompt_sha256": prompt_sha256,
            "request": {
                "media_type": media_type,
                "temperature": 0.0,
            },
        }
    )


def build_dry_run_plan(
    *,
    project_root: str | Path,
    config_path: str | Path,
    dataset_root: str | Path,
    provider_id: str,
    model: str,
    product_max_image_bytes: int,
) -> dict[str, Any]:
    """Validate all approved inputs and return a deterministic no-call plan."""
    project = Path(project_root).resolve()
    config_file = _resolve_project_path(project, config_path, label="config path")
    dataset = _resolve_project_path(project, dataset_root, label="dataset root")
    if product_max_image_bytes <= 0:
        raise DryRunError("product_max_image_bytes must be positive")
    if not provider_id.strip() or not model.strip():
        raise DryRunError("provider_id and model must be non-empty")

    config = _read_json(config_file, label="approval config")
    if config.get("schema_version") != 1:
        raise DryRunError(
            f"Unsupported approval config schema_version: {config.get('schema_version')!r}"
        )
    approval = config.get("approval")
    if not isinstance(approval, dict) or approval.get("status") != "approved":
        status = approval.get("status") if isinstance(approval, dict) else None
        raise DryRunError(
            f"Smoke config is not approved (status={status!r}); refusing to plan a run"
        )

    dataset_config = config.get("dataset")
    manifest_config = config.get("manifest")
    constraints = config.get("constraints")
    samples_config = config.get("samples")
    if not isinstance(dataset_config, dict):
        raise DryRunError("approval config.dataset must be an object")
    if not isinstance(manifest_config, dict):
        raise DryRunError("approval config.manifest must be an object")
    if not isinstance(constraints, dict):
        raise DryRunError("approval config.constraints must be an object")
    if not isinstance(samples_config, list):
        raise DryRunError("approval config.samples must be a list")

    annotation_relative = _safe_relative_path(
        dataset_config.get("annotation_file"),
        label="annotation file",
    )
    annotation_path = _resolve_under(
        dataset,
        annotation_relative.as_posix(),
        label="annotation file",
    )
    if not annotation_path.is_file():
        raise DryRunError(f"Annotation file not found: {annotation_path}")
    annotation_sha256 = _sha256_file(annotation_path)
    expected_annotation_sha256 = dataset_config.get("annotation_sha256")
    if annotation_sha256 != expected_annotation_sha256:
        raise DryRunError(
            "Annotation SHA-256 mismatch: "
            f"expected {expected_annotation_sha256}, got {annotation_sha256}"
        )

    expected_revision = dataset_config.get("revision")
    if not isinstance(expected_revision, str) or not expected_revision:
        raise DryRunError("approval config.dataset.revision must be non-empty")
    detected_revision = _read_download_revision(dataset, annotation_relative)
    if detected_revision is not None and detected_revision != expected_revision:
        raise DryRunError(
            "Dataset revision mismatch: "
            f"expected {expected_revision}, got {detected_revision}"
        )

    manifest_path = _resolve_project_path(
        project,
        manifest_config.get("all_path", ""),
        label="manifest path",
    )
    pilot_path = _resolve_project_path(
        project,
        manifest_config.get("pilot_path", ""),
        label="pilot path",
    )
    manifest_rows = _read_jsonl(manifest_path, label="full manifest")
    pilot_rows = _read_jsonl(pilot_path, label="pilot manifest")
    manifest_by_id = _index_unique(manifest_rows, label="full manifest")
    pilot_by_id = _index_unique(pilot_rows, label="pilot manifest")

    expected_manifest_schema = manifest_config.get("schema_version")
    expected_count = constraints.get("sample_count")
    if not isinstance(expected_count, int) or expected_count <= 0:
        raise DryRunError("constraints.sample_count must be a positive integer")
    if len(samples_config) != expected_count:
        raise DryRunError(
            f"Approved sample count mismatch: expected {expected_count}, "
            f"got {len(samples_config)}"
        )

    expected_quotas = constraints.get("sampling_group_quotas")
    if not isinstance(expected_quotas, dict) or not all(
        isinstance(group, str) and isinstance(count, int) and count >= 0
        for group, count in expected_quotas.items()
    ):
        raise DryRunError("constraints.sampling_group_quotas is invalid")

    approved_ids: set[str] = set()
    group_counts: Counter[str] = Counter()
    purpose_counts: Counter[str] = Counter()
    plans: list[dict[str, Any]] = []

    for expected_order, approved in enumerate(samples_config, start=1):
        if not isinstance(approved, dict):
            raise DryRunError(f"Approved sample {expected_order} must be an object")
        if approved.get("order") != expected_order:
            raise DryRunError(
                f"Approved sample order mismatch at position {expected_order}: "
                f"{approved.get('order')!r}"
            )

        sample_id = _validate_sample_id(approved.get("sample_id"))
        if sample_id in approved_ids:
            raise DryRunError(f"Duplicate approved sample_id: {sample_id}")
        approved_ids.add(sample_id)

        manifest_row = manifest_by_id.get(sample_id)
        if manifest_row is None:
            raise DryRunError(f"Approved sample is missing from full manifest: {sample_id}")
        if sample_id not in pilot_by_id:
            raise DryRunError(f"Approved sample is missing from Pilot: {sample_id}")
        if manifest_row.get("schema_version") != expected_manifest_schema:
            raise DryRunError(
                f"Manifest schema mismatch for {sample_id}: "
                f"expected {expected_manifest_schema}, "
                f"got {manifest_row.get('schema_version')!r}"
            )
        if manifest_row.get("selection_eligible") is not True:
            raise DryRunError(f"Approved sample has no content annotations: {sample_id}")

        for field in ("image_path", "image_sha256", "sampling_group", "purpose"):
            if approved.get(field) != manifest_row.get(field):
                raise DryRunError(
                    f"Approved {field} differs from manifest for {sample_id}: "
                    f"{approved.get(field)!r} != {manifest_row.get(field)!r}"
                )

        purpose = manifest_row["purpose"]
        if purpose not in VALID_PURPOSES:
            raise DryRunError(f"Unsupported OCR purpose for {sample_id}: {purpose!r}")
        sampling_group = manifest_row["sampling_group"]
        if not isinstance(sampling_group, str) or not sampling_group:
            raise DryRunError(f"Missing sampling_group for {sample_id}")
        group_counts[sampling_group] += 1
        purpose_counts[purpose] += 1

        image_relative = _safe_relative_path(
            manifest_row["image_path"],
            label=f"image path for {sample_id}",
        )
        image_path = _resolve_under(
            dataset,
            image_relative.as_posix(),
            label=f"image path for {sample_id}",
        )
        if not image_path.is_file():
            raise DryRunError(f"Approved image not found for {sample_id}: {image_path}")
        image_bytes = image_path.stat().st_size
        if image_bytes != manifest_row.get("image_bytes"):
            raise DryRunError(
                f"Image byte count differs from manifest for {sample_id}: "
                f"{image_bytes} != {manifest_row.get('image_bytes')!r}"
            )
        image_sha256 = _sha256_file(image_path)
        if image_sha256 != approved["image_sha256"]:
            raise DryRunError(
                f"Image SHA-256 mismatch for {sample_id}: "
                f"expected {approved['image_sha256']}, got {image_sha256}"
            )

        image_suffix = image_path.suffix.lower()
        declared_media_type = IMAGE_MEDIA_TYPES.get(image_suffix)
        if declared_media_type is None:
            raise DryRunError(
                f"Product OCR does not support image suffix for {sample_id}: "
                f"{image_suffix!r}"
            )
        detected_media_type = DETECTED_MEDIA_TYPES.get(
            str(manifest_row.get("image_format"))
        )
        if detected_media_type is None:
            raise DryRunError(
                f"Unsupported detected image format for {sample_id}: "
                f"{manifest_row.get('image_format')!r}"
            )

        prompt = build_ocr_prompt(
            [
                OCRImage(
                    data=b"",
                    media_type=declared_media_type,
                    label=image_path.name,
                )
            ],
            purpose,
        )
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        over_product_limit = image_bytes > product_max_image_bytes
        warnings: list[str] = []
        if declared_media_type != detected_media_type:
            warnings.append("extension_content_mismatch")
        if over_product_limit:
            warnings.append("over_product_limit")

        if over_product_limit:
            action = "expected_input_rejection"
            expected_status = "input_rejected"
            cache_key = None
            prediction_path = None
        else:
            action = "ocr"
            expected_status = "pending_ocr"
            cache_key = _cache_key(
                image_sha256=image_sha256,
                provider_id=provider_id,
                model=model,
                purpose=purpose,
                prompt_sha256=prompt_sha256,
                media_type=declared_media_type,
            )
            prediction_path = f"predictions/raw/{sample_id}.md"

        plans.append(
            {
                "order": expected_order,
                "sample_id": sample_id,
                "sampling_group": sampling_group,
                "purpose": purpose,
                "image_path": image_relative.as_posix(),
                "image_sha256": image_sha256,
                "image_bytes": image_bytes,
                "image_width": manifest_row.get("image_width"),
                "image_height": manifest_row.get("image_height"),
                "declared_media_type": declared_media_type,
                "detected_media_type": detected_media_type,
                "prompt_sha256": prompt_sha256,
                "raw_track": {
                    "action": action,
                    "expected_status": expected_status,
                    "cache_key": cache_key,
                    "cache_path": (
                        f"cache/{cache_key}.json" if cache_key is not None else None
                    ),
                    "prediction_path": prediction_path,
                    "result_path": "results.jsonl",
                },
                "normalization_required": over_product_limit,
                "warnings": warnings,
            }
        )

    if dict(group_counts) != expected_quotas:
        raise DryRunError(
            f"Approved sampling quotas differ from config: "
            f"expected {expected_quotas}, got {dict(group_counts)}"
        )
    if constraints.get("all_samples_in_pilot") is True and not (
        approved_ids <= set(pilot_by_id)
    ):
        raise DryRunError("Not all approved samples are in Pilot")
    if constraints.get("all_samples_have_content_annotations") is True and not all(
        manifest_by_id[sample_id].get("selection_eligible") is True
        for sample_id in approved_ids
    ):
        raise DryRunError("Not all approved samples have content annotations")
    oversized_count = sum(
        plan["normalization_required"] for plan in plans
    )
    if (
        constraints.get("contains_over_product_limit_sample") is True
        and oversized_count == 0
    ):
        raise DryRunError("Approved Smoke has no sample over the product input limit")

    raw_ocr_count = sum(
        plan["raw_track"]["action"] == "ocr" for plan in plans
    )
    mismatch_count = sum(
        "extension_content_mismatch" in plan["warnings"] for plan in plans
    )
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "mode": "dry_run",
        "model_calls_performed": 0,
        "approval": {
            "status": approval["status"],
            "approved_by": approval.get("approved_by"),
            "approved_on": approval.get("approved_on"),
            "config_path": _display_project_path(config_file, project),
            "config_sha256": _sha256_file(config_file),
        },
        "dataset": {
            "name": dataset_config.get("name"),
            "root": _display_project_path(dataset, project),
            "revision": expected_revision,
            "revision_metadata_verified": detected_revision is not None,
            "annotation_sha256": annotation_sha256,
        },
        "manifests": {
            "all_path": _display_project_path(manifest_path, project),
            "all_sha256": _sha256_file(manifest_path),
            "pilot_path": _display_project_path(pilot_path, project),
            "pilot_sha256": _sha256_file(pilot_path),
        },
        "provider": {
            "provider_id": provider_id,
            "model": model,
            "vision_required": True,
        },
        "product": {
            "max_image_bytes": product_max_image_bytes,
            "ocr_concurrency": settings.ocr_concurrency,
            "media_type_source": "filename_suffix",
        },
        "outputs": {
            "dry_run_plan": DRY_RUN_FILENAME,
            "results": "results.jsonl",
            "prediction_directory": "predictions/raw",
            "cache_directory": "cache",
        },
        "summary": {
            "samples": len(plans),
            "raw_ocr_planned": raw_ocr_count,
            "raw_expected_input_rejections": len(plans) - raw_ocr_count,
            "normalization_required": oversized_count,
            "extension_content_mismatches": mismatch_count,
            "sampling_groups": dict(sorted(group_counts.items())),
            "purposes": dict(sorted(purpose_counts.items())),
            "unique_cache_keys": len(
                {
                    plan["raw_track"]["cache_key"]
                    for plan in plans
                    if plan["raw_track"]["cache_key"] is not None
                }
            ),
        },
        "samples": plans,
    }


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def _atomic_write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    _atomic_write_text(
        path,
        "".join(
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for row in rows
        ),
    )


def _sanitize_error(exc: BaseException) -> str:
    if isinstance(exc, HTTPException):
        raw = str(exc.detail)
    else:
        raw = str(exc)
    text = raw.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"sk-[A-Za-z0-9_-]{6,}", "sk-***", text)
    text = re.sub(
        r"(?i)(authorization|api[_ -]?key|bearer)(\s*[:=]?\s*)[^\s,;]+",
        r"\1\2<redacted>",
        text,
    )
    return text[:1000]


def _classify_error(exc: BaseException) -> str:
    if isinstance(exc, HTTPException):
        if exc.status_code == 413:
            return "input_too_large"
        if exc.status_code == 422:
            return "empty_output"
        return f"http_{exc.status_code}"
    message = str(exc).casefold()
    if "429" in message or "rate limit" in message or "quota" in message:
        return "rate_limited"
    if "timeout" in message or "timed out" in message:
        return "timeout"
    if "connection" in message or "connect" in message:
        return "connection"
    return "provider_error"


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, HTTPException):
        return exc.status_code == 422 or exc.status_code == 429 or exc.status_code >= 500
    return True


class _RecordingOCRSkill:
    def __init__(self, delegate: Any):
        self.delegate = delegate
        self.last_result: OCRResult | None = None

    async def recognize_images(
        self,
        images: list[OCRImage],
        purpose: str,
    ) -> OCRResult:
        result = await self.delegate.recognize_images(images, purpose)
        self.last_result = result
        return result


class _ResultStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self._lock = asyncio.Lock()
        self._records: dict[tuple[str, str], dict[str, Any]] = {}
        if self.path.is_file():
            for row in _read_jsonl(self.path, label="existing results"):
                track = row.get("track")
                sample_id = row.get("sample_id")
                if not isinstance(track, str) or not isinstance(sample_id, str):
                    raise DryRunError(
                        f"Existing result has no track/sample_id: {self.path}"
                    )
                key = (track, sample_id)
                if key in self._records:
                    raise DryRunError(
                        f"Duplicate existing result for {track}/{sample_id}"
                    )
                self._records[key] = row

    def get(self, track: str, sample_id: str) -> dict[str, Any] | None:
        return self._records.get((track, sample_id))

    def rows(self) -> list[dict[str, Any]]:
        track_order = {"raw_product": 0, "normalized_analysis": 1}
        return sorted(
            self._records.values(),
            key=lambda row: (
                int(row.get("order", 0)),
                track_order.get(str(row.get("track")), 99),
            ),
        )

    async def save(self, record: dict[str, Any]) -> None:
        key = (str(record["track"]), str(record["sample_id"]))
        async with self._lock:
            self._records[key] = record
            _atomic_write_jsonl(self.path, self.rows())


def _result_base(
    descriptor: dict[str, Any],
    *,
    provider_id: str,
    model: str,
    started_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "order": descriptor["order"],
        "sample_id": descriptor["sample_id"],
        "track": descriptor["track"],
        "sampling_group": descriptor["sampling_group"],
        "purpose": descriptor["purpose"],
        "input_path": descriptor["input_path"],
        "input_sha256": descriptor["input_sha256"],
        "input_bytes": descriptor["input_bytes"],
        "provider_id": provider_id,
        "model": model,
        "prompt_sha256": descriptor.get("prompt_sha256"),
        "cache_key": descriptor.get("cache_key"),
        "started_at": started_at,
        "finished_at": None,
        "wall_duration_ms": None,
        "attempts": 0,
        "retries": 0,
        "status": None,
        "error_kind": None,
        "error_message": None,
        "output_path": descriptor.get("prediction_path"),
        "output_sha256": None,
        "output_chars": 0,
        "empty_output": True,
        "unclear_count": 0,
        "response_provider": None,
        "response_model": None,
        "response_duration_ms": None,
        "input_tokens": None,
        "output_tokens": None,
    }


def _valid_existing_success(
    existing: dict[str, Any],
    *,
    descriptor: dict[str, Any],
    output_dir: Path,
) -> bool:
    if existing.get("status") not in {"success", "cached"}:
        return False
    if existing.get("cache_key") != descriptor.get("cache_key"):
        return False
    output_relative = existing.get("output_path")
    output_sha256 = existing.get("output_sha256")
    if not isinstance(output_relative, str) or not isinstance(output_sha256, str):
        return False
    output_path = _resolve_under(
        output_dir,
        output_relative,
        label=f"existing output path for {descriptor['sample_id']}",
    )
    return output_path.is_file() and _sha256_file(output_path) == output_sha256


def _load_valid_cache(
    cache_path: Path,
    *,
    descriptor: dict[str, Any],
    provider_id: str,
    model: str,
) -> dict[str, Any] | None:
    if not cache_path.is_file():
        return None
    cache = _read_json(cache_path, label="OCR cache")
    checks = {
        "cache_key": descriptor["cache_key"],
        "track": descriptor["track"],
        "sample_id": descriptor["sample_id"],
        "input_sha256": descriptor["input_sha256"],
        "provider_id": provider_id,
        "model": model,
        "purpose": descriptor["purpose"],
        "prompt_sha256": descriptor["prompt_sha256"],
    }
    if any(cache.get(field) != expected for field, expected in checks.items()):
        return None
    text = cache.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    if cache.get("text_sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest():
        return None
    return cache


async def _execute_ocr_descriptor(
    descriptor: dict[str, Any],
    *,
    output_dir: Path,
    ocr_skill: Any,
    provider_id: str,
    model: str,
    result_store: _ResultStore,
    max_retries: int,
    retry_failures: bool,
    retry_base_seconds: float,
) -> dict[str, Any]:
    sample_id = descriptor["sample_id"]
    track = descriptor["track"]
    existing = result_store.get(track, sample_id)
    if existing is not None and _valid_existing_success(
        existing,
        descriptor=descriptor,
        output_dir=output_dir,
    ):
        return {
            "track": track,
            "sample_id": sample_id,
            "outcome": "resumed_success",
            "model_calls": 0,
        }
    if (
        existing is not None
        and existing.get("status") == "failed"
        and not retry_failures
    ):
        return {
            "track": track,
            "sample_id": sample_id,
            "outcome": "skipped_failed",
            "model_calls": 0,
        }

    input_path = Path(descriptor["absolute_input_path"])
    input_bytes = input_path.read_bytes()
    if len(input_bytes) != descriptor["input_bytes"]:
        raise DryRunError(f"Input byte count changed before OCR: {sample_id}")
    if hashlib.sha256(input_bytes).hexdigest() != descriptor["input_sha256"]:
        raise DryRunError(f"Input SHA-256 changed before OCR: {sample_id}")

    cache_path = _resolve_under(
        output_dir,
        descriptor["cache_path"],
        label=f"cache path for {sample_id}",
    )
    prediction_path = _resolve_under(
        output_dir,
        descriptor["prediction_path"],
        label=f"prediction path for {sample_id}",
    )
    cache = _load_valid_cache(
        cache_path,
        descriptor=descriptor,
        provider_id=provider_id,
        model=model,
    )
    if cache is not None:
        started_at = _utc_now()
        t0 = time.perf_counter()
        text = cache["text"].strip()
        _atomic_write_text(prediction_path, text + "\n")
        record = _result_base(
            descriptor,
            provider_id=provider_id,
            model=model,
            started_at=started_at,
        )
        record.update(
            {
                "finished_at": _utc_now(),
                "wall_duration_ms": (time.perf_counter() - t0) * 1000,
                "status": "cached",
                "output_sha256": _sha256_file(prediction_path),
                "output_chars": len(text),
                "empty_output": False,
                "unclear_count": text.count("[unclear]"),
                "response_provider": cache.get("response_provider"),
                "response_model": cache.get("response_model"),
                "response_duration_ms": cache.get("response_duration_ms"),
                "input_tokens": cache.get("input_tokens"),
                "output_tokens": cache.get("output_tokens"),
            }
        )
        await result_store.save(record)
        return {
            "track": track,
            "sample_id": sample_id,
            "outcome": "cache_hit",
            "model_calls": 0,
        }

    started_at = _utc_now()
    t0 = time.perf_counter()
    attempts = 0
    last_exc: Exception | None = None
    last_result: OCRResult | None = None
    text = ""

    for attempt in range(1, max_retries + 2):
        attempts = attempt
        recorder = _RecordingOCRSkill(ocr_skill)
        try:
            text = await extract_text_from_upload(
                input_bytes,
                input_path.name,
                ocr_skill=recorder,
                purpose=descriptor["purpose"],
            )
            last_result = recorder.last_result
            break
        except Exception as exc:
            last_exc = exc
            if attempt > max_retries or not _is_retryable(exc):
                break
            wait_seconds = min(
                retry_base_seconds * (2 ** (attempt - 1)),
                30.0,
            )
            await asyncio.sleep(wait_seconds)

    finished_at = _utc_now()
    wall_duration_ms = (time.perf_counter() - t0) * 1000
    record = _result_base(
        descriptor,
        provider_id=provider_id,
        model=model,
        started_at=started_at,
    )
    record.update(
        {
            "finished_at": finished_at,
            "wall_duration_ms": wall_duration_ms,
            "attempts": attempts,
            "retries": max(0, attempts - 1),
        }
    )

    if last_exc is not None and (not text.strip()):
        record.update(
            {
                "status": "failed",
                "error_kind": _classify_error(last_exc),
                "error_message": _sanitize_error(last_exc),
            }
        )
        await result_store.save(record)
        return {
            "track": track,
            "sample_id": sample_id,
            "outcome": "failed",
            "model_calls": attempts,
        }

    text = text.strip()
    if not text:
        empty_error = ValueError("OCR returned empty text")
        record.update(
            {
                "status": "failed",
                "error_kind": "empty_output",
                "error_message": _sanitize_error(empty_error),
            }
        )
        await result_store.save(record)
        return {
            "track": track,
            "sample_id": sample_id,
            "outcome": "failed",
            "model_calls": attempts,
        }

    _atomic_write_text(prediction_path, text + "\n")
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cache_record = {
        "schema_version": CACHE_KEY_SCHEMA_VERSION,
        "cache_key": descriptor["cache_key"],
        "track": track,
        "sample_id": sample_id,
        "input_sha256": descriptor["input_sha256"],
        "provider_id": provider_id,
        "model": model,
        "purpose": descriptor["purpose"],
        "prompt_sha256": descriptor["prompt_sha256"],
        "text": text,
        "text_sha256": text_sha256,
        "response_provider": last_result.provider if last_result else None,
        "response_model": last_result.model if last_result else None,
        "response_duration_ms": last_result.duration_ms if last_result else None,
        "input_tokens": last_result.input_tokens if last_result else None,
        "output_tokens": last_result.output_tokens if last_result else None,
    }
    _atomic_write_json(cache_path, cache_record)
    record.update(
        {
            "status": "success",
            "output_sha256": _sha256_file(prediction_path),
            "output_chars": len(text),
            "empty_output": False,
            "unclear_count": text.count("[unclear]"),
            "response_provider": last_result.provider if last_result else None,
            "response_model": last_result.model if last_result else None,
            "response_duration_ms": last_result.duration_ms if last_result else None,
            "input_tokens": last_result.input_tokens if last_result else None,
            "output_tokens": last_result.output_tokens if last_result else None,
        }
    )
    await result_store.save(record)
    return {
        "track": track,
        "sample_id": sample_id,
        "outcome": "success",
        "model_calls": attempts,
    }


async def _execute_expected_rejection(
    sample: dict[str, Any],
    *,
    dataset_root: Path,
    ocr_skill: Any,
    provider_id: str,
    model: str,
    result_store: _ResultStore,
) -> dict[str, Any]:
    sample_id = sample["sample_id"]
    existing = result_store.get("raw_product", sample_id)
    if (
        existing is not None
        and existing.get("status") == "expected_input_rejection"
        and existing.get("input_sha256") == sample["image_sha256"]
    ):
        return {
            "track": "raw_product",
            "sample_id": sample_id,
            "outcome": "resumed_expected_rejection",
            "model_calls": 0,
        }

    image_path = _resolve_under(
        dataset_root,
        sample["image_path"],
        label=f"raw image path for {sample_id}",
    )
    image_bytes = image_path.read_bytes()
    started_at = _utc_now()
    t0 = time.perf_counter()
    descriptor = {
        "order": sample["order"],
        "sample_id": sample_id,
        "track": "raw_product",
        "sampling_group": sample["sampling_group"],
        "purpose": sample["purpose"],
        "input_path": sample["image_path"],
        "input_sha256": sample["image_sha256"],
        "input_bytes": sample["image_bytes"],
        "prompt_sha256": sample["prompt_sha256"],
        "cache_key": None,
        "prediction_path": None,
    }
    record = _result_base(
        descriptor,
        provider_id=provider_id,
        model=model,
        started_at=started_at,
    )
    try:
        await extract_text_from_upload(
            image_bytes,
            image_path.name,
            ocr_skill=ocr_skill,
            purpose=sample["purpose"],
        )
    except HTTPException as exc:
        if exc.status_code != 413:
            record.update(
                {
                    "status": "failed",
                    "error_kind": _classify_error(exc),
                    "error_message": _sanitize_error(exc),
                }
            )
            outcome = "failed"
        else:
            record.update(
                {
                    "status": "expected_input_rejection",
                    "error_kind": "input_too_large",
                    "error_message": _sanitize_error(exc),
                }
            )
            outcome = "expected_input_rejection"
    except Exception as exc:
        record.update(
            {
                "status": "failed",
                "error_kind": _classify_error(exc),
                "error_message": _sanitize_error(exc),
            }
        )
        outcome = "failed"
    else:
        record.update(
            {
                "status": "failed",
                "error_kind": "unexpected_ocr_acceptance",
                "error_message": (
                    "Product accepted an image that the dry-run classified as oversized"
                ),
            }
        )
        outcome = "failed"

    record.update(
        {
            "finished_at": _utc_now(),
            "wall_duration_ms": (time.perf_counter() - t0) * 1000,
            "attempts": 0,
            "retries": 0,
        }
    )
    await result_store.save(record)
    return {
        "track": "raw_product",
        "sample_id": sample_id,
        "outcome": outcome,
        "model_calls": 0,
    }


def _raw_descriptor(
    sample: dict[str, Any],
    *,
    dataset_root: Path,
) -> dict[str, Any]:
    image_path = _resolve_under(
        dataset_root,
        sample["image_path"],
        label=f"raw image path for {sample['sample_id']}",
    )
    raw_track = sample["raw_track"]
    return {
        "order": sample["order"],
        "sample_id": sample["sample_id"],
        "track": "raw_product",
        "sampling_group": sample["sampling_group"],
        "purpose": sample["purpose"],
        "input_path": sample["image_path"],
        "absolute_input_path": str(image_path),
        "input_sha256": sample["image_sha256"],
        "input_bytes": sample["image_bytes"],
        "prompt_sha256": sample["prompt_sha256"],
        "cache_key": raw_track["cache_key"],
        "cache_path": raw_track["cache_path"],
        "prediction_path": raw_track["prediction_path"],
    }


def normalize_oversized_image(
    source_path: str | Path,
    destination_path: str | Path,
    *,
    product_max_image_bytes: int,
) -> dict[str, Any]:
    """Create the fixed analysis-track JPEG for one oversized source image."""
    if fitz is None:
        raise DryRunError("PyMuPDF is required for normalized analysis inputs")
    source = Path(source_path).resolve()
    destination = Path(destination_path).resolve()
    if not source.is_file():
        raise DryRunError(f"Normalization source not found: {source}")

    try:
        source_pixmap = fitz.Pixmap(str(source))
        original_width = source_pixmap.width
        original_height = source_pixmap.height
        scale = min(
            1.0,
            NORMALIZATION_MAX_LONG_EDGE / max(original_width, original_height),
        )
        normalized_width = max(1, round(original_width * scale))
        normalized_height = max(1, round(original_height * scale))
        document = fitz.open()
        try:
            page = document.new_page(
                width=normalized_width,
                height=normalized_height,
            )
            page.insert_image(page.rect, pixmap=source_pixmap)
            normalized_pixmap = page.get_pixmap(alpha=False)
            normalized_bytes = normalized_pixmap.tobytes(
                "jpeg",
                jpg_quality=NORMALIZATION_JPEG_QUALITY,
            )
        finally:
            document.close()
            source_pixmap = None
    except Exception as exc:
        raise DryRunError(f"Could not normalize {source}: {exc}") from exc

    if len(normalized_bytes) > product_max_image_bytes:
        raise DryRunError(
            f"Normalized image still exceeds product limit: "
            f"{len(normalized_bytes)} > {product_max_image_bytes}"
        )
    _atomic_write_bytes(destination, normalized_bytes)
    return {
        "schema_version": 1,
        "normalization_version": NORMALIZATION_VERSION,
        "source_path": str(source),
        "source_sha256": _sha256_file(source),
        "source_bytes": source.stat().st_size,
        "source_width": original_width,
        "source_height": original_height,
        "normalized_path": str(destination),
        "normalized_sha256": _sha256_file(destination),
        "normalized_bytes": destination.stat().st_size,
        "normalized_width": normalized_width,
        "normalized_height": normalized_height,
        "format": "jpeg",
        "media_type": "image/jpeg",
        "parameters": {
            "max_long_edge": NORMALIZATION_MAX_LONG_EDGE,
            "jpeg_quality": NORMALIZATION_JPEG_QUALITY,
            "alpha": False,
        },
    }


def _normalized_descriptor(
    sample: dict[str, Any],
    normalization: dict[str, Any],
    *,
    output_dir: Path,
    provider_id: str,
    model: str,
) -> dict[str, Any]:
    normalized_path = Path(normalization["normalized_path"]).resolve()
    label = normalized_path.name
    prompt = build_ocr_prompt(
        [
            OCRImage(
                data=b"",
                media_type="image/jpeg",
                label=label,
            )
        ],
        sample["purpose"],
    )
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    cache_key = _cache_key(
        track="normalized_analysis",
        image_sha256=normalization["normalized_sha256"],
        provider_id=provider_id,
        model=model,
        purpose=sample["purpose"],
        prompt_sha256=prompt_sha256,
        media_type="image/jpeg",
    )
    input_relative = normalized_path.relative_to(output_dir).as_posix()
    return {
        "order": sample["order"],
        "sample_id": sample["sample_id"],
        "track": "normalized_analysis",
        "sampling_group": sample["sampling_group"],
        "purpose": sample["purpose"],
        "input_path": input_relative,
        "absolute_input_path": str(normalized_path),
        "input_sha256": normalization["normalized_sha256"],
        "input_bytes": normalization["normalized_bytes"],
        "prompt_sha256": prompt_sha256,
        "cache_key": cache_key,
        "cache_path": f"cache/{cache_key}.json",
        "prediction_path": (
            f"predictions/normalized/{sample['sample_id']}.md"
        ),
    }


def _build_run_summary(
    *,
    plan: dict[str, Any],
    result_store: _ResultStore,
    events: Sequence[dict[str, Any]],
    started_at: str,
    wall_duration_ms: float,
) -> dict[str, Any]:
    rows = result_store.rows()
    status_counts = Counter(str(row.get("status")) for row in rows)
    track_counts = Counter(str(row.get("track")) for row in rows)
    failed = [
        {
            "track": row.get("track"),
            "sample_id": row.get("sample_id"),
            "error_kind": row.get("error_kind"),
            "error_message": row.get("error_message"),
        }
        for row in rows
        if row.get("status") == "failed"
    ]
    expected_result_count = plan["summary"]["samples"] + plan["summary"][
        "normalization_required"
    ]
    complete_statuses = {"success", "cached", "expected_input_rejection"}
    complete = (
        len(rows) == expected_result_count
        and all(row.get("status") in complete_statuses for row in rows)
    )
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "wall_duration_ms": wall_duration_ms,
        "provider": plan["provider"],
        "approval_config_sha256": plan["approval"]["config_sha256"],
        "dry_run_plan_sha256": None,
        "model_calls_this_run": sum(int(event["model_calls"]) for event in events),
        "events": dict(
            sorted(Counter(str(event["outcome"]) for event in events).items())
        ),
        "results": {
            "count": len(rows),
            "expected_count": expected_result_count,
            "complete": complete,
            "statuses": dict(sorted(status_counts.items())),
            "tracks": dict(sorted(track_counts.items())),
            "failed": failed,
            "output_chars": sum(int(row.get("output_chars") or 0) for row in rows),
            "unclear_count": sum(int(row.get("unclear_count") or 0) for row in rows),
            "input_tokens": sum(int(row.get("input_tokens") or 0) for row in rows),
            "output_tokens": sum(int(row.get("output_tokens") or 0) for row in rows),
        },
    }


async def execute_smoke(
    plan: dict[str, Any],
    *,
    dataset_root: str | Path,
    output_dir: str | Path,
    ocr_skill: Any,
    concurrency: int,
    max_retries: int,
    retry_failures: bool = False,
    retry_base_seconds: float = 1.0,
) -> dict[str, Any]:
    """Execute approved raw and normalized Smoke tracks with persistence."""
    if plan.get("mode") != "dry_run" or plan.get("model_calls_performed") != 0:
        raise DryRunError("execute_smoke requires a validated dry-run plan")
    if concurrency <= 0:
        raise DryRunError("concurrency must be positive")
    if max_retries < 0:
        raise DryRunError("max_retries cannot be negative")
    if plan["product"]["max_image_bytes"] != settings.ocr_max_image_bytes:
        raise DryRunError(
            "Dry-run product limit differs from current product settings"
        )

    dataset = Path(dataset_root).resolve()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    result_store = _ResultStore(output / RESULTS_FILENAME)
    provider_id = plan["provider"]["provider_id"]
    model = plan["provider"]["model"]
    semaphore = asyncio.Semaphore(concurrency)
    events: list[dict[str, Any]] = []
    run_started_at = _utc_now()
    run_t0 = time.perf_counter()

    async def run_raw(sample: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            if sample["raw_track"]["action"] == "expected_input_rejection":
                return await _execute_expected_rejection(
                    sample,
                    dataset_root=dataset,
                    ocr_skill=ocr_skill,
                    provider_id=provider_id,
                    model=model,
                    result_store=result_store,
                )
            descriptor = _raw_descriptor(sample, dataset_root=dataset)
            return await _execute_ocr_descriptor(
                descriptor,
                output_dir=output,
                ocr_skill=ocr_skill,
                provider_id=provider_id,
                model=model,
                result_store=result_store,
                max_retries=max_retries,
                retry_failures=retry_failures,
                retry_base_seconds=retry_base_seconds,
            )

    raw_events = await asyncio.gather(
        *(run_raw(sample) for sample in plan["samples"])
    )
    events.extend(raw_events)

    normalization_rows: list[dict[str, Any]] = []
    for sample in plan["samples"]:
        if not sample["normalization_required"]:
            continue
        source_path = _resolve_under(
            dataset,
            sample["image_path"],
            label=f"normalization source for {sample['sample_id']}",
        )
        normalized_path = (
            output
            / "normalized_inputs"
            / f"{sample['sample_id']}.jpg"
        )
        normalization = normalize_oversized_image(
            source_path,
            normalized_path,
            product_max_image_bytes=plan["product"]["max_image_bytes"],
        )
        normalization["sample_id"] = sample["sample_id"]
        normalization["source_path"] = sample["image_path"]
        normalization["normalized_path"] = normalized_path.relative_to(output).as_posix()
        normalization_rows.append(normalization)

        descriptor = _normalized_descriptor(
            sample,
            {
                **normalization,
                "normalized_path": str(normalized_path),
            },
            output_dir=output,
            provider_id=provider_id,
            model=model,
        )
        async with semaphore:
            event = await _execute_ocr_descriptor(
                descriptor,
                output_dir=output,
                ocr_skill=ocr_skill,
                provider_id=provider_id,
                model=model,
                result_store=result_store,
                max_retries=max_retries,
                retry_failures=retry_failures,
                retry_base_seconds=retry_base_seconds,
            )
        events.append(event)

    _atomic_write_jsonl(
        output / NORMALIZATION_MANIFEST_FILENAME,
        normalization_rows,
    )
    summary = _build_run_summary(
        plan=plan,
        result_store=result_store,
        events=events,
        started_at=run_started_at,
        wall_duration_ms=(time.perf_counter() - run_t0) * 1000,
    )
    dry_run_path = output / DRY_RUN_FILENAME
    if dry_run_path.is_file():
        summary["dry_run_plan_sha256"] = _sha256_file(dry_run_path)
    _atomic_write_json(output / RUN_SUMMARY_FILENAME, summary)
    return summary


def write_dry_run_plan(output_dir: str | Path, plan: dict[str, Any]) -> Path:
    """Atomically write the deterministic dry-run plan."""
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / DRY_RUN_FILENAME
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return destination


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate an approved OmniDocBench Smoke list and emit a dry-run plan. "
            "OCR calls require the explicit --execute flag."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--provider-id",
        help="Optional offline provider id; must be supplied together with --model.",
    )
    parser.add_argument(
        "--model",
        help="Optional offline model name; must be supplied together with --provider-id.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Run the approved raw and normalized Smoke tracks. Without this flag, "
            "the command only writes a zero-call dry-run plan."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=settings.ocr_concurrency,
        help="Maximum concurrent OCR pages in execute mode.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=settings.llm_max_retries,
        help="Retries after the first OCR attempt for retryable failures.",
    )
    parser.add_argument(
        "--retry-failures",
        action="store_true",
        help="Retry samples recorded as failed by an earlier execute run.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    project_root = PROJECT_ROOT
    try:
        provider = None
        if args.execute:
            provider = _resolve_registered_vision_provider(
                args.provider_id,
                args.model,
            )
            provider_id, model = provider.provider_id, provider.model
        else:
            provider_id, model = _resolve_provider(args.provider_id, args.model)
        plan = build_dry_run_plan(
            project_root=project_root,
            config_path=args.config,
            dataset_root=args.dataset_root,
            provider_id=provider_id,
            model=model,
            product_max_image_bytes=settings.ocr_max_image_bytes,
        )
        output_dir = (
            args.output_dir
            if args.output_dir.is_absolute()
            else project_root / args.output_dir
        )
        destination = write_dry_run_plan(output_dir, plan)
        run_summary = None
        if args.execute:
            run_summary = asyncio.run(
                execute_smoke(
                    plan,
                    dataset_root=(
                        args.dataset_root
                        if args.dataset_root.is_absolute()
                        else project_root / args.dataset_root
                    ),
                    output_dir=output_dir,
                    ocr_skill=LLMVisionOCRSkill(provider),
                    concurrency=args.concurrency,
                    max_retries=args.max_retries,
                    retry_failures=args.retry_failures,
                )
            )
    except DryRunError as exc:
        print(f"dry-run failed: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Smoke execution interrupted; completed results remain resumable.", file=sys.stderr)
        return 130

    output_payload = {
        "mode": "execute" if args.execute else plan["mode"],
        "provider_id": plan["provider"]["provider_id"],
        "samples": plan["summary"]["samples"],
        "raw_ocr_planned": plan["summary"]["raw_ocr_planned"],
        "raw_expected_input_rejections": plan["summary"][
            "raw_expected_input_rejections"
        ],
        "output": str(destination),
    }
    if run_summary is None:
        output_payload["model_calls_performed"] = plan["model_calls_performed"]
    else:
        output_payload["model_calls_this_run"] = run_summary[
            "model_calls_this_run"
        ]
        output_payload["complete"] = run_summary["results"]["complete"]
        output_payload["statuses"] = run_summary["results"]["statuses"]
        output_payload["run_summary"] = str(
            Path(output_dir).resolve() / RUN_SUMMARY_FILENAME
        )
    print(json.dumps(output_payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
