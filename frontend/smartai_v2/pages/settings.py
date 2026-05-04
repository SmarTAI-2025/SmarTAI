"""Settings + backend status — /settings"""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.auth_guard import require_auth
from smartai_v2.components.cards import info_card, stat_card
from smartai_v2.components.forms import section_header
from smartai_v2.components.layout import with_layout
from smartai_v2.config import BACKEND_URL
from smartai_v2.state.auth import AuthState
from smartai_v2.theme import COLOR


class SettingsState(rx.State):
    backend_status: str = "unknown"
    backend_message: str = ""
    checking: bool = False

    @rx.event
    async def check_backend(self):
        from smartai_v2.api import health
        self.checking = True
        try:
            data = await health.health()
            self.backend_status = data.get("status", "unknown")
            self.backend_message = (
                "engine=" + str(data.get("engine", "?")) +
                " · mem=" + str(data.get("memory_usage_mb", "?")) + "MB"
            )
        except Exception as e:
            self.backend_status = "error"
            self.backend_message = str(e)
        self.checking = False


@rx.page(route="/settings", title="Settings | SmarTAI")
def settings_page() -> rx.Component:
    return require_auth(
        with_layout(
            "Settings",
            section_header("System settings", "Account, backend connectivity, preferences."),
            info_card(
                rx.text("Username: " + AuthState.username, size="2"),
                rx.text("Role: " + AuthState.role, size="2", color=COLOR["text_muted"]),
                rx.button("Sign out", on_click=AuthState.logout, variant="soft", color_scheme="red", size="2"),
                title="Account",
            ),
            info_card(
                rx.text("Backend URL: " + BACKEND_URL, size="2", color=COLOR["text_muted"]),
                rx.hstack(
                    rx.text("Status: ", size="2"),
                    rx.badge(SettingsState.backend_status, color_scheme="green"),
                    rx.spacer(),
                    rx.button(
                        rx.cond(SettingsState.checking, rx.spinner(), rx.icon("refresh-cw", size=14)),
                        "Check",
                        on_click=SettingsState.check_backend,
                        variant="soft",
                        size="1",
                    ),
                    width="100%",
                    align="center",
                ),
                rx.cond(
                    SettingsState.backend_message != "",
                    rx.code(SettingsState.backend_message, size="1"),
                    rx.fragment(),
                ),
                title="Backend connectivity",
            ),
            info_card(
                rx.text("Theme, locale, notifications — coming soon.", size="2", color=COLOR["text_muted"]),
                title="Preferences",
            ),
        ),
    )
