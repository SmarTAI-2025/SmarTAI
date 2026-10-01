"""Offline paired evaluation of saved OCR outputs. Never dispatches a provider.

Input is a JSON object with a `samples` list. Each sample pins source_sha256,
purpose, split, ground_truth, critical_literals and outputs E0/E1/E2/E3/E4.
Each output contains text, source_sha256, route_fingerprint, model,
initial_candidate_sha256, calls, input_tokens, output_tokens and duration_ms.
E2 must reuse E1's initial candidate. Missing evidence is unverified, not zero.
Do not commit source/student text or credentials with public reports.
"""
from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import statistics

MAX_INPUT_BYTES = 16 * 1024 * 1024
ARMS = ("E0", "E1", "E2", "E3", "E4")


def text_metrics(reference, text, critical_literals):
    # Standard-library edit alignment is intentionally labelled, not called
    # OmniDocBench CER/formula equivalence or semantic correctness.
    matcher = SequenceMatcher(None, reference, text, autojunk=False)
    edits = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal")
    return dict(alignment_error_ratio=edits / max(1, len(reference)), exact_match=text == reference,
                missing_critical_literals=[value for value in critical_literals if value not in text])


def evaluate(manifest):
    samples = manifest.get("samples")
    if not isinstance(samples, list) or len(samples) > 1000:
        raise ValueError("invalid sample manifest")
    results, totals = [], {arm: [] for arm in ARMS}
    seen = set()
    for sample in samples:
        key = sample["id"]
        if key in seen or sample.get("purpose") not in {"problems", "submissions", "reference", "rubric", "test_cases", "knowledge"}:
            raise ValueError("duplicate sample or invalid purpose")
        seen.add(key)
        if sample.get("split") not in {"diagnostic", "holdout"}:
            raise ValueError("split must be explicit")
        source = sample["source_sha256"]
        if len(source) != 64 or any(c not in "0123456789abcdef" for c in source):
            raise ValueError("source hash required")
        truth = sample.get("ground_truth")
        literals = sample.get("critical_literals", [])
        if truth is not None and (not isinstance(truth, str) or len(truth) > 200_000):
            raise ValueError("invalid ground truth")
        if any(not isinstance(value, str) or not value or truth is None or value not in truth for value in literals):
            raise ValueError("critical literals must come from the ground truth")
        outputs = sample.get("outputs", {})
        paired = {}
        for arm in ARMS:
            output = outputs.get(arm)
            if output is None:
                paired[arm] = dict(status="unverified")
                continue
            text = output.get("text")
            if not isinstance(text, str) or len(text) > 200_000 or output.get("source_sha256") != source:
                raise ValueError("output source/text mismatch")
            if not output.get("route_fingerprint") or not output.get("model"):
                raise ValueError("output route/model required")
            for field in ("calls", "input_tokens", "output_tokens", "duration_ms"):
                value = output.get(field)
                if value is not None and (type(value) not in {int, float} or not 0 <= value < 1e12):
                    raise ValueError("invalid usage")
            result = dict(status="measured" if truth is not None and sample.get("ground_truth_reviewed") is True else "unverified_ground_truth",
                text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                **{field: output.get(field) for field in ("calls", "input_tokens", "output_tokens", "duration_ms")})
            if result["status"] == "measured":
                result.update(text_metrics(truth, text, literals))
                # Report counts only, so a shareable report carries no source text.
                result["missing_critical_count"] = len(result.pop("missing_critical_literals"))
                totals[arm].append((sample["purpose"], sample["split"], result))
            paired[arm] = result
        for left, right in (("E0", "E1"), ("E1", "E2"), ("E3", "E4")):
            if left not in outputs or right not in outputs:
                continue
            if any(outputs[left].get(field) != outputs[right].get(field) for field in ("route_fingerprint", "model")):
                raise ValueError("paired ablation must use the same frozen route and model")
            if right in {"E2", "E4"} and (not outputs[left].get("initial_candidate_sha256")
                    or outputs[left]["initial_candidate_sha256"] != outputs[right].get("initial_candidate_sha256")):
                raise ValueError("repair comparison must reuse its initial candidate")
        delta = None
        if all(paired[arm]["status"] == "measured" for arm in ("E1", "E2")):
            before, after = paired["E1"], paired["E2"]
            delta = dict(repair_alignment_delta=after["alignment_error_ratio"] - before["alignment_error_ratio"],
                         newly_missing_critical=sum(value in outputs["E1"]["text"] and value not in outputs["E2"]["text"] for value in literals),
                         recovered_critical=sum(value not in outputs["E1"]["text"] and value in outputs["E2"]["text"] for value in literals))
        results.append(dict(id=key, purpose=sample["purpose"], split=sample["split"], source_sha256=source, arms=paired, repair_delta=delta))
    groups = []
    for arm, rows in totals.items():
        for purpose, split in sorted({(purpose, split) for purpose, split, _ in rows}):
            subset = [row for p, s, row in rows if (p, s) == (purpose, split)]
            durations = sorted(row["duration_ms"] for row in subset if row["duration_ms"] is not None)
            groups.append(dict(arm=arm, purpose=purpose, split=split, samples=len(subset),
                exact_match_rate=sum(row["exact_match"] for row in subset) / len(subset),
                missing_critical_count=sum(row["missing_critical_count"] for row in subset),
                duration_p50_ms=statistics.median(durations) if durations else None,
                duration_p95_ms=durations[min(len(durations) - 1, int(len(durations) * .95))] if durations else None,
                usage_complete=all(row["input_tokens"] is not None and row["output_tokens"] is not None for row in subset)))
    return dict(schema_version=1, external_calls=0, quality_acceptance="requires_source_review_and_representative_holdout",
                samples=results, groups=groups)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    if args.manifest.stat().st_size > MAX_INPUT_BYTES:
        parser.error("manifest exceeds offline resource limit")
    report = evaluate(json.loads(args.manifest.read_text(encoding="utf-8")))
    print(json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
