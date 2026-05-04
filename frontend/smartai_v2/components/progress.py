"""Progress bar + phase tracker components."""
from __future__ import annotations

import reflex as rx

from smartai_v2.state.grading import GradingState
from smartai_v2.theme import COLOR, SPACE, RADIUS


PHASES: list[tuple[str, str, str]] = [
    ("ingest", "Ingest", "upload"),
    ("grading", "Grading", "brain-circuit"),
    ("synthesis", "Synthesis", "merge"),
    ("done", "Done", "circle-check"),
]


def _phase_dot(phase_id: str, label: str, icon: str) -> rx.Component:
    is_current = GradingState.progress_phase == phase_id
    is_done = (
        ((phase_id == "ingest") & (GradingState.progress_phase != "ingest"))
        | ((phase_id == "grading") & GradingState.progress_phase.contains("synthesis"))
        | ((phase_id == "grading") & (GradingState.progress_phase == "done"))
        | ((phase_id == "synthesis") & (GradingState.progress_phase == "done"))
        | ((phase_id == "done") & (GradingState.progress_phase == "done"))
    )
    return rx.vstack(
        rx.box(
            rx.icon(icon, size=18, color="white"),
            background=rx.cond(is_current, COLOR["primary"], rx.cond(is_done, COLOR["accent"], COLOR["text_muted"])),
            padding=SPACE["sm"],
            border_radius=RADIUS["full"],
        ),
        rx.text(label, size="1", weight="medium"),
        spacing="1",
        align="center",
    )


def phase_tracker() -> rx.Component:
    return rx.hstack(
        *[_phase_dot(p, l, i) for p, l, i in PHASES],
        rx.spacer(),
        spacing="6",
        align="center",
        width="100%",
        padding=SPACE["md"],
    )


def progress_bars() -> rx.Component:
    return rx.vstack(
        rx.box(
            rx.hstack(
                rx.text("Students", size="2"),
                rx.spacer(),
                rx.text(
                    GradingState.completed_students.to_string() + " / " + GradingState.total_students.to_string(),
                    size="2",
                    color=COLOR["text_muted"],
                ),
                width="100%",
            ),
            rx.progress(
                value=GradingState.completed_students,
                max=GradingState.total_students,
                size="3",
                color_scheme="indigo",
            ),
            width="100%",
        ),
        rx.box(
            rx.hstack(
                rx.text("Questions", size="2"),
                rx.spacer(),
                rx.text(
                    GradingState.completed_questions.to_string() + " / " + GradingState.total_questions.to_string(),
                    size="2",
                    color=COLOR["text_muted"],
                ),
                width="100%",
            ),
            rx.progress(
                value=GradingState.completed_questions,
                max=GradingState.total_questions,
                size="3",
                color_scheme="grass",
            ),
            width="100%",
        ),
        spacing="3",
        width="100%",
    )


def event_log() -> rx.Component:
    return rx.box(
        rx.foreach(
            GradingState.progress_events,
            lambda ev: rx.hstack(
                rx.text(ev["timestamp"], size="1", color=COLOR["text_muted"]),
                rx.text(ev["message"], size="2"),
                spacing="2",
                width="100%",
            ),
        ),
        max_height="240px",
        overflow_y="auto",
        padding=SPACE["sm"],
        border=f"1px solid {COLOR['border']}",
        border_radius=RADIUS["sm"],
        background=COLOR["bg"],
        width="100%",
    )
