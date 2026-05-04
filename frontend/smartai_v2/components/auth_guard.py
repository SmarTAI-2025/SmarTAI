"""Auth guard — wraps protected pages."""
from __future__ import annotations

from typing import Callable

import reflex as rx

from smartai_v2.state.auth import AuthState
from smartai_v2.theme import COLOR, SPACE


def require_auth(content: rx.Component, require_role: str = "") -> rx.Component:
    """Render `content` only when authenticated; else redirect to /login.

    `require_role`: optional role gate ("teacher" / "student"). If set and
    current user doesn't match, render an "access denied" panel instead.
    """
    return rx.cond(
        AuthState.is_authenticated,
        rx.cond(
            (require_role == "")
            | ((require_role == "teacher") & AuthState.is_teacher)
            | ((require_role == "student") & AuthState.is_student),
            content,
            _denied(),
        ),
        _redirect_to_login(),
    )


def _denied() -> rx.Component:
    return rx.center(
        rx.vstack(
            rx.icon("shield-x", size=48, color=COLOR["danger"]),
            rx.heading("Access denied", size="6"),
            rx.text("Your role does not have access to this page.", color=COLOR["text_muted"]),
            rx.button("Back to dashboard", on_click=rx.redirect("/")),
            spacing="3",
            align="center",
            padding=SPACE["xl"],
        ),
        min_height="80vh",
    )


def _redirect_to_login() -> rx.Component:
    return rx.fragment(
        rx.script("window.location.href='/login';"),
        rx.center(
            rx.spinner(size="3"),
            min_height="100vh",
        ),
    )
