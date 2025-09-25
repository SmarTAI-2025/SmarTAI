"""
Programming question correction node implementation.
"""
import structlog
import re
import os
import json
import argparse
import subprocess
import tempfile
from typing import Dict, Any, List
from pydantic import BaseModel

from backend.models import Correction, StepScore
from backend.correct.prompt_utils import prepare_programming_prompt
from backend.dependencies import get_llm

# Setup logger
logger = structlog.get_logger()

# Zhipu AI configuration
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "8dcdf3e9238f48f4ae329f638e66dfe2.HHIbfrj5M4GcjM8f")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://open.bigmodel.cn/api/paas/v4")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "glm-4")

# Global LLM client for connection pooling
LLM_CLIENT = None

def get_llm_client():
    """Get or create a shared LLM client for connection pooling."""
    global LLM_CLIENT
    if LLM_CLIENT is None:
        LLM_CLIENT = get_llm()
    return LLM_CLIENT

class TestCase(BaseModel):
    """Model for a test case."""
    input: str
    expected_output: str
    description: str

class ExecutionResult(BaseModel):
    """Model for code execution result."""
    passed: bool
    output: str
    error: str
    logs: str
    pass_rate: float
    coverage: float

class TestCaseGenerator:
    """Mock test case generator."""
    def generate(self) -> List[TestCase]:
        """Generate mock test cases."""
        return [
            TestCase(
                input="",
                expected_output="",
                description="默认测试用例"
            )
        ]

class CodeExecutor:
    """Simple code executor."""
    def run(self, code: str, test_cases: List[TestCase]) -> ExecutionResult:
        """Execute code with test cases."""
        # In a real implementation, you would run the code in a sandbox
        # For now, we'll just return a mock result
        logger.info("executing_code", code_length=len(code), test_cases_count=len(test_cases))
        
        # Mock execution result
        result = ExecutionResult(
            passed=True,
            output="程序执行成功",
            error="",
            logs="执行日志信息",
            pass_rate=1.0,
            coverage=0.8
        )
        
        return result

class AnswerUnit(BaseModel):
    """Model for programming answer unit."""
    q_id: str
    code: str
    language: str
    test_cases: List[TestCase]

def parse_llm_json_response(response_text: str) -> Dict[str, Any]:
    """
    Parse LLM JSON response, handling common formatting issues.
    
    Args:
        response_text: The raw response text from the LLM
        
    Returns:
        Dict[str, Any]: Parsed JSON object
    """
    # Log the raw response for debugging
    logger.info("parsing_llm_response", response_text=response_text[:500] + "..." if len(response_text) > 500 else response_text)
    
    # Use a more robust approach similar to what's in routers/new.py
    # First try to find JSON objects in the response
    import json
    from pydantic import ValidationError
    
    # Match JSON objects or arrays
    match = re.search(r'\{.*\}', response_text, re.DOTALL)
    if not match:
        match = re.search(r'\[.*\]', response_text, re.DOTALL)
    
    if match:
        json_str = match.group(0)
        try:
            # Try to parse the JSON directly
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            # If direct parsing fails, try to fix common issues
            try:
                # Remove trailing commas before closing braces/brackets
                json_str = re.sub(r',(\s*[}\]])', r'\1', json_str)
                # Remove comments
                json_str = re.sub(r'//.*?(?=\n|$)', '', json_str)
                json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)
                # Try parsing again
                return json.loads(json_str)
            except json.JSONDecodeError:
                pass
    
    # If all parsing attempts fail, create a fallback response
    # Extract individual fields with more robust regex patterns
    try:
        llm_response = {}
        
        # Extract score with more flexible pattern matching
        score_match = re.search(r'"score"\s*:\s*([0-9.]+)', response_text)
        if score_match:
            llm_response["score"] = float(score_match.group(1))
        
        # Extract max_score
        max_score_match = re.search(r'"max_score"\s*:\s*([0-9.]+)', response_text)
        if max_score_match:
            llm_response["max_score"] = float(max_score_match.group(1))
        else:
            llm_response["max_score"] = 10.0
        
        # Extract confidence
        confidence_match = re.search(r'"confidence"\s*:\s*([0-9.]+)', response_text)
        if confidence_match:
            llm_response["confidence"] = float(confidence_match.group(1))
        else:
            llm_response["confidence"] = 0.8
        
        # Extract comment (handle escaped quotes and truncated text)
        comment_match = re.search(r'"comment"\s*:\s*"((?:[^"\\]|\\.)*)"', response_text)
        if comment_match:
            # Unescape the string
            comment = comment_match.group(1).replace('\\"', '"').replace('\\\\', '\\')
            llm_response["comment"] = comment
        else:
            # Try to extract comment with more flexible pattern
            flexible_comment_match = re.search(r'"comment"\s*:\s*"([^"]*)"', response_text)
            if flexible_comment_match:
                llm_response["comment"] = flexible_comment_match.group(1)
            else:
                llm_response["comment"] = "AI评分完成"
        
        # Extract logs (handle escaped quotes)
        logs_match = re.search(r'"logs"\s*:\s*"((?:[^"\\]|\\.)*)"', response_text)
        if logs_match:
            # Unescape the string
            logs = logs_match.group(1).replace('\\"', '"').replace('\\\\', '\\')
            llm_response["logs"] = logs
        else:
            llm_response["logs"] = ""
        
        # Initialize empty steps array
        llm_response["steps"] = []
        
        logger.info("manual_json_parsing_success", parsed_keys=list(llm_response.keys()))
        return llm_response
    except Exception as e:
        logger.warning("manual_json_parsing_failed", error=str(e))
    
    # Final fallback
    llm_response = {
        "score": 5.0,
        "max_score": 10.0,
        "confidence": 0.8,
        "comment": "默认评分",
        "steps": [],
        "logs": ""
    }
    
    logger.info("parse_llm_json_response_complete", response_type=type(llm_response).__name__, 
                response_keys=list(llm_response.keys()) if isinstance(llm_response, dict) else "Not a dict")
    return llm_response

