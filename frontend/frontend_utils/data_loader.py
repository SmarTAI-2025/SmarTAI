"""
数据模型和数据加载器模块

包含学生成绩、作业统计、题目分析等数据类以及AI批改数据加载功能
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
import pandas as pd
import numpy as np
import requests
import streamlit as st
import json
import os

@dataclass
class StudentScore:
    """学生成绩数据类"""
    student_id: str
    student_name: str
    total_score: float
    max_score: float
    submit_time: datetime
    questions: List[Dict[str, Any]] = field(default_factory=list)
    need_review: bool = False
    confidence_score: float = 0.85
    
    @property
    def percentage(self) -> float:
        """计算百分比得分"""
        return (self.total_score / self.max_score) * 100 if self.max_score > 0 else 0
    
    @property
    def grade_level(self) -> str:
        """获取成绩等级"""
        percentage = self.percentage
        if percentage >= 90:
            return "优秀"
        elif percentage >= 80:
            return "良好"
        elif percentage >= 70:
            return "中等"
        elif percentage >= 60:
            return "及格"
        else:
            return "不及格"

@dataclass
class QuestionAnalysis:
    """题目分析数据类"""
    question_id: str
    question_type: str  # concept, calculation, proof, programming
    topic: str
    difficulty: float  # 0-1
    correct_rate: float
    avg_score: float
    max_score: float
    common_errors: List[str] = field(default_factory=list)
    knowledge_points: List[str] = field(default_factory=list)
    
    @property
    def difficulty_level(self) -> str:
        """获取难度等级"""
        if self.difficulty <= 0.3:
            return "简单"
        elif self.difficulty <= 0.6:
            return "中等"
        else:
            return "困难"

@dataclass
class AssignmentStats:
    """作业统计数据类"""
    assignment_id: str
    assignment_name: str
    total_students: int
    submitted_count: int
    avg_score: float
    max_score: float
    min_score: float
    std_score: float
    pass_rate: float
    question_count: int
    create_time: datetime
    
    @property
    def submission_rate(self) -> float:
        """计算提交率"""
        return (self.submitted_count / self.total_students) * 100 if self.total_students > 0 else 0

def check_all_jobs() -> Dict[str, Any]:
    """
    Check all jobs for debugging purposes
    """
    try:
        response = requests.get(
            f"{st.session_state.backend}/ai_grading/all_jobs",
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"error": f"Failed to check jobs: {str(e)}"}

def load_mock_data() -> Dict[str, Any]:
    """
    Load mock data for testing when real data is not available
    """
    try:
        # Try to load from root directory first (where the file actually is)
        mock_data_path = os.path.join(os.path.dirname(__file__), "..", "..", "mock_data.json")
        if not os.path.exists(mock_data_path):
            # Fallback to frontend directory
            mock_data_path = os.path.join(os.path.dirname(__file__), "..", "mock_data.json")
        
        with open(mock_data_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Convert string dates back to datetime objects
        for student in data["student_scores"]:
            if isinstance(student["submit_time"], str):
                student["submit_time"] = datetime.fromisoformat(student["submit_time"])
        
        if isinstance(data["assignment_stats"]["create_time"], str):
            data["assignment_stats"]["create_time"] = datetime.fromisoformat(data["assignment_stats"]["create_time"])
        
        # Convert to proper dataclass objects
        student_scores = []
        for student_data in data["student_scores"]:
            student_scores.append(StudentScore(**student_data))
        
        question_analysis = []
        for question_data in data["question_analysis"]:
            question_analysis.append(QuestionAnalysis(**question_data))
        
        assignment_stats = AssignmentStats(**data["assignment_stats"])
        
        return {
            "student_scores": student_scores,
            "question_analysis": question_analysis,
            "assignment_stats": assignment_stats
        }
    except Exception as e:
        st.error(f"Failed to load mock data: {str(e)}")
        # Return empty data structure instead of error
        return {
            "student_scores": [],
            "question_analysis": [],
            "assignment_stats": None
        }

def load_ai_grading_data(job_id: str) -> Dict[str, Any]:
    """
    从AI批改系统加载实际数据
    """
    try:
        # Debug information
        print(f"Requesting AI grading data for job {job_id}")
        
        # 获取批改结果
        response = requests.get(
            f"{st.session_state.backend}/ai_grading/grade_result/{job_id}",
            timeout=10
        )
        response.raise_for_status()
        result = response.json()
        
        # Debug information
        print(f"Loading AI grading data for job {job_id}")
        print(f"Result status: {result.get('status', 'no status')}")
        print(f"Full result: {result}")
        
        # Check if the job is completed - this is the key fix
        if result.get("status") != "completed":
            # Also check if the result contains data even if status is not explicitly "completed"
            if "results" not in result and "corrections" not in result:
                # If we can't get real data, return mock data
                st.info("这里显示的是【示例模拟任务】！")
                return load_mock_data()
        
        # 映射题目类型：从内部类型到中文显示名称
        type_display_mapping = {
            "concept": "概念题",
            "calculation": "计算题", 
            "proof": "证明题",
            "programming": "编程题"
        }
        
        # 转换AI批改数据为可视化页面所需格式
        if "results" in result:  # Batch grading results
            # 处理批量批改结果
            students_data = []
            all_corrections = []
            
            for student_result in result["results"]:
                student_id = student_result["student_id"]
                corrections = student_result["corrections"]
                all_corrections.extend(corrections)
                
                # 计算学生总分
                total_score = sum(c["score"] for c in corrections)
                max_score = sum(c["max_score"] for c in corrections)
                
                # 转换题目数据
                questions = []
                for correction in corrections:
                    # 直接使用返回的类型，如果已经是中文则直接使用，否则进行映射
                    question_type = correction["type"]
                    if question_type in type_display_mapping:
                        display_type = type_display_mapping[question_type]
                    elif question_type in type_display_mapping.values():
                        display_type = question_type
                    else:
                        display_type = "概念题"  # 默认类型
                    
                    question = {
                        "question_id": correction["q_id"],
                        "question_type": display_type,  # 使用中文显示类型
                        "score": correction["score"],
                        "max_score": correction["max_score"],
                        "confidence": correction["confidence"],
                        "feedback": correction["comment"],
                        "knowledge_points": correction.get("hits", []),
                        "step_analysis": [
                            {
                                "step_number": step["step_no"],
                                "step_title": f"步骤 {step['step_no']}",
                                "is_correct": step.get("is_correct", True),
                                "points_earned": step["score"],
                                "max_points": correction["max_score"] / len(correction.get("steps", [1])),
                                "feedback": step.get("desc", ""),
                                "error_type": None if step.get("is_correct", True) else "逻辑错误"
                            }
                            for step in correction.get("steps", [])
                        ]
                    }
                    questions.append(question)
                
                # 创建StudentScore对象
                student_score = StudentScore(
                    student_id=student_id,
                    student_name=f"学生{student_id}",  # 实际应用中应从学生数据获取真实姓名
                    total_score=total_score,
                    max_score=max_score,
                    submit_time=datetime.now(),
                    questions=questions,
                    confidence_score=np.mean([q["confidence"] for q in questions]) if questions else 0.85
                )
                students_data.append(student_score)
            
            # 生成题目分析数据
            question_analysis = []
            question_stats = {}
            
            # 统计每道题的正确率等信息
            for correction in all_corrections:
                q_id = correction["q_id"]
                if q_id not in question_stats:
                    question_stats[q_id] = {
                        "total_score": 0,
                        "max_score": 0,
                        "count": 0,
                        "correct_count": 0
                    }
                
                question_stats[q_id]["total_score"] += correction["score"]
                question_stats[q_id]["max_score"] += correction["max_score"]
                question_stats[q_id]["count"] += 1
                if correction["score"] >= correction["max_score"] * 0.6:  # 简单定义正确
                    question_stats[q_id]["correct_count"] += 1
            
            # 创建QuestionAnalysis对象
            for q_id, stats in question_stats.items():
                avg_score = stats["total_score"] / stats["count"] if stats["count"] > 0 else 0
                max_score = stats["max_score"] / stats["count"] if stats["count"] > 0 else 10
                correct_rate = stats["correct_count"] / stats["count"] if stats["count"] > 0 else 0
                difficulty = 1 - correct_rate  # 简单反向映射
                
                # 获取题目类型
                # 查找该题目的类型（从统计信息中获取第一个匹配的）
                question_type = "概念题"  # 默认类型
                for correction in all_corrections:
                    if correction["q_id"] == q_id:
                        question_type = correction["type"]
                        if question_type in type_display_mapping:
                            question_type = type_display_mapping[question_type]
                        elif question_type not in type_display_mapping.values():
                            question_type = "概念题"  # 默认类型
                        break
                
                analysis = QuestionAnalysis(
                    question_id=q_id,
                    question_type=question_type,  # 使用中文显示类型
                    topic=f"题目{q_id}",
                    difficulty=difficulty,
                    correct_rate=correct_rate,
                    avg_score=avg_score,
                    max_score=max_score,
                    common_errors=["计算错误", "概念理解不准确"][:2],
                    knowledge_points=[f"知识点{np.random.randint(1, 5)}" for _ in range(np.random.randint(1, 3))]
                )
                question_analysis.append(analysis)
            
            # 生成作业统计数据
            total_students = len(students_data)
            submitted_count = total_students
            scores = [s.total_score for s in students_data]
            
            assignment_stats = AssignmentStats(
                assignment_id="AI_GRADING_JOB",
                assignment_name="AI自动批改作业",
                total_students=total_students,
                submitted_count=submitted_count,
                avg_score=np.mean(scores) if scores else 0,
                max_score=max(scores) if scores else 0,
                min_score=min(scores) if scores else 0,
                std_score=np.std(scores) if scores else 0,
                pass_rate=(len([s for s in students_data if s.percentage >= 60]) / total_students * 100) if total_students > 0 else 0,
                question_count=len(question_analysis),
                create_time=datetime.now()
            )
            
            return {
                "student_scores": students_data,
                "question_analysis": question_analysis,
                "assignment_stats": assignment_stats
            }
            
        elif "corrections" in result:  # Single student grading results
            # 处理单个学生批改结果
            corrections = result["corrections"]
            
            # 计算学生总分
            total_score = sum(c["score"] for c in corrections)
            max_score = sum(c["max_score"] for c in corrections)
            
            # 转换题目数据
            questions = []
            for correction in corrections:
                # 直接使用返回的类型，如果已经是中文则直接使用，否则进行映射
                question_type = correction["type"]
                if question_type in type_display_mapping:
                    display_type = type_display_mapping[question_type]
                elif question_type in type_display_mapping.values():
                    display_type = question_type
                else:
                    display_type = "概念题"  # 默认类型
                
                question = {
                    "question_id": correction["q_id"],
                    "question_type": display_type,  # 使用中文显示类型
                    "score": correction["score"],
                    "max_score": correction["max_score"],
                    "confidence": correction["confidence"],
                    "feedback": correction["comment"],
                    "knowledge_points": correction.get("hits", []),
                    "step_analysis": [
                        {
                            "step_number": step["step_no"],
                            "step_title": f"步骤 {step['step_no']}",
                            "is_correct": step.get("is_correct", True),
                            "points_earned": step["score"],
                            "max_points": correction["max_score"] / len(correction.get("steps", [1])),
                            "feedback": step.get("desc", ""),
                            "error_type": None if step.get("is_correct", True) else "逻辑错误"
                        }
                        for step in correction.get("steps", [])
                    ]
                }
                questions.append(question)
            
            # 创建StudentScore对象
            student_score = StudentScore(
                student_id=result.get("student_id", "unknown"),
                student_name=f"学生{result.get('student_id', 'unknown')}",
                total_score=total_score,
                max_score=max_score,
                submit_time=datetime.now(),
                questions=questions,
                confidence_score=np.mean([q["confidence"] for q in questions]) if questions else 0.85
            )
            
            # 生成题目分析数据（基于单个学生数据，统计意义有限）
            question_analysis = []
            for correction in corrections:
                # 获取题目类型
                question_type = correction["type"]
                if question_type in type_display_mapping:
                    display_type = type_display_mapping[question_type]
                elif question_type in type_display_mapping.values():
                    display_type = question_type
                else:
                    display_type = "概念题"  # 默认类型
                
                analysis = QuestionAnalysis(
                    question_id=correction["q_id"],
                    question_type=display_type,  # 使用中文显示类型
                    topic=f"题目{correction['q_id']}",
                    difficulty=1 - (correction["score"] / correction["max_score"]),
                    correct_rate=correction["score"] / correction["max_score"],
                    avg_score=correction["score"],
                    max_score=correction["max_score"],
                    common_errors=["计算错误", "概念理解不准确"][:2],
                    knowledge_points=correction.get("hits", [f"知识点{np.random.randint(1, 5)}"])
                )
                question_analysis.append(analysis)
            
            # 生成作业统计数据
            assignment_stats = AssignmentStats(
                assignment_id="AI_GRADING_JOB",
                assignment_name="AI自动批改作业",
                total_students=1,
                submitted_count=1,
                avg_score=total_score,
                max_score=max_score,
                min_score=total_score,
                std_score=0,
                pass_rate=100 if (total_score / max_score) >= 0.6 else 0,
                question_count=len(corrections),
                create_time=datetime.now()
            )
            
            return {
                "student_scores": [student_score],
                "question_analysis": question_analysis,
                "assignment_stats": assignment_stats
            }
        
        # If we can't get real data, return mock data
        st.info("这里显示的是【示例模拟任务】！")
        return load_mock_data()
        
    except Exception as e:
        # If there's an error, return mock data
        st.warning(f"加载AI批改数据失败: {str(e)}，显示模拟数据")
        return load_mock_data()