"""Legacy /students/[id] → redirect."""
from __future__ import annotations

import reflex as rx

from smartai_v2.state.task import TaskState


class LegacyStudentDetailState(rx.State):
    @rx.event
    async def on_mount(self):
        ts = await self.get_state(TaskState)
        tid = (ts.current_task_id or "").strip('"').strip()
        sid = (self.id or "").strip('"').strip()
        if tid and sid:
            return rx.redirect(f"/tasks/{tid}/results/{sid}")
        return rx.redirect("/")


@rx.page(route="/students/[id]", title="Redirecting...", on_load=LegacyStudentDetailState.on_mount)
def student_detail_page() -> rx.Component:
    return rx.center(rx.spinner(size="3"), min_height="100vh")
