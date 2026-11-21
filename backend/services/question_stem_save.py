"""Prepare and publish one edited question as a durable workflow operation."""
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
from backend.services.programming_rubric import verified_test_points
from backend.services.question_structure import build_major_question_structure
from backend.services.rubric_drafts import RubricDraft, edit_alternatives, retarget_draft, saved_draft
from backend.services.stage_provider_routing import baidu_ocr_credential_id, resolve_stage_provider_route
from backend.services.workflow_worker import LeasedOperation


def _result(row) -> dict:
    result = {
        "id": row.id, "q_id": row.payload["q_id"], "status": row.status,
        "workflow_revision": (row.terminal_summary or {}).get("workflow_revision"),
        "error_code": row.error_code,
    }
    if row.status != "done":
        result["stem"] = row.payload["stem"]
        result["expected_workflow_revision"] = row.payload["expected_workflow_revision"]
    return result


def start(*, task_id: str, owner_id: str, q_id: str, stem: str,
          expected_workflow_revision: int, retry_of: str | None = None) -> dict:
    if not stem.strip():
        raise ValidationError("Enter the question text.", code="question_stem_required")
    payload = {"q_id": q_id, "stem": stem, "expected_workflow_revision": expected_workflow_revision}
    if retry_of is not None:
        failed = workflow_repository.get_operation(retry_of, owner_id=owner_id)
        if (failed.assignment_id != task_id or failed.operation_type != QUESTION_STEM_SAVE_OPERATION
                or failed.status != "error" or failed.payload["q_id"] != q_id
                or failed.payload["stem"] != stem):
            raise NotFound("question_stem_save")
    input_hash = sha256(json.dumps([payload, retry_of], sort_keys=True).encode()).hexdigest()
    row, _ = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id, operation_type=QUESTION_STEM_SAVE_OPERATION,
        input_hash=input_hash, payload=payload, retry_existing=False,
    )
    return _result(row)


def list_latest(*, task_id: str, owner_id: str) -> list[dict]:
    """Return the latest intent per question without rereading the task body."""
    workflow_repository.get_live_workflow(task_id, owner_id=owner_id)
    with session_scope() as session:
        op = workflow_repository.WorkflowOperationRecord
        latest = select(
            op.id, func.row_number().over(
                partition_by=op.payload["q_id"].as_string(),
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


def _generation_input(question, stem, prior):
    presentation = (question.source or {}).get("presentation") or {}
    provenance = dict(presentation.get("ai_completion_provenance") or {})
    # An earlier AI solution and rubric are stale after a stem change. Keep the
    # teacher's source policy, and pass explicit allocations through the normal generator.
    teacher_policy = presentation.get("stem_save_teacher_criterion")
    if teacher_policy is None:
        teacher_policy = question.criterion if "criterion" not in provenance else ""
    if teacher_policy:
        provenance.pop("criterion", None)
    raw = {
        **problem_from_question(question).model_dump(mode="json"),
        "stem": stem, "reference_answer": "", "criterion": teacher_policy,
        "question_structure": build_major_question_structure(
            {"number": question.number, "stem": stem}, major_order=question.order_index,
            structure_source="deterministic", review_status="needs_review",
            previous=presentation.get("question_structure"),
        ).model_dump(mode="json"),
        "ai_completion_provenance": provenance,
        "teacher_subpart_points": presentation.get("teacher_subpart_points"),
        "rubric_generation_preferences": presentation.get("rubric_generation_preferences"),
    }
    if presentation.get("alternative_rules"):
        revised = problem_from_question(question).model_copy(update={
            "stem": stem, "criterion": teacher_policy, "reference_answer": "",
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


async def run(operation: LeasedOperation) -> None:
    workflow, question = await run_in_threadpool(_load_edit, operation)
    stem = operation.payload["stem"]
    language = workflow.initial_content_language or "zh"
    reporter = ProgressReporter(operation.operation_id, total_questions=1)
    await reporter.set_stage_progress("generating_question_edit", total_steps=2, completed_steps=0)
    requested = workflow.question_recognition_provider_id
    provider = resolve_stage_provider_route(
        owner_id=operation.owner_id, registry=task_facade._registry_for_owner(operation.owner_id),
        requested_route_id=None if baidu_ocr_credential_id(requested) else requested,
    ).provider
    if provider is None:
        raise ValidationError("Enable a model to update question materials.", code="no_provider_configured")
    prior = saved_draft(problem_from_question(question), allow_incomplete_alternative=True)
    q_id = question.q_id
    targets = [{"target_id": f"{q_id}:{field}", "target": field, "q_id": q_id}
               for field in ("reference_answer", "criterion")]
    try:
        source, teacher_policy = _generation_input(question, stem, prior)
        candidates = await generate_major_question_materials(
            problems_data={q_id: source}, requested_targets=targets,
            test_case_count=1, provider=provider, reporter=reporter, generation_language=language,
        )
        answer, rubric = candidates
        criterion = rubric.text_value
        provenance = {**source["ai_completion_provenance"],
                      "reference_answer": {"source_kind": "ai_generated"},
                      "criterion": {"source_kind": "ai_generated"}}
        compiled = RubricDraft.model_validate(rubric.rubric_draft) if rubric.rubric_draft else None
        if uses_math_grading(question.type) or is_programming_question_type(question.type):
            if not compiled or not compiled.plan.part_rubric:
                raise ValidationError("The revised scoring rules are incomplete.",
                                      code="invalid_structured_rubric")
        if compiled:
            rules = compiled.plan.part_rubric
            if rules:
                if prior and prior.plan.part_rubric:
                    rules = rules.model_copy(update={"rubric_version": prior.plan.part_rubric.rubric_version + 1})
                if prior and prior.programming_scoring:
                    code_parts = tuple(part.model_copy(update={
                        "steps": tuple(step for step in part.steps if not step.test_suite),
                    }) for part in rules.parts)
                    criterion = render_parts(rules.model_copy(update={"parts": code_parts}), language)
                elif teacher_policy:
                    criterion = render_parts(rules, language)
                compiled = compiled.model_copy(update={
                    "plan": compiled.plan.model_copy(update={
                        "part_rubric": rules,
                        "original_criterion": criterion,
                        "effective_criterion": criterion,
                    }),
                })
            compiled = retarget_draft(compiled, {
                **source, "criterion": criterion, "reference_answer": answer.text_value,
                "ai_completion_provenance": provenance,
            })
    except DomainError:
        raise
    except Exception as exc:
        code = classify_background_error(exc, "provider_response_invalid")
        raise ValidationError("The updated question materials could not be validated.", code=code) from exc
    await reporter.set_stage_progress("saving_question_edit", total_steps=2, completed_steps=1)
    await run_in_threadpool(
        task_facade.publish_linked_question_edit, operation, question.version,
        {"stem": stem, "reference_answer": answer.text_value, "criterion": criterion,
         "ai_completion_provenance": provenance, "stem_save_teacher_criterion": teacher_policy},
        compiled,
    )
