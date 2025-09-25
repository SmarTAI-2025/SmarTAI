"""
Utility functions for preparing prompts for AI grading.
"""
import os
import structlog
from typing import List, Dict, Any

logger = structlog.get_logger()


def load_prompt_template(template_path: str) -> str:
    """
    Load a prompt template from a file.
    
    Args:
        template_path: Path to the prompt template file
        
    Returns:
        str: The prompt template content
        
    Raises:
        FileNotFoundError: If the template file is not found
    """
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.error("prompt_template_not_found", template_path=template_path)
        raise


def prepare_concept_prompt(template_path: str, context: List[str], problem: str, answer: str, rubric: str) -> str:
    """
    Prepare a prompt for concept question grading.
    
    Args:
        template_path: Path to the prompt template file
        context: List of relevant knowledge points
        problem: The problem statement
        answer: The student's answer
        rubric: The grading rubric
        
    Returns:
        str: The prepared prompt
    """
    template = load_prompt_template(template_path)
    context_str = "\n".join(context)
    
    # Replace the placeholders carefully to avoid JSON format issues
    prompt = template.replace("{context}", context_str)
    prompt = prompt.replace("{problem}", problem)
    prompt = prompt.replace("{answer}", answer)
    prompt = prompt.replace("{rubric}", rubric)
    
    return prompt


def prepare_calc_prompt(template_path: str, problem: str, student_answer: str, correct_answer: str, rubric: str) -> str:
    """
    Prepare a prompt for calculation question grading.
    
    Args:
        template_path: Path to the prompt template file
        problem: The problem statement
        student_answer: The student's numerical answer
        correct_answer: The correct numerical answer
        rubric: The grading rubric
        
    Returns:
        str: The prepared prompt
    """
    template = load_prompt_template(template_path)
    
    # Format prompt
    prompt = template.replace("{problem}", problem)
    prompt = prompt.replace("{answer}", student_answer)
    prompt = prompt.replace("{correct_answer}", str(correct_answer))
    prompt = prompt.replace("{rubric}", rubric)
    
    return prompt


def prepare_proof_prompt(template_path: str, steps: List[Dict[str, Any]], rubric: str) -> str:
    """
    Prepare a prompt for proof question grading.
    
    Args:
        template_path: Path to the prompt template file
        steps: List of proof steps
        rubric: The grading rubric
        
    Returns:
        str: The prepared prompt
    """
    template = load_prompt_template(template_path)
    
    # Format prompt
    steps_str = "\n".join([f"步骤{i+1}: {step['content']}" for i, step in enumerate(steps)])
    prompt = template.replace("{steps}", steps_str)
    prompt = prompt.replace("{rubric}", rubric)
    
    return prompt


def prepare_programming_prompt(template_path: str, problem: str, code: str, test_cases: List[Dict[str, str]], rubric: str) -> str:
    """
    Prepare a prompt for programming question grading.
    
    Args:
        template_path: Path to the prompt template file
        problem: The problem statement
        code: The student's code
        test_cases: List of test cases
        rubric: The grading rubric
        
    Returns:
        str: The prepared prompt
    """
    template = load_prompt_template(template_path)
    
    # Format prompt
    test_cases_str = "\n".join([f"输入: {tc['input']}, 期望输出: {tc['output']}" for tc in test_cases])
    prompt = template.replace("{problem}", problem if problem else "未提供题目描述")
    prompt = template.replace("{code}", code)
    prompt = template.replace("{test_cases}", test_cases_str)
    prompt = template.replace("{rubric}", rubric)
    
    return prompt