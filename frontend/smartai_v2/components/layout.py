"""Application shell — sidebar + topbar + main content area."""
from __future__ import annotations

import reflex as rx

from smartai_v2.state.auth import AuthState
from smartai_v2.state.ui import UIState
from smartai_v2.theme import COLOR, SPACE, RADIUS, SHADOW


NAV_TEACHER: list[tuple[str, str, str]] = [
    ("home", "Dashboard", "/"),
    ("circle-plus", "New Task", "/tasks/new"),
    ("history", "History", "/history"),
    ("brain-circuit", "BYOK Experts", "/experts"),
    ("book-open", "Knowledge Base", "/knowledge-base"),
    ("settings", "Settings", "/settings"),
]


def _nav_item(icon: str, label: str, href: str) -> rx.Component:
    return rx.link(
        rx.hstack(
            rx.icon(icon, size=18),
            rx.text(label, size="2", weight="medium"),
            align="center",
            spacing="3",
            padding=f"{SPACE['sm']} {SPACE['md']}",
            border_radius=RADIUS["sm"],
            width="100%",
            _hover={"background": COLOR["primary_subtle"], "color": COLOR["primary"]},
        ),
        href=href,
        underline="none",
        color="inherit",
        width="100%",
    )


def _sidebar() -> rx.Component:
    return rx.box(
        rx.vstack(
            rx.link(
                rx.hstack(
                    rx.icon("graduation-cap", size=28, color=COLOR["primary"]),
                    rx.heading("SmarTAI", size="5", weight="bold"),
                    align="center",
                    spacing="2",
                    padding=SPACE["md"],
                ),
                href="/",
                underline="none",
                color="inherit",
            ),
            rx.divider(),
            rx.vstack(
                *[_nav_item(i, l, h) for i, l, h in NAV_TEACHER],
                spacing="1",
                width="100%",
                padding=SPACE["sm"],
            ),
            rx.spacer(),
            rx.divider(),
            rx.hstack(
                rx.avatar(fallback=AuthState.username[:1].upper(), size="2"),
                rx.vstack(
                    rx.text(AuthState.username, size="2", weight="medium"),
                    rx.text(AuthState.role, size="1", color=COLOR["text_muted"]),
                    spacing="0",
                    align="start",
                ),
                rx.spacer(),
                rx.icon_button(
                    rx.icon("log-out", size=16),
                    on_click=AuthState.logout,
                    variant="ghost",
                    size="1",
                ),
                width="100%",
                padding=SPACE["md"],
                align="center",
            ),
            spacing="0",
            height="100vh",
            align="start",
        ),
        width="240px",
        background=COLOR["surface"],
        border_right=f"1px solid {COLOR['border']}",
        position="sticky",
        top="0",
    )


def _topbar(title: str) -> rx.Component:
    return rx.hstack(
        rx.heading(title, size="6", weight="bold"),
        rx.spacer(),
        rx.hstack(
            rx.icon_button(
                rx.icon("bell", size=16),
                variant="ghost",
                on_click=rx.toast.info("Notifications feature coming soon!"),
            ),
            rx.menu.root(
                rx.menu.trigger(
                    rx.icon_button(rx.icon("user", size=16), variant="ghost"),
                ),
                rx.menu.content(
                    rx.menu.item(
                        "Settings",
                        on_click=rx.redirect("/settings"),
                        shortcut="⌘S",
                    ),
                    rx.menu.separator(),
                    rx.menu.item(
                        "Logout",
                        on_click=AuthState.logout,
                        shortcut="",
                        color="red",
                    ),
                ),
            ),
            spacing="2",
        ),
        align="center",
        padding=f"{SPACE['md']} {SPACE['lg']}",
        background=COLOR["surface"],
        border_bottom=f"1px solid {COLOR['border']}",
        height="64px",
        width="100%",
    )


def with_layout(page_title: str, *content: rx.Component) -> rx.Component:
    return rx.hstack(
        _sidebar(),
        rx.vstack(
            _topbar(page_title),
            rx.box(
                rx.vstack(*content, spacing="4", width="100%"),
                padding=SPACE["lg"],
                width="100%",
                max_width="1400px",
                margin="0 auto",
            ),
            spacing="0",
            width="100%",
            min_height="100vh",
            background=COLOR["bg"],
        ),
        spacing="0",
        align="start",
        width="100%",
    )


def public_layout(*content: rx.Component) -> rx.Component:
    return rx.center(
        rx.vstack(*content, spacing="4", width="100%", max_width="420px"),
        min_height="100vh",
        background=COLOR["bg"],
        padding=SPACE["lg"],
    )
