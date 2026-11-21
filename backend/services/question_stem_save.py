"""Prepare and publish one edited question as a durable workflow operation."""
from functools import cache
from hashlib import sha256
import json

from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from backend.agents.question_preparation_agent import generate_major_question_materials
from backend.db import assignment_repository, workflow_repository
from backend.db.execution_admission import QUESTION_STEM_SAVE_OPERATION
from backend.db.session import session_scope
from backend.domain.errors import DomainError, InvalidTransition, NotFound, ValidationError
from backend.models import is_programming_question_type
from backend.progress.tracker import ProgressReporter
from backend.services import task_facade
from backend.services.background_errors import classify_background_error
from backend.services.math_grading_context import problem_from_question, uses_math_grading
from backend.services.part_rubric import render_parts
from backend.services.question_material_edits import (
    answer_sections, missing_answer_parts, missing_derivation, review_wording, semantic_text_edits, teacher_material,
)
from backend.services.programming_rubric import verified_test_points
from backend.services.question_structure import build_major_question_structure
from backend.services.rubric_drafts import RubricDraft, edit_alternatives, retarget_draft, saved_draft
from backend.services.stage_provider_routing import baidu_ocr_credential_id, resolve_stage_provider_route
from backend.services.workflow_worker import LeasedOperation


def _result(row) -> dict:
    field = row.payload.get("field", "stem")
    result = {
        "id": row.id, "q_id": row.payload["q_id"], "status": row.status,
        "field": field,
        "workflow_revision": (row.terminal_summary or {}).get("workflow_revision"),
        "error_code": row.error_code,
    }
    if row.status != "done":
        result[field] = row.payload[field]
        result["expected_workflow_revision"] = row.payload["expected_workflow_revision"]
    return result


def start(*, task_id: str, owner_id: str, q_id: str, stem: str,
          expected_workflow_revision: int, retry_of: str | None = None) -> dict:
    if not stem.strip():
        raise ValidationError("Enter the question text.", code="question_stem_required")
    return start_field(task_id=task_id, owner_id=owner_id, q_id=q_id, field="stem", value=stem,
                       expected_workflow_revision=expected_workflow_revision, retry_of=retry_of)


def start_field(*, task_id, owner_id, q_id, field, value, expected_workflow_revision, retry_of=None):
    payload = {"q_id": q_id, field: value, "expected_workflow_revision": expected_workflow_revision}
    if field != "stem":
        payload["field"] = field
    if retry_of is not None:
        failed = workflow_repository.get_operation(retry_of, owner_id=owner_id)
        if (failed.assignment_id != task_id or failed.operation_type != QUESTION_STEM_SAVE_OPERATION
                or failed.status != "error" or failed.payload["q_id"] != q_id
                or failed.payload.get("field", "stem") != field or failed.payload.get(field) != value):
            raise NotFound("question_stem_save")
    input_hash = sha256(json.dumps([payload, retry_of], sort_keys=True).encode()).hexdigest()
    row, _ = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id, operation_type=QUESTION_STEM_SAVE_OPERATION,
        input_hash=input_hash, payload=payload, retry_existing=False,
    )
    return _result(row)


def list_latest(*, task_id: str, owner_id: str) -> list[dict]:
    """Return the latest save of each editable field, including legacy stem saves."""
    workflow_repository.get_live_workflow(task_id, owner_id=owner_id)
    with session_scope() as session:
        op = workflow_repository.WorkflowOperationRecord
        latest = select(
            op.id, func.row_number().over(
                partition_by=(op.payload["q_id"].as_string(), func.coalesce(op.payload["field"].as_string(), "stem")),
                order_by=(op.created_at.desc(), op.id.desc()),
            ).label("position"),
        ).where(op.assignment_id == task_id, op.owner_id == owner_id,
                op.operation_type == QUESTION_STEM_SAVE_OPERATION).subquery()
        rows = session.scalars(select(op).join(latest, latest.c.id == op.id)
                               .where(latest.c.position == 1))
        return [_result(row) for row in rows]


def _load_edit(operation: LeasedOperation):
    task_id, owner_id = operation.assignment_id, operation.owner_id
    revision = operation.payload["expected_workflow_revision"]
    workflow = workflow_repository.get_live_workflow(task_id, owner_id=owner_id)
    if workflow.workflow_revision != revision:
        task_facade._raise_stale_revision()
    if workflow.active_job_id or workflow.presentation_status != "problems_ready":
        raise InvalidTransition("The task is busy or no longer editable.", code="workflow_busy")
    question = next((row for row in assignment_repository.list_questions(task_id, teacher_id=owner_id)
                     if row.q_id == operation.payload["q_id"]), None)
    if question is None:
        raise NotFound("question")
    return workflow, question


