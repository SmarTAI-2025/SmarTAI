"""Create new task — /tasks/new — modal-style entry page."""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.auth_guard import require_auth
from smartai_v2.components.cards import info_card
from smartai_v2.components.forms import section_header
from smartai_v2.components.layout import with_layout
from smartai_v2.state.task import TaskState
from smartai_v2.theme import COLOR, SPACE


@rx.page(route="/tasks/new", title="New Task | SmarTAI")
def new_task_page() -> rx.Component:
    return require_auth(
        with_layout(
            "New Task",
            section_header(
                "Create a new grading task",
                "A task bundles problems + student submissions + grading results into one workflow.",
            ),
            info_card(
                rx.vstack(
                    rx.text("Task name", size="2", weight="medium"),
                    rx.input(
                        placeholder="e.g. Quiz 1, Final Exam Section A, …",
                        value=TaskState.new_task_name,
                        on_change=TaskState.set_new_task_name,
                        size="3",
                        width="100%",
                    ),
                    rx.hstack(
                        rx.button(
                            rx.icon("plus", size=14),
                            "Create",
                            on_click=TaskState.create_task,
                            size="3",
                        ),
                        rx.link(
                            rx.button("Cancel", variant="soft", size="3"),
                            href="/",
                        ),
                        spacing="2",
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                title="Task details",
            ),
        ),
        require_role="teacher",
    )
