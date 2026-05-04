"""Legacy /results/[job_id] → redirect to /history."""
from __future__ import annotations

import reflex as rx


class LegacyResultsState(rx.State):
    @rx.event
    def on_mount(self):
        return rx.redirect("/history")


@rx.page(route="/results/[job_id]", title="Redirecting...", on_load=LegacyResultsState.on_mount)
def results_page() -> rx.Component:
    return rx.center(rx.spinner(size="3"), min_height="100vh")
