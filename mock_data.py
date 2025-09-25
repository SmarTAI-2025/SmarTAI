"""
Mock data generation for testing frontend pages before actual grading is performed.
"""

import random
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any

def generate_mock_student_scores(num_students: int = 40) -> List[Dict[str, Any]]:
    """Generate mock student score data for testing with a more realistic distribution."""
    students = []
    
    # Question types for variety
    question_types = ["概念题", "计算题", "证明题", "编程题"]
    
    # Student names (just for variety)
    student_names = [f"学生{str(i).zfill(4)}" for i in range(1, num_students + 1)]
    student_ids = [f"PB2024{str(i).zfill(4)}" for i in range(1, num_students + 1)]
    
    # Generate scores following a normal distribution
    # Mean around 80-85 with standard deviation to create realistic spread
    # This will result in approximately 10% failure rate (scores below 60)
    mean_score = random.uniform(80, 85)
    std_dev = random.uniform(8, 12)
    
    for i in range(num_students):
        # Generate a score based on normal distribution
        # Using random.gauss for normal distribution
        percentage_score = max(0, min(100, random.gauss(mean_score, std_dev)))
        
        # Ensure we have a low failure rate by adjusting very low scores
        if percentage_score < 60:
            # Only about 10% should fail, so boost most failing scores to passing
            if random.random() > 0.1:  # 90% chance to boost to passing
                percentage_score = random.uniform(60, 70)
        
        # Generate random questions for each student
        questions = []
        num_questions = random.randint(3, 6)
        
        # Calculate total max score first
        total_max_score = sum(random.choice([5, 10, 15, 20]) for _ in range(num_questions))
        
        # Calculate actual total score based on the percentage
        total_score = round(total_max_score * percentage_score / 100, 1)
        
        # Distribute the total score across questions
        remaining_score = total_score
        remaining_questions = num_questions
        
        for q in range(num_questions):
            # For the last question, assign all remaining score
            if remaining_questions == 1:
                score = round(remaining_score, 1)
            else:
                # Distribute score proportionally with some randomness
                max_score = random.choice([5, 10, 15, 20])
                # Calculate proportion of remaining score to assign
                proportion = max_score / (total_max_score * (remaining_questions - 1)) if remaining_questions > 1 else 1
                score = round(min(remaining_score, max_score * (0.7 + 0.6 * random.random())), 1)
            
            max_score = random.choice([5, 10, 15, 20])
            # Ensure score doesn't exceed max_score
            score = min(score, max_score)
            
            # Update remaining values
            remaining_score = max(0, remaining_score - score)
            remaining_questions -= 1
            
            # Generate step analysis for each question
            steps = []
            num_steps = random.randint(2, 5)
            step_max_score = max_score / num_steps
            
            # Calculate question percentage based on overall percentage with some variation
            question_percentage = max(0, min(100, percentage_score + random.uniform(-10, 10)))
            
            for s in range(num_steps):
                # Distribute score across steps
                if s == num_steps - 1:
                    step_score = round(max(0, score - sum(step["points_earned"] for step in steps)), 1)
                else:
                    step_score = round(max(0, min(step_max_score, score * (0.7 + 0.6 * random.random()) / num_steps)), 1)
                
                steps.append({
                    "step_number": s + 1,
                    "step_title": f"步骤 {s + 1}",
                    "is_correct": step_score / step_max_score > 0.6,  # More correct steps for higher scores
                    "points_earned": max(0, step_score),
                    "max_points": round(step_max_score, 1),
                    "feedback": f"这是第{s + 1}步的反馈信息",
                    "error_type": None if step_score / step_max_score > 0.6 else random.choice(["计算错误", "概念理解错误", "逻辑错误"])
                })
            
            questions.append({
                "question_id": f"Q{q + 1}",
                "question_type": random.choice(question_types),
                "score": max(0, score),
                "max_score": max_score,
                "confidence": round(random.uniform(0.7, 1.0), 2),
                "feedback": f"这是一道{questions[-1]['question_type'] if questions else '题目'}，学生解答{'正确' if score/max_score > 0.7 else '基本正确' if score/max_score > 0.5 else '需要改进'}。",
                "knowledge_points": [f"知识点{random.randint(1, 10)}" for _ in range(random.randint(1, 3))],
                "step_analysis": steps
            })
        
        # Recalculate total score to ensure consistency
        total_score = round(sum(q["score"] for q in questions), 1)
        max_total_score = sum(q["max_score"] for q in questions)
        percentage_score = (total_score / max_total_score) * 100 if max_total_score > 0 else 0
        
        # Final check to ensure we maintain the desired failure rate
        if percentage_score < 60:
            # Only about 10% should fail
            if random.random() > 0.1:  # 90% chance to boost to passing
                # Boost the score to passing
                boost_factor = random.uniform(1.2, 1.5)
                for q in questions:
                    q["score"] = min(q["max_score"], q["score"] * boost_factor)
                # Recalculate total score after boosting
                total_score = round(sum(q["score"] for q in questions), 1)
                percentage_score = (total_score / max_total_score) * 100 if max_total_score > 0 else 0
        
        students.append({
            "student_id": student_ids[i],
            "student_name": student_names[i],
            "total_score": total_score,
            "max_score": max_total_score,
            "submit_time": datetime.now() - timedelta(days=random.randint(0, 30), hours=random.randint(0, 24)),
            "questions": questions,
            "need_review": percentage_score < 60 or random.random() > 0.95,  # Higher chance of review for low scores, very low for high scores
            "confidence_score": round(random.uniform(0.75, 0.95), 2)
        })
    
    return students

