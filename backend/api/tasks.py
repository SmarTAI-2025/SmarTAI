"""
Tasks API — task-centric workflow router.

A `Task` bundles `problem_data + student_data + grading_job` into one user-visible
unit so a teacher can pause mid-flow, switch between drafts, or resume work later.
This file replaces the global problem_store/student_store coupling that the
legacy /prob_preview /hw_preview endpoints rely on.

Endpoints:
  POST   /tasks/                              create draft task
  GET    /tasks/                              list current user's tasks (lite)
  GET    /tasks/{task_id}                     full task (incl. problem & student data)
  PUT    /tasks/{task_id}                     rename / update metadata
  DELETE /tasks/{task_id}                     delete task
  POST   /tasks/{task_id}/extract_problems    upload problem file → start extract job
  POST   /tasks/{task_id}/parse_submissions   upload submission archive → start parse job
  POST   /tasks/{task_id}/grade               start batch grading job
  GET    /tasks/{task_id}/state               current status + active reporter snapshot
  GET    /tasks/{task_id}/result              graded result

Idempotency:
  Each upload endpoint computes sha256(file_bytes). If a job for the same hash is
  already running OR has already completed, the endpoint returns
  `{"status": "already_running"}` or `{"status": "already_done"}` and skips the
  LLM call. Choosing a different file invalidates the hash and starts a fresh job.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from backend.auth import require_teacher
from backend.models import (
    GradingJob, Task, User,
)
from backend.state import (
    JobStore, TaskStore,
    get_job_store, get_task_store,
)
from backend.llm.registry import ExpertRegistry, get_expert_registry
from backend.agents.ingest_agent import (
    extract_problems,
    parse_student_answers,
    parse_reference_to_per_question,
    parse_test_cases_to_per_question,
)
from backend.agents.grading_agent import grade_batch
from backend.tools.file_processing import (
    decode_text_bytes, extract_files_from_archive, extract_text_from_pdf,
)
from backend.tools.knowledge import get_retriever
from backend.rag.chunker import extract_text as kb_extract_text, chunk_text, MAX_FILE_BYTES as KB_MAX_FILE_BYTES
from backend.rag.embedder import pick_embedder
from backend.rag.store import InMemoryTaskRetriever
from backend.progress.tracker import (
    get_or_create_reporter, get_reporter, remove_reporter,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])


# ─── Request models ──────────────────────────────────────────────────────────

class CreateTaskRequest(BaseModel):
    name: str = "Untitled task"


class UpdateTaskRequest(BaseModel):
    name: Optional[str] = None


class GradeRequest(BaseModel):
    language: str = "en"
    # ─── Per-task overrides for global settings ─────────────────────────────
    # When None, the corresponding `settings.*` value is used (current behavior).
    # When set, this value is used for THIS grading run only — does NOT persist
    # back to settings or TaskStore. The plan (hyssop-paper-jaybird) uses this
    # to surface a per-task multi-sample slider on the task_setup page so a
    # teacher can spend extra LLM calls on important tasks without changing
    # the global default.
    multi_sample_n: Optional[int] = Field(
        default=None,
        ge=1, le=10,
        description="单专家场景下并行采样次数；None = 用全局默认（settings.multi_sample_n，目前 1）。"
                    "≥ 2 个启用专家时本字段被忽略（变量来自专家本身）。",
    )


class UpdateProblemRequest(BaseModel):
    """Edit a single problem's stem and/or rubric.

    Only fields you pass are applied; the rest stay as-is. Used by the
    Problems-page edit-in-place UI.
    """
    stem: Optional[str] = None
    criterion: Optional[str] = None


class UpdateStudentAnswerRequest(BaseModel):
    """Edit a single student's parsed answer for a specific question.

    Used by the student-answers preview page when the teacher spots an AI
    OCR / segmentation error and wants to fix the recognized content (or
    clear the recognition flag) before grading runs.
    """
    content: Optional[str] = None
    flag: Optional[List[str]] = None      # pass [] to clear flags


class UpdateTeacherCommentRequest(BaseModel):
    """Set or clear a teacher's manual comment on a (student, q_id) pair.

    Stored alongside the AI correction so the teacher's note is preserved
    when the task is reloaded, without overwriting the AI feedback. An empty
    string clears the comment.
    """
    student_id: str
    q_id: str
    comment: str = ""


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _check_owner(task: Task, user: User) -> None:
    if user.role == "admin":
        return
    if task.owner_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Not your task")


def _get_or_404(task_store: TaskStore, task_id: str) -> Task:
    t = task_store.get(task_id)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Task not found")
    return t


# ─── CRUD ────────────────────────────────────────────────────────────────────

@router.post("/")
def create_task(
    req: CreateTaskRequest,
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
):
    task = Task(
        task_id=f"T_{uuid.uuid4().hex[:10]}",
        name=req.name or "Untitled task",
        owner_id=current.id,
        status="draft",
    )
    task_store.create(task)
    logger.info(f"Created task {task.task_id} for {current.id}")
    return task.lite()


@router.get("/")
def list_tasks(
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
):
    tasks = task_store.list_for_owner(current.id)
    tasks.sort(key=lambda t: t.updated_at, reverse=True)
    return {t.task_id: t.lite() for t in tasks}


@router.get("/{task_id}")
def get_task(
    task_id: str,
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
):
    t = _get_or_404(task_store, task_id)
    _check_owner(t, current)
    out = t.lite()
    out["problem_data"] = t.problem_data
    out["student_data"] = t.student_data
    return out


@router.put("/{task_id}")
def update_task(
    task_id: str,
    req: UpdateTaskRequest,
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
):
    t = _get_or_404(task_store, task_id)
    _check_owner(t, current)
    fields: Dict[str, Any] = {}
    if req.name is not None:
        fields["name"] = req.name
    if fields:
        task_store.update(task_id, **fields)
    return task_store.get(task_id).lite()  # type: ignore


@router.delete("/{task_id}")
def delete_task(
    task_id: str,
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
    job_store: JobStore = Depends(get_job_store),
):
    t = _get_or_404(task_store, task_id)
    _check_owner(t, current)
    if t.grading_job_id:
        job_store.discard(t.grading_job_id)
    if t.extract_job_id:
        remove_reporter(t.extract_job_id)
    if t.parse_job_id:
        remove_reporter(t.parse_job_id)
    # Drop any in-memory KB index attached to this task. Safe even if the
    # active retriever is the NoOp default — remove_task is a no-op there.
    retriever = get_retriever()
    if isinstance(retriever, InMemoryTaskRetriever):
        retriever.remove_task(task_id)
    task_store.delete(task_id)
    return {"status": "success"}


# ─── Extract problems (with idempotency) ─────────────────────────────────────

async def _run_extract(
    task: Task,
    text: str,
    provider,
    job_id: str,
    task_store: TaskStore,
):
    reporter = get_or_create_reporter(job_id)
    try:
        await extract_problems(text, provider, task.problem_data, reporter=reporter)
        task_store.update(task.task_id, status="problems_ready", error=None)
        logger.info(f"[task:{task.task_id}] extract done, {len(task.problem_data)} problems")
    except Exception as e:
        logger.exception(f"[task:{task.task_id}] extract failed")
        task_store.update(task.task_id, status="error", error=str(e))


@router.post("/{task_id}/extract_problems")
async def task_extract_problems(
    task_id: str,
    file: UploadFile = File(...),
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
    registry: ExpertRegistry = Depends(get_expert_registry),
):
    t = _get_or_404(task_store, task_id)
    _check_owner(t, current)

    provider = registry.pick_default()
    if provider is None:
        raise HTTPException(503, detail="No LLM provider configured. Add an API key first.")

    bytes_ = await file.read()
    new_hash = hashlib.sha256(bytes_).hexdigest()

    # Idempotency: same task is already extracting
    if t.status == "extracting_problems" and t.extract_job_id:
        return {
            "status": "already_running",
            "job_id": t.extract_job_id,
            "task_id": t.task_id,
        }

    # Idempotency: same file already processed
    if (
        t.problem_file_hash == new_hash
        and t.status in ("problems_ready", "parsing_submissions",
                         "submissions_ready", "grading", "graded")
    ):
        return {
            "status": "already_done",
            "unchanged": True,
            "job_id": t.extract_job_id,
            "task_id": t.task_id,
            "problem_count": len(t.problem_data),
        }

    # Decode text content
    try:
        if file.content_type == "application/pdf" or (file.filename or "").lower().endswith(".pdf"):
            text = await extract_text_from_pdf(bytes_)
        else:
            text = await decode_text_bytes(bytes_)
    except Exception as e:
        raise HTTPException(400, detail=f"Could not decode file: {e}")

    # Start fresh job
    job_id = str(uuid.uuid4())
    task_store.update(
        task_id,
        status="extracting_problems",
        extract_job_id=job_id,
        problem_file_hash=new_hash,
        problem_file_name=file.filename,
        problem_data={},  # clear old data
        error=None,
    )
    asyncio.create_task(_run_extract(t, text, provider, job_id, task_store))
    return {
        "status": "started",
        "job_id": job_id,
        "task_id": t.task_id,
    }


# ─── Parse submissions (with idempotency) ────────────────────────────────────

async def _run_parse(
    task: Task,
    files_data,
    provider,
    job_id: str,
    task_store: TaskStore,
):
    reporter = get_or_create_reporter(job_id, total_students=len(files_data))
    try:
        await parse_student_answers(
            files_data=files_data,
            problems_data=task.problem_data,
            student_store=task.student_data,
            provider=provider,
            reporter=reporter,
        )
        task_store.update(task.task_id, status="submissions_ready", error=None)
        logger.info(f"[task:{task.task_id}] parse done, {len(task.student_data)} students")
    except Exception as e:
        logger.exception(f"[task:{task.task_id}] parse failed")
        task_store.update(task.task_id, status="error", error=str(e))


@router.post("/{task_id}/parse_submissions")
async def task_parse_submissions(
    task_id: str,
    file: UploadFile = File(...),
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
    registry: ExpertRegistry = Depends(get_expert_registry),
):
    t = _get_or_404(task_store, task_id)
    _check_owner(t, current)

    if t.status not in ("problems_ready", "submissions_ready", "graded", "error"):
        if t.status == "draft":
            raise HTTPException(409, detail="Upload problems first.")
        if t.status == "extracting_problems":
            raise HTTPException(409, detail="Wait for problem extraction to finish.")
        if t.status == "parsing_submissions":
            return {
                "status": "already_running",
                "job_id": t.parse_job_id,
                "task_id": t.task_id,
            }
        if t.status == "grading":
            raise HTTPException(409, detail="Cannot replace submissions while grading.")

    provider = registry.pick_default()
    if provider is None:
        raise HTTPException(503, detail="No LLM provider configured.")

    bytes_ = await file.read()
    new_hash = hashlib.sha256(bytes_).hexdigest()

    # Idempotency: already running same task (defensive — covered above)
    if t.status == "parsing_submissions" and t.parse_job_id:
        return {
            "status": "already_running",
            "job_id": t.parse_job_id,
            "task_id": t.task_id,
        }

    # Idempotency: same file already parsed
    if (
        t.submission_file_hash == new_hash
        and t.status in ("submissions_ready", "grading", "graded")
    ):
        return {
            "status": "already_done",
            "unchanged": True,
            "job_id": t.parse_job_id,
            "task_id": t.task_id,
            "student_count": len(t.student_data),
        }

    try:
        files_data = await extract_files_from_archive(bytes_, file.filename or "submissions")
    except Exception as e:
        raise HTTPException(400, detail=f"Could not extract archive: {e}")

    if not files_data:
        raise HTTPException(400, detail="No valid student files found in archive.")

    job_id = str(uuid.uuid4())
    task_store.update(
        task_id,
        status="parsing_submissions",
        parse_job_id=job_id,
        submission_file_hash=new_hash,
        submission_file_name=file.filename,
        student_data={},  # clear old data
        error=None,
    )
    asyncio.create_task(_run_parse(t, files_data, provider, job_id, task_store))
    return {
        "status": "started",
        "job_id": job_id,
        "task_id": t.task_id,
        "file_count": len(files_data),
    }


# ─── Reference answers (auxiliary upload — calculation-style problems) ──────
#
# This is an *auxiliary* upload that does NOT advance task.status. A teacher
# can upload (or re-upload, with a different file) at any point — draft,
# problems_ready, submissions_ready, graded — and the parsed answers are
# merged into problem_data[q_id]["reference_answer"]. CalculationSkill picks
# them up on the next grade pass; already-graded tasks must be re-graded
# manually (we do NOT auto-rerun LLM calls when a reference is added).

async def _read_text_for_parse(file: UploadFile, bytes_: bytes) -> str:
    """Decode a PDF / MD / TXT upload into plain text.

    Shared by reference + test-case parsing. Mirrors the logic in
    task_extract_problems so behavior stays consistent across upload paths.
    """
    if file.content_type == "application/pdf" or (file.filename or "").lower().endswith(".pdf"):
        return await extract_text_from_pdf(bytes_)
    return await decode_text_bytes(bytes_)


async def _run_parse_reference(
    task: Task,
    text: str,
    provider,
    job_id: str,
    task_store: TaskStore,
):
    """Background worker for /tasks/{id}/upload_reference.

    Calls the LLM helper, merges results into per-question reference_answer
    fields, then clears reference_parse_job_id so the frontend knows the
    auxiliary parse is done.
    """
    reporter = get_or_create_reporter(job_id)
    try:
        mapping = await parse_reference_to_per_question(
            text=text,
            problems_data=task.problem_data,
            provider=provider,
            reporter=reporter,
        )
        # Merge into problem_data — preserve existing fields.
        for q_id, ref_text in mapping.items():
            if q_id in task.problem_data:
                task.problem_data[q_id]["reference_answer"] = ref_text
        task_store.update(task.task_id, reference_parse_job_id=None, error=None)
        logger.info(
            f"[task:{task.task_id}] reference parse done, matched "
            f"{len(mapping)}/{len(task.problem_data)} problems"
        )
    except Exception as e:
        logger.exception(f"[task:{task.task_id}] reference parse failed")
        task_store.update(
            task.task_id,
            reference_parse_job_id=None,
            error=f"Reference parse failed: {e}",
        )


@router.post("/{task_id}/upload_reference")
async def task_upload_reference(
    task_id: str,
    file: UploadFile = File(...),
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
    registry: ExpertRegistry = Depends(get_expert_registry),
):
    """Upload a reference-answer document (PDF / MD / TXT).

    Teachers can either upload a dedicated solution file, OR re-upload the
    same file as the problems (which triggers the LLM to extract solution
    text from a doc that contains both questions and answers).

    Idempotency: same sha256 → already_done. Concurrent re-upload while parsing
    → already_running.

    The endpoint does NOT change ``task.status`` — reference answers are an
    auxiliary annotation that can be added in any state including ``graded``
    (re-grading is manual).
    """
    t = _get_or_404(task_store, task_id)
    _check_owner(t, current)

    # Need problems to anchor q_ids — uploading reference for a draft (no
    # problems yet) doesn't make sense.
    if t.status == "draft" or not t.problem_data:
        raise HTTPException(
            409, detail="Upload problems first — reference answers are matched per problem."
        )

    bytes_ = await file.read()
    new_hash = hashlib.sha256(bytes_).hexdigest()

    # Idempotency: parsing in flight
    if t.reference_parse_job_id:
        return {
            "status": "already_running",
            "job_id": t.reference_parse_job_id,
            "task_id": t.task_id,
        }

    # Idempotency: same file already merged
    if t.reference_file_hash == new_hash:
        return {
            "status": "already_done",
            "unchanged": True,
            "task_id": t.task_id,
            "reference_file_name": t.reference_file_name,
        }

    provider = registry.pick_default()
    if provider is None:
        raise HTTPException(503, detail="No LLM provider configured. Add an API key first.")

    try:
        text = await _read_text_for_parse(file, bytes_)
    except Exception as e:
        raise HTTPException(400, detail=f"Could not decode file: {e}")

    job_id = str(uuid.uuid4())
    task_store.update(
        task_id,
        reference_file_hash=new_hash,
        reference_file_name=file.filename,
        reference_parse_job_id=job_id,
        error=None,
    )
    asyncio.create_task(_run_parse_reference(t, text, provider, job_id, task_store))
    return {
        "status": "started",
        "job_id": job_id,
        "task_id": t.task_id,
    }


# ─── Test cases (auxiliary upload — programming problems) ──────────────────

async def _run_parse_test_cases(
    task: Task,
    text: str,
    provider,
    job_id: str,
    task_store: TaskStore,
):
    """Background worker for /tasks/{id}/upload_test_cases.

    Mirrors _run_parse_reference. Stores TestCase objects as model_dump()ed
    dicts so JSON round-tripping (via Task.lite() etc.) stays clean.
    """
    reporter = get_or_create_reporter(job_id)
    try:
        mapping = await parse_test_cases_to_per_question(
            text=text,
            problems_data=task.problem_data,
            provider=provider,
            reporter=reporter,
        )
        for q_id, cases in mapping.items():
            if q_id in task.problem_data:
                # Store as list[dict] for JSON serialization compatibility.
                task.problem_data[q_id]["test_cases"] = [tc.model_dump() for tc in cases]
        total = sum(len(v) for v in mapping.values())
        task_store.update(task.task_id, test_cases_parse_job_id=None, error=None)
        logger.info(
            f"[task:{task.task_id}] test-case parse done, "
            f"{len(mapping)} programming problems, {total} cases total"
        )
    except Exception as e:
        logger.exception(f"[task:{task.task_id}] test-case parse failed")
        task_store.update(
            task.task_id,
            test_cases_parse_job_id=None,
            error=f"Test-case parse failed: {e}",
        )


@router.post("/{task_id}/upload_test_cases")
async def task_upload_test_cases(
    task_id: str,
    file: UploadFile = File(...),
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
    registry: ExpertRegistry = Depends(get_expert_registry),
):
    """Upload a test-case document (any format — JSON / Markdown / natural language).

    The LLM normalizes whatever shape into structured stdin/stdout cases keyed
    by q_id. Only programming problems are populated; non-programming
    problems are silently skipped.

    Same idempotency contract as upload_reference (sha256 + parse_job_id).
    Same constraint: does NOT change task.status.
    """
    t = _get_or_404(task_store, task_id)
    _check_owner(t, current)

    if t.status == "draft" or not t.problem_data:
        raise HTTPException(
            409, detail="Upload problems first — test cases are matched per problem."
        )

    bytes_ = await file.read()
    new_hash = hashlib.sha256(bytes_).hexdigest()

    if t.test_cases_parse_job_id:
        return {
            "status": "already_running",
            "job_id": t.test_cases_parse_job_id,
            "task_id": t.task_id,
        }

    if t.test_cases_file_hash == new_hash:
        return {
            "status": "already_done",
            "unchanged": True,
            "task_id": t.task_id,
            "test_cases_file_name": t.test_cases_file_name,
        }

    provider = registry.pick_default()
    if provider is None:
        raise HTTPException(503, detail="No LLM provider configured. Add an API key first.")

    try:
        text = await _read_text_for_parse(file, bytes_)
    except Exception as e:
        raise HTTPException(400, detail=f"Could not decode file: {e}")

    job_id = str(uuid.uuid4())
    task_store.update(
        task_id,
        test_cases_file_hash=new_hash,
        test_cases_file_name=file.filename,
        test_cases_parse_job_id=job_id,
        error=None,
    )
    asyncio.create_task(_run_parse_test_cases(t, text, provider, job_id, task_store))
    return {
        "status": "started",
        "job_id": job_id,
        "task_id": t.task_id,
    }


# ─── Grade ───────────────────────────────────────────────────────────────────

async def _run_grade(
    task: Task,
    registry: ExpertRegistry,
    job_id: str,
    task_store: TaskStore,
    job_store: JobStore,
    language: str,
    multi_sample_n: Optional[int] = None,
):
    reporter = get_or_create_reporter(
        job_id,
        total_students=len(task.student_data),
        total_questions=len(task.problem_data),
    )
    try:
        results = await grade_batch(
            student_store=task.student_data,
            problem_store=task.problem_data,
            registry=registry,
            reporter=reporter,
            language=language,
            task_id=task.task_id,
            multi_sample_n=multi_sample_n,
        )
        # Serialize corrections
        serialized = []
        for r in results:
            corrections_ser = []
            for c in r.get("corrections", []):
                corrections_ser.append(c.model_dump() if hasattr(c, "model_dump") else c)
            serialized.append({
                "student_id": r.get("student_id"),
                "student_name": r.get("student_name"),
                "corrections": corrections_ser,
                "student_answers": r.get("student_answers", []),
            })

        # Mirror into JobStore for backwards compat with /ai_grading/grade_result/{id}
        job = GradingJob(
            job_id=job_id,
            job_name=task.name,
            job_type="batch",
        )
        job_store.create(job)
        job_store.complete(job_id, {
            "results": serialized,
            "task_id": task.task_id,
            "problem_data": task.problem_data,
            "student_data": task.student_data,
            "timestamp": time.time(),
        })

        # ── Pre-bake per-question common-mistakes (D1) ─────────────────────
        # Run sequentially (the user explicitly chose serial over parallel
        # to avoid LLM rework). The deep-dive page is uncached without this.
        # Failures per-question are non-fatal; we log + continue so a single
        # bad LLM call doesn't block the "graded" transition.
        try:
            from backend.api.analytics import _cm_cache
            from backend.agents import analytics_agent
            provider_for_cm = registry.pick_default()
            results_payload = {"results": serialized}
            await reporter._emit_message("正在分析全班易错点…", "info")
            for q_id in task.problem_data.keys():
                try:
                    breakdown = analytics_agent.per_question_breakdown(
                        q_id, results_payload, task.problem_data,
                    )
                    if breakdown.get("rows") and provider_for_cm is not None:
                        out = await analytics_agent.question_common_mistakes(
                            q_id=q_id,
                            breakdown=breakdown,
                            provider=provider_for_cm,
                        )
                        _cm_cache[f"{task.task_id}::{q_id}"] = out.common_mistakes_md
                        await reporter._emit_message(f"易错点完成：{q_id}", "info")
                except Exception as cm_err:
                    logger.warning(
                        f"[task:{task.task_id}] common_mistakes for {q_id} failed: {cm_err}"
                    )
        except Exception as e:
            logger.warning(f"[task:{task.task_id}] common_mistakes pre-bake failed: {e}")

        # Only NOW mark the task as graded — the user's complaint was that
        # "graded" fired before the deep-dive analytics were ready.
        task_store.update(task.task_id, status="graded", error=None)
        logger.info(f"[task:{task.task_id}] grading done, {len(serialized)} students")

    except Exception as e:
        logger.exception(f"[task:{task.task_id}] grading failed")
        task_store.update(task.task_id, status="error", error=str(e))


@router.post("/{task_id}/grade")
async def task_grade(
    task_id: str,
    req: GradeRequest = GradeRequest(),
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
    job_store: JobStore = Depends(get_job_store),
    registry: ExpertRegistry = Depends(get_expert_registry),
):
    t = _get_or_404(task_store, task_id)
    _check_owner(t, current)

    # Status gate
    if t.status == "grading" and t.grading_job_id:
        return {
            "status": "already_running",
            "job_id": t.grading_job_id,
            "task_id": t.task_id,
        }

    if t.status == "graded" and t.grading_job_id:
        return {
            "status": "already_done",
            "job_id": t.grading_job_id,
            "task_id": t.task_id,
        }

    if t.status not in ("submissions_ready", "graded", "error"):
        raise HTTPException(409, detail=f"Cannot grade in status '{t.status}'")

    if not t.problem_data:
        raise HTTPException(409, detail="Task has no problems")

    if not t.student_data:
        raise HTTPException(409, detail="Task has no student submissions")

    if registry.count() == 0:
        raise HTTPException(503, detail="No LLM provider configured.")

    if job_store.active_count() >= 10:
        raise HTTPException(429, detail="Too many concurrent jobs. Try again later.")

    job_id = str(uuid.uuid4())
    task_store.update(
        task_id,
        status="grading",
        grading_job_id=job_id,
        error=None,
    )
    asyncio.create_task(_run_grade(
        t, registry, job_id, task_store, job_store, req.language,
        multi_sample_n=req.multi_sample_n,
    ))

    return {
        "status": "started",
        "job_id": job_id,
        "task_id": t.task_id,
    }


# ─── State / Result ──────────────────────────────────────────────────────────

@router.get("/{task_id}/state")
async def task_state(
    task_id: str,
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
):
    """
    Unified snapshot: task metadata + active reporter progress.
    Frontend polls this single endpoint to drive the entire UI state.
    """
    t = _get_or_404(task_store, task_id)
    _check_owner(t, current)

    out = t.lite()

    # Pick the active job's reporter, if any
    active_job_id: Optional[str] = None
    if t.status == "extracting_problems":
        active_job_id = t.extract_job_id
    elif t.status == "parsing_submissions":
        active_job_id = t.parse_job_id
    elif t.status == "grading":
        active_job_id = t.grading_job_id

    progress = None
    if active_job_id:
        reporter = get_reporter(active_job_id)
        if reporter is not None:
            snap = await reporter.snapshot()
            progress = snap.model_dump()

    out["progress"] = progress
    out["active_job_id"] = active_job_id
    return out


@router.get("/{task_id}/result")
def task_result(
    task_id: str,
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
    job_store: JobStore = Depends(get_job_store),
):
    """Return the grading result if available, else status info."""
    t = _get_or_404(task_store, task_id)
    _check_owner(t, current)

    if t.status != "graded" or not t.grading_job_id:
        return {"status": t.status, "task_id": t.task_id, "error": t.error}

    job = job_store.get(t.grading_job_id)
    if job is None or job.results is None:
        return {"status": "not_found", "task_id": t.task_id}
    return {"status": "completed", "task_id": t.task_id, **(job.results or {})}


# ─── Edit problem (manual stem / rubric refinement) ──────────────────────────

@router.put("/{task_id}/problems/{q_id}")
def update_problem(
    task_id: str,
    q_id: str,
    req: UpdateProblemRequest,
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
):
    """Update a single problem's stem and/or criterion.

    Allowed in any post-extract status (problems_ready through graded). The
    new text is stored verbatim — math delimiters / markdown are preserved
    so the teacher can re-read their edits without re-conversion.

    Returns the updated problem dict.
    """
    t = _get_or_404(task_store, task_id)
    _check_owner(t, current)

    if t.status in ("draft", "extracting_problems"):
        raise HTTPException(409, detail="Problems not extracted yet")

    if q_id not in t.problem_data:
        raise HTTPException(404, detail=f"Problem {q_id} not found")

    # Patch in-place; updates Task.updated_at via TaskStore
    new_problem = dict(t.problem_data[q_id])
    if req.stem is not None:
        new_problem["stem"] = req.stem
    if req.criterion is not None:
        new_problem["criterion"] = req.criterion

    new_problems = dict(t.problem_data)
    new_problems[q_id] = new_problem
    task_store.update(task_id, problem_data=new_problems)
    logger.info(f"[task:{task_id}] problem {q_id} edited by {current.id}")

    return {"status": "ok", "q_id": q_id, "problem": new_problem}


# ─── Edit student answer (manual OCR/segmentation correction) ────────────────

@router.put("/{task_id}/students/{stu_id}/answers/{q_id}")
def update_student_answer(
    task_id: str,
    stu_id: str,
    q_id: str,
    req: UpdateStudentAnswerRequest,
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
):
    """Patch a single (student, question) parsed answer.

    Allowed once submissions are parsed (status `submissions_ready` or any
    later status — it's safe to fix recognition errors even after grading,
    though the existing grade will not auto-rerun).

    Returns the patched answer dict.
    """
    t = _get_or_404(task_store, task_id)
    _check_owner(t, current)

    if t.status in ("draft", "extracting_problems", "problems_ready", "parsing_submissions"):
        raise HTTPException(409, detail="Submissions not parsed yet")

    student = t.student_data.get(stu_id)
    if student is None:
        raise HTTPException(404, detail=f"Student {stu_id} not found")

    answers = student.get("stu_ans") if isinstance(student, dict) else None
    if not isinstance(answers, list):
        raise HTTPException(500, detail="Malformed student data")

    # Locate the matching answer
    target_idx = None
    for i, a in enumerate(answers):
        if isinstance(a, dict) and a.get("q_id") == q_id:
            target_idx = i
            break
    if target_idx is None:
        raise HTTPException(404, detail=f"No answer for q_id={q_id} on student {stu_id}")

    # Patch — copy-on-write so TaskStore's update detects the change
    new_answer = dict(answers[target_idx])
    if req.content is not None:
        new_answer["content"] = req.content
    if req.flag is not None:
        new_answer["flag"] = list(req.flag)

    new_answers = list(answers)
    new_answers[target_idx] = new_answer

    new_student = dict(student)
    new_student["stu_ans"] = new_answers

    new_student_data = dict(t.student_data)
    new_student_data[stu_id] = new_student
    task_store.update(task_id, student_data=new_student_data)
    logger.info(f"[task:{task_id}] student {stu_id} answer for {q_id} edited by {current.id}")

    return {"status": "ok", "stu_id": stu_id, "q_id": q_id, "answer": new_answer}


# ─── Teacher comments (manual annotation on AI corrections) ──────────────────

@router.post("/{task_id}/teacher_comment")
def set_teacher_comment(
    task_id: str,
    req: UpdateTeacherCommentRequest,
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
    job_store: JobStore = Depends(get_job_store),
):
    """Set / update / clear a teacher's manual comment on a graded answer.

    The comment is stored on the matching `Correction` entry in the grading
    job's results dict (mirrored into JobStore on grading completion). It
    coexists with — never replaces — the AI's `comment` field.

    Empty string clears the comment.
    """
    t = _get_or_404(task_store, task_id)
    _check_owner(t, current)

    if t.status != "graded" or not t.grading_job_id:
        raise HTTPException(409, detail=f"Task not graded yet (status={t.status})")

    job = job_store.get(t.grading_job_id)
    if job is None or job.results is None:
        raise HTTPException(404, detail="Grading result not found")

    results = job.results or {}
    students = results.get("results", []) or []
    if not isinstance(students, list):
        raise HTTPException(500, detail="Malformed results payload")

    target_student = None
    for s in students:
        if str(s.get("student_id", "")) == req.student_id:
            target_student = s
            break
    if target_student is None:
        raise HTTPException(404, detail=f"Student {req.student_id} not found in results")

    target_correction = None
    for c in target_student.get("corrections", []) or []:
        if str(c.get("q_id", "")) == req.q_id:
            target_correction = c
            break
    if target_correction is None:
        raise HTTPException(404, detail=f"No correction for q_id={req.q_id} on student {req.student_id}")

    # Mutate in place — JobStore keeps a reference to the dict, so this persists
    # for the lifetime of the in-memory job.
    target_correction["teacher_comment"] = (req.comment or "").strip()
    logger.info(
        f"[task:{task_id}] teacher comment {'cleared' if not req.comment else 'set'} "
        f"on student={req.student_id} q={req.q_id}"
    )
    return {
        "status": "ok",
        "student_id": req.student_id,
        "q_id": req.q_id,
        "teacher_comment": target_correction["teacher_comment"],
    }


@router.get("/{task_id}/teacher_comments")
def list_teacher_comments(
    task_id: str,
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
    job_store: JobStore = Depends(get_job_store),
):
    """Return all teacher comments for a task as a flat dict.

    Keyed by f"{student_id}::{q_id}" — easy for the frontend to merge into
    its TaskState.teacher_comments dict on task load.
    """
    t = _get_or_404(task_store, task_id)
    _check_owner(t, current)

    if t.status != "graded" or not t.grading_job_id:
        return {"comments": {}}

    job = job_store.get(t.grading_job_id)
    if job is None or job.results is None:
        return {"comments": {}}

    out: Dict[str, str] = {}
    for s in job.results.get("results", []) or []:
        sid = str(s.get("student_id", ""))
        for c in s.get("corrections", []) or []:
            qid = str(c.get("q_id", ""))
            tc = c.get("teacher_comment", "")
            if tc:
                out[f"{sid}::{qid}"] = tc
    return {"comments": out}


# ─── Task-scoped knowledge base (RAG MVP) ────────────────────────────────────
#
# Upload a reference document (PDF / MD / TXT) for the current task. The
# backend chunks + embeds it and indexes it in
# `backend.rag.store.InMemoryTaskRetriever` keyed by task_id. Grading skills
# (concept, proof) retrieve from this scope at LLM-call time.
#
# Lifecycle:
#   - Pure in-memory: lost on Render free-tier sleep / restart. The user has
#     accepted this trade-off — it matches the "测一两个 task,退出失效" UX.
#   - Cleaned up on DELETE /tasks/{id} (see delete_task).
#   - Limits: 5 MB / file, 500 chunks / doc, 3 docs / task. See
#     backend/rag/chunker.py and backend/rag/store.py.
#
# Idempotency: same as extract_problems / parse_submissions — sha256(file)
# hash is stored on each KBDoc; uploading the same bytes returns the
# existing doc_id with status "already_done".


@router.post("/{task_id}/kb")
async def task_upload_kb(
    task_id: str,
    file: UploadFile = File(...),
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
    registry: ExpertRegistry = Depends(get_expert_registry),
):
    """Chunk + embed a reference document and add it to this task's KB index."""
    t = _get_or_404(task_store, task_id)
    _check_owner(t, current)

    if registry.count() == 0:
        raise HTTPException(
            503,
            detail="Configure at least one BYOK provider before uploading KB; "
                   "the embedder needs an API key (Zhipu / OpenAI for dense; "
                   "any provider for BM25 fallback).",
        )

    retriever = get_retriever()
    if not isinstance(retriever, InMemoryTaskRetriever):
        raise HTTPException(
            503,
            detail="Task-scoped KB retriever is not active on this deployment.",
        )

    body = await file.read()
    if len(body) > KB_MAX_FILE_BYTES:
        raise HTTPException(
            413,
            detail=f"KB file too large ({len(body)} bytes > {KB_MAX_FILE_BYTES}).",
        )

    sha256 = hashlib.sha256(body).hexdigest()

    # Idempotency: same file already indexed for this task
    existing = retriever.find_doc_by_hash(task_id, sha256)
    if existing is not None:
        return {
            "status": "already_done",
            "task_id": t.task_id,
            "doc_id": existing.doc_id,
            "filename": existing.filename,
            "chunk_count": existing.chunk_count,
        }

    # Extract → chunk
    text = await kb_extract_text(file.filename or "kb.txt", body)
    chunks = chunk_text(text)
    if not chunks:
        raise HTTPException(400, detail="Document produced no usable chunks.")

    # Embed + index. pick_embedder picks zhipu > openai > BM25 from BYOK.
    embedder = pick_embedder(registry)
    doc_id = f"kb_{uuid.uuid4().hex[:10]}"
    try:
        entry = await retriever.add_document(
            task_id=task_id,
            doc_id=doc_id,
            filename=file.filename or "kb.txt",
            sha256=sha256,
            chunks=chunks,
            embedder=embedder,
        )
    except ValueError as e:
        # Limit exceeded / dim mismatch / embedder switch — caller-facing 4xx
        raise HTTPException(409, detail=str(e))
    except Exception as e:
        logger.exception(f"[task:{task_id}] KB embed failed")
        raise HTTPException(502, detail=f"Embedding failed: {e}")

    # Mirror metadata into the Task so frontend can list without hitting the
    # retriever directly.
    new_kb_docs = dict(t.kb_docs)
    new_kb_docs[doc_id] = entry.public()
    task_store.update(task_id, kb_docs=new_kb_docs)
    logger.info(
        f"[task:{task_id}] KB upload doc_id={doc_id} filename={file.filename!r} "
        f"chunks={len(chunks)} embedder={embedder.name}"
    )
    return {
        "status": "started",  # synchronous in MVP, kept for symmetry with other endpoints
        "task_id": t.task_id,
        "doc_id": doc_id,
        "filename": entry.filename,
        "chunk_count": entry.chunk_count,
        "embedder": embedder.name,
    }


