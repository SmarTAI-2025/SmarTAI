"""Legacy /student_report/[job_id]/[student_id] → redirect to /history."""
from __future__ import annotations

import reflex as rx


class LegacyStudentReportState(rx.State):
    @rx.event
    def on_mount(self):
        return rx.redirect("/history")


@rx.page(
    route="/student_report/[job_id]/[student_id]",
    title="Redirecting...",
    on_load=LegacyStudentReportState.on_mount,
)
def student_report_page() -> rx.Component:
    return rx.center(rx.spinner(size="3"), min_height="100vh")
