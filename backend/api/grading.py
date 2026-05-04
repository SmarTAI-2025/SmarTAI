"""
Grading API router — thin HTTP layer over agents/grading_agent.py.

Preserves the exact endpoint paths for frontend compatibility:
  POST   /ai_grading/grade_student/       — grade one student
  POST   /ai_grading/grade_all/           — grade all students
  GET    /ai_grading/grade_result/{id}    — get result
  GET    /ai_grading/progress/{id}        — get fine-grained progress
  GET    /ai_grading/progress/{id}/stream — SSE stream
  DELETE /ai_grading/discard_job/{id}     — discard a job
  DELETE /ai_grading/reset_all_grading    — reset all
  GET    /ai_grading/all_job_metadata     — list jobs
  GET    /ai_grading/job_metadata/{id}    — one job metadata
  GET    /ai_grading/all_jobs             — all job statuses
  GET    /ai_grading/history/{id}         — history entry
  GET    /ai_grading/all_history          — all history
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.config import settings
from backend.state import get_problem_store, get_student_store, get_job_store, JobStore
from backend.models import GradingJob, JobProgress
from backend.llm.registry import get_expert_registry, ExpertRegistry
from backend.agents.grading_agent import grade_student, grade_batch
from backend.progress.tracker import get_or_create_reporter, get_reporter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai_grading", tags=["ai_grading"])


# ─── Request models ──────────────────────────────────────────────────────────

class GradingRequest(BaseModel):
    student_id: str
    language: str = "en"

class BatchGradingRequest(BaseModel):
    language: str = "en"


# ─── Background task runners ─────────────────────────────────────────────────

async def _run_student_grading(
    job_id: str,
    student_id: str,
    problem_store: Dict,
    student_store: Dict,
    registry: ExpertRegistry,
    job_store: JobStore,
    language: str = "en",
):
    reporter = get_or_create_reporter(job_id, total_students=1, total_questions=len(problem_store))
    await reporter.set_phase("grading")
    try:
        student_data = student_store.get(student_id)
        if not student_data:
            job_store.fail(job_id, f"Student {student_id} not found")
            return

        result = await grade_student(
            student_data=student_data,
            problem_store=problem_store,
            registry=registry,
            reporter=reporter,
            language=language,
        )
        # Serialize corrections for storage
        corrections_serialized = []
        for c in result.get("corrections", []):
            corrections_serialized.append(c.model_dump() if hasattr(c, "model_dump") else c)

        job_store.complete(job_id, {
            "status": "completed",
            "student_id": student_id,
            "student_name": result.get("student_name", ""),
            "corrections": corrections_serialized,
            "timestamp": time.time(),
        })
        await reporter.set_phase("done")
        logger.info(f"[grading] Job {job_id} completed for student {student_id}")

    except Exception as e:
        logger.exception(f"[grading] Job {job_id} failed")
        job_store.fail(job_id, str(e))
        await reporter.set_error(str(e))


async def _run_batch_grading(
    job_id: str,
    problem_store: Dict,
    student_store: Dict,
    registry: ExpertRegistry,
    job_store: JobStore,
    language: str = "en",
):
    reporter = get_or_create_reporter(
        job_id, total_students=len(student_store), total_questions=len(problem_store)
    )
    try:
        results = await grade_batch(
            student_store=student_store,
            problem_store=problem_store,
            registry=registry,
            reporter=reporter,
            language=language,
        )
        # Serialize
        serialized = []
        for r in results:
            corrections_ser = []
            for c in r.get("corrections", []):
                corrections_ser.append(c.model_dump() if hasattr(c, "model_dump") else c)
            serialized.append({
                "student_id": r.get("student_id"),
                "student_name": r.get("student_name"),
                "corrections": corrections_ser,
            })

        job_store.complete(job_id, {
            "status": "completed",
            "results": serialized,
            "timestamp": time.time(),
        })
        logger.info(f"[grading] Batch job {job_id} completed, {len(serialized)} students")

    except Exception as e:
        logger.exception(f"[grading] Batch job {job_id} failed")
        job_store.fail(job_id, str(e))
        reporter_obj = get_reporter(job_id)
        if reporter_obj:
            await reporter_obj.set_error(str(e))


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/grade_student/")
async def start_grading(
    request: GradingRequest,
    problem_store: Dict = Depends(get_problem_store),
    student_store: Dict = Depends(get_student_store),
    registry: ExpertRegistry = Depends(get_expert_registry),
    job_store: JobStore = Depends(get_job_store),
):
    if job_store.active_count() >= settings.max_concurrent_jobs:
        return {"status": "error", "message": "Too many concurrent jobs. Try again later."}

    job_id = str(uuid.uuid4())
    job_name = f"Job {time.strftime('%Y-%m-%d %H:%M')}"
    job = GradingJob(job_id=job_id, job_name=job_name, job_type="student", student_id=request.student_id)
    job_store.create(job)

    asyncio.create_task(_run_student_grading(
        job_id, request.student_id, problem_store, student_store, registry, job_store, request.language
    ))
    return {"job_id": job_id}


@router.post("/grade_all/")
async def start_batch_grading(
    request: BatchGradingRequest,
    problem_store: Dict = Depends(get_problem_store),
    student_store: Dict = Depends(get_student_store),
    registry: ExpertRegistry = Depends(get_expert_registry),
    job_store: JobStore = Depends(get_job_store),
):
    if job_store.active_count() >= settings.max_concurrent_jobs:
        return {"status": "error", "message": "Too many concurrent jobs. Try again later."}

    job_id = str(uuid.uuid4())
    job_name = f"Batch Job {time.strftime('%Y-%m-%d %H:%M')}"
    job = GradingJob(job_id=job_id, job_name=job_name, job_type="batch")
    job_store.create(job)

    asyncio.create_task(_run_batch_grading(
        job_id, problem_store, student_store, registry, job_store, request.language
    ))
    return {"job_id": job_id}


@router.get("/grade_result/{job_id}")
def get_grading_result(job_id: str, job_store: JobStore = Depends(get_job_store)):
    if job_id.startswith("MOCK_JOB_"):
        return {"status": "completed", "message": "Mock job", "results": []}

    job = job_store.get(job_id)
    if job is None:
        return {"status": "not_found", "message": f"Job {job_id} not found."}

    if job.status == "completed":
        return {"status": "completed", **(job.results or {})}
    elif job.status == "error":
        return {"status": "error", "message": job.error or "Unknown error"}
    else:
        return {"status": "pending"}


@router.get("/progress/{job_id}")
async def get_progress(job_id: str):
    """Fine-grained progress for frontend polling."""
    reporter = get_reporter(job_id)
    if reporter is None:
        return JobProgress().model_dump()
    snapshot = await reporter.snapshot()
    return snapshot.model_dump()


@router.get("/progress/{job_id}/stream")
async def stream_progress(job_id: str):
    """SSE stream for real-time progress updates."""
    reporter = get_reporter(job_id)
    if reporter is None:
        async def empty():
            yield "data: {\"message\": \"Job not found\"}\n\n"
        return StreamingResponse(empty(), media_type="text/event-stream")

    q = reporter.subscribe()

    async def event_generator():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"data: {event.model_dump_json()}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            reporter.unsubscribe(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.delete("/discard_job/{job_id}")
def discard_job(job_id: str, job_store: JobStore = Depends(get_job_store)):
    job_store.discard(job_id)
    return {"status": "success", "message": f"Job {job_id} discarded."}


class RenameRequest(BaseModel):
    name: str

@router.put("/rename_job/{job_id}")
def rename_job(job_id: str, req: RenameRequest, job_store: JobStore = Depends(get_job_store)):
    job = job_store.get(job_id)
    if job is None:
        return {"status": "error", "message": "Job not found"}
    job_store.update(job_id, job_name=req.name)
    return {"status": "success", "job_name": req.name}


@router.delete("/reset_all_grading")
def reset_all(job_store: JobStore = Depends(get_job_store)):
    job_store.reset_active()
    return {"status": "success", "message": "All active grading results reset."}


@router.get("/job_metadata/{job_id}")
def get_job_metadata(job_id: str, job_store: JobStore = Depends(get_job_store)):
    if job_id.startswith("MOCK_JOB_"):
        return {"job_id": job_id, "type": "mock", "status": "completed"}
    job = job_store.get(job_id)
    if job is None:
        return {"status": "not_found"}
    return {
        "job_id": job.job_id,
        "job_name": job.job_name,
        "type": job.job_type,
        "status": job.status,
        "student_id": job.student_id,
        "created_at": job.created_at,
        "completed_at": job.completed_at,
    }


@router.get("/all_job_metadata")
def get_all_job_metadata(job_store: JobStore = Depends(get_job_store)):
    metadata_list = job_store.list_metadata()
    return {m["job_id"]: m for m in metadata_list}


@router.get("/all_jobs")
def get_all_jobs(job_store: JobStore = Depends(get_job_store)):
    metadata_list = job_store.list_metadata()
    return {m["job_id"]: m["status"] for m in metadata_list}


@router.get("/history/{job_id}")
def get_history_result(job_id: str, job_store: JobStore = Depends(get_job_store)):
    if job_id.startswith("MOCK_JOB_"):
        return {"status": "completed", "message": "Mock job", "results": []}
    job = job_store.get(job_id)
    if job is None:
        return {"status": "not_found"}
    if job.status == "completed" and job.results:
        return {"status": "completed", **job.results}
    return {"status": job.status}


@router.get("/all_history")
def get_all_history(job_store: JobStore = Depends(get_job_store)):
    result = {}
    for m in job_store.list_metadata():
        if m.get("status") == "completed":
            job = job_store.get(m["job_id"])
            if job and job.results:
                result[m["job_id"]] = {"status": "completed", **job.results}
    return result
