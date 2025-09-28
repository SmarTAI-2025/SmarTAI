import uuid
import asyncio
import logging
import time
from typing import Dict, Any
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from functools import lru_cache
from collections import OrderedDict

from backend.dependencies import get_problem_store, get_student_store
from backend.models import Correction
from backend.correct.calc import calc_node
from backend.correct.concept import concept_node
from backend.correct.proof import proof_node
from backend.correct.programming import programming_node
from backend.dependencies import OPENAI_API_KEY, OPENAI_API_BASE, OPENAI_MODEL

# Setup logger
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/ai_grading",
    tags=["ai_grading"]
)

# Store for grading results with timestamp for cleanup
GRADING_RESULTS: Dict[str, Dict[str, Any]] = OrderedDict()
# Store for completed grading results (history)
HISTORY_RESULTS: Dict[str, Dict[str, Any]] = OrderedDict()
# Maximum number of results to keep
MAX_RESULTS = 1000
# Time to keep results in seconds (24 hours)
RESULT_TTL = 24 * 60 * 60

# Add a function to get all job IDs for debugging
def get_all_job_ids():
    return list(GRADING_RESULTS.keys())

@router.delete("/discard_job/{job_id}")
def discard_job(job_id: str):
    """
    Immediately discard a job (e.g., when user navigates away before completion).
    """
    # Remove from active jobs
    ACTIVE_JOBS.discard(job_id)
    
    # Remove from grading results
    if job_id in GRADING_RESULTS:
        GRADING_RESULTS.pop(job_id, None)
    
    # Remove from job metadata
    if job_id in JOB_METADATA:
        JOB_METADATA.pop(job_id, None)
    
    return {"status": "success", "message": f"Job {job_id} has been discarded."}

# Cache for LLM clients to avoid repeated initialization
LLM_CLIENT_CACHE: Dict[int, Any] = {}
# Timestamps for LLM client cache entries
LLM_CACHE_TIMESTAMPS: Dict[int, float] = {}
# Time to keep LLM clients in cache (1 hour)
LLM_CACHE_TTL = 60 * 60

# Track active grading jobs to prevent overload
ACTIVE_JOBS = set()
MAX_CONCURRENT_JOBS = 10

# Store job metadata for history tracking
JOB_METADATA = OrderedDict()
MAX_METADATA = 1000
METADATA_TTL = 30 * 24 * 60 * 60  # 30 days

async def cleanup_old_metadata():
    """Remove old job metadata to prevent memory leaks."""
    current_time = time.time()
    expired_keys = []
    
    # Find expired metadata
    for job_id, metadata in JOB_METADATA.items():
        timestamp = metadata.get('timestamp', 0)
        time_diff = current_time - timestamp
        logger.info(f"Checking metadata {job_id}: current_time={current_time}, timestamp={timestamp}, time_diff={time_diff}, METADATA_TTL={METADATA_TTL}")
        if time_diff > METADATA_TTL:
            expired_keys.append(job_id)
    
    # Remove expired metadata
    for job_id in expired_keys:
        logger.info(f"Removing expired metadata {job_id}")
        JOB_METADATA.pop(job_id, None)
    
    # Remove excess metadata if we're over the limit
    while len(JOB_METADATA) > MAX_METADATA:
        # Remove the oldest metadata (OrderedDict maintains insertion order)
        removed_item = JOB_METADATA.popitem(last=False)
        logger.info(f"Removed excess metadata {removed_item[0]}")

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
    task_id = id(asyncio.current_task())
    current_time = time.time()
    
    # Clean up expired cache entries
    expired_keys = []
    for tid, timestamp in LLM_CACHE_TIMESTAMPS.items():
        if current_time - timestamp > LLM_CACHE_TTL:
            expired_keys.append(tid)
    
    for tid in expired_keys:
        LLM_CLIENT_CACHE.pop(tid, None)
        LLM_CACHE_TIMESTAMPS.pop(tid, None)
    
    if task_id not in LLM_CLIENT_CACHE:
        # Import here to avoid circular imports
        from langchain_openai import ChatOpenAI
        # API keys are now imported from dependencies.py
        # Use the model from dependencies
        model_name = OPENAI_MODEL if OPENAI_MODEL else "glm-4-plus"
        
        LLM_CLIENT_CACHE[task_id] = ChatOpenAI(
            model=model_name,
            temperature=0.0,
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_API_BASE,
        )
    
    # Update timestamp for this entry
    LLM_CACHE_TIMESTAMPS[task_id] = current_time
    return LLM_CLIENT_CACHE[task_id]

