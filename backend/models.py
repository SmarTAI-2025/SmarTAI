"""
SmarTAI unified data models.
Extends the original Correction/StepScore with multi-expert support and progress tracking.
"""
from __future__ import annotations

import time
from typing import List, Optional, Literal, Dict, Any
from pydantic import BaseModel, Field, field_validator


# ─── Grading result models ────────────────────────────────────────────────────

class StepScore(BaseModel):
    step_no: int
    desc: str
    is_correct: bool
    score: float


class ExpertResult(BaseModel):
    """Result from a single expert (provider) grading a question."""
    provider: str = Field(description="Provider identifier, e.g. 'openai:gpt-4o', 'gemini:gemini-2.5-pro'")
    score: float
    max_score: float = 10.0
    confidence: float
    comment: str
    steps: List[StepScore] = []
    hits: Optional[List[str]] = None
    logs: Optional[str] = None
    raw_output: Optional[str] = Field(None, description="Raw LLM output for traceability")
    duration_ms: Optional[float] = Field(None, description="Wall-clock time for this expert's grading")
    error_kind: Optional[str] = Field(
        None,
        description="When confidence==0 (skill failed), why: "
                    "'quota_exhausted' | 'transient_llm' | 'parse_failed' | 'general'. "
                    "Used by multi_expert/grading_agent to pick a friendly comment.",
    )


class Correction(BaseModel):
    """Grading result for a single question."""
    q_id: str
    type: str
    score: float
    max_score: float
    confidence: float
    comment: str
    steps: List[StepScore]
    hits: Optional[List[str]] = None
    logs: Optional[str] = None
    # Multi-expert traceability
    expert_results: List[ExpertResult] = Field(default_factory=list, description="Individual expert results (empty for single-expert)")
    synthesis_method: Optional[str] = Field(None, description="'single' | 'multi_sample' | 'weighted_average' | 'judge_agent' | 'degraded_to_single' | 'all_failed' | 'quota_exhausted'")

    # ─── P0 fairness signals (Indecisiveness Score + Minority Veto) ───────────
    is_score: Optional[float] = Field(
        None,
        description="Indecisiveness Score: std-of-scores / max_score across experts/samples. "
                    "None when only one sample was available (cannot estimate variance).",
    )
    requires_human_review: bool = Field(
        False,
        description="True when IS > settings.is_threshold OR a minority-veto rule fires. "
                    "The score is still set (median-based) so the pipeline does not block; "
                    "the frontend should surface this flag for teacher attention.",
    )
    review_reasons: List[str] = Field(
        default_factory=list,
        description="Why review was flagged: e.g. 'high_indecisiveness', 'minority_veto'. "
                    "Stable string IDs so the frontend can localize without parsing.",
    )


# ─── Problem & student answer models ──────────────────────────────────────────

class TestCase(BaseModel):
    """A single sandbox test case for programming problems.

    Replaces the dataclass previously defined in backend/tools/code_interpreter.py
    so the same shape is used by api/tasks upload, ingest_agent parsing,
    ProblemInfo storage, and the sandbox executor.
    """
    # Tell pytest NOT to try to collect this as a test class — without this,
    # the leading "Test" prefix triggers a PytestCollectionWarning.
    __test__ = False

    input: str = ""
    expected_output: str = ""
    description: str = ""
    source: Literal["teacher", "llm_generated"] = "teacher"
    sandbox_feasible: bool = Field(
        default=True,
        description="LLM marks False when the test requires GUI / network / "
                    "large input / special env; teachers' uploads default True.",
    )
    # ─── LeetCode-style function-call mode (optional) ────────────────────────
    # Populated when the problem asks the student to *implement a function*
    # (e.g. "实现 fibonacci(n)"). Sandbox then injects student's code, parses
    # function_args from JSON on stdin, calls fn(*args), prints repr(result).
    # When `function_name` is set, `input` / `expected_output` are ignored;
    # `function_args` / `expected_return` drive the comparison instead.
    function_name: Optional[str] = Field(
        default=None,
        description="If set, run student code in function-call mode (LeetCode style).",
    )
    function_args: Optional[List[Any]] = Field(
        default=None,
        description="Positional arguments passed to function_name. JSON-serializable.",
    )
    expected_return: Optional[str] = Field(
        default=None,
        description="repr() of expected return value; compared after ast.literal_eval normalization.",
    )


