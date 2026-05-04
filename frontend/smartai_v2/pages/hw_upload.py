"""Legacy /upload/homework → redirect."""
from __future__ import annotations

import reflex as rx

from smartai_v2.pages._redirect_helpers import redirect_to_task_subpage


class LegacyHwUploadState(rx.State):
    @rx.event
    async def on_mount(self):
        return await redirect_to_task_subpage(self, "setup")


@rx.page(route="/upload/homework", title="Redirecting...", on_load=LegacyHwUploadState.on_mount)
def hw_upload_page() -> rx.Component:
    return rx.center(rx.spinner(size="3"), min_height="100vh")