@router.get("/{task_id}/kb")
def task_list_kb(
    task_id: str,
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
):
    """Return the metadata for all KB documents currently indexed under this task."""
    t = _get_or_404(task_store, task_id)
    _check_owner(t, current)
    retriever = get_retriever()
    # Source of truth = retriever (Task.kb_docs is just a serialization mirror).
    if isinstance(retriever, InMemoryTaskRetriever):
        return {"docs": retriever.list_docs(task_id)}
    return {"docs": list((t.kb_docs or {}).values())}


@router.delete("/{task_id}/kb/{doc_id}")
def task_delete_kb(
    task_id: str,
    doc_id: str,
    current: User = Depends(require_teacher),
    task_store: TaskStore = Depends(get_task_store),
):
    """Remove a single KB document from this task's index."""
    t = _get_or_404(task_store, task_id)
    _check_owner(t, current)
    retriever = get_retriever()
    removed = False
    if isinstance(retriever, InMemoryTaskRetriever):
        removed = retriever.remove_doc(task_id, doc_id)

    if doc_id in (t.kb_docs or {}):
        new_kb_docs = dict(t.kb_docs)
        new_kb_docs.pop(doc_id, None)
        task_store.update(task_id, kb_docs=new_kb_docs)
        removed = True

    if not removed:
        raise HTTPException(404, detail=f"KB doc {doc_id} not found on task {task_id}")
    return {"status": "success", "doc_id": doc_id}

