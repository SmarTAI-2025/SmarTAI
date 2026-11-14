"""Keep independently parsed scoring fields for review, never as executable rules."""
from typing import Annotated

from pydantic import TypeAdapter, ValidationError

from backend.services.part_rubric import PartRubric, RubricPart, PointStep, Penalty


def review_extraction(proposal, problem):
    """Preserve valid field values even when a sibling or cross-field rule fails.

    These are pending extraction results, not an approved ledger. Each value
    passes its existing field schema; full source/budget/semantic validation is
    still required before any result may enter rubric_draft or a grading run.
    Unknown provider keys are never persisted.
    """
    from backend.services.rubric_drafts import draft_fingerprint

    raw = proposal.get("part_rubric") if isinstance(proposal, dict) else None
    if not isinstance(raw, dict):
        return None
    fields, invalid = [], []
    children = {(PartRubric, "parts"): RubricPart, (RubricPart, "steps"): PointStep,
                (RubricPart, "deductions"): Penalty}
    server_fields = {"teacher_rubric", "rubric_origin", "rubric_version", "teacher_gaps",
                     "follow_through_strictness", "origin_overrides", "subpart_id"}

    def collect(model, value, path):
        if not isinstance(value, dict):
            invalid.append(path)
            return
        for name, field in model.model_fields.items():
            if name in server_fields or name not in value:
                continue
            location = f"{path}.{name}"
            nested = children.get((model, name))
            if nested:
                values = value[name]
                if not isinstance(values, list) or len(values) > 100:
                    invalid.append(location)
                    continue
                for index, item in enumerate(values):
                    collect(nested, item, f"{location}[{index}]")
                continue
            adapter = TypeAdapter(Annotated[field.annotation, *field.metadata] if field.metadata else field.annotation)
            try:
                parsed = adapter.validate_python(value[name])
            except (ValidationError, TypeError, ValueError):
                invalid.append(location)
                continue
            fields.append({"path": location, "value": adapter.dump_python(parsed, mode="json")})

    collect(PartRubric, raw, "part_rubric")
    if not fields or len(fields) > 5000:
        return None
    return {"version": 1, "status": "needs_review", "input_fingerprint": draft_fingerprint(problem),
            "fields": fields, "invalid_fields": invalid[:500]}


def review_issue(q_id, extraction):
    """Use the existing red rubric review and progress gate."""
    return {"issue_id": "rubric_review_" + extraction["input_fingerprint"][:24],
            "q_id": q_id, "field": "rubric", "code": "generation_failed", "severity": "blocking",
            "status": "open", "source_tokens": [],
            "details": {"target": "criterion", "reason": "invalid_structured_rubric",
                        "rubric_review_draft": extraction}}
