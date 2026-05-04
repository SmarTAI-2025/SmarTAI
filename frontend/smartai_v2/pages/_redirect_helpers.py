"""Legacy redirect helpers.

The old `/upload/problems`, `/students`, `/results/[job_id]` etc. routes were
removed from the sidebar in v2 but are kept here as redirect-only stubs so old
bookmarks don't 404. They look up the user's `current_task_id` (LocalStorage)
and bounce to the corresponding `/tasks/{id}/...` page.
"""
from __future__ import annotations

import reflex as rx

from smartai_v2.state.task import TaskState


async def redirect_to_task_subpage(state: rx.State, suffix: str) -> rx.event.EventSpec:
    """Common helper: get current_task_id from TaskState, redirect to /tasks/{id}/{suffix}."""
    ts = await state.get_state(TaskState)
    tid = (ts.current_task_id or "").strip('"').strip()
    if tid:
        return rx.redirect(f"/tasks/{tid}/{suffix}")
    return rx.redirect("/")
