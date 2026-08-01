"""Normalized education domain ORM records.

Single source of truth for the course → assignment → submission → grading →
review → release workflow. The legacy ``tasks`` / ``grading_jobs`` JSON-payload
aggregates and the ``users.course_ids`` compatibility mirror are intentionally
absent: membership is read only from ``course_enrollments`` and every editable
record carries an optimistic-lock ``version`` column so concurrent writes are
rejected at the database predicate rather than by application memory.

JSON is used only for bounded leaf data (test cases, score steps, expert
traces, progress counters, reference answers, parsed metadata). Question
collections, student collections, and whole grading results are never embedded
as JSON aggregates — each is its own normalized row.
"""
from __future__ import annotations

import time

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base


# ─── Identity (retained, LLM-independent) ─────────────────────────────────────


class UserRecord(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, nullable=True, index=True)
    # teacher | student | admin. Checked in SQL so a buggy insert cannot store
    # an unknown role even if the application layer skips validation.
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="teacher")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        default=True, server_default=text("true"), nullable=False
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    updated_at: Mapped[float] = mapped_column(
        Float, nullable=False, default=time.time, onupdate=time.time
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('teacher', 'student', 'admin')", name="ck_users_role"
        ),
    )


class InviteCodeRecord(Base):
    __tablename__ = "invite_codes"

    code: Mapped[str] = mapped_column(String(128), primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="teacher")
    # Optional course pre-binding for student invites: registration auto-enrolls.
    course_id: Mapped[str | None] = mapped_column(
        ForeignKey("courses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    invited_by: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    used_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    used_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('teacher', 'student', 'admin')", name="ck_invite_codes_role"
        ),
    )


class RefreshSessionRecord(Base):
    __tablename__ = "refresh_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    last_used_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)


# ─── LLM provider config (retained) ───────────────────────────────────────────


class ProviderConfigRecord(Base):
    __tablename__ = "provider_configs"
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "provider_type", "model",
            name="uq_provider_configs_owner_provider_model",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(
        default=True, server_default=text("true"), nullable=False
    )
    max_concurrent: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    rpm: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unverified"
    )
    last_checked_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    verification_error_code: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    updated_at: Mapped[float] = mapped_column(
        Float, nullable=False, default=time.time, onupdate=time.time
    )


# ─── Course → enrollment ──────────────────────────────────────────────────────


class CourseRecord(Base):
    __tablename__ = "courses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    teacher_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    updated_at: Mapped[float] = mapped_column(
        Float, nullable=False, default=time.time, onupdate=time.time
    )


class CourseEnrollmentRecord(Base):
    __tablename__ = "course_enrollments"
    __table_args__ = (
        # Membership has exactly one row per (course, student); a student cannot
        # be enrolled twice and the table is the authorization source for every
        # student-facing assignment/submission read.
        UniqueConstraint("course_id", "student_id", name="uq_course_enrollments_course_student"),
    )

    course_id: Mapped[str] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True
    )
    student_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    enrolled_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)


# ─── Assignment → questions + knowledge selection ─────────────────────────────


class AssignmentRecord(Base):
    __tablename__ = "assignments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    course_id: Mapped[str] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    teacher_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # draft -> ready -> published -> closed -> archived (CheckConstraint below).
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    due_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    updated_at: Mapped[float] = mapped_column(
        Float, nullable=False, default=time.time, onupdate=time.time
    )
    published_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Optimistic-lock version: every editable update must match the expected
    # value in its WHERE clause and bump it, so a stale client write is a 409
    # rather than a silent last-writer-wins overwrite.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'ready', 'published', 'closed', 'archived')",
            name="ck_assignments_status",
        ),
    )