def _generation_input(question, before, patch, prior, edited):
    stem = patch.get("stem", question.stem)
    presentation = (question.source or {}).get("presentation") or {}
    provenance = dict(presentation.get("ai_completion_provenance") or {})
    # An earlier AI solution and rubric are stale after a stem change. Keep the
    # teacher's source policy, and pass explicit allocations through the normal generator.
    teacher_policy = presentation.get("stem_save_teacher_criterion")
    if teacher_policy is None:
        teacher_policy = teacher_material(before, "criterion")
    if "criterion" in edited:
        teacher_policy = patch["criterion"] or ""
    reference = (patch["reference_answer"] if "reference_answer" in edited
                 else teacher_material(before, "reference_answer")) or ""
    if teacher_policy:
        provenance.pop("criterion", None)
    raw = {
        **before, **patch,
        "stem": stem, "reference_answer": reference, "criterion": teacher_policy,
        "question_structure": build_major_question_structure(
            {"number": question.number, "stem": stem}, major_order=question.order_index,
            structure_source="deterministic", review_status="needs_review",
            previous=presentation.get("question_structure"),
        ).model_dump(mode="json"),
        "ai_completion_provenance": provenance,
        "alternative_rules": presentation.get("alternative_rules") or {},
        "teacher_subpart_points": presentation.get("teacher_subpart_points"),
        "rubric_generation_preferences": presentation.get("rubric_generation_preferences"),
    }
    if presentation.get("alternative_rules"):
        revised = problem_from_question(question).model_copy(update={
            "stem": stem, "criterion": teacher_policy, "reference_answer": reference,
        })
        revised_presentation = {**presentation, "question_structure": raw["question_structure"]}
        revised._math_rubric_source = {"presentation": revised_presentation}
        edit_alternatives(revised, revised_presentation, None, previous=problem_from_question(question),
                          allow_pending=True)
        alternatives = revised_presentation["alternative_rules"]
        raw["alternative_rules"] = alternatives
        raw["source"] = {"presentation": {"alternative_rules": alternatives}}
    if is_programming_question_type(question.type) and prior:
        points = (prior.programming_scoring.test_points if prior.programming_scoring
                  else verified_test_points(prior.plan))
        if points is not None:
            raw["required_programming_test_points"] = points
    return raw, teacher_policy


def provider_for_edit(workflow, owner_id):
    @cache
    def get_provider():
        requested = workflow.question_recognition_provider_id
        provider = resolve_stage_provider_route(
            owner_id=owner_id, registry=task_facade._registry_for_owner(owner_id),
            requested_route_id=None if baidu_ocr_credential_id(requested) else requested,
        ).provider
        if provider is None:
            raise ValidationError("Enable a model to update question materials.", code="no_provider_configured")
        return provider
    return get_provider


def _adopt_rubric(rubric, teacher_policy, prior, source, language):
    criterion = teacher_policy or rubric.text_value
    draft = RubricDraft.model_validate(rubric.rubric_draft) if rubric.rubric_draft else None
    if not draft or not draft.plan.part_rubric:
        if uses_math_grading(source["type"]) or is_programming_question_type(source["type"]):
            raise ValidationError("The revised scoring rules are incomplete.", code="invalid_structured_rubric")
        return criterion, draft
    rules = draft.plan.part_rubric
    if prior and prior.plan.part_rubric:
        rules = rules.model_copy(update={"rubric_version": prior.plan.part_rubric.rubric_version + 1})
    if not teacher_policy and prior and prior.programming_scoring:
        code_parts = tuple(part.model_copy(update={
            "steps": tuple(step for step in part.steps if not step.test_suite),
        }) for part in rules.parts)
        criterion = render_parts(rules.model_copy(update={"parts": code_parts}), language)
    plan = draft.plan.model_copy(update={
        "part_rubric": rules, "original_criterion": criterion, "effective_criterion": criterion,
    })
    return criterion, draft.model_copy(update={"plan": plan})


