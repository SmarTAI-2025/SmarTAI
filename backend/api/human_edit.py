"""
Human edit API router — allows manual updates to problem data and student answers.

Preserves:
  POST /human_edit/problems   — update problem store
  POST /human_edit/stu_ans    — update student store
"""
from __future__ import annotations

import logging
from typing import Dict

from fastapi import APIRouter, Depends

from backend.state import get_problem_store, get_student_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/human_edit", tags=["human_edit"])


@router.post("/problems")
async def update_problems_data(
    problems_new: Dict[str, Dict[str, str]],
    problems_store: Dict = Depends(get_problem_store),
):
    problems_store.clear()
    problems_store.update(problems_new)
    logger.info(f"[human_edit] Updated {len(problems_new)} problems")
    return {"status": "success", "count": len(problems_new)}


@router.post("/stu_ans")
async def update_stu_ans_data(
    students_new: Dict[str, Dict[str, str]],
    students_store: Dict = Depends(get_student_store),
):
    students_store.clear()
    students_store.update(students_new)
    logger.info(f"[human_edit] Updated {len(students_new)} student answers")
    return {"status": "success", "count": len(students_new)}
