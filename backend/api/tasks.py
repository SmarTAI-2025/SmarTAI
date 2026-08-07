"""Figma task presentation façade backed by normalized repositories.

The public paths remain ``/tasks/*`` so the shipped UI does not change, while
``task_id`` maps directly to ``AssignmentRecord.id``.  No legacy TaskStore or
JobStore is imported here.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import time
from typing import Any, Literal
from urllib.parse import quote

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field, ValidationError as PydanticValidationError

from backend.api.errors import domain_error_response
from backend.auth import require_teacher
from backend.db import assignment_repository, grading_repository, workflow_repository
from backend.domain.errors import DomainError, InvalidTransition, NotFound, ValidationError
from backend.knowledge.service import ingest_document
from backend.llm.registry import ExpertRegistry, get_scoped_expert_registry
from backend.models import TaskGradingSetup, User
from backend.services import task_facade


router = APIRouter(prefix="/tasks", tags=["tasks"])


class CreateTaskRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    semester_id: str | None = Field(default=None, max_length=64)
    course_id: str | None = Field(default=None, max_length=64)
    tag_ids: list[str] = Field(default_factory=list, max_length=30)


class UpdateTaskRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    semester_id: str | None = Field(default=None, max_length=64)
    course_id: str | None = Field(default=None, max_length=64)
    tag_ids: list[str] | None = Field(default=None, max_length=30)


class InterpretTaskQueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class GradeRequest(BaseModel):
    language: str = "en"
    multi_sample_n: int | None = Field(default=None, ge=1, le=10)


class UpdateGradingSetupRequest(BaseModel):
    expected_workflow_revision: int = Field(ge=0)
    grading_setup: dict[str, Any]


class UpdateProblemRequest(BaseModel):
    stem: str | None = None
    criterion: str | None = None
    max_score: float | None = Field(
        default=None,
        gt=0,
        le=10_000,
        allow_inf_nan=False,
    )
    reference_answer: str | None = None
    solution_code: str | None = None
    test_cases: list[dict] | None = None
    review_status: Literal["needs_review", "edited", "confirmed"] | None = None


class UpdateStudentAnswerRequest(BaseModel):
    expected_workflow_revision: int | None = Field(default=None, ge=0)
    content: str | None = None
    flag: list[str] | None = None
    review_status: Literal["pending", "confirmed"] | None = None


class UpdateStudentIdentityRequest(BaseModel):
    expected_workflow_revision: int = Field(ge=0)
    student_id: str = Field(min_length=1, max_length=160)
    student_name: str = Field(min_length=1, max_length=160)


class UpdateCorrectionReviewRequest(BaseModel):
    expected_workflow_revision: int = Field(ge=0)
    teacher_score: float = Field(ge=0)
    teacher_comment: str = Field(default="", max_length=4000)
    confirm: bool = False


class UpdateTeacherCommentRequest(BaseModel):
    student_id: str
    q_id: str
    comment: str = Field(default="", max_length=4000)


class ConfirmFinalResultRequest(BaseModel):
    expected_workflow_revision: int = Field(ge=0)


class GenerateResultArtifactsRequest(BaseModel):
    expected_workflow_revision: int = Field(ge=0)


def _domain(call):
    try:
        return call()
    except DomainError as exc:
        return domain_error_response(exc)


@router.post("/")
def create_task(
    request: CreateTaskRequest,
    current: User = Depends(require_teacher),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = idempotency_key or f"legacy-{hashlib.sha256(json.dumps(request.model_dump(), sort_keys=True).encode()).hexdigest()}"
    return _domain(lambda: task_facade.create_task(
        owner_id=current.id, name=request.name, semester_id=request.semester_id,
        course_id=request.course_id, tag_ids=request.tag_ids,
        idempotency_key=key,
    ))


@router.get("/")
def list_tasks(
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=100),
    q: str | None = Query(default=None, max_length=200),
    semester_id: str | None = Query(default=None, max_length=64),
    course_id: str | None = Query(default=None, max_length=64),
    tag_ids: str | None = None,
    statuses: str | None = None,
    unfinished: bool | None = None,
    needs_attention: bool | None = None,
    sort: str = "updated_desc",
    current: User = Depends(require_teacher),
):
    try:
        task_map = task_facade.list_tasks(owner_id=current.id)
    except DomainError as exc:
        return domain_error_response(exc)
    # The dashboard contract is a mapping. Supplying page/page_size selects the
    # history contract without changing the path used by the Figma client.
    if page is None and page_size is None and not any(
        [q, semester_id, course_id, tag_ids, statuses, unfinished, needs_attention]
    ):
        return task_map
    items = list(task_map.values())
    selected_tags = {item for item in (tag_ids or "").split(",") if item}
    selected_statuses = {item for item in (statuses or "").split(",") if item}
    if q:
        needle = q.casefold().strip()
        items = [item for item in items if needle in item["name"].casefold()]
    if semester_id:
        items = [item for item in items if item.get("semester_id") == semester_id]
    if course_id:
        items = [item for item in items if item.get("course_id") == course_id]
    if selected_tags:
        items = [item for item in items if selected_tags.issubset(set(item.get("tag_ids") or []))]
    if selected_statuses:
        items = [item for item in items if item.get("status") in selected_statuses]
    if unfinished:
        items = [item for item in items if item.get("status") not in {"finalized"}]
    if needs_attention is not None:
        items = [item for item in items if bool(item.get("needs_attention")) == needs_attention]
    items = _sort_tasks(items, sort)
    current_page = page or 1
    size = page_size or 25
    start = (current_page - 1) * size
    return {
        "items": items[start:start + size], "total": len(items),
        "page": current_page, "page_size": size,
        "available_facets": _history_facets(list(task_map.values()), current.id),
    }


def _sort_tasks(items: list[dict], sort: str) -> list[dict]:
    reverse = sort in {"updated_desc", "created_desc", "name_desc", "attention_first", "stage_desc"}
    if sort.startswith("created"):
        key = lambda item: item.get("created_at") or 0
    elif sort.startswith("name"):
        key = lambda item: item.get("name", "").casefold()
    elif sort == "attention_first":
        key = lambda item: (bool(item.get("needs_attention")), item.get("updated_at") or 0)
    elif sort.startswith("stage"):
        order = {name: index for index, name in enumerate([
            "draft", "extracting_problems", "problems_ready", "parsing_submissions",
            "submissions_ready", "grading", "graded", "review_confirmed", "finalized", "error",
        ])}
        key = lambda item: order.get(item.get("status"), 99)
    else:
        key = lambda item: item.get("updated_at") or 0
    return sorted(items, key=key, reverse=reverse)


def _history_facets(items: list[dict], owner_id: str) -> dict:
    from backend.services.courses import list_courses_for

    statuses: dict[str, int] = {}
    for item in items:
        statuses[item["status"]] = statuses.get(item["status"], 0) + 1
    courses = [course for course in list_courses_for(owner_id, "teacher") if course.code != task_facade.SYSTEM_COURSE_CODE]
    try:
        from backend.db import tag_repository
        tags = tag_repository.list_tags(owner_id=owner_id)
        rendered_tags = [tag_repository.serialize_tag(tag, usage_count=tag_repository.usage_count(tag.id)) for tag in tags]
    except (ImportError, AttributeError):
        rendered_tags = []
    return {
        "semesters": sorted({item["semester_id"] for item in items if item.get("semester_id")}),
        "courses": [{"id": course.id, "name": course.name, "code": course.code} for course in courses],
        "tags": rendered_tags, "statuses": statuses,
    }


@router.post("/query/interpret")
def interpret_task_query(
    request: InterpretTaskQueryRequest,
    current: User = Depends(require_teacher),
):
    """Safe deterministic fallback for the history natural-language box.

    It recognizes stable status/attention words and leaves the remaining text
    as a keyword. This avoids an LLM call (and BYOK use) for a filter action.
    """
    text = request.query.strip()
    folded = text.casefold()
    filters: dict[str, Any] = {}
    conditions = []
    status_words = {
        "draft": ("draft", "草稿"), "grading": ("grading", "批改中"),
        "graded": ("graded", "已批改"), "finalized": ("finalized", "已完成"),
        "error": ("error", "失败", "错误"),
    }
    matched = []
    for status_name, words in status_words.items():
        if any(word in folded for word in words):
            matched.append(status_name)
    if matched:
        filters["statuses"] = matched
        conditions.append({"field": "statuses", "label": "Status", "value": matched})
    if any(word in folded for word in ("attention", "待处理", "需关注")):
        filters["needs_attention"] = True
        conditions.append({"field": "needs_attention", "label": "Needs attention", "value": True})
    if not filters:
        filters["q"] = text
        conditions.append({"field": "q", "label": "Keyword", "value": text})
    return {
        "filters": filters, "sort": "updated_desc",
        "explanation": "Applied deterministic history filters.",
        "conditions": conditions, "ambiguities": [], "source": "deterministic",
        "query_id": f"query_{hashlib.sha256(text.encode()).hexdigest()[:12]}",
    }


@router.get("/{task_id}")
def get_task(task_id: str, current: User = Depends(require_teacher)):
    return _domain(lambda: task_facade.get_task(task_id=task_id, owner_id=current.id))


@router.put("/{task_id}")
def update_task(task_id: str, request: UpdateTaskRequest, current: User = Depends(require_teacher)):
    body = request.model_dump(exclude_unset=True)
    return _domain(lambda: task_facade.update_task(
        task_id=task_id, owner_id=current.id,
        name=body.get("name"),
        semester_id=body["semester_id"] if "semester_id" in body else ...,
        course_id=body["course_id"] if "course_id" in body else ...,
        tag_ids=body.get("tag_ids") if "tag_ids" in body else None,
    ))


@router.delete("/{task_id}")
def delete_task(task_id: str, current: User = Depends(require_teacher)):
    try:
        task_facade.delete_task(task_id=task_id, owner_id=current.id)
    except DomainError as exc:
        return domain_error_response(exc)
    return {"status": "success"}


@router.post("/{task_id}/extract_problems")
async def extract_problems_endpoint(
    task_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile | None = File(default=None),
    source_token: str | None = Form(default=None),
    confirmed_candidate_ids: str = Form(default="[]"),
    replace_confirmed: bool = Form(default=False),
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    try:
        selected_candidates = json.loads(confirmed_candidate_ids)
        if not isinstance(selected_candidates, list) or not all(
            isinstance(item, str) for item in selected_candidates
        ):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_confirmed_candidate_ids"},
        )
    if source_token and file is None:
        return await _extract_from_source_token(
            task_id, source_token, selected_candidates, replace_confirmed,
            current, registry, background_tasks,
        )
    if file is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "source_required"})
    content = await file.read()
    try:
        queued = task_facade.queue_task_problem_extraction(
            task_id=task_id, owner_id=current.id,
            filename=file.filename or "problems", content=content,
            content_type=file.content_type, registry=registry,
            replace_confirmed=replace_confirmed,
        )
        if queued["status"] == "started":
            job_attempt = queued.pop("_job_attempt")
            background_tasks.add_task(
                task_facade.run_task_problem_extraction,
                task_id=task_id, owner_id=current.id,
                job_id=queued["job_id"], filename=file.filename or "problems",
                content=content, registry=registry,
                job_attempt=job_attempt,
                claimed_workflow_revision=queued["workflow_revision"],
                replace_confirmed=replace_confirmed,
            )
        return queued
    except DomainError as exc:
        return domain_error_response(exc)


@router.post("/{task_id}/parse_submissions")
async def parse_submissions_endpoint(
    task_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    identity_mode: str = Form(default="filename"),
    roster_file: UploadFile | None = File(default=None),
    recognition_provider_id: str | None = Form(default=None),
    replace_confirmed: bool = Form(default=False),
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    body = await file.read()
    roster_entries: list[dict[str, str]] = []
    roster_name = None
    if roster_file is not None:
        roster_name = roster_file.filename or "roster.csv"
        roster_entries = _parse_roster(await roster_file.read())
    try:
        queued = task_facade.queue_task_submission_parsing(
            task_id=task_id, owner_id=current.id,
            filename=file.filename or "submissions", content=body,
            content_type=file.content_type, registry=registry,
            identity_mode=identity_mode, roster_entries=roster_entries,
            roster_name=roster_name,
            recognition_provider_id=recognition_provider_id,
            replace_confirmed=replace_confirmed,
        )
        if queued["status"] == "started":
            job_attempt = queued.pop("_job_attempt")
            background_tasks.add_task(
                task_facade.run_task_submission_parsing,
                task_id=task_id, owner_id=current.id,
                job_id=queued["job_id"], filename=file.filename or "submissions",
                content=body, registry=registry, identity_mode=identity_mode,
                job_attempt=job_attempt,
                roster_entries=roster_entries,
                recognition_provider_id=recognition_provider_id,
                replace_confirmed=replace_confirmed,
                claimed_workflow_revision=queued["workflow_revision"],
            )
        return queued
    except DomainError as exc:
        return domain_error_response(exc)


def _parse_roster(body: bytes) -> list[dict[str, str]]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = body.decode("gbk")
    reader = csv.DictReader(io.StringIO(text))
    items = []
    for row in reader:
        normalized = {str(key or "").strip().casefold(): str(value or "").strip() for key, value in row.items()}
        student_id = normalized.get("student_id") or normalized.get("stu_id") or normalized.get("学号") or ""
        student_name = normalized.get("student_name") or normalized.get("stu_name") or normalized.get("name") or normalized.get("姓名") or ""
        if student_id:
            items.append({"stu_id": student_id, "stu_name": student_name})
    return items


@router.post("/{task_id}/grade")
def start_grading(
    task_id: str,
    request: GradeRequest,
    current: User = Depends(require_teacher),
):
    del request  # immutable setup snapshot controls the real run
    return _domain(lambda: task_facade.start_task_grading(task_id=task_id, owner_id=current.id))


@router.get("/{task_id}/state")
async def task_state(task_id: str, current: User = Depends(require_teacher)):
    try:
        return await task_facade.async_task_state(task_id=task_id, owner_id=current.id)
    except DomainError as exc:
        return domain_error_response(exc)


@router.get("/{task_id}/result")
def task_result(task_id: str, current: User = Depends(require_teacher)):
    return _domain(lambda: task_facade.task_results(task_id=task_id, owner_id=current.id))


@router.put("/{task_id}/problems/{q_id}")
def update_problem(task_id: str, q_id: str, request: UpdateProblemRequest,
                   current: User = Depends(require_teacher)):
    return _domain(lambda: task_facade.update_problem(
        task_id=task_id, owner_id=current.id, q_id=q_id,
        patch=request.model_dump(exclude_unset=True),
    ))


@router.put("/{task_id}/students/{student_id}/answers/{q_id}")
def update_student_answer(
    task_id: str, student_id: str, q_id: str,
    request: UpdateStudentAnswerRequest,
    current: User = Depends(require_teacher),
):
    return _domain(lambda: task_facade.update_student_answer(
        task_id=task_id, owner_id=current.id, display_student_id=student_id,
        q_id=q_id, patch=request.model_dump(exclude_unset=True),
        expected_revision=request.expected_workflow_revision,
    ))


@router.put("/{task_id}/students/{student_id}/identity")
def update_student_identity(
    task_id: str, student_id: str, request: UpdateStudentIdentityRequest,
    current: User = Depends(require_teacher),
):
    return _domain(lambda: task_facade.update_student_identity(
        task_id=task_id, owner_id=current.id,
        current_display_id=student_id, new_display_id=request.student_id,
        new_display_name=request.student_name,
        expected_revision=request.expected_workflow_revision,
    ))


@router.put("/{task_id}/reviews/{student_id}/{q_id}")
def update_correction_review(
    task_id: str, student_id: str, q_id: str,
    request: UpdateCorrectionReviewRequest,
    current: User = Depends(require_teacher),
):
    return _domain(lambda: task_facade.update_correction_review(
        task_id=task_id, owner_id=current.id, display_student_id=student_id,
        q_id=q_id, teacher_score=request.teacher_score,
        teacher_comment=request.teacher_comment, confirm=request.confirm,
        expected_revision=request.expected_workflow_revision,
    ))


@router.post("/{task_id}/teacher_comment")
def set_teacher_comment(
    task_id: str, request: UpdateTeacherCommentRequest,
    current: User = Depends(require_teacher),
):
    try:
        workflow = workflow_repository.get_workflow(task_id, owner_id=current.id)
        results = task_facade.task_results(task_id=task_id, owner_id=current.id)
        student = next((item for item in results.get("results", []) if item["student_id"] == request.student_id), None)
        correction = next((item for item in (student or {}).get("corrections", []) if item["q_id"] == request.q_id), None)
        if correction is None:
            raise NotFound("grade_result")
        task_facade.update_correction_review(
            task_id=task_id, owner_id=current.id,
            display_student_id=request.student_id, q_id=request.q_id,
            teacher_score=correction.get("teacher_score") if correction.get("teacher_score") is not None else correction["score"],
            teacher_comment=request.comment, confirm=False,
            expected_revision=workflow.workflow_revision,
        )
        return {"status": "ok", "student_id": request.student_id,
                "q_id": request.q_id, "teacher_comment": request.comment}
    except DomainError as exc:
        return domain_error_response(exc)


@router.get("/{task_id}/teacher_comments")
def list_teacher_comments(task_id: str, current: User = Depends(require_teacher)):
    try:
        results = task_facade.task_results(task_id=task_id, owner_id=current.id)
    except DomainError as exc:
        return domain_error_response(exc)
    comments = {}
    for student in results.get("results", []):
        for correction in student.get("corrections", []):
            if correction.get("teacher_comment"):
                comments[f"{student['student_id']}::{correction['q_id']}"] = correction["teacher_comment"]
    return {"comments": comments}


@router.get("/{task_id}/grading-setup")
def get_grading_setup(
    task_id: str,
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    try:
        return _grading_setup_payload(task_id, current.id, registry)
    except DomainError as exc:
        return domain_error_response(exc)


@router.put("/{task_id}/grading-setup")
def save_grading_setup(
    task_id: str,
    request: UpdateGradingSetupRequest,
    current: User = Depends(require_teacher),
    registry: ExpertRegistry = Depends(get_scoped_expert_registry),
):
    try:
        workflow = workflow_repository.get_workflow(task_id, owner_id=current.id)
        setup = TaskGradingSetup.model_validate(request.grading_setup)
        _validate_grading_setup(setup, registry)
        body = setup.model_dump(mode="json")
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        unchanged = workflow.grading_setup_fingerprint == fingerprint
        if not unchanged:
            workflow_repository.update_workflow(
                task_id, owner_id=current.id,
                expected_revision=request.expected_workflow_revision,
                grading_setup=body, grading_setup_fingerprint=fingerprint,
                grading_setup_updated_at=time.time(),
            )
        else:
            workflow_repository.update_workflow(
                task_id, owner_id=current.id,
                expected_revision=request.expected_workflow_revision,
                bump_revision=False,
            )
        return {**_grading_setup_payload(task_id, current.id, registry),
                "status": "unchanged" if unchanged else "saved"}
    except PydanticValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"code": "invalid_grading_setup"}) from exc
    except DomainError as exc:
        return domain_error_response(exc)


def _validate_grading_setup(setup: TaskGradingSetup, registry: ExpertRegistry) -> None:
    configs = {str(item["provider_id"]): item for item in registry.list_configs()}
    selected = setup.selected_provider_ids
    if len(set(selected)) != len(selected):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "duplicate_provider_ids"})
    if setup.primary_provider_id not in selected:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "primary_provider_not_selected"})
    if any(provider_id not in configs or not configs[provider_id].get("enabled") for provider_id in selected):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "provider_not_enabled"})
    if setup.aggregation_method == "single" and len(selected) != 1:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_provider_count"})
    if setup.aggregation_method != "single" and len(selected) < 2:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_provider_count"})
    if registry.uses_shared_pool() and (len(selected) != 1 or setup.aggregation_method != "single" or setup.multi_sample_n != 1):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "shared_pool_single_expert_required"})


def _grading_setup_payload(task_id: str, owner_id: str, registry: ExpertRegistry) -> dict:
    task = task_facade.get_task(task_id=task_id, owner_id=owner_id, full=False)
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    configs = []
    for item in registry.list_configs():
        configs.append({
            key: item.get(key) for key in (
                "provider_id", "provider_type", "model", "display_name", "enabled",
                "scope", "is_shared", "editable", "max_concurrent", "rpm",
            )
        })
    default_id = registry.pick_default_id()
    suggested = None
    if default_id is not None:
        suggested = TaskGradingSetup(
            selected_provider_ids=[default_id],
            primary_provider_id=default_id,
            knowledge_scope="all_task_docs" if task["kb_doc_count"] else "none",
        ).model_dump(mode="json")
    blocking = []
    if not configs:
        blocking.append("provider_required")
    if task["status"] not in {"problems_ready", "submissions_ready", "error"}:
        blocking.append("invalid_state")
    if workflow.grading_setup:
        try:
            _validate_grading_setup(TaskGradingSetup.model_validate(workflow.grading_setup), registry)
        except (HTTPException, PydanticValidationError):
            blocking.append("invalid_grading_setup")
    return {
        "task_id": task_id, "task_status": task["status"],
        "workflow_revision": workflow.workflow_revision,
        "configured": workflow.grading_setup is not None,
        "grading_setup": workflow.grading_setup,
        "suggested_setup": suggested,
        "grading_setup_fingerprint": workflow.grading_setup_fingerprint,
        "grading_setup_updated_at": workflow.grading_setup_updated_at,
        "available_experts": configs,
        "knowledge": {
            "scope_options": ["none", "all_task_docs"],
            "task_doc_count": task["kb_doc_count"],
            "task_docs": list(task["kb_docs"].values()),
        },
        "readiness": {"ready": not blocking, "blocking_issues": list(dict.fromkeys(blocking)), "warnings": []},
    }


@router.get("/{task_id}/finalization")
def get_finalization(task_id: str, current: User = Depends(require_teacher)):
    return _domain(lambda: task_facade.finalization(task_id=task_id, owner_id=current.id))


@router.post("/{task_id}/finalization/confirm")
def confirm_finalization(
    task_id: str, request: ConfirmFinalResultRequest,
    current: User = Depends(require_teacher),
):
    return _domain(lambda: task_facade.confirm_finalization(
        task_id=task_id, owner_id=current.id,
        expected_revision=request.expected_workflow_revision,
    ))


@router.get("/{task_id}/artifacts")
def list_artifacts(task_id: str, current: User = Depends(require_teacher)):
    return _domain(lambda: task_facade.artifact_index(task_id=task_id, owner_id=current.id))


@router.post("/{task_id}/artifacts/generate")
def generate_artifacts(
    task_id: str, request: GenerateResultArtifactsRequest,
    current: User = Depends(require_teacher),
):
    return _domain(lambda: task_facade.generate_artifacts(
        task_id=task_id, owner_id=current.id,
        expected_revision=request.expected_workflow_revision,
    ))


@router.get("/{task_id}/artifacts/{version_number}/{artifact_id}")
def download_artifact(
    task_id: str, version_number: int, artifact_id: str,
    current: User = Depends(require_teacher),
):
    try:
        content, media_type, filename = task_facade.artifact_bytes(
            task_id=task_id, owner_id=current.id,
            version=version_number, artifact_id=artifact_id,
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return Response(
        content=content, media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}"},
    )


# ─── Assignment-scoped personal knowledge compatibility ─────────────────────


@router.post("/{task_id}/kb")
async def upload_task_knowledge(
    task_id: str,
    file: UploadFile | None = File(default=None),
    library_material_id: str | None = Form(default=None),
    save_to_library: bool = Form(default=False),
    expected_workflow_revision: int | None = Form(default=None),
    current: User = Depends(require_teacher),
):
    try:
        assignment_repository.get_assignment(task_id, actor_id=current.id)
        workflow = workflow_repository.get_workflow(task_id, owner_id=current.id)
        if expected_workflow_revision is not None and workflow.workflow_revision != expected_workflow_revision:
            from backend.domain.errors import VersionConflict
            raise VersionConflict("workflow_revision_conflict")
        if library_material_id:
            from backend.db import course_library_repository as library_repo

            material = library_repo.get_material(library_material_id, current.id)
            if material is None:
                raise NotFound("course_material")
            document_id = material.document_id
            document = _knowledge_document(document_id, current.id)
            created = False
            source_kind = "library"
            effective_material_id = material.material_id
            saved_material_created = False
        elif file is not None:
            body = await file.read()
            document = await ingest_document(
                owner_id=current.id, original_name=file.filename or "knowledge.txt",
                content=body, content_type=file.content_type,
            )
            document_id = document.id
            created = True
            source_kind = "upload"
            from backend.db import course_library_repository as library_repo

            material = library_repo.get_material_by_document(
                document_id, current.id
            )
            saved_material_created = False
            if save_to_library and material is None:
                material, saved_material_created = library_repo.create_material(
                    owner_id=current.id,
                    document_id=document_id,
                    filename=document.original_name,
                    category="other",
                    labels=[],
                    course_id=None,
                    group_id=None,
                )
            effective_material_id = (
                material.material_id if material is not None else None
            )
        else:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "knowledge_source_required"})
        from backend.db.knowledge_repository import (
            list_selected_documents,
            set_selected_document_metadata,
            set_task_documents,
        )
        selected = list_selected_documents(task_id, current.id)
        ids = [item.id for item in selected]
        if document_id not in ids:
            ids.append(document_id)
        set_task_documents(assignment_id=task_id, owner_id=current.id, document_ids=ids)
        try:
            set_selected_document_metadata(
                assignment_id=task_id,
                owner_id=current.id,
                document_id=document_id,
                source_kind=source_kind,
                library_material_id=effective_material_id,
            )
        except ValueError as exc:
            raise ValidationError(
                "Knowledge attachment metadata is invalid.",
                code="knowledge_attachment_invalid",
            ) from exc
        revised = workflow_repository.update_workflow(task_id, owner_id=current.id)
        return {
            "status": "started" if created else "already_done", "task_id": task_id,
            "doc_id": document.id, "filename": document.original_name,
            "chunk_count": document.chunk_count, "workflow_revision": revised.workflow_revision,
            "source_kind": source_kind,
            "library_material_id": effective_material_id,
            "saved_to_library": effective_material_id is not None,
            "saved_material_id": effective_material_id if save_to_library else None,
            "saved_material_created": saved_material_created,
        }
    except DomainError as exc:
        return domain_error_response(exc)


@router.get("/{task_id}/kb")
def list_task_knowledge(task_id: str, current: User = Depends(require_teacher)):
    try:
        task = task_facade.get_task(task_id=task_id, owner_id=current.id, full=False)
    except DomainError as exc:
        return domain_error_response(exc)
    return {"docs": list(task["kb_docs"].values())}


@router.delete("/{task_id}/kb/{doc_id}")
def delete_task_knowledge(
    task_id: str, doc_id: str,
    expected_workflow_revision: int | None = Query(default=None, ge=0),
    current: User = Depends(require_teacher),
):
    try:
        workflow = workflow_repository.get_workflow(task_id, owner_id=current.id)
        if expected_workflow_revision is not None and workflow.workflow_revision != expected_workflow_revision:
            from backend.domain.errors import VersionConflict
            raise VersionConflict("workflow_revision_conflict")
        from backend.db.knowledge_repository import list_selected_documents, set_task_documents
        selected = list_selected_documents(task_id, current.id)
        if doc_id not in {item.id for item in selected}:
            raise NotFound("knowledge_document")
        set_task_documents(
            assignment_id=task_id, owner_id=current.id,
            document_ids=[item.id for item in selected if item.id != doc_id],
        )
        revised = workflow_repository.update_workflow(task_id, owner_id=current.id)
        return {"status": "success", "doc_id": doc_id, "workflow_revision": revised.workflow_revision}
    except DomainError as exc:
        return domain_error_response(exc)


def _material_document_id(material_id: str, owner_id: str) -> str:
    try:
        from backend.db import material_repository
        material = material_repository.get_material(material_id=material_id, owner_id=owner_id)
        return material.document_id
    except (ImportError, AttributeError):
        raise NotFound("course_material")


def _knowledge_document(document_id: str, owner_id: str):
    from backend.db.knowledge_repository import get_document
    document = get_document(document_id, owner_id)
    if document is None or document.status != "ready":
        raise NotFound("knowledge_document")
    return document


# Source-token implementation is added below the router endpoints so the core
# task contract remains readable.
async def _extract_from_source_token(
    task_id: str, source_token: str, confirmed_candidate_ids: list[str],
    replace_confirmed: bool, current: User, registry: ExpertRegistry,
    background_tasks: BackgroundTasks,
):
    try:
        operation = workflow_repository.get_operation(source_token, owner_id=current.id)
        if operation.assignment_id != task_id or operation.operation_type != "problem_source":
            raise NotFound("problem_source")
        if operation.expires_at is not None and operation.expires_at <= time.time():
            raise InvalidTransition("Problem source expired.", code="stale_revision")
        payload = dict(operation.payload or {})
        candidates = list(payload.get("candidates") or [])
        available = {
            str(candidate.get("candidate_id"))
            for candidate in candidates
            if candidate.get("candidate_id")
        }
        selected = set(confirmed_candidate_ids)
        if not selected.issubset(available):
            raise ValidationError(
                "One or more selected problem candidates are unknown.",
                code="stale_revision",
            )
        if (
            payload.get("structure_mode") == "extract_from_source"
            and candidates
            and not selected
        ):
            raise InvalidTransition(
                "Problem candidates must be confirmed before extraction.",
                code="replacement_confirmation_required",
            )
        content = str(payload.get("text") or "").encode("utf-8")
        extraction_options = {
            "structure_mode": payload.get("structure_mode", "organized"),
            "extraction_hint": payload.get("extraction_hint", ""),
            "confirmed_candidates": [
                candidate for candidate in candidates
                if not selected or candidate.get("candidate_id") in selected
            ],
        }
        queued = task_facade.queue_task_problem_extraction(
            task_id=task_id, owner_id=current.id,
            filename=str(payload.get("filename") or "source.txt"),
            content=content,
            content_type=str(payload.get("content_type") or "text/plain"),
            registry=registry,
            input_hash=operation.input_hash,
            expected_workflow_revision=int(payload.get("base_workflow_revision") or 0),
            replace_confirmed=replace_confirmed,
            extraction_options=extraction_options,
        )
        if queued["status"] == "started":
            job_attempt = queued.pop("_job_attempt")
            background_tasks.add_task(
                task_facade.run_task_problem_extraction,
                task_id=task_id, owner_id=current.id,
                job_id=queued["job_id"],
                filename=str(payload.get("filename") or "source.txt"),
                content=content, registry=registry,
                job_attempt=job_attempt,
                claimed_workflow_revision=queued["workflow_revision"],
                replace_confirmed=replace_confirmed,
                extraction_options=extraction_options,
            )
        return queued
    except DomainError as exc:
        return domain_error_response(exc)
