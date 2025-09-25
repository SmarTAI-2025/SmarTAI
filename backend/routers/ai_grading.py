import time
import uuid
import threading
import logging
import concurrent.futures
from typing import Dict, List, Any
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from functools import lru_cache

from backend.dependencies import get_problem_store, get_student_store, get_llm
from backend.models import Correction
from backend.correct.calc import calc_node
from backend.correct.concept import concept_node
from backend.correct.proof import proof_node
from backend.correct.programming import programming_node

# Setup logger
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/ai_grading",
    tags=["ai_grading"]
)

# Store for grading results
GRADING_RESULTS: Dict[str, Any] = {}

# Add a function to get all job IDs for debugging
def get_all_job_ids():
    return list(GRADING_RESULTS.keys())

# Cache for LLM clients to avoid repeated initialization
LLM_CLIENT_CACHE: Dict[int, Any] = {}

# Cache for processed rubrics to avoid redundant processing
@lru_cache(maxsize=128)
def get_processed_rubric(q_id: str, rubric_text: str) -> str:
    """Cache processed rubrics to avoid redundant processing."""
    # In a real implementation, this could do more complex processing
    # For now, we just return the rubric as-is but cache it
    return rubric_text

class GradingRequest(BaseModel):
    student_id: str

class BatchGradingRequest(BaseModel):
    # Empty for now, but could include options for batch grading
    pass

def get_cached_llm():
    """Get a cached LLM client instance to avoid repeated initialization."""
    thread_id = threading.get_ident()
    if thread_id not in LLM_CLIENT_CACHE:
        # Import here to avoid circular imports
        from langchain_openai import ChatOpenAI
        import os
        OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "66ea05a8d4484dbd98063dbde387149d.pCG80vNPAyKrdmBq")
        OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://open.bigmodel.cn/api/paas/v4")
        OPENAI_MODEL = os.getenv("OPENAI_MODEL", "glm-4-plus")
        
        LLM_CLIENT_CACHE[thread_id] = ChatOpenAI(
            model=OPENAI_MODEL,
            temperature=0.0,
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_API_BASE,
        )
    return LLM_CLIENT_CACHE[thread_id]

def process_student_answer(answer: Dict[str, Any], problem_store: Dict[str, Any]) -> Correction:
    """Process a single student answer and return the correction result."""
    q_id = answer.get("q_id")
    answer_type = answer.get("type")
    content = answer.get("content")
    
    # Get the problem rubric
    problem = problem_store.get(q_id)
    if not problem:
        logger.warning(f"Problem {q_id} not found in problem store")
        # Create a default correction for missing problems
        return Correction(
            q_id=q_id,
            type=answer_type or "概念题",
            score=0.0,
            max_score=10.0,
            confidence=0.0,
            comment=f"Problem {q_id} not found",
            steps=[]
        )
        
    # Use cached rubric processing
    rubric_raw = problem.get("criterion", "")
    rubric = get_processed_rubric(q_id, rubric_raw)
    max_score = 10.0  # Default max score
    
    # Prepare answer unit based on type
    answer_unit = {
        "q_id": q_id,
        "text": content
    }
    
    # Map Chinese question types to internal English types for processing
    type_mapping = {
        "概念题": "concept",
        "其他": "concept",
        "其它": "concept",
        "计算题": "calculation", 
        "证明题": "proof",
        "推理题": "proof", # 推理题和证明题可以使用相同的处理节点
        "编程题": "programming"
    }
    
    # Get internal type for processing
    internal_type = type_mapping.get(answer_type, "concept")
    
    # Get cached LLM client
    llm = get_cached_llm()
    
    # Call the appropriate correction node based on question type
    try:
        correction = None
        if internal_type == "calculation":
            # For calculation questions, we need to parse steps
            answer_unit["steps"] = [{"step_no": 1, "content": content, "formula": ""}]
            correction = calc_node(answer_unit, rubric, max_score, llm)
        
        elif internal_type == "concept":
            correction = concept_node(answer_unit, rubric, max_score, llm)

        elif internal_type == "proof":
            # For proof/reasoning questions, parse steps from content
            answer_unit["steps"] = [{"step_no": 1, "content": content}]
            correction = proof_node(answer_unit, rubric, max_score, llm)

        elif internal_type == "programming":
            answer_unit["code"] = content
            answer_unit["language"] = "python"  # Default language
            answer_unit["test_cases"] = []  # Empty test cases for now
            correction = programming_node(answer_unit, rubric, max_score, llm)
        
        else:
            # For other types, create a default correction
            return Correction(
                q_id=q_id,
                type=answer_type,
                score=5.0,
                max_score=max_score,
                confidence=0.5,
                comment=f"Unsupported question type: {answer_type}",
                steps=[]
            )
        
        # Ensure the type in the correction is the original Chinese type
        if correction:
            correction.type = answer_type
        return correction

    except Exception as e:
        logger.error(f"Error grading question {q_id}: {e}")
        # Create a default correction for errors
        return Correction(
            q_id=q_id,
            type=answer_type,
            score=0.0,
            max_score=max_score,
            confidence=0.0,
            comment=f"Grading error: {str(e)}",
            steps=[]
        )

