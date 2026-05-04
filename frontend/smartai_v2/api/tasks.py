"""Tasks API client (task-centric workflow)."""
from __future__ import annotations

from typing import Any

from .client import post_json, get_json, put_json, delete_json, post_file


async def create_task(name: str, *, token: str | None = None) -> dict:
    return await post_json("/tasks/", {"name": name}, token=token)


async def list_tasks(*, token: str | None = None) -> dict:
    return await get_json("/tasks/", token=token)


async def get_task(task_id: str, *, token: str | None = None) -> dict:
    return await get_json(f"/tasks/{task_id}", token=token)


async def update_task(task_id: str, *, name: str | None = None, token: str | None = None) -> dict:
    return await put_json(f"/tasks/{task_id}", {"name": name}, token=token)


async def delete_task(task_id: str, *, token: str | None = None) -> dict:
    return await delete_json(f"/tasks/{task_id}", token=token)


async def extract_problems(task_id: str, file_name: str, content: bytes,
                           content_type: str = "application/octet-stream",
                           *, token: str | None = None) -> dict:
    return await post_file(
        f"/tasks/{task_id}/extract_problems",
        file_name, content, content_type, token=token,
    )


async def parse_submissions(task_id: str, file_name: str, content: bytes,
                            content_type: str = "application/octet-stream",
                            *, token: str | None = None) -> dict:
    return await post_file(
        f"/tasks/{task_id}/parse_submissions",
        file_name, content, content_type, token=token,
    )


async def upload_reference(task_id: str, file_name: str, content: bytes,
                           content_type: str = "application/octet-stream",
                           *, token: str | None = None) -> dict:
    """Upload a reference-answer document (auxiliary — does NOT advance task.status).

    Backend parses the doc with the LLM and merges the result into
    ``problem_data[q_id]["reference_answer"]`` for every q_id it can match.
    Calculation skill picks it up on the next grade pass.

    Idempotent on file sha256: re-uploading the same bytes returns
    ``status="already_done"`` instead of re-running the LLM.
    """
    return await post_file(
        f"/tasks/{task_id}/upload_reference",
        file_name, content, content_type, token=token,
    )


async def upload_test_cases(task_id: str, file_name: str, content: bytes,
                            content_type: str = "application/octet-stream",
                            *, token: str | None = None) -> dict:
    """Upload a programming test-case document (any format — JSON / Markdown / NL).

    Backend's LLM normalizes the document into structured stdin/stdout
    test cases keyed by q_id and merges them into
    ``problem_data[q_id]["test_cases"]``. ProgrammingSkill picks them up on
    the next grade pass and uses them in the sandbox.

    Same idempotency contract as upload_reference.
    """
    return await post_file(
        f"/tasks/{task_id}/upload_test_cases",
        file_name, content, content_type, token=token,
    )


async def start_grading(task_id: str, *, language: str = "en",
                        multi_sample_n: int | None = None,
                        token: str | None = None) -> dict:
    """Trigger grading for a task.

    `multi_sample_n` is a per-task override for the backend's
    `settings.multi_sample_n`. We only send it when ≥ 2 (the backend default
    of 1 is the most common case and an explicit `1` is identical to omitting
    the field — keeping the payload minimal also makes idempotency hashing
    cleaner if we ever add it).
    """
    payload: dict = {"language": language}
    if multi_sample_n is not None and multi_sample_n > 1:
        payload["multi_sample_n"] = int(multi_sample_n)
    return await post_json(f"/tasks/{task_id}/grade", payload, token=token)


async def get_task_state(task_id: str, *, token: str | None = None) -> dict:
    return await get_json(f"/tasks/{task_id}/state", token=token)


async def get_task_result(task_id: str, *, token: str | None = None) -> dict:
    return await get_json(f"/tasks/{task_id}/result", token=token)


async def update_problem(task_id: str, q_id: str, *,
                         stem: str | None = None,
                         criterion: str | None = None,
                         token: str | None = None) -> dict:
    """Edit a single problem's stem/criterion — see PUT /tasks/{id}/problems/{q_id}."""
    payload: dict = {}
    if stem is not None:
        payload["stem"] = stem
    if criterion is not None:
        payload["criterion"] = criterion
    return await put_json(f"/tasks/{task_id}/problems/{q_id}", payload, token=token)


async def update_student_answer(task_id: str, stu_id: str, q_id: str, *,
                                content: str | None = None,
                                flag: list[str] | None = None,
                                token: str | None = None) -> dict:
    """Edit a single student's parsed answer — see PUT /tasks/{id}/students/{stu}/answers/{q}."""
    payload: dict = {}
    if content is not None:
        payload["content"] = content
    if flag is not None:
        payload["flag"] = list(flag)
    return await put_json(
        f"/tasks/{task_id}/students/{stu_id}/answers/{q_id}", payload, token=token,
    )


async def set_teacher_comment(task_id: str, student_id: str, q_id: str,
                              comment: str, *, token: str | None = None) -> dict:
    """Set / update / clear a teacher's manual comment on a graded answer."""
    return await post_json(
        f"/tasks/{task_id}/teacher_comment",
        {"student_id": student_id, "q_id": q_id, "comment": comment},
        token=token,
    )


async def list_teacher_comments(task_id: str, *, token: str | None = None) -> dict:
    """Get all teacher comments on this task as a flat {student::q: text} dict."""
    return await get_json(f"/tasks/{task_id}/teacher_comments", token=token)
