"""Metrics with explicit denominators. Missing instrumentation is never a pass."""
from __future__ import annotations

import math
from collections import defaultdict


def rate(values):
    known = [value for value in values if value is not None]
    return {"value": sum(known) / len(known) if known else None,
            "observed": len(known), "total": len(values)}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def assess(case, result):
    value = result.get("score")
    valid = finite(value) and 0 <= value <= case["problem"]["max_score"]
    interval = case["expected"]["score_interval"]
    error = None
    over = under = hit = None
    if interval is not None and valid:
        lo, hi = interval
        error = max(lo - value, 0, value - hi)
        over, under, hit = value > hi, value < lo, lo <= value <= hi
    checks = {
        "score_available_as_expected": valid == case["expected"]["score_required"],
        "synthetic_interval_hit": hit,
        "over_credit": over, "under_credit": under, "interval_distance": error,
        "no_score": not valid,
        "advisory": result.get("advisory"),
        "required_handling": result.get("required_handling"),
        "forced_review_wording": result.get("forced_review_wording"),
        "no_unwanted_forced_handling": (
            None if result.get("required_handling") is None
            else case["expected"]["forced_review_allowed"] or not result["required_handling"]),
        "step_sum_consistent": (
            abs(sum(s["score"] for s in result["steps"]) - value) <= 1e-8
            if valid and result.get("steps") else None),
        # Adapter must pass explicit internal evidence; comments are not evidence.
        "quote_alignment": None, "duplicate_deduction_free": None,
        "unsupported_verified_claim_free": None,
        "plan_identity_present": bool(result.get("plan_id")) if "plan_id" in result else None,
    }
    claims = result.get("claims")
    if claims:
        checks["quote_alignment"] = all(
            isinstance(c.get("quote"), str) and bool(c["quote"]) and c["quote"] in case["answer"]
            for c in claims)
    deductions = result.get("deductions")
    if deductions is not None:
        ids = [d.get("error_id") for d in deductions]
        checks["duplicate_deduction_free"] = (
            all(ids) and all(d.get("rule_id") for d in deductions) and len(ids) == len(set(ids)))
    verifications = result.get("verification_claims")
    if verifications is not None:
        checks["unsupported_verified_claim_free"] = all(
            not v.get("claimed_verified") or v.get("evidence_status") == "supported"
            for v in verifications)
    return checks


def summarize(cases, results):
    by_id = {r["id"]: r for r in results}
    expected_ids = {c["id"] for c in cases}
    if len(by_id) != len(results) or set(by_id) != expected_ids:
        raise ValueError("result IDs must match the corpus exactly, without duplicates")
    rows = []
    for case in cases:
        result = by_id[case["id"]]
        rows.append({"id": case["id"], "category": case["category"],
                     "subject": case["subject"], "checks": assess(case, result)})
    groups = defaultdict(list)
    for row in rows:
        groups["all"].append(row)
        groups[f"category:{row['category']}"].append(row)
        groups[f"subject:{row['subject']}"].append(row)
    aggregates = {
        key: {metric: rate([r["checks"][metric] for r in group])
              for metric in rows[0]["checks"]}
        for key, group in sorted(groups.items())
    }
    consistency = []
    for group in sorted({c["shared_group"] for c in cases if c.get("shared_group")}):
        ids = [by_id[c["id"]].get("plan_id") for c in cases if c.get("shared_group") == group]
        consistency.append({"group": group, "consistent": len(set(ids)) == 1 if all(ids) else None})
    return {
        "label_kind": "synthetic_explicit_rule",
        "real_teacher_interval_hit": {"value": None, "observed": 0, "total": 0},
        "aggregates": aggregates, "cases": rows, "shared_rule_consistency": consistency,
        "duration_ms": rate([r.get("duration_ms") for r in results]),
        "provider_entry_calls": sum(r.get("provider_entry_calls", 0) for r in results),
        "fixed_response_calls": sum(r.get("fixed_response_calls", 0) for r in results),
        "paid_cost": None,
    }


def compare(before, after):
    if before["corpus_sha256"] != after["corpus_sha256"]:
        raise ValueError("cannot compare different corpus versions")
    old, new = ({r["id"]: r for r in report["results"]} for report in (before, after))
    if old.keys() != new.keys():
        raise ValueError("cannot compare different case sets")
    return [
        {"id": id, "before_score": old[id].get("score"), "after_score": new[id].get("score"),
         "delta": new[id]["score"] - old[id]["score"]
         if finite(old[id].get("score")) and finite(new[id].get("score")) else None,
         "before_required_handling": old[id].get("required_handling"),
         "after_required_handling": new[id].get("required_handling")}
        for id in old
    ]
