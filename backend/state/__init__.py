"""
In-memory state stores for problems, students, and jobs.

Replaces the globals in backend/dependencies.py. Interface is dict-like to
preserve compatibility with existing routers; swap to Redis/Postgres later
without changing callers.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional
from threading import RLock

from backend.models import GradingJob, Task

logger = logging.getLogger(__name__)


# ─── Problem store ────────────────────────────────────────────────────────────

_problem_data: Dict[str, Dict[str, Any]] = {}


def get_problem_store() -> Dict[str, Dict[str, Any]]:
    """FastAPI dependency: returns the problem data dict."""
    return _problem_data


# ─── Student store ────────────────────────────────────────────────────────────

_student_data: Dict[str, Dict[str, Any]] = {}


def get_student_store() -> Dict[str, Dict[str, Any]]:
    """FastAPI dependency: returns the student data dict."""
    return _student_data


# ─── Job store ────────────────────────────────────────────────────────────────

class JobStore:
    """Thread-safe storage for grading jobs + history."""

    MAX_ACTIVE = 1000
    MAX_HISTORY = 1000
    DEFAULT_TTL = 24 * 60 * 60  # 24 hours
    HISTORY_TTL = 30 * 24 * 60 * 60  # 30 days

    def __init__(self) -> None:
        self._active: OrderedDict[str, GradingJob] = OrderedDict()
        self._history: OrderedDict[str, GradingJob] = OrderedDict()
        self._lock = RLock()

    def create(self, job: GradingJob) -> None:
        with self._lock:
            self._active[job.job_id] = job
            self._prune_if_needed()

    def get(self, job_id: str) -> Optional[GradingJob]:
        with self._lock:
            return self._active.get(job_id) or self._history.get(job_id)

    def update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._active.get(job_id) or self._history.get(job_id)
            if job:
                for k, v in fields.items():
                    setattr(job, k, v)

    def complete(self, job_id: str, results: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            job = self._active.pop(job_id, None)
            if job is None:
                return
            job.status = "completed"
            job.completed_at = time.time()
            job.results = results
            self._history[job_id] = job
            self._prune_if_needed()

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._active.pop(job_id, None)
            if job is None:
                return
            job.status = "error"
            job.error = error
            job.completed_at = time.time()
            job.results = {"status": "error", "message": error}
            self._history[job_id] = job
            self._prune_if_needed()

    def list_active_ids(self) -> List[str]:
        with self._lock:
            return list(self._active.keys())

    def list_metadata(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "job_id": j.job_id,
                    "job_name": j.job_name,
                    "job_type": j.job_type,
                    "status": j.status,
                    "student_id": j.student_id,
                    "created_at": j.created_at,
                    "completed_at": j.completed_at,
                }
                for j in list(self._active.values()) + list(self._history.values())
            ]

    def discard(self, job_id: str) -> bool:
        with self._lock:
            if job_id in self._active:
                self._active.pop(job_id)
                return True
            if job_id in self._history:
                self._history.pop(job_id)
                return True
            return False

    def reset_active(self) -> None:
        with self._lock:
            self._active.clear()

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._active.values() if j.status in ("pending", "running"))

    def _prune_if_needed(self) -> None:
        now = time.time()
        for k in list(self._active.keys()):
            job = self._active[k]
            if job.status not in ("pending", "running") and job.completed_at and now - job.completed_at > self.DEFAULT_TTL:
                self._active.pop(k)
        while len(self._active) > self.MAX_ACTIVE:
            self._active.popitem(last=False)
        for k in list(self._history.keys()):
            job = self._history[k]
            if job.completed_at and now - job.completed_at > self.HISTORY_TTL:
                self._history.pop(k)
        while len(self._history) > self.MAX_HISTORY:
            self._history.popitem(last=False)


_job_store = JobStore()


def get_job_store() -> JobStore:
    return _job_store


# ─── Task store (task-centric workflow) ───────────────────────────────────────

class TaskStore:
    """Thread-safe in-memory store for `Task` (problems + submissions + grading).

    Owner-scoped: list_for_owner(owner_id) filters by `task.owner_id`. The demo
    auth token (`demo-teacher-foo`) maps to `User.id = "demo_foo"`, so every
    user (real or demo) gets isolated tasks.
    """

    MAX_TASKS = 500
    DEFAULT_TTL = 7 * 24 * 60 * 60  # 7 days

    def __init__(self) -> None:
        self._tasks: OrderedDict[str, Task] = OrderedDict()
        self._lock = RLock()

    def create(self, task: Task) -> None:
        with self._lock:
            self._tasks[task.task_id] = task
            self._prune_if_needed()

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def update(self, task_id: str, **fields: Any) -> Optional[Task]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            for k, v in fields.items():
                setattr(task, k, v)
            task.updated_at = time.time()
            return task

    def delete(self, task_id: str) -> bool:
        with self._lock:
            return self._tasks.pop(task_id, None) is not None

    def list_for_owner(self, owner_id: str) -> List[Task]:
        with self._lock:
            return [t for t in self._tasks.values() if t.owner_id == owner_id]

    def list_all(self) -> List[Task]:
        with self._lock:
            return list(self._tasks.values())

    def _prune_if_needed(self) -> None:
        now = time.time()
        for k in list(self._tasks.keys()):
            t = self._tasks[k]
            if now - t.updated_at > self.DEFAULT_TTL and t.status in ("graded", "error", "draft"):
                self._tasks.pop(k)
        while len(self._tasks) > self.MAX_TASKS:
            self._tasks.popitem(last=False)


_task_store = TaskStore()


def get_task_store() -> TaskStore:
    return _task_store


# ─── User / Course / Assignment / Submission stores ───────────────────────────
# (P0 — face full product. In-memory for now; swap to PostgreSQL in Phase 1.)

from backend.models import User, Course, Assignment, Submission

_user_store: Dict[str, User] = {}
_user_by_username: Dict[str, str] = {}  # username → user_id

_course_store: Dict[str, Course] = {}

_assignment_store: Dict[str, Assignment] = {}

_submission_store: Dict[str, Submission] = {}
# Index for quick "get my submission for assignment X"
_submissions_by_assignment_student: Dict[str, str] = {}  # f"{aid}:{sid}" → submission_id

_invite_codes: Dict[str, Dict[str, Any]] = {}  # code → {role, course_id, email, expires_at}


def get_user_store() -> Dict[str, User]:
    return _user_store


def find_user_by_username(username: str) -> Optional[User]:
    uid = _user_by_username.get(username)
    return _user_store.get(uid) if uid else None


def register_user(user: User) -> None:
    _user_store[user.id] = user
    _user_by_username[user.username] = user.id


def remove_user(user_id: str) -> bool:
    user = _user_store.pop(user_id, None)
    if user is None:
        return False
    _user_by_username.pop(user.username, None)
    return True


def get_course_store() -> Dict[str, Course]:
    return _course_store


def get_assignment_store() -> Dict[str, Assignment]:
    return _assignment_store


def get_submission_store() -> Dict[str, Submission]:
    return _submission_store


def submission_key(assignment_id: str, student_id: str) -> str:
    return f"{assignment_id}:{student_id}"


def get_submission_by_assignment_student(assignment_id: str, student_id: str) -> Optional[Submission]:
    sid = _submissions_by_assignment_student.get(submission_key(assignment_id, student_id))
    return _submission_store.get(sid) if sid else None


def index_submission(sub: Submission) -> None:
    _submission_store[sub.id] = sub
    _submissions_by_assignment_student[submission_key(sub.assignment_id, sub.student_id)] = sub.id


def get_invite_store() -> Dict[str, Dict[str, Any]]:
    return _invite_codes
