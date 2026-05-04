"""Legacy /visualization → redirect."""
from __future__ import annotations

import reflex as rx

from smartai_v2.pages._redirect_helpers import redirect_to_task_subpage


class LegacyVisualizationState(rx.State):
    @rx.event
    async def on_mount(self):
        return await redirect_to_task_subpage(self, "visualization")


@rx.page(route="/visualization", title="Redirecting...", on_load=LegacyVisualizationState.on_mount)
def visualization_page() -> rx.Component:
    return rx.center(rx.spinner(size="3"), min_height="100vh")
