"""Form helpers — labeled fields, validation hints."""
from __future__ import annotations

from typing import Callable, Optional

import reflex as rx

from smartai_v2.theme import COLOR, SPACE


def labeled_input(
    label: str,
    value,
    on_change: Callable,
    *,
    placeholder: str = "",
    type: str = "text",
    error: Optional[str] = None,
    required: bool = False,
) -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.text(label, size="2", weight="medium"),
            rx.cond(required, rx.text("*", color=COLOR["danger"]), rx.fragment()),
            spacing="1",
        ),
        rx.input(
            value=value,
            on_change=on_change,
            placeholder=placeholder,
            type=type,
            size="3",
            width="100%",
        ),
        rx.cond(
            (error is not None) & (error != ""),
            rx.text(error, size="1", color=COLOR["danger"]),
            rx.fragment(),
        ),
        spacing="1",
        align="start",
        width="100%",
    )


def labeled_textarea(label: str, value, on_change: Callable, *, placeholder: str = "", rows: int = 4) -> rx.Component:
    return rx.vstack(
        rx.text(label, size="2", weight="medium"),
        rx.text_area(
            value=value,
            on_change=on_change,
            placeholder=placeholder,
            rows=str(rows),
            size="3",
            width="100%",
        ),
        spacing="1",
        align="start",
        width="100%",
    )


def labeled_select(label: str, options: list[str], value, on_change: Callable) -> rx.Component:
    return rx.vstack(
        rx.text(label, size="2", weight="medium"),
        rx.select(
            options,
            value=value,
            on_change=on_change,
            size="3",
            width="100%",
        ),
        spacing="1",
        align="start",
        width="100%",
    )


def section_header(title: str, subtitle: str = "") -> rx.Component:
    return rx.vstack(
        rx.heading(title, size="5", weight="bold"),
        rx.cond(
            subtitle != "",
            rx.text(subtitle, size="2", color=COLOR["text_muted"]),
            rx.fragment(),
        ),
        spacing="1",
        align="start",
        width="100%",
    )
