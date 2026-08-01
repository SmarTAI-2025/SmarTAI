#!/usr/bin/env python3
"""Validate OmniDocBench and build deterministic OCR benchmark manifests.

Outputs:

* ``manifest.jsonl``: every annotated page.
* ``smoke.jsonl``: the fixed 12-page manual-cost smoke split.
* ``pilot.jsonl``: the fixed 120-page stratified pilot split.
* ``summary.json``: deterministic integrity and distribution metadata.

The script deliberately contains no model calls.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import struct
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

try:
    import fitz
except ImportError:  # pragma: no cover - exercised only in incomplete environments
    fitz = None


SCHEMA_VERSION = 2
DEFAULT_SEED = 20260723
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024
SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png"}
NON_CONTENT_CATEGORIES = {"abandon", "footer", "header", "page_number"}

GROUP_PRIORITY = (
    "equation_hard",
    "table_hard",
    "layout_hard",
    "fuzzy_scan",
    "exam_paper",
    "note",
)

SMOKE_QUOTAS = {
    "exam_paper": 4,
    "note": 3,
    "fuzzy_scan": 2,
    "equation_hard": 1,
    "table_hard": 1,
    "layout_hard": 1,
}

PILOT_QUOTAS = {
    "exam_paper": 48,
    "note": 36,
    "fuzzy_scan": 12,
    "equation_hard": 8,
    "table_hard": 8,
    "layout_hard": 8,
}

JPEG_START_OF_FRAME_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}


class ManifestError(ValueError):
    """Raised when dataset integrity or sampling requirements are not met."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_seed(seed: int, *parts: str) -> int:
    payload = ":".join((str(seed), *parts)).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _normalise_special_issues(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise ManifestError(f"special_issue must be a string or list, got {type(value).__name__}")

    result: list[str] = []
    for item in values:
        text = str(item).strip()
        if not text or text.casefold() in {"none", "null", "n/a"}:
            continue
        if text not in result:
            result.append(text)
    return sorted(result)


def _safe_image_relative_path(raw_path: Any) -> PurePosixPath:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ManifestError("page_info.image_path must be a non-empty string")

    normalised = raw_path.replace("\\", "/").strip()
    path = PurePosixPath(normalised)
    if path.is_absolute() or ".." in path.parts:
        raise ManifestError(f"Unsafe image path in annotation: {raw_path!r}")

    if path.parts and path.parts[0] == "images":
        relative = path
    else:
        relative = PurePosixPath("images") / path

    if len(relative.parts) < 2:
        raise ManifestError(f"Invalid image path in annotation: {raw_path!r}")
    return relative


def _png_size(handle, path: Path) -> tuple[int, int]:
    header = handle.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ManifestError(f"Invalid PNG header: {path}")
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        raise ManifestError(f"Invalid PNG dimensions for {path}: {width}x{height}")
    return width, height


def _jpeg_size(handle, path: Path) -> tuple[int, int]:
    if handle.read(2) != b"\xff\xd8":
        raise ManifestError(f"Invalid JPEG header: {path}")

    while True:
        byte = handle.read(1)
        if not byte:
            break
        if byte != b"\xff":
            continue

        marker_byte = handle.read(1)
        while marker_byte == b"\xff":
            marker_byte = handle.read(1)
        if not marker_byte:
            break

        marker = marker_byte[0]
        if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
            continue

        length_bytes = handle.read(2)
        if len(length_bytes) != 2:
            break
        segment_length = struct.unpack(">H", length_bytes)[0]
        if segment_length < 2:
            raise ManifestError(f"Invalid JPEG segment length in {path}")

        if marker in JPEG_START_OF_FRAME_MARKERS:
            frame = handle.read(5)
            if len(frame) != 5:
                break
            height, width = struct.unpack(">HH", frame[1:5])
            if width <= 0 or height <= 0:
                raise ManifestError(f"Invalid JPEG dimensions for {path}: {width}x{height}")
            return width, height

        handle.seek(segment_length - 2, 1)

    raise ManifestError(f"Could not find JPEG dimensions: {path}")


def _read_image_header(path: Path) -> tuple[str, int, int]:
    with path.open("rb") as handle:
        signature = handle.read(12)
        handle.seek(0)
        if signature.startswith(b"\x89PNG\r\n\x1a\n"):
            width, height = _png_size(handle, path)
            return "png", width, height
        if signature.startswith(b"\xff\xd8"):
            width, height = _jpeg_size(handle, path)
            return "jpeg", width, height
    raise ManifestError(f"Unsupported or invalid image content: {path}")


def _verify_image_decode(path: Path, expected_width: int, expected_height: int) -> None:
    if fitz is None:
        raise ManifestError(
            "PyMuPDF is required for image decode verification. "
            "Install project dependencies or pass verify_decode=False."
        )
    try:
        pixmap = fitz.Pixmap(str(path))
    except Exception as exc:
        raise ManifestError(f"Image decode failed for {path}: {exc}") from exc
    try:
        actual = (pixmap.width, pixmap.height)
        expected = (expected_width, expected_height)
        if actual != expected:
            raise ManifestError(
                f"Decoded dimensions differ from header for {path}: "
                f"decoded={actual[0]}x{actual[1]}, header={expected[0]}x{expected[1]}"
            )
    finally:
        pixmap = None


def _sampling_group(record: dict[str, Any]) -> str | None:
    subset = record["subset"]
    if subset in {"equation_hard", "table_hard", "layout_hard"}:
        return subset
    if "fuzzy_scan" in record["special_issues"]:
        return "fuzzy_scan"
    if record["data_source"] == "exam_paper":
        return "exam_paper"
    if record["data_source"] == "note":
        return "note"
    return None


def _purpose_for_source(data_source: str) -> str:
    if data_source == "exam_paper":
        return "problems"
    if data_source == "note":
        return "submissions"
    return "reference"


def _annotation_block_stats(
    page: dict[str, Any],
    *,
    relative_text: str,
) -> tuple[int, int, int]:
    layout_dets = page.get("layout_dets")
    if not isinstance(layout_dets, list):
        raise ManifestError(f"layout_dets must be a list for {relative_text}")

    active_count = 0
    content_count = 0
    for block_index, block in enumerate(layout_dets):
        if not isinstance(block, dict):
            raise ManifestError(
                f"layout_dets[{block_index}] must be an object for {relative_text}"
            )
        if block.get("ignore") is True:
            continue

        category = str(block.get("category_type", "")).strip()
        if not category:
            raise ManifestError(
                f"layout_dets[{block_index}] has no category_type for {relative_text}"
            )
        active_count += 1
        if category not in NON_CONTENT_CATEGORIES:
            content_count += 1

    return len(layout_dets), active_count, content_count


def _load_records(
    dataset_root: Path,
    annotation_path: Path,
    *,
    max_image_bytes: int,
    verify_decode: bool,
) -> tuple[list[dict[str, Any]], str]:
    if not annotation_path.is_file():
        raise ManifestError(f"Annotation file not found: {annotation_path}")

    try:
        payload = json.loads(annotation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"Could not read annotation JSON {annotation_path}: {exc}") from exc
    if not isinstance(payload, list):
        raise ManifestError("OmniDocBench annotation root must be a JSON list")

    image_root = dataset_root / "images"
    if not image_root.is_dir():
        raise ManifestError(f"Image directory not found: {image_root}")

    records: list[dict[str, Any]] = []
    annotated_paths: set[str] = set()
    sample_ids: set[str] = set()

    for annotation_index, page in enumerate(payload):
        if not isinstance(page, dict):
            raise ManifestError(f"Annotation entry {annotation_index} must be an object")
        page_info = page.get("page_info")
        if not isinstance(page_info, dict):
            raise ManifestError(f"Annotation entry {annotation_index} has no page_info object")
        attributes = page_info.get("page_attribute")
        if not isinstance(attributes, dict):
            raise ManifestError(
                f"Annotation entry {annotation_index} has no page_attribute object"
            )

        relative = _safe_image_relative_path(page_info.get("image_path"))
        relative_text = relative.as_posix()
        if relative_text in annotated_paths:
            raise ManifestError(f"Duplicate annotated image path: {relative_text}")
        annotated_paths.add(relative_text)

        image_path = dataset_root / Path(*relative.parts)
        if not image_path.is_file():
            raise ManifestError(f"Annotated image is missing: {relative_text}")
        if image_path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise ManifestError(f"Unsupported image suffix for {relative_text}")

        image_format, width, height = _read_image_header(image_path)
        expected_suffixes = {".png"} if image_format == "png" else {".jpg", ".jpeg"}
        extension_content_mismatch = image_path.suffix.lower() not in expected_suffixes
        if verify_decode:
            _verify_image_decode(image_path, width, height)

        try:
            annotated_width = int(page_info["width"])
            annotated_height = int(page_info["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError(
                f"Invalid annotated dimensions for {relative_text}"
            ) from exc
        if (width, height) != (annotated_width, annotated_height):
            raise ManifestError(
                f"Annotation dimensions differ from image for {relative_text}: "
                f"annotation={annotated_width}x{annotated_height}, "
                f"image={width}x{height}"
            )

        data_source = str(attributes.get("data_source", "")).strip()
        language = str(attributes.get("language", "")).strip()
        layout = str(attributes.get("layout", "")).strip()
        subset = str(attributes.get("subset", "")).strip()
        if not all((data_source, language, layout, subset)):
            raise ManifestError(f"Incomplete page attributes for {relative_text}")

        sample_id = relative.with_suffix("").relative_to("images").as_posix().replace("/", "__")
        if sample_id in sample_ids:
            raise ManifestError(f"Duplicate sample_id generated for {relative_text}: {sample_id}")
        sample_ids.add(sample_id)

        image_bytes = image_path.stat().st_size
        (
            annotation_block_count,
            active_annotation_block_count,
            content_annotation_block_count,
        ) = _annotation_block_stats(page, relative_text=relative_text)
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "sample_id": sample_id,
            "annotation_index": annotation_index,
            "page_no": page_info.get("page_no"),
            "image_path": relative_text,
            "image_sha256": _sha256_file(image_path),
            "image_bytes": image_bytes,
            "image_width": width,
            "image_height": height,
            "image_format": image_format,
            "image_suffix": image_path.suffix.lower(),
            "extension_content_mismatch": extension_content_mismatch,
            "over_product_limit": image_bytes > max_image_bytes,
            "annotation_block_count": annotation_block_count,
            "active_annotation_block_count": active_annotation_block_count,
            "content_annotation_block_count": content_annotation_block_count,
            "selection_eligible": content_annotation_block_count > 0,
            "data_source": data_source,
            "language": language,
            "layout": layout,
            "special_issues": _normalise_special_issues(attributes.get("special_issue")),
            "subset": subset,
            "purpose": _purpose_for_source(data_source),
            "sampling_group": None,
            "splits": [],
        }
        record["sampling_group"] = _sampling_group(record)
        records.append(record)

    actual_paths = {
        path.relative_to(dataset_root).as_posix()
        for path in image_root.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    }
    missing = sorted(annotated_paths - actual_paths)
    extra = sorted(actual_paths - annotated_paths)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing={len(missing)} ({missing[:3]})")
        if extra:
            details.append(f"extra={len(extra)} ({extra[:3]})")
        raise ManifestError("Annotation/image mismatch: " + ", ".join(details))

    records.sort(key=lambda item: item["image_path"])
    return records, _sha256_file(annotation_path)


def _balanced_select(
    candidates: Sequence[dict[str, Any]],
    target: int,
    *,
    seed: int,
    label: str,
    preselected: Sequence[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    selected = list(preselected)
    selected_ids = {record["sample_id"] for record in selected}
    if len(selected_ids) != len(selected):
        raise ManifestError(f"Duplicate preselected sample in {label}")
    if len(selected) > target:
        raise ManifestError(
            f"Preselected count for {label} exceeds target: {len(selected)} > {target}"
        )

    remaining = [record for record in candidates if record["sample_id"] not in selected_ids]
    if len(selected) + len(remaining) < target:
        raise ManifestError(
            f"Not enough candidates for {label}: "
            f"need {target}, have {len(selected) + len(remaining)}"
        )

    by_language: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in remaining:
        by_language[record["language"]].append(record)

    for language, rows in by_language.items():
        rows.sort(key=lambda item: item["sample_id"])
        random.Random(_stable_seed(seed, label, language)).shuffle(rows)

    language_counts = Counter(record["language"] for record in selected)
    language_tie_order = {
        language: rank
        for rank, language in enumerate(
            sorted(
                by_language,
                key=lambda item: _stable_seed(seed, label, "language-order", item),
            )
        )
    }

    while len(selected) < target:
        available_languages = [
            language for language, rows in by_language.items() if rows
        ]
        if not available_languages:  # defensive; candidate count was checked above
            raise ManifestError(f"Candidate pool unexpectedly exhausted for {label}")
        language = min(
            available_languages,
            key=lambda item: (language_counts[item], language_tie_order[item]),
        )
        record = by_language[language].pop()
        selected.append(record)
        language_counts[language] += 1

    return selected


def _select_by_quotas(
    records: Sequence[dict[str, Any]],
    quotas: dict[str, int],
    *,
    seed: int,
    split_name: str,
    preselected: Sequence[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    preselected_by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in preselected:
        group = record["sampling_group"]
        if group is None:
            raise ManifestError(f"Preselected sample has no sampling group: {record['sample_id']}")
        preselected_by_group[group].append(record)

    for group, target in quotas.items():
        candidates = [
            record
            for record in records
            if record["sampling_group"] == group and record["selection_eligible"]
        ]
        chosen = _balanced_select(
            candidates,
            target,
            seed=seed,
            label=f"{split_name}:{group}",
            preselected=preselected_by_group.get(group, ()),
        )
        selected.extend(chosen)

    expected_count = sum(quotas.values())
    selected_ids = {record["sample_id"] for record in selected}
    if len(selected) != expected_count or len(selected_ids) != expected_count:
        raise ManifestError(
            f"{split_name} selection is not unique: "
            f"rows={len(selected)}, unique={len(selected_ids)}, expected={expected_count}"
        )
    return selected


def _ensure_oversized_smoke_sample(
    records: Sequence[dict[str, Any]],
    smoke: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if any(record["over_product_limit"] for record in smoke):
        return smoke

    eligible_groups = {record["sampling_group"] for record in smoke}
    oversized = sorted(
        (
            record
            for record in records
            if record["selection_eligible"]
            and record["over_product_limit"]
            and record["sampling_group"] in eligible_groups
        ),
        key=lambda item: item["sample_id"],
    )
    if not oversized:
        return smoke

    replacement = oversized[0]
    same_group = [
        record
        for record in smoke
        if record["sampling_group"] == replacement["sampling_group"]
        and not record["over_product_limit"]
    ]
    same_language = [
        record for record in same_group if record["language"] == replacement["language"]
    ]
    victims = same_language or same_group
    if not victims:
        return smoke

    victim = sorted(victims, key=lambda item: item["sample_id"])[-1]
    return [
        replacement if record["sample_id"] == victim["sample_id"] else record
        for record in smoke
    ]


def _apply_splits(records: list[dict[str, Any]], *, seed: int) -> tuple[list, list]:
    smoke = _select_by_quotas(
        records,
        SMOKE_QUOTAS,
        seed=seed,
        split_name="smoke",
    )
    smoke = _ensure_oversized_smoke_sample(records, smoke)
    pilot = _select_by_quotas(
        records,
        PILOT_QUOTAS,
        seed=seed,
        split_name="pilot",
        preselected=smoke,
    )

    smoke_ids = {record["sample_id"] for record in smoke}
    pilot_ids = {record["sample_id"] for record in pilot}
    if not smoke_ids <= pilot_ids:
        raise ManifestError("Smoke split must be a subset of pilot split")

    for record in records:
        splits = []
        if record["sample_id"] in smoke_ids:
            splits.append("smoke")
        if record["sample_id"] in pilot_ids:
            splits.append("pilot")
        record["splits"] = splits

    smoke.sort(key=lambda item: item["image_path"])
    pilot.sort(key=lambda item: item["image_path"])
    return smoke, pilot


def _counter(records: Iterable[dict[str, Any]], field: str) -> dict[str, int]:
    return dict(sorted(Counter(str(record[field]) for record in records).items()))


def _split_summary(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(records),
        "sampling_groups": _counter(records, "sampling_group"),
        "data_sources": _counter(records, "data_source"),
        "languages": _counter(records, "language"),
        "layouts": _counter(records, "layout"),
        "subsets": _counter(records, "subset"),
        "purposes": _counter(records, "purpose"),
        "over_product_limit": sum(record["over_product_limit"] for record in records),
        "selection_ineligible": sum(
            not record["selection_eligible"] for record in records
        ),
    }


def _build_summary(
    records: Sequence[dict[str, Any]],
    smoke: Sequence[dict[str, Any]],
    pilot: Sequence[dict[str, Any]],
    *,
    annotation_sha256: str,
    seed: int,
    max_image_bytes: int,
    verify_decode: bool,
) -> dict[str, Any]:
    special_issue_counts = Counter(
        issue for record in records for issue in record["special_issues"]
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": "opendatalab/OmniDocBench",
        "annotation_file": "OmniDocBench.json",
        "annotation_sha256": annotation_sha256,
        "seed": seed,
        "max_image_bytes": max_image_bytes,
        "decode_verified": verify_decode,
        "files": {
            "all": "manifest.jsonl",
            "smoke": "smoke.jsonl",
            "pilot": "pilot.jsonl",
        },
        "quotas": {
            "smoke": SMOKE_QUOTAS,
            "pilot": PILOT_QUOTAS,
        },
        "all": {
            "count": len(records),
            "image_formats": _counter(records, "image_format"),
            "image_suffixes": _counter(records, "image_suffix"),
            "data_sources": _counter(records, "data_source"),
            "languages": _counter(records, "language"),
            "layouts": _counter(records, "layout"),
            "subsets": _counter(records, "subset"),
            "purposes": _counter(records, "purpose"),
            "special_issues": dict(sorted(special_issue_counts.items())),
            "over_product_limit": sum(
                record["over_product_limit"] for record in records
            ),
            "extension_content_mismatch": sum(
                record["extension_content_mismatch"] for record in records
            ),
            "selection_ineligible": sum(
                not record["selection_eligible"] for record in records
            ),
            "total_image_bytes": sum(record["image_bytes"] for record in records),
        },
        "smoke": _split_summary(smoke),
        "pilot": _split_summary(pilot),
        "invariants": {
            "unique_sample_ids": len({record["sample_id"] for record in records})
            == len(records),
            "smoke_is_subset_of_pilot": {
                record["sample_id"] for record in smoke
            }
            <= {record["sample_id"] for record in pilot},
            "smoke_contains_oversized": any(
                record["over_product_limit"] for record in smoke
            ),
            "selected_pages_have_content": all(
                record["selection_eligible"] for record in pilot
            ),
        },
    }


def _write_jsonl(path: Path, records: Sequence[dict[str, Any]]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            handle.write("\n")
    temporary.replace(path)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def generate_manifests(
    dataset_root: str | Path,
    output_dir: str | Path,
    *,
    annotation_file: str | Path | None = None,
    seed: int = DEFAULT_SEED,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    verify_decode: bool = True,
) -> dict[str, Any]:
    """Validate the dataset, generate all manifest files, and return the summary."""
    root = Path(dataset_root).resolve()
    output = Path(output_dir).resolve()
    annotation = (
        Path(annotation_file).resolve()
        if annotation_file is not None
        else root / "OmniDocBench.json"
    )
    if max_image_bytes <= 0:
        raise ManifestError("max_image_bytes must be positive")

    records, annotation_sha256 = _load_records(
        root,
        annotation,
        max_image_bytes=max_image_bytes,
        verify_decode=verify_decode,
    )
    smoke, pilot = _apply_splits(records, seed=seed)
    summary = _build_summary(
        records,
        smoke,
        pilot,
        annotation_sha256=annotation_sha256,
        seed=seed,
        max_image_bytes=max_image_bytes,
        verify_decode=verify_decode,
    )

    output.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output / "manifest.jsonl", records)
    _write_jsonl(output / "smoke.jsonl", smoke)
    _write_jsonl(output / "pilot.jsonl", pilot)
    _write_json(output / "summary.json", summary)
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate OmniDocBench and build deterministic OCR manifests."
    )
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=Path("data/benchmarks/OmniDocBench"),
        help="Directory containing OmniDocBench.json and images/.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/ocr_benchmark/omnidocbench_manifest"),
        help="Directory for manifest.jsonl, smoke.jsonl, pilot.jsonl, and summary.json.",
    )
    parser.add_argument(
        "--annotation-file",
        type=Path,
        help="Optional annotation JSON path; defaults to DATASET_ROOT/OmniDocBench.json.",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--max-image-bytes",
        type=int,
        default=DEFAULT_MAX_IMAGE_BYTES,
        help="Current raw product input limit used to flag oversized images.",
    )
    parser.add_argument(
        "--skip-decode-check",
        action="store_true",
        help="Skip full PyMuPDF image decoding; header and dimension checks still run.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        summary = generate_manifests(
            args.dataset_root,
            args.output_dir,
            annotation_file=args.annotation_file,
            seed=args.seed,
            max_image_bytes=args.max_image_bytes,
            verify_decode=not args.skip_decode_check,
        )
    except ManifestError as exc:
        parser.error(str(exc))

    print(
        json.dumps(
            {
                "output_dir": str(args.output_dir),
                "all": summary["all"]["count"],
                "smoke": summary["smoke"]["count"],
                "pilot": summary["pilot"]["count"],
                "annotation_sha256": summary["annotation_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
