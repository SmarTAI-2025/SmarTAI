"""Legacy /students → redirect."""
from __future__ import annotations

import reflex as rx

from smartai_v2.pages._redirect_helpers import redirect_to_task_subpage


class LegacyStudentsState(rx.State):
    @rx.event
    async def on_mount(self):
        return await redirect_to_task_subpage(self, "students")


@rx.page(route="/students", title="Redirecting...", on_load=LegacyStudentsState.on_mount)
def students_page() -> rx.Component:
    return rx.center(rx.spinner(size="3"), min_height="100vh")