async def process_student_answer(answer: Dict[str, Any], problem_store: Dict[str, Any]) -> Correction:
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
        "stem" :problem.get("stem", ""),
        "text": content,
        "correct_ans": "未提供，请你自行分析题目给出标答！"
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
            correction = await calc_node(answer_unit, rubric, max_score, llm)
        
        elif internal_type == "concept":
            correction = await concept_node(answer_unit, rubric, max_score, llm)

        elif internal_type == "proof":
            # For proof/reasoning questions, parse steps from content
            answer_unit["steps"] = [{"step_no": 1, "content": content}]
            correction = await proof_node(answer_unit, rubric, max_score, llm)

        elif internal_type == "programming":
            answer_unit["code"] = content
            answer_unit["language"] = "python"  # Default language
            answer_unit["test_cases"] = []  # Empty test cases for now
            correction = await programming_node(answer_unit, rubric, max_score, llm)
        
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

async def process_student_submission(student: Dict[str, Any], problem_store: Dict[str, Any]) -> Dict[str, Any]:
    """Process all answers for a single student and return the results."""
    student_id = student.get("stu_id")
    if not student_id:
        return None
        
    logger.info(f"Processing submission for student {student_id}")
    
    corrections = []
    student_answers = student.get("stu_ans", [])
    
    # Process answers concurrently for each student
    tasks = [
        process_student_answer(answer, problem_store) 
        for answer in student_answers
    ]
    
    # Gather all results
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Handle results and exceptions
    for i, result in enumerate(results):
        try:
            if isinstance(result, Exception):
                answer = student_answers[i]
                q_id = answer.get('q_id', 'unknown')
                logger.error(f"Error processing answer {q_id} for student {student_id}: {result}")
                # Add a default correction for failed answers
                corrections.append(Correction(
                    q_id=q_id,
                    type=answer.get("type", "概念题"),
                    score=0.0,
                    max_score=10.0,
                    confidence=0.0,
                    comment=f"Processing error: {str(result)}",
                    steps=[]
                ))
            elif result:
                corrections.append(result)
        except Exception as e:
            answer = student_answers[i]
            q_id = answer.get('q_id', 'unknown')
            logger.error(f"Error handling result for answer {q_id} for student {student_id}: {e}")
            # Add a default correction for failed answers
            corrections.append(Correction(
                q_id=q_id,
                type=answer.get("type", "概念题"),
                score=0.0,
                max_score=10.0,
                confidence=0.0,
                comment=f"Result handling error: {str(e)}",
                steps=[]
            ))
    
    logger.info(f"Completed processing for student {student_id}")
    return {
        "student_id": student_id,
        "corrections": corrections
    }

