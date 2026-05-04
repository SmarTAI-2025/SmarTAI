"""Student dashboard — /student"""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.auth_guard import require_auth
from smartai_v2.components.cards import stat_card, feature_card, empty_state
from smartai_v2.components.layout import with_layout
from smartai_v2.state.auth import AuthState
from smartai_v2.theme import COLOR


def _stats() -> rx.Component:
    return rx.grid(
        stat_card("Pending Assignments", "—", "clipboard-list", COLOR["warning"]),
        stat_card("Submitted", "—", "circle-check", COLOR["accent"]),
        stat_card("Average Score", "—", "award", COLOR["primary"]),
        columns="3",
        spacing="4",
        width="100%",
    )


@rx.page(route="/student", title="Student Dashboard | SmarTAI")
def student_dashboard_page() -> rx.Component:
    return require_auth(
        with_layout(
            "My Dashboard",
            rx.card(
                rx.vstack(
                    rx.heading("Hi, " + AuthState.username, size="6"),
                    rx.text("View your assignments and grades.", size="3", color=COLOR["text_muted"]),
                    spacing="1",
                    align="start",
                ),
                size="3",
                width="100%",
            ),
            _stats(),
            rx.heading("Coming soon", size="4"),
            empty_state(
                "construction",
                "Student-facing pages under construction",
                "These pages will populate once backend assignments/submissions endpoints are ready.",
            ),
        ),
        require_role="student",
    )
