"""Optional proof notes, isolated from the score parser and its repair budget.

These notes check bookkeeping and explicit scoring concerns, not mathematical
truth. An absent, truncated or invalid appendix never invalidates a grade.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.services.math_source_evidence import source_catalog
from backend.tools.structured_llm import _extract_balanced_json

MARKER = "PROOF_AUDIT"
OUTPUT_INSTRUCTION = (
    "Return the complete scoring JSON object first, then append PROOF_AUDIT on a new line "
    "followed by a separate JSON object with the concise proof notes requested in the task. "
    "The appendix is diagnostic: never omit the scoring JSON or put the appendix inside it."
)
REVIEW_INSTRUCTION = (
    "Resolve the supplied proof concerns against the unchanged original rubric and work. "
    "Missing audit coverage is not a missing student proof. Do not change a score for audit "
    "format/coverage failures or a rubric-exempt flaw. Keep true conclusions distinct from "
    "valid derivations. Use the same optional PROOF_AUDIT appendix to mark each reviewed "
    "claim's score_effect as accounted or none with a concrete explanation; unresolved "
    "means a specific remaining scoring concern. Do not start another review."
)


class _Premise(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    condition: str = Field(min_length=1, max_length=600)
    status: Literal["established", "missing", "uncertain"]
    source_ids: list[str] = Field(default_factory=list, max_length=12)
    claim_ids: list[str] = Field(default_factory=list, max_length=12)
    basis: str = Field(default="", max_length=600)


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    claim_id: str
    conclusion: str = Field(min_length=1, max_length=600)
    rule: str = Field(min_length=1, max_length=600)
    premises: list[_Premise] = Field(default_factory=list, max_length=12)
    inference: Literal["justified", "gap", "invalid", "uncertain"]
    score_effect: Literal["none", "accounted", "unresolved"] = "none"
    rubric_source_ids: list[str] = Field(default_factory=list, max_length=12)
    concern: str = Field(default="", max_length=1000)


def read_appendix(content: str | None, *, expert_index: int | None = None):
    """Only read after the first complete grading JSON; never invoke a model."""
    if not isinstance(content, str):
        return None
    first = _extract_balanced_json(content)
    if first is None:
        return None
    tail = content[content.find(first) + len(first):]
    if MARKER not in tail:
        return None
    raw = tail.split(MARKER, 1)[1].strip()
    if len(raw) > 60000:
        return False
    try:
        parsed = json.loads(_extract_balanced_json(raw) or "null")
        if expert_index is not None:
            parsed = parsed.get(str(expert_index)) if isinstance(parsed, dict) else False
        return parsed
    except (ValueError, RecursionError):
        return False


def inspect_audit(raw, assessment, answer: str, problem: str, plan, *, previous=None) -> dict:
    """Keep valid notes, flag unrecorded source lines without judging the student.

Only a concrete, source-bound, rubric-linked unresolved scoring concern can
request review. Existing concerns survive a broken/missing review appendix.
    """
    catalog = source_catalog(answer, problem, plan.original_criterion)
    student_ids = {key for key in catalog if key.startswith("student:")}
    claims = {claim.id: claim for claim in assessment.claims}
    result = {"status": "missing" if raw is None else "invalid", "steps": [],
              "unmapped_source_ids": sorted(student_ids), "invalid_records": 0,
              "review_concerns": []}
    covered, resolved = set(), set()
    if isinstance(raw, dict) and isinstance(raw.get("steps"), list) and len(raw["steps"]) <= 80:
        result["status"] = "recorded"
        counts = Counter(data.get("claim_id") for data in raw["steps"]
                         if isinstance(data, dict) and isinstance(data.get("claim_id"), str))
        context = raw.get("context_source_ids", [])
        if isinstance(context, list) and len(context) <= 240:
            covered.update(key for key in context if isinstance(key, str) and key in student_ids)
        for data in raw["steps"]:
            try:
                step = _Step.model_validate(data)
                claim = claims.get(step.claim_id)
                if claim is None or counts[step.claim_id] != 1:
                    raise ValueError("unknown_or_duplicate_claim")
                ancestors, pending = set(), list(claim.depends_on)
                while pending:
                    key = pending.pop()
                    if key not in ancestors:
                        ancestors.add(key)
                        pending.extend(claims[key].depends_on)
                if any(key not in catalog or not key.startswith("rubric:")
                       for key in step.rubric_source_ids):
                    raise ValueError("unknown_rubric_source")
                for premise in step.premises:
                    if (not set(premise.source_ids) <= catalog.keys()
                            or not set(premise.claim_ids) <= ancestors):
                        raise ValueError("unknown_premise_source")
                    if (premise.status == "established" and not premise.source_ids
                            and not premise.claim_ids and not premise.basis.strip()):
                        raise ValueError("unrecorded_premise_basis")
            except (ValidationError, ValueError, TypeError):
                result["invalid_records"] += 1
                continue
            result["steps"].append(step.model_dump(mode="json"))
            if claim.kind == "statement" and claim.source_span:
                left, right = claim.source_span
                covered.update(key for key in student_ids
                               if left <= catalog[key][1] and catalog[key][2] <= right)
            missing = any(p.status == "missing" for p in step.premises)
            contradiction = step.inference == "justified" and missing
            # 'Accounted' may mean a deduction or an explicit teacher waiver.
            # A true conclusion and an invalid derivation are not contradictory.
            if (step.score_effect == "unresolved" and step.concern.strip()
                    and (step.inference != "justified" or contradiction)
                    and (step.rubric_source_ids or claim.item_ids)):
                result["review_concerns"].append({"claim_id": step.claim_id,
                    "reason": step.concern, "conclusion": step.conclusion,
                    "rubric_source_ids": step.rubric_source_ids,
                    "item_ids": claim.item_ids, "contradiction": contradiction})
            elif step.score_effect in {"none", "accounted"} and step.concern.strip() and not contradiction:
                resolved.add(step.claim_id)
        result["unmapped_source_ids"] = sorted(student_ids - covered)
        if result["invalid_records"] or result["unmapped_source_ids"]:
            result["status"] = "partial"
    current = {entry["claim_id"] for entry in result["review_concerns"]}
    for entry in (previous or {}).get("review_concerns", []):
        if entry["claim_id"] not in resolved | current:
            result["review_concerns"].append(entry)
    return result
