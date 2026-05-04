"""NL query box — single-input LLM-powered filter / summary / chart trigger.

Limits enforced UI-side (backend also enforces):
  - 30s cooldown between queries
  - 500 char max input
"""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.markdown import smart_markdown
from smartai_v2.state.task import TaskState
from smartai_v2.theme import COLOR, SPACE, RADIUS


MODE_OPTIONS = ["filter", "summary", "chart"]
MODE_LABELS = {
    "filter": "🔎 Filter students",
    "summary": "📝 Summarize",
    "chart": "📊 Make chart",
}
PLACEHOLDERS = {
    "filter": "e.g. Show students who failed Q3 calculation",
    "summary": "e.g. What are the most common errors on Q3?",
    "chart": "e.g. Bar chart of average score by question type",
}


def nl_query_box(*, embed_chart: bool = True, embed_summary: bool = True) -> rx.Component:
    """Render the Ask SmarTAI box.

    Args:
        embed_chart: render the chart inline below when mode=chart.
        embed_summary: render the markdown inline below when mode=summary.
    """
    return rx.box(
        rx.vstack(
            rx.hstack(
                rx.icon("sparkles", size=16, color=COLOR["primary"]),
                rx.text("Ask SmarTAI", size="2", weight="bold"),
                rx.spacer(),
                rx.select(
                    MODE_OPTIONS,
                    value=TaskState.nl_query_mode,
                    on_change=TaskState.set_nl_query_mode,
                    size="1",
                    width="160px",
                ),
                spacing="2",
                align="center",
                width="100%",
            ),
            rx.hstack(
                rx.input(
                    placeholder=PLACEHOLDERS["filter"],
                    value=TaskState.nl_query_input,
                    on_change=TaskState.set_nl_query_input,
                    max_length=500,
                    size="2",
                    width="100%",
                ),
                rx.button(
                    rx.cond(
                        TaskState.nl_loading,
                        rx.spinner(size="1"),
                        rx.icon("send", size=14),
                    ),
                    "Ask",
                    on_click=TaskState.submit_nl_query,
                    disabled=TaskState.nl_loading,
                    size="2",
                ),
                spacing="2",
                align="center",
                width="100%",
            ),
            rx.text(
                "Note: limited to 1 query / 30s. Single-turn — no chat. Max 500 chars.",
                size="1",
                color=COLOR["text_muted"],
            ),
            rx.cond(
                TaskState.nl_error != "",
                rx.callout(TaskState.nl_error, icon="triangle-alert", color_scheme="red", size="1"),
                rx.fragment(),
            ),
            rx.cond(
                embed_summary & (TaskState.nl_summary_md != ""),
                rx.box(
                    rx.heading("Summary", size="3"),
                    smart_markdown(TaskState.nl_summary_md),
                    padding=SPACE["md"],
                    background=COLOR["component_bg"],
                    border_radius=RADIUS["md"],
                    width="100%",
                ),
                rx.fragment(),
            ),
            rx.cond(
                embed_chart & (TaskState.nl_chart_traces.length() > 0),
                rx.box(
                    rx.heading(TaskState.nl_chart_title, size="3"),
                    rx.cond(
                        TaskState.nl_chart_rationale != "",
                        rx.text(TaskState.nl_chart_rationale, size="1", color=COLOR["text_muted"]),
                        rx.fragment(),
                    ),
                    rx.plotly(data=TaskState.nl_chart_figure, height="380px"),
                    padding=SPACE["md"],
                    background=COLOR["component_bg"],
                    border_radius=RADIUS["md"],
                    width="100%",
                ),
                rx.fragment(),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        padding=SPACE["md"],
        border=f"1px solid {COLOR['border']}",
        border_radius=RADIUS["md"],
        background=COLOR["surface"],
        width="100%",
    )
