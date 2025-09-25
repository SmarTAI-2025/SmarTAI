import os
import io
import logging
import json
import asyncio
from typing import List, Dict, Any
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from langchain_openai import ChatOpenAI
# from langchain_core.pydantic_v1 import BaseModel, Field

from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage
from langchain.schema.document import Document
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.chains.summarize import load_summarize_chain


# from ..dependencies import get_problem_store, get_student_store, get_llm, StudentSubmission
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
def update_problems_data(
    problems_new: Dict[str, Dict[str,str]],
    problems_store: Dict[str, Dict[str,str]] = Depends(get_problem_store)
):
    problems_store.clear()
    problems_store.update(problems_new)
    logger.info(f"更新题目成功！")

@router.post("/stu_ans")
def update_stu_ans_data(
    students_new: Dict[str, Dict[str,str]],
    students_store: Dict[str, Dict[str,str]] = Depends(get_student_store)
):
    students_store.clear()
    students_store.update(students_new)
    logger.info(f"更新题目学生作答成功！")