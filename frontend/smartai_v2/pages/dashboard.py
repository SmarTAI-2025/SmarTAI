"""Teacher dashboard — / — task-centric overview."""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.auth_guard import require_auth
from smartai_v2.components.cards import stat_card, info_card, empty_state
from smartai_v2.components.layout import with_layout
from smartai_v2.components.task_card import task_card
from smartai_v2.state.auth import AuthState
from smartai_v2.state.task import TaskState
from smartai_v2.theme import COLOR, SPACE


def _hero() -> rx.Component:
    return rx.card(
        rx.hstack(
            rx.vstack(
                rx.heading("Welcome back, " + AuthState.username, size="6", weight="bold"),
                rx.text(
                    "Each grading task bundles problems, submissions, and results. "
                    "Switch between tasks, leave a draft, and resume anytime.",
                    size="2",
                    color=COLOR["text_muted"],
                ),
                spacing="1",
                align="start",
            ),
            rx.spacer(),
            rx.button(
                rx.icon("plus", size=16),
                "New Task",
                on_click=rx.redirect("/tasks/new"),
                size="3",
            ),
            align="center",
            width="100%",
        ),
        size="3",
        width="100%",
    )


def _stats() -> rx.Component:
    return rx.grid(
        stat_card(
            "Active drafts",
            TaskState.active_tasks.length(),
            "circle-dashed",
            COLOR["warning"],
        ),
        stat_card(
            "Completed",
            TaskState.graded_tasks.length(),
            "circle-check",
            COLOR["accent"],
        ),
        stat_card(
            "All tasks",
            TaskState.task_list.length(),
            "list",
            COLOR["primary"],
        ),
        columns="3",
        spacing="4",
        width="100%",
    )


def _active_section() -> rx.Component:
    return info_card(
        rx.cond(
            TaskState.active_tasks.length() > 0,
            rx.grid(
                rx.foreach(TaskState.active_tasks, task_card),
                columns="2",
                spacing="3",
                width="100%",
            ),
            empty_state(
                "circle-dashed",
                "No active tasks",
                "Create a new task to start grading.",
                rx.button(
                    rx.icon("plus", size=14),
                    "New Task",
                    on_click=rx.redirect("/tasks/new"),
                    size="2",
                ),
            ),
        ),
        title="Active & drafts",
    )


def _completed_section() -> rx.Component:
    return info_card(
        rx.cond(
            TaskState.graded_tasks.length() > 0,
            rx.grid(
                rx.foreach(TaskState.graded_tasks, task_card),
                columns="2",
                spacing="3",
                width="100%",
            ),
            rx.text("No completed tasks yet.", size="2", color=COLOR["text_muted"]),
        ),
        title="Recently completed",
    )


@rx.page(route="/", title="Dashboard | SmarTAI", on_load=TaskState.load_tasks)
def dashboard_page() -> rx.Component:
    return require_auth(
        with_layout(
            "Dashboard",
            _hero(),
            _stats(),
            _active_section(),
            _completed_section(),
        ),
        require_role="teacher",
    )