def programming_node(answer_unit: Dict[str, Any], rubric: str, max_score: float = 10.0, llm=None) -> Correction:
    """
    Programming question correction node.
    
    Args:
        answer_unit: The answer unit containing the student's code
        rubric: The grading rubric
        max_score: The maximum score for this question
        llm: Optional LLM client to use for processing (if None, uses shared client)
        
    Returns:
        Correction: The correction result
    """
    logger.info("programming_node_start", q_id=answer_unit.get("q_id", "unknown"))
    
    # Convert dict to AnswerUnit model
    # Handle the case where test_cases might not be in the expected format
    if "test_cases" in answer_unit and isinstance(answer_unit["test_cases"], list):
        # Ensure test_cases are in the expected dictionary format
        test_cases = []
        for test_case in answer_unit["test_cases"]:
            if isinstance(test_case, dict):
                test_cases.append(test_case)
            # If test_cases are already in the correct format, keep them as is
        answer_unit["test_cases"] = test_cases
    
    answer_unit_model = AnswerUnit(**answer_unit)
    
    # Step 1: Generate test cases if none provided
    test_cases = answer_unit_model.test_cases
    if not test_cases:
        generator = TestCaseGenerator()
        test_cases = generator.generate()
        logger.info("test_cases_generated", count=len(test_cases))
    
    # Step 2: Execute code with test cases
    executor = CodeExecutor()
    result = executor.run(answer_unit_model.code, test_cases)
    
    # Step 3: Calculate score based on pass rate
    score = max_score * result.pass_rate
    
    # Step 4: Calculate confidence based on pass rate and coverage
    confidence = 0.7 * result.pass_rate + 0.3 * result.coverage
    
    # Step 5: Create step score
    steps = [
        StepScore(
            step_no=1,
            desc=f"代码通过率: {result.pass_rate:.2%}, 覆盖率: {result.coverage:.2%}",
            is_correct=result.passed,
            score=score
        )
    ]
    
    # Step 6: Prepare prompt using the new prompt_utils module
    try:
        template_path = "backend/prompts/programming.txt"
        # For now, we use a placeholder problem statement since we don't have the actual problem
        # In a real implementation, you would get the problem from the problem store
        problem = "编程题"
        test_cases = [{"input": "", "output": ""}]
        prompt = prepare_programming_prompt(template_path, problem, answer_unit_model.code, test_cases, rubric)
        
        # In a real implementation, you would call an LLM with this prompt
        # For now, we'll just log that we would use it
        logger.info("programming_prompt_prepared", prompt=prompt[:100] + "..." if len(prompt) > 100 else prompt)
        
        # Step 7: Call LLM with the prepared prompt using connection pooling
        try:
            # Use provided LLM client or get shared LLM client for connection pooling
            if llm is None:
                llm = get_llm_client()
            from langchain.schema import HumanMessage
            
            # Use invoke method instead of direct call to avoid deprecation warning
            response = llm.invoke([HumanMessage(content=prompt)])
            
            # Log the raw response for debugging
            logger.info("llm_raw_response", content=response.content[:500] + "..." if len(response.content) > 500 else response.content)
            
            # Parse the JSON response
            llm_response = parse_llm_json_response(response.content)
            
            # Create step scores from LLM response
            step_scores = []
            if "steps" in llm_response and isinstance(llm_response["steps"], list):
                for step in llm_response["steps"]:
                    try:
                        if isinstance(step, dict):
                            step_score = StepScore(
                                step_no=step.get("step_no", len(step_scores) + 1),
                                desc=step.get("desc", step.get("comment", f"步骤 {step.get('step_no', len(step_scores) + 1)}")),
                                is_correct=step.get("is_correct", True) if step.get("is_correct") is not None else True,
                                score=step.get("score", 0.0)
                            )
                            step_scores.append(step_score)
                    except Exception as step_creation_error:
                        logger.warning("step_creation_failed", error=str(step_creation_error), step_data=step)
            
            # Calculate total score and confidence
            total_score = llm_response.get("score", sum(step.score for step in step_scores) if step_scores else 5.0)
            overall_confidence = llm_response.get("confidence", 0.8)
            comment = llm_response.get("comment", f"代码通过率: {result.pass_rate:.2%}, 覆盖率: {result.coverage:.2%}")
            response_max_score = llm_response.get("max_score", max_score)  # Use AI response max_score if available
            logs = llm_response.get("logs", result.logs) if "logs" in llm_response else result.logs
            
            # Ensure scores are within valid ranges
            total_score = max(0.0, min(total_score, response_max_score))
            overall_confidence = max(0.0, min(overall_confidence, 1.0))
            
            # Create correction object with LLM results
            try:
                correction = Correction(
                    q_id=answer_unit_model.q_id,
                    type="编程题",
                    score=total_score,
                    max_score=response_max_score,  # Use the AI response max_score
                    confidence=overall_confidence,
                    comment=comment,
                    steps=step_scores,
                    logs=logs
                )
            except Exception as correction_error:
                logger.error("correction_creation_failed", error=str(correction_error), 
                           q_id=answer_unit_model.q_id, type="编程题", score=total_score, 
                           max_score=max_score, confidence=overall_confidence,
                           comment=comment,
                           steps_count=len(step_scores))
                raise
            
            logger.info("programming_node_complete", q_id=answer_unit_model.q_id, score=correction.score, steps_count=len(correction.steps))
            return correction
            
        except Exception as e:
            # Continue with rule-based correction if LLM call fails
            logger.warning("llm_call_failed", error=str(e))
            # Create a simple rule-based correction as fallback
            correction = Correction(
                q_id=answer_unit_model.q_id,
                type="编程题",
                score=score,
                max_score=max_score,
                confidence=confidence,
                comment="LLM调用失败，使用默认评分",
                steps=steps,
                logs=result.logs
            )
            return correction
    except FileNotFoundError:
        logger.warning("programming_prompt_template_not_found", template_path="backend/prompts/programming.txt")
        # Create a simple rule-based correction as fallback with a default prompt
        default_prompt = f"""
        你是一个编程老师，需要对学生的编程题解答进行评分。

        学生代码：
        {answer_unit_model.code}

        执行结果：
        通过率: {result.pass_rate:.2%}
        覆盖率: {result.coverage:.2%}
        输出: {result.output}
        错误: {result.error}

        评分标准：
        {rubric}

        请按照以下格式返回JSON结果：
        {{
            "score": 0-10的分数,
            "max_score": 10,
            "confidence": 0-1的置信度,
            "comment": "评语",
            "steps": [
                {{
                    "step_no": 1,
                    "desc": "步骤描述",
                    "is_correct": true/false,
                    "score": 0-分数
                }}
            ],
            "logs": "执行日志"
        }}
        """
        
        # Try to call LLM with default prompt
        try:
            # Use provided LLM client or get shared LLM client for connection pooling
            if llm is None:
                llm = get_llm_client()
            from langchain.schema import HumanMessage
            
            response = llm.invoke([HumanMessage(content=default_prompt)])
            llm_response = parse_llm_json_response(response.content)
            
            # Create step scores from LLM response
            step_scores = []
            if "steps" in llm_response and isinstance(llm_response["steps"], list):
                for step in llm_response["steps"]:
                    try:
                        if isinstance(step, dict):
                            step_score = StepScore(
                                step_no=step.get("step_no", len(step_scores) + 1),
                                desc=step.get("desc", step.get("comment", f"步骤 {step.get('step_no', len(step_scores) + 1)}")),
                                is_correct=step.get("is_correct", True) if step.get("is_correct") is not None else True,
                                score=step.get("score", 0.0)
                            )
                            step_scores.append(step_score)
                    except Exception as step_creation_error:
                        logger.warning("step_creation_failed", error=str(step_creation_error), step_data=step)
            
            # Calculate total score and confidence
            total_score = llm_response.get("score", sum(step.score for step in step_scores) if step_scores else 5.0)
            overall_confidence = llm_response.get("confidence", 0.8)
            comment = llm_response.get("comment", f"代码通过率: {result.pass_rate:.2%}, 覆盖率: {result.coverage:.2%}")
            response_max_score = llm_response.get("max_score", max_score)  # Use AI response max_score if available
            logs = llm_response.get("logs", result.logs) if "logs" in llm_response else result.logs
            
            # Ensure scores are within valid ranges
            total_score = max(0.0, min(total_score, response_max_score))
            overall_confidence = max(0.0, min(overall_confidence, 1.0))
            
            # Create correction object with LLM results
            correction = Correction(
                q_id=answer_unit_model.q_id,
                type="编程题",
                score=total_score,
                max_score=response_max_score,
                confidence=overall_confidence,
                comment=comment,
                steps=step_scores,
                logs=logs
            )
            return correction
        except Exception as fallback_error:
            logger.warning("fallback_llm_call_failed", error=str(fallback_error))
            # Create a simple rule-based correction as final fallback
            correction = Correction(
                q_id=answer_unit_model.q_id,
                type="编程题",
                score=score,
                max_score=max_score,
                confidence=confidence,
                comment="模板文件未找到，使用默认评分",
                steps=steps,
                logs=result.logs
            )
            return correction
    except Exception as e:
        logger.warning("prompt_preparation_failed", error=str(e))
        # Create a simple rule-based correction as fallback
        correction = Correction(
            q_id=answer_unit_model.q_id,
            type="编程题",
            score=score,
            max_score=max_score,
            confidence=confidence,
            comment=f"提示准备失败: {str(e)}",
            steps=steps,
            logs=result.logs
        )
        return correction

