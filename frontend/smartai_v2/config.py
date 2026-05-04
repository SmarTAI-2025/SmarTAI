"""Application-wide configuration constants."""
from __future__ import annotations

import os


BACKEND_URL: str = os.environ.get("SMARTAI_BACKEND_URL", "http://localhost:8000")

REQUEST_TIMEOUT: float = 30.0
UPLOAD_TIMEOUT: float = 600.0

PROGRESS_POLL_INTERVAL: float = 1.5

QUESTION_TYPES_CN: list[str] = ["概念题", "计算题", "证明题", "推理题", "编程题", "其他"]
QUESTION_TYPES_EN: list[str] = ["Concept", "Calculation", "Proof", "Reasoning", "Programming", "Other"]
TYPE_CN_TO_EN: dict[str, str] = dict(zip(QUESTION_TYPES_CN, QUESTION_TYPES_EN))
TYPE_EN_TO_CN: dict[str, str] = dict(zip(QUESTION_TYPES_EN, QUESTION_TYPES_CN))

PROVIDER_TYPES: list[str] = ["openai", "gemini", "anthropic", "zhipu"]
PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.0-flash",
    "anthropic": "claude-3-5-haiku-20241022",
    "zhipu": "glm-4-flash",
}

ROLE_TEACHER = "teacher"
ROLE_STUDENT = "student"
ROLE_ADMIN = "admin"
