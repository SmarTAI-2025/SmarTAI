"""Task workspace landing — /tasks/[task_id] — redirects to the right phase."""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.auth_guard import require_auth
from smartai_v2.components.layout import with_layout
from smartai_v2.state.task import TaskState
from smartai_v2.theme import COLOR


class TaskWorkspaceState(rx.State):
    """Route param `task_id` is auto-injected by Reflex."""

    @rx.event
    async def on_mount(self):
        tid = self.task_id
        if tid:
            return [
                TaskState.load_task(tid),
                TaskState.watch_active_job,
                rx.redirect(f"/tasks/{tid}/setup"),
            ]


@rx.page(
    route="/tasks/[task_id]",
    title="Task | SmarTAI",
    on_load=TaskWorkspaceState.on_mount,
)
def task_workspace_page() -> rx.Component:
    return require_auth(
        with_layout(
            "Task workspace",
            rx.center(rx.spinner(size="3"), padding="48px", width="100%"),
        ),
        require_role="teacher",
    )
