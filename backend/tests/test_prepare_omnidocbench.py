from __future__ import annotations

import base64
import json
from collections import Counter
from pathlib import Path

import pytest

from tools.ocr_benchmark.prepare_omnidocbench import (
    ManifestError,
    PILOT_QUOTAS,
    SMOKE_QUOTAS,
    generate_manifests,
)


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

DEFAULT_COUNTS = {
    "exam_paper": 48,
    "note": 36,
    "fuzzy_scan": 12,
    "equation_hard": 8,
    "table_hard": 8,
    "layout_hard": 8,
}

LANGUAGES = ("simplified_chinese", "english", "en_ch_mixed")


def _attributes(group: str, index: int) -> dict:
    data_source = {
        "exam_paper": "exam_paper",
        "note": "note",
    }.get(group, "book")
    special_issue = ["fuzzy_scan"] if group == "fuzzy_scan" else ["None"]
    subset = group if group.endswith("_hard") else "v1.5"
    return {
        "data_source": data_source,
        "language": LANGUAGES[index % len(LANGUAGES)],
        "layout": "single_column",
        "special_issue": special_issue,
        "subset": subset,
    }


def _build_dataset(
    tmp_path: Path,
    *,
    counts: dict[str, int] | None = None,
    oversized: bool = True,
) -> Path:
    dataset_root = tmp_path / "dataset"
    image_root = dataset_root / "images"
    image_root.mkdir(parents=True)
    annotations = []

    for group, count in (counts or DEFAULT_COUNTS).items():
        for index in range(count):
            filename = f"{group}_{index:03}.png"
            body = PNG_1X1
            if oversized and group == "layout_hard" and index == 0:
                body += b"x" * 128
            (image_root / filename).write_bytes(body)
            annotations.append(
                {
                    "layout_dets": [
                        {
                            "category_type": "text_block",
                            "ignore": False,
                            "text": f"{group} sample {index}",
                        }
                    ],
                    "page_info": {
                        "page_no": index,
                        "height": 1,
                        "width": 1,
                        "image_path": filename,
                        "page_attribute": _attributes(group, index),
                    },
                    "extra": {},
                }
            )

    (dataset_root / "OmniDocBench.json").write_text(
        json.dumps(annotations, ensure_ascii=False),
        encoding="utf-8",
    )
    return dataset_root


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def test_generate_manifests_is_deterministic_and_respects_quotas(tmp_path):
    dataset_root = _build_dataset(tmp_path)
    first_output = tmp_path / "out-1"
    second_output = tmp_path / "out-2"

    first_summary = generate_manifests(
        dataset_root,
        first_output,
        seed=20260723,
        max_image_bytes=len(PNG_1X1) + 64,
    )
    second_summary = generate_manifests(
        dataset_root,
        second_output,
        seed=20260723,
        max_image_bytes=len(PNG_1X1) + 64,
    )

    assert first_summary == second_summary
    for filename in ("manifest.jsonl", "smoke.jsonl", "pilot.jsonl", "summary.json"):
        assert (first_output / filename).read_bytes() == (second_output / filename).read_bytes()

    manifest = _read_jsonl(first_output / "manifest.jsonl")
    smoke = _read_jsonl(first_output / "smoke.jsonl")
    pilot = _read_jsonl(first_output / "pilot.jsonl")

    assert len(manifest) == 120
    assert len(smoke) == sum(SMOKE_QUOTAS.values()) == 12
    assert len(pilot) == sum(PILOT_QUOTAS.values()) == 120
    assert {row["sample_id"] for row in smoke} <= {row["sample_id"] for row in pilot}
    assert Counter(row["sampling_group"] for row in smoke) == SMOKE_QUOTAS
    assert Counter(row["sampling_group"] for row in pilot) == PILOT_QUOTAS
    assert any(row["over_product_limit"] for row in smoke)
    assert first_summary["invariants"]["smoke_contains_oversized"] is True
    assert first_summary["decode_verified"] is True

    exam = next(row for row in manifest if row["data_source"] == "exam_paper")
    note = next(row for row in manifest if row["data_source"] == "note")
    reference = next(row for row in manifest if row["data_source"] == "book")
    assert exam["purpose"] == "problems"
    assert note["purpose"] == "submissions"
    assert reference["purpose"] == "reference"
    assert note["special_issues"] == []
    assert all(row["selection_eligible"] for row in smoke)
    assert all(row["selection_eligible"] for row in pilot)
    assert first_summary["invariants"]["selected_pages_have_content"] is True
    assert first_summary["all"]["extension_content_mismatch"] == 0


