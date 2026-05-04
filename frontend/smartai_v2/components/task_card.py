"""Task card — dashboard summary for a single Task."""
from __future__ import annotations

import reflex as rx

from smartai_v2.state.task import TaskState
from smartai_v2.theme import COLOR, RADIUS, SPACE


def _status_badge(status) -> rx.Component:
    return rx.match(
        status,
        ("draft", rx.badge("Draft", color_scheme="gray", variant="soft")),
        ("extracting_problems", rx.badge("Extracting…", color_scheme="indigo", variant="soft")),
        ("problems_ready", rx.badge("Problems ready", color_scheme="blue", variant="soft")),
        ("parsing_submissions", rx.badge("Parsing…", color_scheme="indigo", variant="soft")),
        ("submissions_ready", rx.badge("Ready to grade", color_scheme="cyan", variant="soft")),
        ("grading", rx.badge("Grading…", color_scheme="amber", variant="soft")),
        ("graded", rx.badge("Graded", color_scheme="green", variant="solid")),
        ("error", rx.badge("Error", color_scheme="red", variant="soft")),
        rx.badge(status, variant="soft"),
    )


def _step_dot(label: str, complete) -> rx.Component:
    return rx.hstack(
        rx.icon(
            "circle-check",
            size=14,
            color=rx.cond(complete, COLOR["accent"], COLOR["text_muted"]),
        ),
        rx.text(label, size="1", color=rx.cond(complete, COLOR["text"], COLOR["text_muted"])),
        spacing="1",
        align="center",
    )


def task_card(task: dict) -> rx.Component:
    """Single dashboard card for a task. Shows status, step progress, actions."""
    tid = task["task_id"].to(str)
    return rx.card(
        rx.vstack(
            rx.hstack(
                rx.vstack(
                    rx.text(task["name"], size="3", weight="bold"),
                    rx.hstack(
                        rx.text("Updated", size="1", color=COLOR["text_muted"]),
                        rx.text(task["updated_at_fmt"], size="1", color=COLOR["text_muted"]),
                        spacing="1",
                    ),
                    spacing="0",
                    align="start",
                ),
                rx.spacer(),
                _status_badge(task["status"]),
                width="100%",
                align="start",
            ),
            rx.hstack(
                _step_dot(
                    "Problems",
                    task["status"].to_string().contains("ready") | (task["status"] == "graded") | (task["status"] == "grading") | (task["status"] == "submissions_ready") | (task["status"] == "parsing_submissions"),
                ),
                _step_dot(
                    "Submissions",
                    (task["status"] == "submissions_ready") | (task["status"] == "grading") | (task["status"] == "graded"),
                ),
                _step_dot("Grading", task["status"] == "graded"),
                spacing="3",
                width="100%",
            ),
            rx.hstack(
                rx.hstack(
                    rx.text(task["problem_count"].to_string(), size="1", color=COLOR["text_muted"]),
                    rx.text("problems ·", size="1", color=COLOR["text_muted"]),
                    rx.text(task["student_count"].to_string(), size="1", color=COLOR["text_muted"]),
                    rx.text("students", size="1", color=COLOR["text_muted"]),
                    spacing="1",
                ),
                rx.spacer(),
                rx.link(
                    rx.button("Open", size="1", variant="soft"),
                    href="/tasks/" + tid,
                ),
                rx.button(
                    rx.icon("trash-2", size=12),
                    on_click=lambda: TaskState.delete_task(tid),
                    color_scheme="red",
                    variant="ghost",
                    size="1",
                ),
                width="100%",
                align="center",
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        size="2",
        width="100%",
    )
