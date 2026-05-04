"""Simple data table built on rx.table with row click + zebra stripes."""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

import reflex as rx

from smartai_v2.theme import COLOR, RADIUS, SPACE


def data_table(
    headers: list[str],
    rows: Any,
    render_row: Callable[[Any], rx.Component],
    *,
    empty_message: str = "No data",
) -> rx.Component:
    return rx.cond(
        rows.length() > 0,
        rx.table.root(
            rx.table.header(
                rx.table.row(*[rx.table.column_header_cell(h) for h in headers]),
            ),
            rx.table.body(
                rx.foreach(rows, render_row),
            ),
            variant="surface",
            width="100%",
        ),
        rx.center(
            rx.text(empty_message, color=COLOR["text_muted"], size="2"),
            padding=SPACE["xl"],
            width="100%",
            border=f"1px dashed {COLOR['border']}",
            border_radius=RADIUS["md"],
        ),
    )
