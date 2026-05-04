"""Task progress card — spinner + phase label + latest event message.

Earlier this rendered a percentage progress bar driven by
`progress.completed_units / total_students`, but that ratio is only meaningful
in the parsing/grading phases — extract problems hard-codes 50%. The user
saw a "stuck" bar and assumed something was broken.

Replaced with: spinner + Chinese phase label + last event line. Honest about
"this is in flight, no granular progress available", which matches reality.
"""
from __future__ import annotations

import reflex as rx

from smartai_v2.state.task import TaskState
from smartai_v2.theme import COLOR, SPACE, RADIUS


def _phase_label(phase) -> rx.Component:
    return rx.match(
        phase,
        ("extracting", rx.text("正在识别题目…", size="2", weight="medium")),
        ("parsing", rx.text("正在解析学生作答…", size="2", weight="medium")),
        ("grading", rx.text("正在批改…", size="2", weight="medium")),
        ("ingesting", rx.text("正在导入…", size="2", weight="medium")),
        ("classifying", rx.text("正在分类…", size="2", weight="medium")),
        ("synthesis", rx.text("正在综合多专家结果…", size="2", weight="medium")),
        ("aggregating", rx.text("正在汇总…", size="2", weight="medium")),
        ("done", rx.text("已完成", size="2", weight="medium", color=COLOR["accent"])),
        ("error", rx.text("出错", size="2", weight="medium", color=COLOR["danger"])),
        rx.text("AI 正在处理…", size="2", weight="medium"),
    )


def task_progress_card() -> rx.Component:
    """Active progress indicator — self-hides if idle."""
    return rx.cond(
        TaskState.is_active,
        rx.box(
            rx.hstack(
                rx.spinner(size="3"),
                rx.vstack(
                    _phase_label(TaskState.progress_phase),
                    rx.cond(
                        TaskState.latest_message != "",
                        rx.text(
                            TaskState.latest_message,
                            size="1",
                            color=COLOR["text_muted"],
                        ),
                        rx.fragment(),
                    ),
                    rx.text(
                        "AI 正在处理，请耐心等待。可切换其他页面，进度不丢失。",
                        size="1",
                        color=COLOR["text_muted"],
                    ),
                    spacing="1",
                    align="start",
                ),
                spacing="3",
                align="center",
                width="100%",
            ),
            padding=SPACE["md"],
            background=COLOR["primary_subtle"],
            border_radius=RADIUS["md"],
            border=f"1px solid {COLOR['border']}",
            width="100%",
        ),
        rx.fragment(),
    )


def task_event_log() -> rx.Component:
    """Scrollable event log from the current reporter snapshot."""
    return rx.box(
        rx.foreach(
            TaskState.progress.get("messages", []).to(list),
            lambda ev: rx.hstack(
                rx.text(ev["message"], size="1"),
                spacing="2",
                width="100%",
            ),
        ),
        max_height="200px",
        overflow_y="auto",
        padding=SPACE["sm"],
        border=f"1px solid {COLOR['border']}",
        border_radius=RADIUS["sm"],
        background=COLOR["bg"],
        width="100%",
    )