def process_student_submission(student: Dict[str, Any], problem_store: Dict[str, Any]) -> Dict[str, Any]:
    """Process all answers for a single student and return the results."""
    student_id = student.get("stu_id")
    if not student_id:
        return None
        
    logger.info(f"Processing submission for student {student_id}")
    
    corrections = []
    student_answers = student.get("stu_ans", [])
    
    # Use ThreadPoolExecutor to process answers in parallel for each student
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor: # Increased workers slightly
        future_to_answer = {
            executor.submit(process_student_answer, answer, problem_store): answer 
            for answer in student_answers
        }
        
        for future in concurrent.futures.as_completed(future_to_answer):
            try:
                correction = future.result()
                if correction:
                    corrections.append(correction)
            except Exception as e:
                answer = future_to_answer[future]
                q_id = answer.get('q_id', 'unknown')
                logger.error(f"Error processing answer {q_id} for student {student_id}: {e}")
                # Add a default correction for failed answers
                corrections.append(Correction(
                    q_id=q_id,
                    type=answer.get("type", "概念题"),
                    score=0.0,
                    max_score=10.0,
                    confidence=0.0,
                    comment=f"Processing error: {str(e)}",
                    steps=[]
                ))
    
    logger.info(f"Completed processing for student {student_id}")
    return {
        "student_id": student_id,
        "corrections": corrections
    }

# MODIFICATION: Changed student_store type from List to Dict
def run_grading_task(job_id: str, student_id: str, problem_store: Dict, student_store: Dict[str, Any]):
    """Run the grading task for a specific student."""
    logger.info(f"Grading task {job_id} started for student {student_id}")
    
    try:
        # MODIFICATION: Changed from list iteration to direct dictionary lookup for O(1) efficiency.
        student_data = student_store.get(student_id)
        
        if not student_data:
            logger.error(f"Student {student_id} not found in student store")
            GRADING_RESULTS[job_id] = {
                "status": "error",
                "message": f"Student {student_id} not found"
            }
            return
            
        # Process the student's submission using the existing parallel function
        result = process_student_submission(student_data, problem_store)

        # Store the results
        GRADING_RESULTS[job_id] = {
            "status": "completed",
            "student_id": student_id,
            "corrections": result.get("corrections", [])
        }
        
        logger.info(f"Grading task {job_id} completed for student {student_id}")
        
    except Exception as e:
        logger.error(f"Error in grading task {job_id}: {e}")
        GRADING_RESULTS[job_id] = {
            "status": "error",
            "message": str(e)
        }

# MODIFICATION: Changed student_store type from List to Dict
def run_batch_grading_task(job_id: str, problem_store: Dict, student_store: Dict[str, Any]):
    """Run the grading task for all students using parallel processing."""
    logger.info(f"Batch grading task {job_id} started for all students")
    
    try:
        student_count = len(student_store)
        logger.info(f"Found {student_count} students to process")
        
        all_results = []
        
        # Increased max_workers for better parallelization across students
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            # MODIFICATION: Iterate over dictionary values() instead of the list itself.
            future_to_student = {
                executor.submit(process_student_submission, student, problem_store): student 
                for student in student_store.values() if student.get("stu_id")
            }
            
            for future in concurrent.futures.as_completed(future_to_student):
                try:
                    result = future.result()
                    if result:
                        all_results.append(result)
                        logger.info(f"Processed student result: {result.get('student_id', 'unknown')}")
                except Exception as e:
                    student = future_to_student[future]
                    student_id = student.get("stu_id", "unknown")
                    logger.error(f"Error processing student {student_id}: {e}")
    
        # Store the results
        GRADING_RESULTS[job_id] = {
            "status": "completed",
            "results": all_results
        }
        
        logger.info(f"Batch grading task {job_id} completed for all students. Processed {len(all_results)} students.")
        
    except Exception as e:
        logger.error(f"Error in batch grading task {job_id}: {e}")
        GRADING_RESULTS[job_id] = {
            "status": "error",
            "message": str(e)
        }

@router.post("/grade_student/")
# MODIFICATION: Changed student_store type hint from List to Dict
def start_grading(request: GradingRequest, 
                  problem_store: Dict[str, Any] = Depends(get_problem_store),
                  student_store: Dict[str, Any] = Depends(get_student_store)):
    """
    Start grading for a specific student.
    """
    job_id = str(uuid.uuid4())
    GRADING_RESULTS[job_id] = {"status": "pending"}
    
    # Start grading in a background thread
    thread = threading.Thread(
        target=run_grading_task, 
        args=(job_id, request.student_id, problem_store, student_store)
    )
    thread.start()
    
    return {"job_id": job_id}

@router.post("/grade_all/")
# MODIFICATION: Changed student_store type hint from List to Dict
def start_batch_grading(request: BatchGradingRequest,
                        problem_store: Dict[str, Any] = Depends(get_problem_store),
                        student_store: Dict[str, Any] = Depends(get_student_store)):
    """
    Start grading for all students.
    """
    job_id = str(uuid.uuid4())
    GRADING_RESULTS[job_id] = {"status": "pending"}
    
    logger.info(f"Created new batch grading job: {job_id}")
    
    # Start grading in a background thread
    thread = threading.Thread(
        target=run_batch_grading_task, 
        args=(job_id, problem_store, student_store)
    )
    thread.start()
    
    return {"job_id": job_id}

@router.get("/grade_result/{job_id}")
def get_grading_result(job_id: str):
    """
    Get the grading result for a job.
    """
    result = GRADING_RESULTS.get(job_id, {"status": "not_found", "message": "Job ID not found in results."})
    return result

@router.get("/all_jobs")
def get_all_jobs():
    """
    Get all job IDs and their statuses for debugging.
    """
    return {job_id: result.get("status", "unknown") for job_id, result in GRADING_RESULTS.items()}