async def run_grading_task(job_id: str, student_id: str, problem_store: Dict, student_store: Dict[str, Any]):
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
        result = await process_student_submission(student_data, problem_store)

        # Store the results with timestamp
        GRADING_RESULTS[job_id] = {
            "status": "completed",
            "student_id": student_id,
            "corrections": result.get("corrections", []),
            "timestamp": time.time()
        }
        
        # Update job metadata
        if job_id in JOB_METADATA:
            JOB_METADATA[job_id].update({
                "status": "completed",
                "completed_at": time.time(),
                "student_id": student_id
            })
        
        # Move completed results to history storage
        if job_id in GRADING_RESULTS:
            HISTORY_RESULTS[job_id] = GRADING_RESULTS[job_id].copy()
        
        # Only clean up old results after a delay to ensure pending jobs are not removed
        # Clean up will happen in a separate task to avoid blocking the current task
        asyncio.create_task(cleanup_after_delay())
        
        logger.info(f"Grading task {job_id} completed for student {student_id}")
        
    except Exception as e:
        logger.error(f"Error in grading task {job_id}: {e}")
        
        # Store error results with timestamp
        GRADING_RESULTS[job_id] = {
            "status": "error",
            "message": str(e),
            "timestamp": time.time()
        }
        
        # Update job metadata
        if job_id in JOB_METADATA:
            JOB_METADATA[job_id].update({
                "status": "error",
                "completed_at": time.time(),
                "error": str(e)
            })
        
        # Only clean up old results after a delay to ensure pending jobs are not removed
        # Clean up will happen in a separate task to avoid blocking the current task
        asyncio.create_task(cleanup_after_delay())
    finally:
        # Remove job from active jobs
        ACTIVE_JOBS.discard(job_id)

async def cleanup_old_results():
    """Remove old grading results to prevent memory leaks."""
    current_time = time.time()
    expired_keys = []
    
    # Find expired results
    for job_id, result in GRADING_RESULTS.items():
        # Don't clean up pending jobs
        if result.get('status') == 'pending':
            continue
            
        timestamp = result.get('timestamp', 0)
        time_diff = current_time - timestamp
        logger.info(f"Checking job {job_id}: current_time={current_time}, timestamp={timestamp}, time_diff={time_diff}, RESULT_TTL={RESULT_TTL}")
        if time_diff > RESULT_TTL:
            expired_keys.append(job_id)
    
    # Remove expired results
    for job_id in expired_keys:
        logger.info(f"Removing expired job {job_id}")
        GRADING_RESULTS.pop(job_id, None)
    
    # Remove excess results if we're over the limit
    while len(GRADING_RESULTS) > MAX_RESULTS:
        # Remove the oldest result (OrderedDict maintains insertion order)
        # But don't remove pending jobs
        oldest_key = None
        for key in list(GRADING_RESULTS.keys()):
            if GRADING_RESULTS[key].get('status') != 'pending':
                oldest_key = key
                break
        
        if oldest_key is None:
            # All jobs are pending, don't remove any
            break
            
        removed_item = GRADING_RESULTS.popitem(last=False)
        logger.info(f"Removed excess job {removed_item[0]}")

async def cleanup_after_delay():
    """Clean up old results after a delay to ensure pending jobs are not removed immediately."""
    # Wait for 10 seconds before cleaning up to ensure pending jobs are not removed
    await asyncio.sleep(10)
    await cleanup_old_results()
    await cleanup_old_metadata()
    await cleanup_old_history()

async def cleanup_old_history():
    """Remove old history results to prevent memory leaks."""
    current_time = time.time()
    expired_keys = []
    
    # Find expired history results
    for job_id, result in HISTORY_RESULTS.items():
        timestamp = result.get('timestamp', 0)
        time_diff = current_time - timestamp
        logger.info(f"Checking history {job_id}: current_time={current_time}, timestamp={timestamp}, time_diff={time_diff}, RESULT_TTL={RESULT_TTL}")
        if time_diff > RESULT_TTL:
            expired_keys.append(job_id)
    
    # Remove expired history results
    for job_id in expired_keys:
        logger.info(f"Removing expired history {job_id}")
        HISTORY_RESULTS.pop(job_id, None)
    
    # Remove excess history results if we're over the limit
    while len(HISTORY_RESULTS) > MAX_RESULTS:
        # Remove the oldest history result (OrderedDict maintains insertion order)
        removed_item = HISTORY_RESULTS.popitem(last=False)
        logger.info(f"Removed excess history {removed_item[0]}")

