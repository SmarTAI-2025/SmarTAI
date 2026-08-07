from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from backend.config import settings
from backend.skills.ocr_ingest import OCRResult
from tools.ocr_benchmark.run_omnidocbench import (
    DryRunError,
    build_dry_run_plan,
    execute_smoke,
    write_dry_run_plan,
)

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
PRODUCT_LIMIT = 2048


class FakeExecutionOCR:
    def __init__(self):
        self.calls = []

    async def recognize_images(self, images, purpose):
        self.calls.append({"label": images[0].label, "purpose": purpose})
        return OCRResult(
            text=f"OCR output for {images[0].label}",
            provider="openai:test-vision",
            model="test-vision",
            duration_ms=12.5,
            input_tokens=100,
            output_tokens=20,
        )


class FlakyExecutionOCR(FakeExecutionOCR):
    async def recognize_images(self, images, purpose):
        if not self.calls:
            self.calls.append({"label": images[0].label, "purpose": purpose})
            raise ConnectionError("temporary test connection failure")
        return await super().recognize_images(images, purpose)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _build_fixture(tmp_path: Path, *, status: str = "approved") -> dict[str, Path]:
    project = tmp_path / "project"
    dataset = project / "data" / "benchmarks" / "OmniDocBench"
    images = dataset / "images"
    images.mkdir(parents=True)

    accepted_body = PNG_1X1
    oversized_body = PNG_1X1 + (b"x" * 4096)
    (images / "accepted.png").write_bytes(accepted_body)
    (images / "oversized.png").write_bytes(oversized_body)
    annotation_body = b"fixture annotation"
    (dataset / "OmniDocBench.json").write_bytes(annotation_body)
    metadata = (
        dataset
        / ".cache"
        / "huggingface"
        / "download"
        / "OmniDocBench.json.metadata"
    )
    metadata.parent.mkdir(parents=True)
    metadata.write_text("fixture-revision\nfixture-etag\n", encoding="utf-8")

    rows = [
        {
            "schema_version": 2,
            "sample_id": "accepted",
            "image_path": "images/accepted.png",
            "image_sha256": _sha256(accepted_body),
            "image_bytes": len(accepted_body),
            "image_width": 1,
            "image_height": 1,
            "image_format": "jpeg",
            "image_suffix": ".png",
            "extension_content_mismatch": True,
            "over_product_limit": False,
            "selection_eligible": True,
            "sampling_group": "note",
            "purpose": "submissions",
        },
        {
            "schema_version": 2,
            "sample_id": "oversized",
            "image_path": "images/oversized.png",
            "image_sha256": _sha256(oversized_body),
            "image_bytes": len(oversized_body),
            "image_width": 2,
            "image_height": 2,
            "image_format": "png",
            "image_suffix": ".png",
            "extension_content_mismatch": False,
            "over_product_limit": True,
            "selection_eligible": True,
            "sampling_group": "layout_hard",
            "purpose": "problems",
        },
    ]
    manifest_path = project / "artifacts" / "manifest.jsonl"
    pilot_path = project / "artifacts" / "pilot.jsonl"
    _write_jsonl(manifest_path, rows)
    _write_jsonl(pilot_path, rows)

    config = {
        "schema_version": 1,
        "approval": {
            "status": status,
            "approved_by": "user" if status == "approved" else None,
            "approved_on": "2026-07-24" if status == "approved" else None,
        },
        "dataset": {
            "name": "opendatalab/OmniDocBench",
            "revision": "fixture-revision",
            "annotation_file": "OmniDocBench.json",
            "annotation_sha256": _sha256(annotation_body),
        },
        "manifest": {
            "schema_version": 2,
            "seed": 20260723,
            "all_path": "artifacts/manifest.jsonl",
            "pilot_path": "artifacts/pilot.jsonl",
        },
        "constraints": {
            "sample_count": 2,
            "sampling_group_quotas": {
                "note": 1,
                "layout_hard": 1,
            },
            "all_samples_in_pilot": True,
            "all_samples_have_content_annotations": True,
            "contains_over_product_limit_sample": True,
        },
        "samples": [
            {
                "order": index,
                "sample_id": row["sample_id"],
                "image_path": row["image_path"],
                "image_sha256": row["image_sha256"],
                "sampling_group": row["sampling_group"],
                "purpose": row["purpose"],
            }
            for index, row in enumerate(rows, start=1)
        ],
    }
    config_path = project / "tools" / "approved.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "project": project,
        "dataset": dataset,
        "config": config_path,
    }


def _plan(paths: dict[str, Path]) -> dict:
    return build_dry_run_plan(
        project_root=paths["project"],
        config_path=paths["config"],
        dataset_root=paths["dataset"],
        provider_id="openai:test-vision",
        model="test-vision",
        product_max_image_bytes=PRODUCT_LIMIT,
    )


