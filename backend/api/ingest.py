"""
Ingest API router — thin HTTP layer over agents/ingest_agent.py.

Preserves the exact endpoint paths for frontend compatibility:
  POST /prob_preview/   — upload assignment doc, extract problems
  POST /hw_preview/     — upload student submissions, parse answers
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

from backend.state import get_problem_store, get_student_store
from backend.llm.registry import get_expert_registry, ExpertRegistry
from backend.agents.ingest_agent import extract_problems, parse_student_answers
from backend.tools.file_processing import extract_files_from_archive, decode_text_bytes, extract_text_from_pdf

logger = logging.getLogger(__name__)


# ─── Problem preview router ──────────────────────────────────────────────────

prob_router = APIRouter(prefix="/prob_preview", tags=["prob_preview"])


@prob_router.post("/")
async def handle_problem_upload(
    file: UploadFile = File(...),
    problem_store: Dict = Depends(get_problem_store),
    registry: ExpertRegistry = Depends(get_expert_registry),
):
    """Upload assignment doc → extract and classify problems."""
    logger.info(f"[prob_preview] Received file: {file.filename}, type: {file.content_type}")

    provider = registry.pick_default()
    if provider is None:
        raise HTTPException(status_code=503, detail="No LLM provider configured. Add an API key first.")

    try:
        text_bytes = await file.read()
        if file.content_type == "application/pdf" or file.filename.lower().endswith(".pdf"):
            text = await extract_text_from_pdf(text_bytes)
        else:
            text = await decode_text_bytes(text_bytes)
        logger.info(f"[prob_preview] File decoded, {len(text)} chars")

        result = await extract_problems(text, provider, problem_store)
        logger.info(f"[prob_preview] Extracted {len(result)} problems")
        return JSONResponse(content=result)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[prob_preview] Error processing {file.filename}")
        raise HTTPException(status_code=500, detail=f"Error processing file: {e}")


# ─── Homework preview router ─────────────────────────────────────────────────

hw_router = APIRouter(prefix="/hw_preview", tags=["hw_preview"])


@hw_router.post("/")
async def handle_answer_upload(
    file: UploadFile = File(...),
    problem_store: Dict = Depends(get_problem_store),
    student_store: Dict = Depends(get_student_store),
    registry: ExpertRegistry = Depends(get_expert_registry),
):
    """Upload student submissions (archive or single file) → parse answers."""
    logger.info(f"[hw_preview] Received file: {file.filename}, type: {file.content_type}")

    provider = registry.pick_default()
    if provider is None:
        raise HTTPException(status_code=503, detail="No LLM provider configured. Add an API key first.")

    try:
        file_bytes = await file.read()
        files_data = await extract_files_from_archive(file_bytes, file.filename)

        if not files_data:
            raise HTTPException(status_code=400, detail="No valid text files found in upload.")

        logger.info(f"[hw_preview] Extracted {len(files_data)} files from {file.filename}")

        result = await parse_student_answers(
            files_data=files_data,
            problems_data=problem_store,
            student_store=student_store,
            provider=provider,
        )
        logger.info(f"[hw_preview] Parsed {len(result)} student submissions")
        return result

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"[hw_preview] Error processing {file.filename}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")