def generate_mock_question_analysis(num_questions: int = 10) -> List[Dict[str, Any]]:
    """Generate mock question analysis data."""
    question_types = ["概念题", "计算题", "证明题", "编程题"]
    topics = [f"题目{q_id}" for q_id in range(1, num_questions + 1)]
    
    questions = []
    for i in range(num_questions):
        # Generate more realistic difficulty and correct rates
        # Make some questions intentionally difficult with low correct rates
        if i < 2:  # First 2 questions are difficult
            difficulty = random.uniform(0.7, 0.9)
            correct_rate = random.uniform(0.3, 0.5)
        elif i < 5:  # Next 3 are medium
            difficulty = random.uniform(0.4, 0.7)
            correct_rate = random.uniform(0.5, 0.7)
        else:  # Rest are easier
            difficulty = random.uniform(0.2, 0.5)
            correct_rate = random.uniform(0.7, 0.9)
        
        questions.append({
            "question_id": f"Q{i + 1}",
            "question_type": random.choice(question_types),
            "topic": topics[i],
            "difficulty": round(difficulty, 2),
            "correct_rate": round(correct_rate, 2),
            "avg_score": round(random.uniform(2, 9), 1),
            "max_score": random.choice([5, 10, 15, 20]),
            "common_errors": random.sample(["计算错误", "概念理解不准确", "步骤不完整", "逻辑推理错误"], 
                                         random.randint(1, 3)),
            "knowledge_points": [f"知识点{random.randint(1, 10)}" for _ in range(random.randint(2, 5))]
        })
    
    return questions

def generate_mock_assignment_stats() -> Dict[str, Any]:
    """Generate mock assignment statistics."""
    return {
        "assignment_id": "MOCK_JOB_001",
        "assignment_name": "模拟作业批改任务",
        "total_students": 40,
        "submitted_count": 38,
        "avg_score": round(random.uniform(78, 87), 1),
        "max_score": 100,
        "min_score": round(random.uniform(45, 65), 1),
        "std_score": round(random.uniform(8, 15), 1),
        "pass_rate": round(random.uniform(90, 95), 1),
        "question_count": 10,
        "create_time": datetime.now() - timedelta(days=1)
    }

def generate_mock_jobs() -> Dict[str, Any]:
    """Generate mock job data for history page."""
    # Return empty dict to disable mock jobs
    return {}

# Generate and save mock data
if __name__ == "__main__":
    mock_data = {
        "student_scores": generate_mock_student_scores(),
        "question_analysis": generate_mock_question_analysis(),
        "assignment_stats": generate_mock_assignment_stats()
        # Removed jobs section to prevent mock jobs from being loaded
    }
    
    # Save to a JSON file
    with open("mock_data.json", "w", encoding="utf-8") as f:
        # Convert datetime objects to strings for JSON serialization
        def json_serializer(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
        
        json.dump(mock_data, f, ensure_ascii=False, indent=2, default=json_serializer)
    
    print("Mock data generated and saved to mock_data.json")