class AssignmentQuestionRecord(Base):
    __tablename__ = "assignment_questions"
    __table_args__ = (
        # A revision of a question keeps the same q_id but is a new row; the
        # (assignment, q_id, version) triple is unique so historical grading
        # results still point at the exact question text they were graded on.
        UniqueConstraint(
            "assignment_id", "q_id", "version",
            name="uq_assignment_questions_assignment_qid_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    q_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Stable display order independent of insertion order; reordering updates
    # this column without rewriting question identity.
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    number: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    stem: Mapped[str] = mapped_column(Text, nullable=False, default="")
    criterion: Mapped[str] = mapped_column(Text, nullable=False, default="")
    max_score: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    # Bounded leaf JSON: a teacher/LLM reference answer, sandbox test cases,
    # and parsed source metadata. Never a collection of sibling questions.
    reference_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    test_cases: Mapped[list | None] = mapped_column(JSON, nullable=True)
    source: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    updated_at: Mapped[float] = mapped_column(
        Float, nullable=False, default=time.time, onupdate=time.time
    )


class AssignmentKnowledgeDocumentRecord(Base):
    """Teacher's selection of up to three ready personal documents per assignment.

    Selection is assignment-scoped (replaces the legacy task-scoped
    task_knowledge_documents) and owner-scoped through the assignment's teacher.
    """

    __tablename__ = "assignment_knowledge_documents"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id", "document_id",
            name="uq_assignment_knowledge_documents_assignment_doc",
        ),
    )

    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), primary_key=True
    )
    # Presentation provenance required by the Figma grading-setup surface.
    # Content still lives only in KnowledgeDocumentRecord; this records how the
    # document was attached and whether it is backed by Course Library metadata.
    source_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="upload"
    )
    library_material_id: Mapped[str | None] = mapped_column(
        ForeignKey("course_materials.id", ondelete="SET NULL"), nullable=True
    )
    selected_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)


# ─── Submission → immutable revisions → answers ──────────────────────────────


