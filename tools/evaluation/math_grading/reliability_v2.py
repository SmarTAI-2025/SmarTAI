"""B2 wire-only translations; the immutable B1 corpus and oracles stay intact.

These functions receive model replies, never case oracles or observed scores.
They do not add checks, change amounts/awards, repair adversarial quotes, or
convert an unknown tool result into a mathematical judgment.
"""
from copy import deepcopy

CANDIDATE = "e69aec05aa33351ffb248526919d6996abc2bf6a"


def plan_v2(payload):
    result = deepcopy(payload)
    for route in result["routes"]:
        for item in route.get("items", []):
            # B1 description was the literal source. Preserve explicit R41
            # source_quote, including its different display description.
            item.setdefault("source_quote", item["description"])
            if route["mode"] == "deductions" and item["description"].startswith("每"):
                item.setdefault("max_occurrences", None)
        if result["effective_criterion"] == "每个错误扣1分；同源后续错误不重复扣分。":
            route.update(error_policy="no_repeat_deduction",
                         error_policy_source_quote="同源后续错误不重复扣分")
    return result


def assessment_v2(payload):
    if not isinstance(payload, dict):
        return payload  # Intentionally malformed JSON remains malformed.
    result = deepcopy(payload)
    claims = result.get("claims", [])
    # A declared first error already has an authored deduction and reason in
    # B1. v2 requires its verdict explicitly when used as an error ancestor.
    origins = {d["caused_by"] for d in result.get("deductions", []) if d.get("caused_by")}
    errors = {d["claim_id"] for d in result.get("deductions", []) if not d.get("caused_by")}
    for claim in claims:
        if claim["id"] in origins & errors and claim.get("verdict") == "unknown":
            claim["verdict"] = "refuted"
    for index, missing in enumerate(result.pop("missing_obligations", [])):
        id = f"omission_{index + 1}"
        claims.append(dict(id=id, kind="omission", quote="", source_quote=missing["source_quote"],
            item_ids=[missing["item_id"]], verdict="refuted",
            reason="该原始评分义务在已检查作答中缺失。"))
        for award in result.get("awards", []):
            if award["item_id"] == missing["item_id"] and not award["claim_ids"]:
                award["claim_ids"] = [id]
    return result


def wire_v2(wire):
    result = deepcopy(wire)
    result["plan"] = plan_v2(result["plan"])
    for key in ("assessment", "feedback", "source_repair", "repair"):
        if key in result:
            result[key] = assessment_v2(result[key])
    if "experts" in result:
        result["experts"] = [assessment_v2(p) for p in result["experts"]]
    return result


def feedback_v2(payload, evidence):
    """Acknowledge actual IDs without guessing claim-wide applicability.

    An already authored matching verdict may apply. Otherwise the synthetic
    reply explicitly remains uncertain and should preserve nonblocking advice.
    No score/verdict is inferred from the verifier or the acceptance oracle.
    """
    if not isinstance(payload, dict):
        return payload
    result = deepcopy(payload)
    claims = {c["id"]: c for c in result.get("claims", [])}
    result["evidence_judgments"] = [dict(evidence_id=e["evidence_id"],
        applicability="applies" if claims.get(e["claim_id"], {}).get("verdict") == e["status"] else "uncertain",
        reason="与原先明确判词一致。" if claims.get(e["claim_id"], {}).get("verdict") == e["status"]
               else "本合成回复未确认工具结果覆盖整个论断，保留具体核对建议。")
        for e in evidence if e.get("status") in {"supported", "refuted"}]
    return result
