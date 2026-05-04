"""Student filter bar — search-with-autocomplete + sort + quick filter + NL trigger.

The search input is wired up to a native HTML `<datalist>` that shows the full
roster as suggestions while the teacher types — so they can either type a
fragment OR pick directly from a dropdown of "id — name" entries.

This replaces the previous plain text input which had no awareness of who's
in the class.
"""
from __future__ import annotations

import reflex as rx

from smartai_v2.state.task import TaskState
from smartai_v2.theme import COLOR, SPACE, RADIUS


SORT_OPTIONS = ["score_desc", "score_asc", "name", "id"]
SORT_LABELS = {
    "score_desc": "Score ↓",
    "score_asc": "Score ↑",
    "name": "Name",
    "id": "Student ID",
}

# datalist id reused across renders — must be unique on the page
_DATALIST_ID = "student-search-options"


def _quick_chip(label: str, key: str) -> rx.Component:
    return rx.button(
        label,
        on_click=lambda: TaskState.set_quick_filter(key),
        variant=rx.cond(TaskState.quick_filter == key, "solid", "soft"),
        size="1",
    )


def _search_with_dropdown() -> rx.Component:
    """Native HTML input bound to a <datalist> for autocomplete dropdown."""
    return rx.box(
        rx.el.input(
            placeholder="Type to search, or pick from list…",
            value=TaskState.search_query,
            on_change=TaskState.set_search_query,
            list=_DATALIST_ID,
            style={
                "width": "320px",
                "padding": "6px 12px",
                "border": f"1px solid {COLOR['border']}",
                "borderRadius": RADIUS["sm"],
                "fontSize": "14px",
                "fontFamily": "inherit",
                "background": COLOR["surface"],
                "color": COLOR["text"],
                "outline": "none",
            },
            _focus={"border_color": COLOR["primary"]},
        ),
        rx.el.datalist(
            rx.foreach(
                TaskState.student_search_options,
                lambda opt: rx.el.option(value=opt),
            ),
            id=_DATALIST_ID,
        ),
    )


def student_filter_bar() -> rx.Component:
    return rx.vstack(
        rx.hstack(
            _search_with_dropdown(),
            rx.text("Sort by:", size="2"),
            rx.select(
                SORT_OPTIONS,
                value=TaskState.sort_by,
                on_change=TaskState.set_sort_by,
                size="2",
                width="140px",
            ),
            rx.spacer(),
            rx.text(
                TaskState.filtered_student_stats.length().to_string()
                + " / "
                + TaskState.students_count.to_string()
                + " students",
                size="2",
                color=COLOR["text_muted"],
            ),
            spacing="3",
            align="center",
            width="100%",
        ),
        rx.hstack(
            rx.text("Quick:", size="2"),
            _quick_chip("All", "all"),
            _quick_chip("Failing (<60)", "failing"),
            _quick_chip("Top 10%", "top10"),
            _quick_chip("Has flags / low conf", "flagged"),
            rx.spacer(),
            rx.cond(
                TaskState.nl_filter_active,
                rx.button(
                    rx.icon("x", size=12),
                    "Clear NL filter",
                    on_click=TaskState.clear_nl_filter,
                    size="1",
                    variant="soft",
                    color_scheme="amber",
                ),
                rx.fragment(),
            ),
            spacing="2",
            align="center",
            width="100%",
        ),
        rx.cond(
            TaskState.nl_filter_active,
            rx.callout(
                "NL filter applied: " + TaskState.nl_filter_explanation,
                icon="info",
                color_scheme="amber",
                size="1",
            ),
            rx.fragment(),
        ),
        spacing="3",
        align="start",
        width="100%",
    )