class SubmissionRecord(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        # One current submission per (assignment, student). Resubmission creates
        # a new immutable revision and repoints current_revision_id; it never
        # inserts a second submission row.
        UniqueConstraint("assignment_id", "student_id", name="uq_submissions_assignment_student"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    current_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("submission_revisions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    updated_at: Mapped[float] = mapped_column(
        Float, nullable=False, default=time.time, onupdate=time.time
    )


class SubmissionRevisionRecord(Base):
    __tablename__ = "submission_revisions"
    __table_args__ = (
        # Immutable revision numbering: (submission, revision_number) cannot
        # collide, so a regrade or audit can always address a specific revision.
        UniqueConstraint(
            "submission_id", "revision_number",
            name="uq_submission_revisions_submission_number",
        ),
        CheckConstraint(
            "source IN ('online', 'teacher_import')",
            name="ck_submission_revisions_source",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # "online" (student web form) | "teacher_import" (batch archive) — both flow
    # through the same revision pipeline so there is one data chain, not two.
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="online")
    file_name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)


class SubmissionAnswerRecord(Base):
    __tablename__ = "submission_answers"
    __table_args__ = (
        # Exactly one structured answer per (revision, question).
        UniqueConstraint(
            "revision_id", "question_id",
            name="uq_submission_answers_revision_question",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("submission_revisions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    question_id: Mapped[str] = mapped_column(
        ForeignKey("assignment_questions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    q_id: Mapped[str] = mapped_column(String(64), nullable=False)
    number: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    type: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Recognition flags (handwriting/parse issues) carried from the extractor.
    flag: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)


# ─── GradingRun → frozen revisions → results → reviews ───────────────────────


class GradingRunRecord(Base):
    __tablename__ = "grading_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    teacher_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # queued -> running -> completed | partial_failed | failed | cancelled.
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    # Durable lease: only the worker that claimed the row may write terminal
    # results, and only while the lease has not expired. lease_owner is the
    # worker identity; lease_expiry is the absolute deadline; heartbeat updates
    # lease_expiry. A late worker whose lease lapsed is rejected by predicate.
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    lease_expiry: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    last_heartbeat_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_submissions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_submissions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_submissions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    # Teacher release timestamp. Null until the teacher explicitly releases the
    # run; students see results only after this is set, so a completed run is
    # not automatically visible. Decoupling "graded" from "released" matches the
    # design's "学生只能读取已发布结果" rule.
    released_at: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'partial_failed', "
            "'failed', 'cancelled')",
            name="ck_grading_runs_status",
        ),
        # Partial unique index: at most one *active* run per assignment. Active
        # = status in the non-terminal set. The dialect-specific migration
        # materializes this with a WHERE clause; the ORM metadata declares the
        # named unique index so the constraint is visible to tooling and tests.
        Index(
            "ux_grading_runs_active_per_assignment",
            "assignment_id",
            unique=True,
            sqlite_where=text(
                "status IN ('queued', 'running')"
            ),
            postgresql_where=text(
                "status IN ('queued', 'running')"
            ),
        ),
    )


class GradingRunSubmissionRecord(Base):
    """Frozen (run, submission_revision) pair graded by a run.

    The revision snapshot is captured at run creation so a regrade of a later
    revision cannot retroactively change what a prior run graded.
    """

    __tablename__ = "grading_run_submissions"
    __table_args__ = (
        UniqueConstraint(
            "grading_run_id", "submission_revision_id",
            name="uq_grading_run_submissions_run_revision",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    grading_run_id: Mapped[str] = mapped_column(
        ForeignKey("grading_runs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    submission_revision_id: Mapped[str] = mapped_column(
        ForeignKey("submission_revisions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    student_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)


class GradingRunEventRecord(Base):
    """Persisted progress event for a grading run.

    Frontend recovers run progress by polling these rows; the in-process
    reporter is an optimization, not the source of truth.
    """

    __tablename__ = "grading_run_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    grading_run_id: Mapped[str] = mapped_column(
        ForeignKey("grading_runs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Bounded progress counter payload (phase, totals, completed units).
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)


class GradeResultRecord(Base):
    """One graded (run, revision, question) triple.

    ``ai_*`` columns are immutable once written — a teacher review never
    overwrites them; the display value comes from the latest TeacherReview.
    Failure states (``failed`` / ``needs_review``) are explicit and never
    masquerade as a real zero score.
    """

    __tablename__ = "grade_results"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    grading_run_id: Mapped[str] = mapped_column(
        ForeignKey("grading_runs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    submission_revision_id: Mapped[str] = mapped_column(
        ForeignKey("submission_revisions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    question_id: Mapped[str] = mapped_column(
        ForeignKey("assignment_questions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    student_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    q_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # ai_* are the immutable original LLM outputs.
    ai_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_max_score: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    ai_comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ai_steps: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_expert_results: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    ai_synthesis_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requires_review: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )
    review_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Immutable audit facts captured when the AI result is first persisted.
    # Teacher decisions may resolve ``requires_review``/``review_reason`` but
    # must never erase why the result originally entered the review queue.
    initial_requires_review: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )
    initial_review_reason: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    # graded | failed | needs_review. "failed"/"needs_review" are excluded from
    # totals and from release; the frontend routes them to the review queue.
    result_status: Mapped[str] = mapped_column(String(32), nullable=False, default="graded")
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    updated_at: Mapped[float] = mapped_column(
        Float, nullable=False, default=time.time, onupdate=time.time
    )

    __table_args__ = (
        UniqueConstraint(
            "grading_run_id", "submission_revision_id", "question_id",
            name="uq_grade_results_run_revision_question",
        ),
        CheckConstraint(
            "result_status IN ('graded', 'failed', 'needs_review')",
            name="ck_grade_results_status",
        ),
    )


class TeacherReviewRecord(Base):
    """Teacher adjustment of a GradeResult.

    The latest effective review for a result supplies the display score/comment;
    the AI original (GradeResult.ai_*) is preserved for audit. Students see
    reviewed values only after the run is released.
    """

    __tablename__ = "teacher_reviews"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    grade_result_id: Mapped[str] = mapped_column(
        ForeignKey("grade_results.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    teacher_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    previous_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    previous_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_score: Mapped[float] = mapped_column(Float, nullable=False)
    new_comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confirmed: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )
    # Monotonic within one grade result. This is the primary ordering key;
    # wall-clock timestamps are audit metadata and may move backwards.
    review_sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)

    __table_args__ = (
        UniqueConstraint(
            "grade_result_id", "review_sequence",
            name="uq_teacher_reviews_result_sequence",
        ),
    )


# ─── Stored files ─────────────────────────────────────────────────────────────


class StoredFileRecord(Base):
    """File metadata linked to an assignment, a submission revision, or a
    knowledge document via explicit nullable FK columns (replaces task_id)."""

    __tablename__ = "stored_files"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(64), nullable=False, default="local")
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Explicit resource links instead of a generic task_id. Exactly one of the
    # business FKs is expected to be set per file (enforced in application code;
    # kept nullable here so a knowledge-only upload that predates a document row
    # can still be recorded).
    assignment_id: Mapped[str | None] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), nullable=True, index=True
    )
    submission_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("submission_revisions.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    knowledge_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)


# ─── Knowledge documents & chunks (retained) ──────────────────────────────────


class KnowledgeDocumentRecord(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint("owner_id", "sha256", name="uq_knowledge_documents_owner_sha256"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stored_file_id: Mapped[str | None] = mapped_column(
        ForeignKey("stored_files.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="processing", index=True)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False, default="v1")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    updated_at: Mapped[float] = mapped_column(
        Float, nullable=False, default=time.time, onupdate=time.time
    )


class KnowledgeChunkRecord(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunks_document_index"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)


# ─── Course library metadata (document/file content stays canonical) ─────────────


class CourseMaterialGroupRecord(Base):
    """Owner-scoped folder metadata for Course Library materials."""

    __tablename__ = "course_material_groups"
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "normalized_name",
            name="uq_course_material_groups_owner_normalized_name",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    course_id: Mapped[str | None] = mapped_column(
        ForeignKey("courses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    updated_at: Mapped[float] = mapped_column(
        Float, nullable=False, default=time.time, onupdate=time.time
    )


class CourseMaterialRecord(Base):
    """Presentation/classification metadata for a canonical knowledge document.

    Bytes, hashes, parse state, and extracted chunks remain owned by
    ``StoredFileRecord`` / ``KnowledgeDocumentRecord``.  Renaming a library
    item therefore changes only ``display_name`` and never rewrites the stored
    object's immutable original filename.
    """

    __tablename__ = "course_materials"
    __table_args__ = (
        CheckConstraint(
            "category IN ('textbook', 'answer', 'lecture', 'rubric', 'other')",
            name="ck_course_materials_category",
        ),
        UniqueConstraint("document_id", name="uq_course_materials_document_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    course_id: Mapped[str | None] = mapped_column(
        ForeignKey("courses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    group_id: Mapped[str | None] = mapped_column(
        ForeignKey("course_material_groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    display_name: Mapped[str] = mapped_column(String(512), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    # Bounded presentation leaf: the API enforces <=20 unique labels and <=60
    # characters per item. Tags used for task history are normalized rows below.
    labels: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    updated_at: Mapped[float] = mapped_column(
        Float, nullable=False, default=time.time, onupdate=time.time
    )


# ─── Assignment tags ──────────────────────────────────────────────────────


class TagRecord(Base):
    __tablename__ = "tags"
    __table_args__ = (
        CheckConstraint(
            "color IN ('slate', 'blue', 'teal', 'green', 'amber', 'rose', 'violet')",
            name="ck_tags_color",
        ),
        UniqueConstraint(
            "owner_id", "normalized_name", name="uq_tags_owner_normalized_name"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(80), nullable=False)
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="slate")
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    updated_at: Mapped[float] = mapped_column(
        Float, nullable=False, default=time.time, onupdate=time.time
    )


class AssignmentTagRecord(Base):
    """Normalized many-to-many assignment/tag membership."""

    __tablename__ = "assignment_tags"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id", "tag_id", name="uq_assignment_tags_assignment_tag"
        ),
    )

    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[str] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    assigned_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
