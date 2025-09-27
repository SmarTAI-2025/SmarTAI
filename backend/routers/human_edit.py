import logging
from typing import Dict
from fastapi import APIRouter, Depends
from ..dependencies import *
from ..utils import *

# --- 日志和应用基础设置 ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/human_edit",
    tags=["human_edit"]
)

@router.post("/problems")
async def update_problems_data(
    problems_new: Dict[str, Dict[str,str]],
    problems_store: Dict[str, Dict[str,str]] = Depends(get_problem_store)
):
    problems_store.clear()
    problems_store.update(problems_new)
    logger.info(f"更新题目成功！")

@router.post("/stu_ans")
async def update_stu_ans_data(
    students_new: Dict[str, Dict[str,str]],
    students_store: Dict[str, Dict[str,str]] = Depends(get_student_store)
):
    students_store.clear()
    students_store.update(students_new)
    logger.info(f"更新题目学生作答成功！")