def test_generate_manifests_rejects_missing_image(tmp_path):
    dataset_root = _build_dataset(tmp_path)
    missing = dataset_root / "images" / "exam_paper_000.png"
    missing.unlink()

    with pytest.raises(ManifestError, match="Annotated image is missing"):
        generate_manifests(dataset_root, tmp_path / "out", verify_decode=False)


def test_generate_manifests_rejects_dimension_mismatch(tmp_path):
    dataset_root = _build_dataset(tmp_path)
    annotation_path = dataset_root / "OmniDocBench.json"
    annotations = json.loads(annotation_path.read_text(encoding="utf-8"))
    annotations[0]["page_info"]["width"] = 2
    annotation_path.write_text(json.dumps(annotations), encoding="utf-8")

    with pytest.raises(ManifestError, match="Annotation dimensions differ"):
        generate_manifests(dataset_root, tmp_path / "out", verify_decode=False)


def test_generate_manifests_rejects_unsafe_image_path(tmp_path):
    dataset_root = _build_dataset(tmp_path)
    annotation_path = dataset_root / "OmniDocBench.json"
    annotations = json.loads(annotation_path.read_text(encoding="utf-8"))
    annotations[0]["page_info"]["image_path"] = "../outside.png"
    annotation_path.write_text(json.dumps(annotations), encoding="utf-8")

    with pytest.raises(ManifestError, match="Unsafe image path"):
        generate_manifests(dataset_root, tmp_path / "out", verify_decode=False)


def test_generate_manifests_rejects_insufficient_sampling_group(tmp_path):
    counts = dict(DEFAULT_COUNTS)
    counts["exam_paper"] = 47
    dataset_root = _build_dataset(tmp_path, counts=counts)

    with pytest.raises(ManifestError, match="Not enough candidates for pilot:exam_paper"):
        generate_manifests(dataset_root, tmp_path / "out", verify_decode=False)


def test_generate_manifests_records_extension_content_mismatch(tmp_path):
    dataset_root = _build_dataset(tmp_path)
    annotation_path = dataset_root / "OmniDocBench.json"
    annotations = json.loads(annotation_path.read_text(encoding="utf-8"))
    old_name = annotations[0]["page_info"]["image_path"]
    new_name = Path(old_name).with_suffix(".jpg").name
    (dataset_root / "images" / old_name).rename(dataset_root / "images" / new_name)
    annotations[0]["page_info"]["image_path"] = new_name
    annotation_path.write_text(json.dumps(annotations), encoding="utf-8")

    output = tmp_path / "out"
    summary = generate_manifests(dataset_root, output, verify_decode=False)
    manifest = _read_jsonl(output / "manifest.jsonl")
    mismatched = [row for row in manifest if row["extension_content_mismatch"]]

    assert summary["all"]["extension_content_mismatch"] == 1
    assert len(mismatched) == 1
    assert mismatched[0]["image_format"] == "png"
    assert mismatched[0]["image_suffix"] == ".jpg"


def test_generate_manifests_keeps_marginal_only_page_out_of_splits(tmp_path):
    counts = dict(DEFAULT_COUNTS)
    counts["note"] += 1
    dataset_root = _build_dataset(tmp_path, counts=counts)
    annotation_path = dataset_root / "OmniDocBench.json"
    annotations = json.loads(annotation_path.read_text(encoding="utf-8"))
    blank = next(
        row
        for row in annotations
        if row["page_info"]["image_path"] == "note_000.png"
    )
    blank["layout_dets"] = [
        {
            "category_type": "header",
            "ignore": False,
            "text": "NO. Date",
        }
    ]
    annotation_path.write_text(json.dumps(annotations), encoding="utf-8")

    output = tmp_path / "out"
    summary = generate_manifests(dataset_root, output, verify_decode=False)
    manifest = _read_jsonl(output / "manifest.jsonl")
    smoke = _read_jsonl(output / "smoke.jsonl")
    pilot = _read_jsonl(output / "pilot.jsonl")
    blank_record = next(row for row in manifest if row["sample_id"] == "note_000")

    assert blank_record["annotation_block_count"] == 1
    assert blank_record["active_annotation_block_count"] == 1
    assert blank_record["content_annotation_block_count"] == 0
    assert blank_record["selection_eligible"] is False
    assert blank_record["splits"] == []
    assert blank_record["sample_id"] not in {row["sample_id"] for row in smoke}
    assert blank_record["sample_id"] not in {row["sample_id"] for row in pilot}
    assert summary["all"]["selection_ineligible"] == 1
    assert summary["invariants"]["selected_pages_have_content"] is True
