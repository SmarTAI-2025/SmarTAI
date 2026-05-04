"""Legacy /upload/problems → redirect to /tasks/{current}/setup or /."""
from __future__ import annotations

import reflex as rx

from smartai_v2.pages._redirect_helpers import redirect_to_task_subpage


class LegacyProbUploadState(rx.State):
    @rx.event
    async def on_mount(self):
        return await redirect_to_task_subpage(self, "setup")


@rx.page(route="/upload/problems", title="Redirecting...", on_load=LegacyProbUploadState.on_mount)
def prob_upload_page() -> rx.Component:
    return rx.center(rx.spinner(size="3"), min_height="100vh")