class ProblemInfo(BaseModel):
    q_id: str = Field(description="Unique question ID, starting from 'q1'")
    number: str = Field(description="Display question number, e.g. '1', '2.3', 'III.'")
    type: str = Field(description="Question type: 概念题/计算题/编程题/证明题/推理题/其他")
    stem: str = Field(description="Complete question stem including all text, formulas, and code")
    criterion: str = Field(description="Grading rubric/criteria")
    reference_answer: Optional[str] = Field(
        default=None,
        description="Teacher-supplied reference answer (calculation-style problems). "
                    "If None, CalculationSkill will ask the LLM to generate sympy code "
                    "and execute it in the sandbox to compute a reference value.",
    )
    test_cases: Optional[List[TestCase]] = Field(
        default=None,
        description="Teacher-supplied sandbox test cases (programming problems). "
                    "If None, ProgrammingSkill will scan keywords + ask the LLM to "
                    "generate up to 8 cases with a sandbox_feasible flag.",
    )

    @field_validator("q_id", mode="before")
    @classmethod
    def _coerce_q_id(cls, v):
        # Some LLMs (notably Gemini Flash variants) emit q_id as a bare JSON
        # integer (e.g. `"q_id": 1`) despite the prompt asking for "q1".
        # Normalize so Pydantic doesn't reject an otherwise-correct response.
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return f"q{v}"
        return v

    @field_validator("number", mode="before")
    @classmethod
    def _coerce_number(cls, v):
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return str(v)
        return v


class ProblemSet(BaseModel):
    problems: List[ProblemInfo] = Field(description="List of parsed problems")


class StudentAnswerInfo(BaseModel):
    q_id: str
    number: str
    type: str
    content: str = Field(description="Student's answer content; empty string if unanswered")
    flag: List[str] = Field(default_factory=list, description="Recognition issues/flags")

    @field_validator("q_id", mode="before")
    @classmethod
    def _coerce_q_id(cls, v):
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return f"q{v}"
        return v

    @field_validator("number", mode="before")
    @classmethod
    def _coerce_number(cls, v):
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return str(v)
        return v


class StudentSubmission(BaseModel):
    stu_id: str = Field(description="Student ID extracted from filename")
    stu_name: str = Field(description="Student name extracted from filename")
    stu_ans: List[StudentAnswerInfo]


# ─── Progress tracking models (for frontend feedback) ─────────────────────────

class ActiveUnit(BaseModel):
    """Represents a currently-running grading unit."""
    student_id: str
    q_id: str
    skill: str = Field(description="e.g. 'ConceptSkill', 'CalculationSkill'")
    expert: Optional[str] = Field(None, description="e.g. 'gemini:gemini-2.5-pro'; None for single-model")
    step: str = Field(description="Current substep, e.g. 'retrieve_knowledge', 'llm_grade', 'sympy_verify'")


class ProgressEvent(BaseModel):
    """A single progress event for the frontend timeline."""
    ts: float = Field(default_factory=time.time)
    level: Literal["info", "warn", "error"] = "info"
    message: str = Field(description="Human-readable message for frontend display")
    unit: Optional[ActiveUnit] = None


class JobProgress(BaseModel):
    """Fine-grained progress for a grading job, polled by frontend."""
    phase: Literal[
        "pending",
        "ingesting",
        "extracting",      # NEW: extracting problems from assignment file
        "parsing",         # NEW: parsing student submissions
        "classifying",
        "grading",
        "reviewing",
        "aggregating",
        "done",
        "error",
    ] = "pending"
    total_students: int = 0
    total_questions: int = 0
    completed_units: int = Field(0, description="Number of (student, question) pairs finished")
    active: List[ActiveUnit] = Field(default_factory=list, description="Currently running units")
    messages: List[ProgressEvent] = Field(default_factory=list, description="Ring buffer of last N events")
    error_detail: Optional[str] = None


# ─── Job lifecycle model ──────────────────────────────────────────────────────

class GradingJob(BaseModel):
    """Represents a grading job (single student or batch)."""
    job_id: str
    job_name: Optional[str] = None
    job_type: Literal["student", "batch"] = "student"
    status: Literal["pending", "running", "completed", "error"] = "pending"
    student_id: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
    completed_at: Optional[float] = None
    progress: JobProgress = Field(default_factory=JobProgress)
    results: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


# ─── LLM provider config ─────────────────────────────────────────────────────

class ProviderConfig(BaseModel):
    """Configuration for a single LLM provider."""
    provider_type: Literal["openai", "gemini", "anthropic", "zhipu"]
    api_key: str
    model: str = Field(description="Model name, e.g. 'gpt-4o', 'gemini-2.5-pro'")
    base_url: Optional[str] = None
    enabled: bool = True
    display_name: Optional[str] = Field(
        default=None,
        description="User-supplied label shown in dropdowns. Falls back to f'{provider_type}:{model}'.",
    )
    max_concurrent: int = Field(
        default=5,
        ge=1,
        description="Max in-flight LLM calls for this key. GLM Air ≤ 5, OpenAI/Gemini may set 10+.",
    )
    rpm: int = Field(
        default=0,
        ge=0,
        description="Requests per minute cap for this key (sliding-window token bucket). "
                    "0 = no rate gating (only `max_concurrent` applies). Set this to the "
                    "provider's per-minute quota (e.g. Gemini free-tier flash-lite = 15). "
                    "When grading would exceed this, calls automatically queue until the "
                    "rolling 60s window has room — prevents 429 quota errors instead of "
                    "burning retries on them.",
    )


# ─── User / Course / Assignment models (P0 — multi-role product) ──────────────

Role = Literal["teacher", "student", "admin"]