def test_dry_run_is_deterministic_and_plans_no_model_calls(tmp_path):
    paths = _build_fixture(tmp_path)

    first = _plan(paths)
    second = _plan(paths)
    first_path = write_dry_run_plan(tmp_path / "out-1", first)
    second_path = write_dry_run_plan(tmp_path / "out-2", second)

    assert first == second
    assert first_path.read_bytes() == second_path.read_bytes()
    assert first["mode"] == "dry_run"
    assert first["model_calls_performed"] == 0
    assert first["summary"]["samples"] == 2
    assert first["summary"]["raw_ocr_planned"] == 1
    assert first["summary"]["raw_expected_input_rejections"] == 1
    assert first["summary"]["normalization_required"] == 1
    assert first["summary"]["extension_content_mismatches"] == 1
    assert first["summary"]["unique_cache_keys"] == 1

    accepted, oversized = first["samples"]
    assert accepted["declared_media_type"] == "image/png"
    assert accepted["detected_media_type"] == "image/jpeg"
    assert accepted["warnings"] == ["extension_content_mismatch"]
    assert accepted["raw_track"]["action"] == "ocr"
    assert len(accepted["raw_track"]["cache_key"]) == 64
    assert accepted["raw_track"]["prediction_path"] == "predictions/raw/accepted.md"

    assert oversized["warnings"] == ["over_product_limit"]
    assert oversized["raw_track"]["action"] == "expected_input_rejection"
    assert oversized["raw_track"]["expected_status"] == "input_rejected"
    assert oversized["raw_track"]["cache_key"] is None
    assert oversized["raw_track"]["prediction_path"] is None
    assert oversized["normalization_required"] is True


def test_dry_run_refuses_pending_config(tmp_path):
    paths = _build_fixture(tmp_path, status="pending")

    with pytest.raises(DryRunError, match="not approved"):
        _plan(paths)


def test_dry_run_refuses_tampered_image(tmp_path):
    paths = _build_fixture(tmp_path)
    (paths["dataset"] / "images" / "accepted.png").write_bytes(b"tampered")

    with pytest.raises(DryRunError, match="byte count differs|SHA-256 mismatch"):
        _plan(paths)


def test_dry_run_refuses_sample_not_in_pilot(tmp_path):
    paths = _build_fixture(tmp_path)
    pilot_path = paths["project"] / "artifacts" / "pilot.jsonl"
    first_row = json.loads(pilot_path.read_text(encoding="utf-8").splitlines()[0])
    _write_jsonl(pilot_path, [first_row])

    with pytest.raises(DryRunError, match="missing from Pilot"):
        _plan(paths)


def test_runner_cli_can_import_project_packages():
    project_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "tools/ocr_benchmark/run_omnidocbench.py",
            "--help",
        ],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "explicit --execute flag" in result.stdout


def test_execute_smoke_runs_product_and_normalized_tracks_with_resume(
    tmp_path,
    monkeypatch,
):
    paths = _build_fixture(tmp_path)
    plan = _plan(paths)
    output = tmp_path / "execution"
    ocr = FakeExecutionOCR()
    monkeypatch.setattr(settings, "ocr_max_image_bytes", PRODUCT_LIMIT)

    first = asyncio.run(
        execute_smoke(
            plan,
            dataset_root=paths["dataset"],
            output_dir=output,
            ocr_skill=ocr,
            concurrency=2,
            max_retries=0,
            retry_base_seconds=0,
        )
    )

    assert first["results"]["complete"] is True
    assert first["model_calls_this_run"] == 2
    assert first["results"]["statuses"] == {
        "expected_input_rejection": 1,
        "success": 2,
    }
    assert first["results"]["tracks"] == {
        "normalized_analysis": 1,
        "raw_product": 2,
    }
    assert len(ocr.calls) == 2
    assert {call["purpose"] for call in ocr.calls} == {"problems", "submissions"}

    rows = [
        json.loads(line)
        for line in (output / "results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    expected_rejection = next(
        row for row in rows if row["status"] == "expected_input_rejection"
    )
    assert expected_rejection["track"] == "raw_product"
    assert expected_rejection["attempts"] == 0
    assert expected_rejection["error_kind"] == "input_too_large"

    successful = [row for row in rows if row["status"] == "success"]
    assert all(row["response_duration_ms"] == 12.5 for row in successful)
    assert all(row["input_tokens"] == 100 for row in successful)
    assert all(row["output_tokens"] == 20 for row in successful)
    assert all((output / row["output_path"]).is_file() for row in successful)

    normalization = json.loads(
        (output / "normalization_manifest.jsonl").read_text(encoding="utf-8")
    )
    assert normalization["normalization_version"] == "pymupdf-jpeg-v1"
    assert normalization["normalized_bytes"] <= PRODUCT_LIMIT
    assert normalization["normalized_path"].endswith(".jpg")

    second = asyncio.run(
        execute_smoke(
            plan,
            dataset_root=paths["dataset"],
            output_dir=output,
            ocr_skill=ocr,
            concurrency=2,
            max_retries=0,
            retry_base_seconds=0,
        )
    )

    assert second["results"]["complete"] is True
    assert second["model_calls_this_run"] == 0
    assert second["events"] == {
        "resumed_expected_rejection": 1,
        "resumed_success": 2,
    }
    assert len(ocr.calls) == 2


def test_execute_smoke_retries_transient_ocr_failure(tmp_path, monkeypatch):
    paths = _build_fixture(tmp_path)
    plan = _plan(paths)
    output = tmp_path / "retry-execution"
    ocr = FlakyExecutionOCR()
    monkeypatch.setattr(settings, "ocr_max_image_bytes", PRODUCT_LIMIT)

    summary = asyncio.run(
        execute_smoke(
            plan,
            dataset_root=paths["dataset"],
            output_dir=output,
            ocr_skill=ocr,
            concurrency=2,
            max_retries=1,
            retry_base_seconds=0,
        )
    )

    assert summary["results"]["complete"] is True
    assert summary["model_calls_this_run"] == 3
    rows = [
        json.loads(line)
        for line in (output / "results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    retried = next(
        row
        for row in rows
        if row["sample_id"] == "accepted" and row["track"] == "raw_product"
    )
    assert retried["status"] == "success"
    assert retried["attempts"] == 2
    assert retried["retries"] == 1
