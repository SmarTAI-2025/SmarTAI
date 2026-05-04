"""Task problems list — /tasks/[task_id]/problems"""
from __future__ import annotations

from typing import Any

import reflex as rx

from smartai_v2.components.auth_guard import require_auth
from smartai_v2.components.cards import empty_state
from smartai_v2.components.forms import section_header
from smartai_v2.components.layout import with_layout
from smartai_v2.components.markdown import smart_markdown
from smartai_v2.components.task_stepper import task_stepper
from smartai_v2.state.task import TaskState
from smartai_v2.theme import COLOR, RADIUS, SPACE


class TaskProblemsState(rx.State):
    @rx.event
    async def on_mount(self):
        tid = self.task_id
        if tid:
            return [TaskState.load_task(tid), TaskState.watch_active_job]


def _rubric_row(item) -> rx.Component:
    """Render one rubric item with an explicit ``Rubric N`` label.

    The grading LLM cites criteria as ``rubric 1`` / ``rubric 2`` etc. inside
    student comments — without a matching label on the criterion display the
    teacher cannot tell which bullet is rubric 1. Each row pairs the label
    with the bullet text so the mapping is unambiguous.
    """
    return rx.hstack(
        rx.badge(
            "Rubric " + item["index"].to_string(),
            color_scheme="gray",
            variant="soft",
            size="1",
            high_contrast=True,
        ),
        rx.box(
            smart_markdown(item["text"]),
            flex="1",
        ),
        spacing="2",
        align="start",
        width="100%",
    )


def _problem_card(q: dict) -> rx.Component:
    is_editing = TaskState.editing_q_id == q["q_id"]
    return rx.card(
        rx.vstack(
            rx.hstack(
                rx.badge(q["type"], variant="soft"),
                rx.heading(q["number"], size="3"),
                rx.spacer(),
                rx.cond(
                    is_editing,
                    rx.hstack(
                        rx.button(
                            rx.icon("save", size=12),
                            "保存",
                            on_click=lambda: TaskState.save_problem_edit(q["q_id"]),
                            size="1",
                            color_scheme="blue",
                        ),
                        rx.button(
                            rx.icon("x", size=12),
                            "取消",
                            on_click=TaskState.cancel_problem_edit,
                            size="1",
                            variant="soft",
                            color_scheme="gray",
                        ),
                        spacing="1",
                    ),
                    rx.button(
                        rx.icon("pencil", size=12),
                        "编辑",
                        on_click=lambda: TaskState.start_problem_edit(
                            q["q_id"], q["stem_raw"], q["criterion_raw"],
                        ),
                        size="1",
                        variant="soft",
                        color_scheme="gray",
                    ),
                ),
                width="100%",
                align="center",
            ),
            rx.text("题干", size="1", weight="medium", color=COLOR["text_muted"]),
            rx.cond(
                is_editing,
                rx.text_area(
                    value=TaskState.edit_stem_input,
                    on_change=TaskState.set_edit_stem_input,
                    rows="6",
                    width="100%",
                    placeholder="编辑题干（保留原始 LaTeX 分隔符 \\(...\\) 或 \\[...\\]）",
                ),
                smart_markdown(q["stem"]),
            ),
            rx.text("评分标准", size="1", weight="medium", color=COLOR["text_muted"]),
            rx.cond(
                is_editing,
                rx.text_area(
                    value=TaskState.edit_criterion_input,
                    on_change=TaskState.set_edit_criterion_input,
                    rows="4",
                    width="100%",
                    placeholder="编辑评分标准 / 评分要点",
                ),
                # Read-only view: render the criterion as a numbered list of
                # ``Rubric N`` items so the teacher can map "rubric 1 / 2 / 3"
                # references in student comments back to specific standards.
                # Falls back to the raw markdown when the criterion couldn't
                # be split into ≥ 1 items.
                rx.cond(
                    q["rubric_items"].to(list[dict[str, Any]]).length() > 0,
                    rx.vstack(
                        rx.foreach(
                            q["rubric_items"].to(list[dict[str, Any]]),
                            _rubric_row,
                        ),
                        spacing="2",
                        align="start",
                        width="100%",
                    ),
                    smart_markdown(q["criterion"]),
                ),
            ),
            spacing="2",
            align="start",
            width="100%",
        ),
        size="2",
        width="100%",
    )


def _action_bar() -> rx.Component:
    return rx.hstack(
        rx.link(
            rx.button(
                rx.icon("arrow-left", size=14),
                "重新上传题目",
                variant="soft",
                color_scheme="gray",
                size="3",
            ),
            href="/tasks/" + TaskState.current_task_id + "/upload_problems",
            underline="none",
        ),
        rx.spacer(),
        rx.link(
            rx.button(
                "下一步：上传学生作业",
                rx.icon("arrow-right", size=14),
                size="3",
                color_scheme="blue",
            ),
            href="/tasks/" + TaskState.current_task_id + "/upload_submissions",
            underline="none",
        ),
        width="100%",
        align="center",
        padding=f"{SPACE['md']} 0",
        border_top=f"1px solid {COLOR['border']}",
    )


@rx.page(
    route="/tasks/[task_id]/problems",
    title="题目预览 | SmarTAI",
    on_load=TaskProblemsState.on_mount,
)
def task_problems_page() -> rx.Component:
    return require_auth(
        with_layout(
            "题目预览",
            task_stepper(),
            section_header(
                "AI 识别结果",
                "AI 已自动识别题号、题型、题干和评分标准。如有错漏，点击右上角"
                + "「编辑」修改原文。确认无误后进入下一步上传学生作业。",
            ),
            rx.cond(
                TaskState.problem_list.length() > 0,
                rx.vstack(
                    rx.foreach(TaskState.problem_list, _problem_card),
                    spacing="3",
                    width="100%",
                ),
                empty_state(
                    "inbox",
                    "暂无题目",
                    "请先上传题目文件。",
                    rx.link(
                        rx.button("去上传题目", color_scheme="blue"),
                        href="/tasks/" + TaskState.current_task_id + "/upload_problems",
                        underline="none",
                    ),
                ),
            ),
            _action_bar(),
        ),
        require_role="teacher",
    )