class User(BaseModel):
    """A user record (teacher / student / admin)."""
    id: str
    username: str
    email: str = ""
    role: Role = "teacher"
    password_hash: str = Field("", description="bcrypt hash; never returned to clients")
    course_ids: List[str] = Field(default_factory=list, description="Courses this user belongs to (teacher: owns; student: enrolled)")
    created_at: float = Field(default_factory=time.time)

    def public(self) -> Dict[str, Any]:
        """Dict safe to return to clients (no password hash)."""
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "role": self.role,
            "course_ids": self.course_ids,
            "created_at": self.created_at,
        }


class Course(BaseModel):
    """A course / class."""
    id: str
    name: str
    code: str = ""
    description: str = ""
    teacher_id: str
    student_ids: List[str] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)


class Assignment(BaseModel):
    """An assignment within a course.

    Wraps the existing problem_data structure (dict[q_id → ProblemInfo-like]) and
    adds metadata (course, due date, publish status) so students can see and
    submit to it.
    """
    id: str
    course_id: str
    teacher_id: str
    name: str
    description: str = ""
    problem_data: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    status: Literal["draft", "published", "closed"] = "draft"
    due_at: Optional[float] = None
    created_at: float = Field(default_factory=time.time)
    published_at: Optional[float] = None


class Submission(BaseModel):
    """A student's submission for an assignment."""
    id: str
    assignment_id: str
    student_id: str
    answers: Dict[str, str] = Field(default_factory=dict, description="{q_id: answer_text}")
    file_name: str = ""
    submitted_at: float = Field(default_factory=time.time)
    job_id: Optional[str] = Field(None, description="Linked grading job_id")
    grade: Optional[Dict[str, Any]] = Field(None, description="Final grade dict (corrections + total)")


# ─── Task lifecycle (frontend_v2 task-centric workflow) ───────────────────────

TaskStatus = Literal[
    "draft",
    "extracting_problems",
    "problems_ready",
    "parsing_submissions",
    "submissions_ready",
    "grading",
    "graded",
    "error",
]


class Task(BaseModel):
    """A grading task — bundles problems + submissions + grading job into one
    user-visible unit. Replaces the global problem_store/student_store coupling
    by making each task carry its own data.

    Status machine (linear, with `error` as a sink):
        draft
          → extracting_problems → problems_ready
          → parsing_submissions → submissions_ready
          → grading → graded
        any phase → error (recoverable by re-uploading)
    """
    task_id: str
    name: str = "Untitled task"
    owner_id: str = "anonymous"
    status: TaskStatus = "draft"

    problem_data: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    student_data: Dict[str, Dict[str, Any]] = Field(default_factory=dict)

    extract_job_id: Optional[str] = None
    parse_job_id: Optional[str] = None
    grading_job_id: Optional[str] = None

    problem_file_hash: Optional[str] = None
    submission_file_hash: Optional[str] = None
    problem_file_name: Optional[str] = None
    submission_file_name: Optional[str] = None

    # Reference answers (calculation-style problems) — auxiliary upload, does NOT
    # change task.status. Stored per-question in problem_data[q_id]["reference_answer"]
    # after parsing; these top-level fields hold the upload metadata.
    reference_file_hash: Optional[str] = None
    reference_file_name: Optional[str] = None
    reference_parse_job_id: Optional[str] = None

    # Test cases (programming problems) — same model as reference. Stored per-question
    # in problem_data[q_id]["test_cases"] after parsing.
    test_cases_file_hash: Optional[str] = None
    test_cases_file_name: Optional[str] = None
    test_cases_parse_job_id: Optional[str] = None

    # ─── Task-scoped knowledge base (RAG MVP) ─────────────────────────────
    # Mirror metadata for documents uploaded via POST /tasks/{id}/kb. The
    # actual chunks + vectors live in backend.rag.store.InMemoryTaskRetriever
    # (pure in-memory, evicted with the task). Keys = doc_id (random hex);
    # values = KBDoc.public() shape. Frontend reads this dict to render the
    # uploaded-files list on the Setup page.
    kb_docs: Dict[str, Dict[str, Any]] = Field(default_factory=dict)

    error: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    def lite(self) -> Dict[str, Any]:
        """Metadata-only representation for list views (no problem/student data)."""
        return {
            "task_id": self.task_id,
            "name": self.name,
            "owner_id": self.owner_id,
            "status": self.status,
            "extract_job_id": self.extract_job_id,
            "parse_job_id": self.parse_job_id,
            "grading_job_id": self.grading_job_id,
            "problem_file_name": self.problem_file_name,
            "submission_file_name": self.submission_file_name,
            "reference_file_name": self.reference_file_name,
            "test_cases_file_name": self.test_cases_file_name,
            "reference_parse_job_id": self.reference_parse_job_id,
            "test_cases_parse_job_id": self.test_cases_parse_job_id,
            "problem_count": len(self.problem_data),
            "student_count": len(self.student_data),
            "kb_docs": dict(self.kb_docs),
            "kb_doc_count": len(self.kb_docs),
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
