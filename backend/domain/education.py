"""Normalized education domain enums and immutable DTOs.

These types are the single business vocabulary shared by repositories,
services, and the API layer. ORM records (backend.db.models) persist them and
FastAPI request/response models mirror them, but neither owns the vocabulary —
this module does. Keeping the enums here (rather than on the ORM records)
means a status transition rule is enforced by code that imports this module
regardless of which table a row lives in.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class Role(str, Enum):
    ADMIN = "admin"
    TEACHER = "teacher"
    STUDENT = "student"


class AssignmentStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    PUBLISHED = "published"
    CLOSED = "closed"
    ARCHIVED = "archived"


# Assignments may be edited while in these states; publishing freezes the
# question set. Status transitions are validated by the assignment service
# against this set rather than by ad-hoc checks in endpoints.
EDITABLE_ASSIGNMENT_STATUSES = frozenset(
    {AssignmentStatus.DRAFT.value, AssignmentStatus.READY.value}
)


class GradingRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# A run is "active" (and thus blocks a new run for the same assignment) while
# in one of these states. The DB partial unique index uses the same set.
ACTIVE_GRADING_RUN_STATUSES = frozenset(
    {GradingRunStatus.QUEUED.value, GradingRunStatus.RUNNING.value}
)


class GradeResultStatus(str, Enum):
    GRADED = "graded"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


# Results in these states are excluded from totals and from release; they route
# to the teacher review queue instead of being shown as a real score.
NON_GRADED_RESULT_STATUSES = frozenset(
    {GradeResultStatus.FAILED.value, GradeResultStatus.NEEDS_REVIEW.value}
)


class SubmissionRevisionSource(str, Enum):
    ONLINE = "online"
    TEACHER_IMPORT = "teacher_import"


# ─── Immutable DTOs (returned by repositories, never the ORM record) ──────────


class CourseDTO(BaseModel):
    id: str
    name: str
    code: str = ""
    description: str = ""
    teacher_id: str
    created_at: float
    updated_at: float
    student_ids: list[str] = Field(default_factory=list)


class EnrollmentDTO(BaseModel):
    course_id: str
    student_id: str
    enrolled_at: float


class QuestionDTO(BaseModel):
    id: str
    assignment_id: str
    q_id: str
    order_index: int
    number: str = ""
    type: str
    stem: str = ""
    criterion: str = ""
    max_score: float = 10.0
    reference_answer: str | None = None
    test_cases: list[dict[str, Any]] | None = None
    source: dict[str, Any] | None = None
    version: int = 1
    created_at: float
    updated_at: float


class AssignmentDTO(BaseModel):
    id: str
    course_id: str
    teacher_id: str
    name: str
    description: str = ""
    status: str = AssignmentStatus.DRAFT.value
    due_at: float | None = None
    created_at: float
    updated_at: float
    published_at: float | None = None
    version: int = 1
    question_count: int = 0


class SubmissionDTO(BaseModel):
    id: str
    assignment_id: str
    student_id: str
    current_revision_id: str | None = None
    current_revision_number: int | None = None
    created_at: float
    updated_at: float


class SubmissionRevisionDTO(BaseModel):
    id: str
    submission_id: str
    revision_number: int
    source: str = SubmissionRevisionSource.ONLINE.value
    file_name: str = ""
    created_at: float
    answers: list["SubmissionAnswerDTO"] = Field(default_factory=list)


class SubmissionAnswerDTO(BaseModel):
    id: str
    revision_id: str
    question_id: str
    q_id: str
    number: str = ""
    type: str = ""
    content: str = ""
    flag: list[str] = Field(default_factory=list)


class GradingRunDTO(BaseModel):
    id: str
    assignment_id: str
    teacher_id: str
    status: str = GradingRunStatus.QUEUED.value
    lease_owner: str | None = None
    lease_expiry: float | None = None
    last_heartbeat_at: float | None = None
    total_submissions: int = 0
    completed_submissions: int = 0
    failed_submissions: int = 0
    error_message: str | None = None
    created_at: float
    started_at: float | None = None
    completed_at: float | None = None
    released_at: float | None = None


class GradeResultDTO(BaseModel):
    id: str
    grading_run_id: str
    submission_revision_id: str
    question_id: str
    student_id: str
    q_id: str
    ai_score: float | None = None
    ai_max_score: float = 10.0
    ai_comment: str = ""
    ai_steps: list[dict[str, Any]] = Field(default_factory=list)
    ai_confidence: float | None = None
    ai_expert_results: list[dict[str, Any]] = Field(default_factory=list)
    ai_synthesis_method: str | None = None
    requires_review: bool = False
    review_reason: str | None = None
    initial_requires_review: bool = False
    initial_review_reason: str | None = None
    result_status: str = GradeResultStatus.GRADED.value
    effective_score: float | None = None
    effective_comment: str = ""
    teacher_review: dict[str, Any] | None = None
    created_at: float
    updated_at: float

    @model_validator(mode="after")
    def initialize_review_audit_facts(self) -> "GradeResultDTO":
        """Default omitted initial facts to the DTO's original live state.

        Adapters constructing a new result need not duplicate the same fields,
        while repository reads explicitly supply the durable audit values.
        """
        if "initial_requires_review" not in self.model_fields_set:
            self.initial_requires_review = self.requires_review
        if "initial_review_reason" not in self.model_fields_set:
            self.initial_review_reason = self.review_reason
        return self


class TeacherReviewDTO(BaseModel):
    id: str
    grade_result_id: str
    teacher_id: str
    previous_score: float | None = None
    previous_comment: str | None = None
    new_score: float
    new_comment: str = ""
    comment: str = ""
    confirmed: bool = False
    created_at: float


SubmissionRevisionDTO.model_rebuild()
