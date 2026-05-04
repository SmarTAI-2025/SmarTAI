"""Legacy /grading/[job_id] → redirect to /history."""
from __future__ import annotations

import reflex as rx


class LegacyGradingState(rx.State):
    @rx.event
    def on_mount(self):
        return rx.redirect("/history")


@rx.page(route="/grading/[job_id]", title="Redirecting...", on_load=LegacyGradingState.on_mount)
def grading_page() -> rx.Component:
    return rx.center(rx.spinner(size="3"), min_height="100vh")
