"""Page modules — registered at app entry via @rx.page decorators."""
from __future__ import annotations

from . import (
    # Auth
    login, register,
    # Dashboard (teacher only — student dashboard removed)
    dashboard,
    # Task workflow (v2 task-centric)
    task_new,
    task_workspace,
    task_setup,
    task_upload_problems,
    task_upload_submissions,
    task_problems,
    task_students,
    task_student_answers,
    task_results,
    task_results_by_question,
    task_student_detail,
    task_question_detail,
    task_visualization,
    # Misc
    history,
    experts,
    knowledge_base,
    settings,
    # Legacy redirects (kept so old bookmarks don't 404)
    prob_upload, hw_upload,
    problems, students, student_detail,
    grading, results,
    student_report,
    score_report, visualization,
)