def process_programming_from_files(input_file: str, rubric_file: str, output_file: str, max_score: float = 10.0):
    """
    Process a programming question from input files and write results to output file.
    
    Args:
        input_file: Path to the JSON file containing the answer unit
        rubric_file: Path to the text file containing the grading rubric
        output_file: Path to the output JSON file for the correction result
        max_score: The maximum score for this question
    """
    # Read the answer unit from input file
    with open(input_file, 'r', encoding='utf-8') as f:
        answer_unit = json.load(f)
    
    # Read the rubric from rubric file
    with open(rubric_file, 'r', encoding='utf-8') as f:
        rubric = f.read().strip()
    
    # Process the programming question
    correction = programming_node(answer_unit, rubric, max_score)
    
    # Log the correction result before writing
    logger.info("writing_correction_to_file", output_file=output_file, 
                score=correction.score, max_score=correction.max_score, 
                comment=correction.comment, steps_count=len(correction.steps))
    
    # Write the correction result to output file
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(correction.model_dump_json(indent=2))
    
    logger.info("programming_processing_complete", input_file=input_file, output_file=output_file)
    return correction

if __name__ == "__main__":
    # Set up argument parser for file-based processing
    parser = argparse.ArgumentParser(description="Process programming question from files")
    parser.add_argument("--input", required=True, help="Input JSON file containing answer unit")
    parser.add_argument("--rubric", required=True, help="Rubric text file")
    parser.add_argument("--output", required=True, help="Output JSON file for correction result")
    parser.add_argument("--max-score", type=float, default=10.0, help="Maximum score for this question")
    
    args = parser.parse_args()
    
    try:
        correction = process_programming_from_files(args.input, args.rubric, args.output, args.max_score)
        print(f"Programming question processed successfully. Results written to {args.output}")
        print(f"Score: {correction.score}/{correction.max_score}")
        print(f"Comment: {correction.comment}")
        # Print steps for debugging
        print(f"Steps: {len(correction.steps)}")
        for step in correction.steps:
            print(f"  Step {step.step_no}: {step.desc} (Correct: {step.is_correct}, Score: {step.score})")
        # Print logs for debugging
        print(f"Logs: {correction.logs}")
    except Exception as e:
        logger.error("programming_processing_failed", error=str(e), exc_info=True)
        print(f"Error processing programming question: {e}")
        exit(1)