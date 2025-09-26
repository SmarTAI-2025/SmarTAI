from pydantic import BaseModel
from typing import List, Optional


class StepScore(BaseModel):
    step_no: int
    desc: str
    is_correct: bool
    score: float


class Correction(BaseModel):
    q_id: str
    type: str
    score: float
    max_score: float
    confidence: float
    comment: str
    steps: List[StepScore]
    hits: Optional[List[str]] = None
    logs: Optional[str] = None