async def run_batch_grading_task(job_id: str, problem_store: Dict, student_store: Dict[str, Any]):
    """Run the grading task for all students using parallel processing."""
    logger.info(f"Batch grading task {job_id} started for all students")
    
    try:
        student_count = len(student_store)
        logger.info(f"Found {student_count} students to process")
        
        all_results = []
        
        # Create tasks for all students
        tasks = [
            process_student_submission(student, problem_store)
            for student in student_store.values() 
            if student.get("stu_id")
        ]
        
        # Gather all results
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Handle results and exceptions
        for result in results:
            try:
                if isinstance(result, Exception):
                    logger.error(f"Error processing student: {result}")
                elif result:
                    all_results.append(result)
                    logger.info(f"Processed student result: {result.get('student_id', 'unknown')}")
            except Exception as e:
                logger.error(f"Error handling student result: {e}")
    
        # Store the results with timestamp
        GRADING_RESULTS[job_id] = {
            "status": "completed",
            "results": all_results,
            "timestamp": time.time()
        }
        
        # Update job metadata
        if job_id in JOB_METADATA:
            JOB_METADATA[job_id].update({
                "status": "completed",
                "completed_at": time.time(),
                "student_count": len(all_results)
            })
        
        # Move completed results to history storage
        if job_id in GRADING_RESULTS:
            HISTORY_RESULTS[job_id] = GRADING_RESULTS[job_id].copy()
        
        # Only clean up old results after a delay to ensure pending jobs are not removed
        # Clean up will happen in a separate task to avoid blocking the current task
        asyncio.create_task(cleanup_after_delay())
        
        logger.info(f"Batch grading task {job_id} completed for all students. Processed {len(all_results)} students.")
        
    except Exception as e:
        logger.error(f"Error in batch grading task {job_id}: {e}")
        
        # Store error results with timestamp
        GRADING_RESULTS[job_id] = {
            "status": "error",
            "message": str(e),
            "timestamp": time.time()
        }
        
        # Update job metadata
        if job_id in JOB_METADATA:
            JOB_METADATA[job_id].update({
                "status": "error",
                "completed_at": time.time(),
                "error": str(e)
            })
        
        # Only clean up old results after a delay to ensure pending jobs are not removed
        # Clean up will happen in a separate task to avoid blocking the current task
        asyncio.create_task(cleanup_after_delay())
    finally:
        # Remove job from active jobs
        ACTIVE_JOBS.discard(job_id)

@router.post("/grade_student/")
async def start_grading(request: GradingRequest, 
                  problem_store: Dict[str, Any] = Depends(get_problem_store),
                  student_store: Dict[str, Any] = Depends(get_student_store)):
    """
    Start grading for a specific student.
    """
    # Check if we're at the job limit
    if len(ACTIVE_JOBS) >= MAX_CONCURRENT_JOBS:
        return {
            "status": "error",
            "message": "Too many concurrent grading jobs. Please try again later."
        }
    
    job_id = str(uuid.uuid4())
    GRADING_RESULTS[job_id] = {
        "status": "pending",
        "timestamp": time.time()
    }
    ACTIVE_JOBS.add(job_id)
    
    # Store job metadata
    JOB_METADATA[job_id] = {
        "job_id": job_id,
        "type": "student",
        "student_id": request.student_id,
        "status": "pending",
        "created_at": time.time(),
        "timestamp": time.time()
    }
    
    # Clean up old metadata
    cleanup_old_metadata()
    
    # Start grading in a background task
    asyncio.create_task(run_grading_task(job_id, request.student_id, problem_store, student_store))
    
    return {"job_id": job_id}