async def prepare(question, workflow, patch, *, owner_id, reporter):
    """Prepare all affected fields outside the transaction; teacher inputs win."""
    before = task_facade._serialize_problem(question)
    presentation = (question.source or {}).get("presentation") or {}
    for key in ("teacher_material_supplements", "programming_reference_binding", "code_output"):
        if key in presentation:
            before[key] = presentation[key]
    edited = {key for key in patch if before.get(key) != patch[key]}
    if not edited.intersection({"stem", "reference_answer", "solution_code", "test_cases"}):
        return {key: value for key, value in patch.items() if key != "test_case_origins"}, None
    semantic = semantic_text_edits(before, patch)
    manual_missing = {
        field for issue in presentation.get("preparation_issues", [])
        if issue.get("code") == "manual_completion_required" and issue.get("status") == "open"
        for field in (issue.get("details") or {}).get("required_fields", ()) if not before.get(field)
    }
    result = dict(patch)
    provenance = dict(before.get("ai_completion_provenance") or {})
    supplements = dict(before.get("teacher_material_supplements") or {})
    for key in edited.intersection({"reference_answer", "criterion", "solution_code"}):
        provenance.pop(key, None)
        supplements.pop(key, None)
    prior = saved_draft(problem_from_question(question), allow_incomplete_alternative=True)
    source, teacher_policy = _generation_input(question, before, patch, prior, edited)
    if "stem" in edited and before.get("code_output"):
        from backend.domain.code_output import rebind_edited_code_output
        result["code_output"] = rebind_edited_code_output(before["code_output"], source["stem"]).model_dump(mode="json")
        source["code_output"] = result["code_output"]
    source["ai_completion_provenance"] = provenance
    get_provider = provider_for_edit(workflow, owner_id)
    language = workflow.initial_content_language or "zh"
    candidate_type = source["type"]
    programming = is_programming_question_type(candidate_type)
    case_edits = None
    if programming and "test_cases" in edited:
        from backend.services.programming_question_edit import validate_case_edits
        case_edits = validate_case_edits(before, {**before, **patch})
    original_answer = source["reference_answer"]
    missing = missing_answer_parts(original_answer, source["question_structure"])
    derivation = not source["question_structure"].get("subparts") and missing_derivation(original_answer, source["stem"])
    impact = None
    # A type conversion or an explicit replacement implementation already
    # requires execution validation; a wording review cannot substitute for it.
    review_prose = "type" not in edited and not (programming and "solution_code" in edited)
    if review_prose and before.get("stem") and (programming or not teacher_policy and "criterion" not in manual_missing
            or not original_answer and "reference_answer" not in manual_missing or missing or derivation):
        impact = await review_wording(before, patch, semantic, get_provider, reporter)
    previous_supplement = supplements.get("reference_answer")
    new_gap = (missing or derivation) and (
        not previous_supplement or tuple(previous_supplement.get("parts", ())) != missing)
    if previous_supplement and "stem" in semantic and not missing and not derivation:
        # A removed subquestion can retire an old AI appendix. Keep the
        # original teacher text even if no new material needs generation.
        result["reference_answer"] = original_answer
        supplements.pop("reference_answer")
    execution_edits = semantic if impact is None or impact.execution else set()
    if programming and execution_edits:
        source.pop("programming_reference_binding", None)
        if "solution_code" not in edited:
            source["solution_code"] = ""
    compiled = None
    fields = []
    if ("stem" in semantic and before.get("stem") and "reference_answer" not in edited | manual_missing
            and (impact is None or impact.answer or not before.get("reference_answer") or new_gap)
            and not source.get("code_output") and (not original_answer or missing or derivation)):
        fields.append("reference_answer")
    if (semantic and before.get("stem") and source["stem"] and "criterion" not in edited | manual_missing
            and (impact is None or impact.rubric or not before.get("criterion"))
            and not teacher_policy):
        fields.append("criterion")
    # The programming editor already froze the teacher's code rules and test
    # allocation. Publication compiles that policy against the revised question.
    saved_programming_policy = programming and prior and prior.programming_scoring is not None
    if (fields and teacher_policy and not saved_programming_policy and
            (impact is None or impact.rubric or not prior) and (uses_math_grading(candidate_type) or programming)):
        fields.append("criterion")
    if fields:
        q_id = question.q_id
        generation_source = source
        if programming and "stem" in execution_edits and "reference_answer" not in edited and not (missing or derivation):
            generation_source = {**source, "reference_answer": ""}
        if missing and "reference_answer" in fields:
            generation_source = {**generation_source, "missing_reference_parts": list(missing)}
        elif derivation and "reference_answer" in fields:
            generation_source = {**generation_source, "missing_derivation": True}
        targets = [{"target_id": f"{q_id}:{field}", "target": field, "q_id": q_id} for field in fields]
        try:
            candidates = await generate_major_question_materials(
                problems_data={q_id: generation_source}, requested_targets=targets, test_case_count=1,
                provider=get_provider(), reporter=reporter, generation_language=language,
            )
            values = {item.target: item for item in candidates}
            result["regenerated_fields"] = list(values)
            if "reference_answer" in values:
                answer = values["reference_answer"].text_value
                if original_answer:
                    if missing:
                        sections = answer_sections(answer, source["question_structure"])
                        if any(label not in sections for label in missing):
                            raise ValidationError("Required answer parts are missing.", code="provider_response_invalid")
                        answer = "\n\n".join(sections[label] for label in missing)
                    elif missing_derivation(answer, source["stem"]):
                        raise ValidationError("The requested reasoning is missing.", code="provider_response_invalid")
                    answer = original_answer + "\n\n" + answer
                    supplements["reference_answer"] = {"original": original_answer, "parts": list(missing)}
                else:
                    provenance["reference_answer"] = {"source_kind": "ai_generated"}
                result["reference_answer"] = answer
            if "criterion" in values:
                criterion, compiled = _adopt_rubric(values["criterion"], teacher_policy, prior, source, language)
                if not teacher_policy:
                    provenance["criterion"] = {"source_kind": "ai_generated"}
                result["criterion"] = criterion
                result["stem_save_teacher_criterion"] = teacher_policy
                if compiled:
                    compiled = retarget_draft(compiled, {
                        **source, **result, "reference_answer": result.get("reference_answer", before["reference_answer"]),
                        "ai_completion_provenance": provenance,
                    })
        except DomainError:
            raise
        except Exception as exc:
            code = classify_background_error(exc, "provider_response_invalid")
            raise ValidationError("The updated question materials could not be validated.", code=code) from exc
    if fields or provenance != before["ai_completion_provenance"]:
        result["ai_completion_provenance"] = provenance
    if supplements != before.get("teacher_material_supplements", {}):
        result["teacher_material_supplements"] = supplements
    if impact is not None and not impact.rubric and "criterion" not in edited:
        result["reuse_scoring_rules"] = True
    if programming and semantic and not execution_edits and before.get("programming_reference_binding"):
        from backend.services.programming_materials import material_problem
        from backend.services.programming_runs import reference_material, reference_materials_digest
        binding = before["programming_reference_binding"]
        rebound = {**binding, "materials_sha256": reference_materials_digest(
            result.get("stem", before["stem"]), result.get("reference_answer", before["reference_answer"]))}
        candidate = {**before, **result, "programming_reference_binding": rebound}
        if (reference_material(material_problem(before), include_generated=True)
                and reference_material(material_problem(candidate), include_generated=True)):
            result["programming_reference_binding"] = rebound
        else:
            execution_edits = semantic
    first_manual_entry = manual_missing and not any(before.get(key) for key in (
        "solution_code", "test_cases", "reference_answer", "programming_reference_binding"))
    if programming and (execution_edits and not first_manual_entry or edited.intersection({"solution_code", "test_cases"})):
        from backend.services.programming_question_edit import prepare_programming_edit
        candidate = {**before, **result, "programming_semantic_fields": sorted(execution_edits)}
        if compiled and (points := verified_test_points(compiled.plan)) is not None:
            candidate["programming_scoring"] = {**(candidate.get("programming_scoring") or {}), "test_points": points}
        verified = await prepare_programming_edit(
            before=before, candidate=candidate, edited_fields=edited, get_provider=get_provider, reporter=reporter,
            case_edits=case_edits,
        )
        result.update(verified)
        if "test_cases" in verified and "test_cases" not in edited:
            result["derived_test_cases"] = True
    result.pop("test_case_origins", None)
    return result, compiled


async def run(operation: LeasedOperation) -> None:
    workflow, question = await run_in_threadpool(_load_edit, operation)
    field = operation.payload.get("field", "stem")
    patch = {field: operation.payload[field]}
    reporter = ProgressReporter(operation.operation_id, total_questions=1)
    await reporter.set_stage_progress("generating_question_edit", total_steps=2, completed_steps=0)
    result, compiled = await prepare(question, workflow, patch, owner_id=operation.owner_id, reporter=reporter)
    await reporter.set_stage_progress("saving_question_edit", total_steps=2, completed_steps=1)
    await run_in_threadpool(task_facade.publish_linked_question_edit, operation, question.version, result, compiled)
