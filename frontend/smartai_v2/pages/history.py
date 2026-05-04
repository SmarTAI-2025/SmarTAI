"""Task history — /history — list all tasks (replaces grading-job history)."""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.auth_guard import require_auth
from smartai_v2.components.cards import empty_state
from smartai_v2.components.forms import section_header
from smartai_v2.components.layout import with_layout
from smartai_v2.components.tables import data_table
from smartai_v2.state.task import TaskState
from smartai_v2.theme import COLOR


def _status_badge(status) -> rx.Component:
    return rx.match(
        status,
        ("draft", rx.badge("Draft", color_scheme="gray", variant="soft")),
        ("extracting_problems", rx.badge("Extracting", color_scheme="indigo", variant="soft")),
        ("problems_ready", rx.badge("Problems ready", color_scheme="blue", variant="soft")),
        ("parsing_submissions", rx.badge("Parsing", color_scheme="indigo", variant="soft")),
        ("submissions_ready", rx.badge("Ready", color_scheme="cyan", variant="soft")),
        ("grading", rx.badge("Grading", color_scheme="amber", variant="soft")),
        ("graded", rx.badge("Graded", color_scheme="green", variant="solid")),
        ("error", rx.badge("Error", color_scheme="red", variant="soft")),
        rx.badge(status, variant="soft"),
    )


def _task_row(t: dict) -> rx.Component:
    tid = t["task_id"].to(str)
    return rx.table.row(
        rx.table.cell(rx.code(tid)),
        rx.table.cell(t["name"]),
        rx.table.cell(_status_badge(t["status"])),
        rx.table.cell(t["problem_count"].to_string() + " / " + t["student_count"].to_string()),
        rx.table.cell(rx.text(t["updated_at_fmt"], size="1")),
        rx.table.cell(
            rx.hstack(
                rx.link(
                    rx.button(rx.icon("eye", size=12), "Open", variant="soft", size="1"),
                    href="/tasks/" + tid,
                ),
                rx.cond(
                    t["status"] == "graded",
                    rx.link(
                        rx.button(rx.icon("bar-chart-3", size=12), "Results", variant="soft", size="1"),
                        href="/tasks/" + tid + "/results",
                    ),
                    rx.fragment(),
                ),
                rx.button(
                    rx.icon("trash-2", size=12),
                    on_click=lambda: TaskState.delete_task(tid),
                    color_scheme="red",
                    variant="ghost",
                    size="1",
                ),
                spacing="1",
            ),
        ),
    )


@rx.page(route="/history", title="History | SmarTAI", on_load=TaskState.load_tasks)
def history_page() -> rx.Component:
    return require_auth(
        with_layout(
            "History",
            section_header("All tasks", "Drafts, in-progress, and completed grading tasks."),
            rx.hstack(
                rx.button(
                    rx.icon("refresh-cw", size=14),
                    "Refresh",
                    on_click=TaskState.load_tasks,
                    variant="soft",
                    size="2",
                ),
                rx.spacer(),
                rx.link(
                    rx.button(rx.icon("plus", size=14), "New Task", size="2"),
                    href="/tasks/new",
                ),
                width="100%",
            ),
            rx.cond(
                TaskState.task_list.length() > 0,
                data_table(
                    ["Task ID", "Name", "Status", "Problems / Students", "Updated", "Actions"],
                    TaskState.task_list,
                    _task_row,
                ),
                empty_state(
                    "inbox",
                    "No tasks yet",
                    "Create one to start grading.",
                    rx.link(rx.button("New Task"), href="/tasks/new"),
                ),
            ),
        ),
        require_role="teacher",
    )