@router.post("/grade_all/")
async def start_batch_grading(request: BatchGradingRequest,
                        problem_store: Dict[str, Any] = Depends(get_problem_store),
                        student_store: Dict[str, Any] = Depends(get_student_store)):
    """
    Start grading for all students.
    """
    # Check if we're at the job limit
    if len(ACTIVE_JOBS) >= MAX_CONCURRENT_JOBS:
        return {
            "status": "error",
            "message": "Too many concurrent grading jobs. Please try again later."
        }
    
    job_id = str(uuid.uuid4())
    GRADING_RESULTS[job_id] = {
        "status": "pending",
        "timestamp": time.time()
    }
    ACTIVE_JOBS.add(job_id)
    
    logger.info(f"Created new batch grading job: {job_id}")
    
    # Store job metadata
    JOB_METADATA[job_id] = {
        "job_id": job_id,
        "type": "batch",
        "status": "pending",
        "created_at": time.time(),
        "timestamp": time.time()
    }
    
    # Clean up old metadata
    cleanup_old_metadata()
    
    # Start grading in a background task
    asyncio.create_task(run_batch_grading_task(job_id, problem_store, student_store))
    
    return {"job_id": job_id}

@router.get("/grade_result/{job_id}")
def get_grading_result(job_id: str):
    """
    Get the grading result for a job.
    """
    # Handle mock jobs specially
    if job_id.startswith("MOCK_JOB_"):
        # Return mock data as completed result
        return {
            "status": "completed",
            "message": "Mock job completed successfully",
            "results": []  # Empty results since mock data is handled in frontend
        }
    
    # First check current results
    result = GRADING_RESULTS.get(job_id)
    if result is None:
        # If not found in current results, check history
        result = HISTORY_RESULTS.get(job_id, {"status": "not_found", "message": "Job ID not found in results or history."})
    return result

@router.delete("/reset_all_grading")
def reset_all_grading_results():
    """
    Reset all grading results (except for history records).
    """
    global GRADING_RESULTS, ACTIVE_JOBS
    GRADING_RESULTS = OrderedDict()
    ACTIVE_JOBS.clear()
    return {"status": "success", "message": "All grading results have been reset (history preserved)."}

@router.get("/job_metadata/{job_id}")
def get_job_metadata(job_id: str):
    """
    Get metadata for a specific job (for history tracking).
    """
    # Handle mock jobs specially
    if job_id.startswith("MOCK_JOB_"):
        # Return mock job metadata
        return {
            "job_id": job_id,
            "type": "mock",
            "status": "completed",
            "created_at": "2025-01-01T00:00:00",
            "completed_at": "2025-01-01T00:00:00",
            "message": "Mock job completed successfully"
        }
    
    metadata = JOB_METADATA.get(job_id, {"status": "not_found", "message": "Job ID not found in metadata."})
    return metadata

@router.get("/all_job_metadata")
def get_all_job_metadata():
    """
    Get all job metadata (for history tracking).
    """
    # Get all real job metadata
    all_metadata = dict(JOB_METADATA)
    
    # Check if there are any mock jobs in the session (this would need to be passed from frontend)
    # For now, we'll just return the real metadata
    # In a real implementation, we might want to check if mock data should be included
    
    return all_metadata

@router.get("/all_jobs")
def get_all_jobs():
    """
    Get all job IDs and their statuses for debugging.
    """
    return {job_id: result.get("status", "unknown") for job_id, result in GRADING_RESULTS.items()}

@router.get("/history/{job_id}")
def get_history_result(job_id: str):
    """
    Get a specific history result.
    """
    # Handle mock jobs specially
    if job_id.startswith("MOCK_JOB_"):
        # Return mock data as completed result
        return {
            "status": "completed",
            "message": "Mock job completed successfully",
            "results": []  # Empty results since mock data is handled in frontend
        }
    
    result = HISTORY_RESULTS.get(job_id, {"status": "not_found", "message": "Job ID not found in history."})
    return result

@router.get("/all_history")
def get_all_history():
    """
    Get all history results.
    """
    # Get all real history results
    all_history = dict(HISTORY_RESULTS)
    
    # Check if there are any mock jobs that should be included
    # For now, we'll just return the real history
    # In a real implementation, we might want to check if mock data should be included
    
    return all_history