"""Reusable card-style components.

`info_card` 默认是带边框 + 阴影的卡片。如果把 `flat=True` 传进去，会变成
"区块式": 只有标题 + 分隔线 + 内容，无 border / 无 padding 包裹 — 用于
减少嵌套层级（详见 COLORS.md / Phase E UI 重设计原则）。
"""
from __future__ import annotations

from typing import Optional

import reflex as rx

from smartai_v2.theme import COLOR, SPACE, RADIUS, SHADOW


def stat_card(label: str, value, icon: str = "activity", color: str = COLOR["primary"]) -> rx.Component:
    return rx.card(
        rx.hstack(
            rx.box(
                rx.icon(icon, size=22, color="white"),
                background=color,
                padding=SPACE["sm"],
                border_radius=RADIUS["sm"],
            ),
            rx.vstack(
                rx.text(label, size="2", color=COLOR["text_muted"]),
                rx.heading(value, size="6", weight="bold"),
                spacing="1",
                align="start",
            ),
            spacing="3",
            align="center",
        ),
        size="2",
        width="100%",
    )


def feature_card(icon: str, title: str, description: str, button_label: str, href: str) -> rx.Component:
    return rx.card(
        rx.vstack(
            rx.hstack(
                rx.icon(icon, size=24, color=COLOR["primary"]),
                rx.heading(title, size="4", weight="bold"),
                align="center",
                spacing="2",
            ),
            rx.text(description, size="2", color=COLOR["text_muted"]),
            rx.spacer(),
            rx.link(
                rx.button(button_label, width="100%", variant="soft"),
                href=href,
                width="100%",
                underline="none",
            ),
            spacing="3",
            align="start",
            height="100%",
        ),
        size="2",
        width="100%",
        height="200px",
    )


def info_card(
    *children: rx.Component,
    title: Optional[str] = None,
    flat: bool = False,
) -> rx.Component:
    """A content block.

    Args:
        *children: body components.
        title: optional heading.
        flat: if True, render as a borderless section (heading + divider +
            content) rather than a card. Useful for reducing visual noise
            on pages that already group via the page-level container.
    """
    inner = list(children)
    if title:
        inner = [rx.heading(title, size="4", weight="medium"), rx.divider(), *inner]

    if flat:
        return rx.vstack(
            *inner,
            spacing="3",
            align="start",
            width="100%",
            padding=f"{SPACE['md']} 0",
        )

    return rx.card(
        rx.vstack(*inner, spacing="3", align="start", width="100%"),
        size="2",
        width="100%",
    )


def empty_state(icon: str, title: str, description: str = "", action: Optional[rx.Component] = None) -> rx.Component:
    children = [
        rx.icon(icon, size=48, color=COLOR["text_muted"]),
        rx.heading(title, size="4"),
    ]
    if description:
        children.append(rx.text(description, size="2", color=COLOR["text_muted"]))
    if action is not None:
        children.append(action)
    return rx.center(
        rx.vstack(*children, spacing="3", align="center", padding=SPACE["xl"]),
        width="100%",
        min_height="40vh",
    )
