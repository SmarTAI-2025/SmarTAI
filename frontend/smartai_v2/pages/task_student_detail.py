"""Per-student detail view — /tasks/[task_id]/results/[student_id]"""
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


class TaskStudentDetailState(rx.State):
    """Route params `task_id`, `student_id` are auto-injected by Reflex."""

    @rx.event
    async def on_mount(self):
        tid = self.task_id
        sid = (self.student_id or "").strip('"').strip()
        events: list = []
        if tid:
            events.append(TaskState.load_task(tid))
        if sid:
            events.append(TaskState.set_current_student_id(sid))
        return events


def _confidence_badge(c) -> rx.Component:
    return rx.cond(
        c["low_confidence"],
        rx.tooltip(
            rx.badge(
                rx.icon("triangle-alert", size=12),
                "AI 置信 " + c["confidence_display"].to_string(),
                color_scheme="amber",
                variant="solid",
                size="1",
            ),
            content="AI 置信度低 — 建议人工复核。",
        ),
        rx.badge(
            rx.icon("circle-check", size=12),
            "AI 置信 " + c["confidence_display"].to_string(),
            color_scheme="green",
            variant="soft",
            size="1",
        ),
    )


def _teacher_comment_section(c) -> rx.Component:
    comment_key = TaskState.current_student_id + "::" + c["q_id"].to(str)
    is_editing = TaskState.editing_comment_key == comment_key

    return rx.box(
        rx.hstack(
            rx.text("教师评语", size="1", weight="medium", color=COLOR["text_muted"]),
            rx.spacer(),
            rx.cond(
                is_editing,
                rx.hstack(
                    rx.button(
                        rx.icon("save", size=12), "保存",
                        on_click=lambda: TaskState.save_teacher_comment(c["q_id"]),
                        size="1",
                        color_scheme="blue",
                    ),
                    rx.button(
                        rx.icon("x", size=12), "取消",
                        on_click=TaskState.cancel_teacher_comment,
                        size="1",
                        variant="soft",
                        color_scheme="gray",
                    ),
                    spacing="1",
                ),
                rx.button(
                    rx.icon("pencil", size=12),
                    rx.cond(c["teacher_comment"] != "", "编辑", "添加"),
                    on_click=lambda: TaskState.start_teacher_comment(
                        c["q_id"], c["teacher_comment"],
                    ),
                    size="1",
                    variant="soft",
                    color_scheme="gray",
                ),
            ),
            width="100%",
        ),
        rx.cond(
            is_editing,
            rx.text_area(
                value=TaskState.edit_comment_input,
                on_change=TaskState.set_edit_comment_input,
                rows="3",
                width="100%",
                placeholder="添加你的人工评语 / 复核意见…",
            ),
            rx.cond(
                c["teacher_comment"] != "",
                rx.box(
                    smart_markdown(c["teacher_comment"]),
                    padding="10px",
                    background="var(--green-2)",
                    border="1px solid var(--green-6)",
                    border_radius=RADIUS["sm"],
                    width="100%",
                ),
                rx.text(
                    "尚无教师评语。",
                    size="1",
                    color=COLOR["text_muted"],
                    font_style="italic",
                ),
            ),
        ),
        width="100%",
    )


def _expert_card(e) -> rx.Component:
    """One row inside the 各专家详情 accordion."""
    return rx.card(
        rx.vstack(
            rx.hstack(
                rx.code(e["provider"].to_string(), size="2"),
                rx.spacer(),
                rx.cond(
                    e["failed"].to(bool),
                    rx.badge("失败", color_scheme="red", variant="solid", size="1"),
                    # Per-expert score badge — kept neutral (light gray) so it
                    # doesn't compete visually with the synthesized score on the
                    # row above. Earlier this was blue and read as a primary CTA.
                    rx.badge(
                        e["score"].to_string() + " / " + e["max_score"].to_string()
                        + "  ·  conf " + e["confidence"].to_string(),
                        color_scheme="gray",
                        variant="soft",
                        size="1",
                    ),
                ),
                width="100%",
                align="center",
            ),
            rx.cond(
                e["comment"].to(str) != "",
                smart_markdown(e["comment"]),
                rx.text("（无评语）", size="1", color=COLOR["text_muted"]),
            ),
            spacing="2",
            align="start",
            width="100%",
        ),
        size="1",
        width="100%",
    )


def _experts_panel(c) -> rx.Component:
    """Collapsible '各专家详情' under the synthesized comment.

    Hidden when there is exactly one expert AND it isn't a fallback path —
    no need to show a duplicate of the main comment. The toggle is computed
    on the State as `show_experts_panel`.
    """
    return rx.cond(
        c["show_experts_panel"].to(bool),
        rx.accordion.root(
            rx.accordion.item(
                header=rx.hstack(
                    rx.text(
                        "各专家详情 (" + c["experts_count"].to_string() + " 位)",
                        size="2",
                        weight="medium",
                    ),
                    rx.badge(
                        c["synthesis_method"].to_string(),
                        # Synthesis-method badge: red for total failure, amber
                        # for degraded fallback, otherwise neutral gray (was
                        # blue, but blue read as a primary action chip).
                        color_scheme=rx.cond(
                            c["all_failed"].to(bool),
                            "red",
                            rx.cond(
                                c["degraded"].to(bool), "amber", "gray",
                            ),
                        ),
                        variant="soft",
                        size="1",
                    ),
                    spacing="2",
                    align="center",
                ),
                content=rx.vstack(
                    rx.foreach(
                        c["experts_summary"].to(list[dict[str, Any]]),
                        _expert_card,
                    ),
                    spacing="2",
                    width="100%",
                ),
                value="experts",
            ),
            collapsible=True,
            type="single",
            width="100%",
        ),
        rx.fragment(),
    )


def _question_block(c: dict) -> rx.Component:
    return rx.card(
        rx.vstack(
            rx.hstack(
                rx.heading("Q" + c["q_id"].to(str), size="3"),
                rx.badge(c["type"], size="1", variant="soft"),
                rx.spacer(),
                rx.text(
                    c["score"].to_string() + " / " + c["max_score"].to_string(),
                    size="3", weight="bold",
                ),
                _confidence_badge(c),
                width="100%",
                align="center",
            ),
            rx.cond(
                c["all_failed"].to(bool),
                rx.callout(
                    "AI 批改全部失败 — 请检查 BYOK 配额或更换专家后重新批改本任务。",
                    icon="circle-x",
                    color_scheme="red",
                    size="1",
                ),
                rx.cond(
                    c["low_confidence"],
                    rx.callout(
                        "AI 批改置信度低 — 请人工核对。",
                        icon="triangle-alert",
                        color_scheme="amber",
                        size="1",
                    ),
                    rx.fragment(),
                ),
            ),
            rx.cond(
                c["stem"] != "",
                rx.box(
                    rx.text("题目", size="1", weight="medium", color=COLOR["text_muted"]),
                    smart_markdown(c["stem"]),
                    width="100%",
                ),
                rx.fragment(),
            ),
            rx.text("学生作答", size="1", weight="medium", color=COLOR["text_muted"]),
            rx.box(
                smart_markdown(c["answer_content"]),
                padding="12px",
                background=COLOR["bg_paper"],
                border_radius=RADIUS["sm"],
                width="100%",
            ),
            rx.text("AI 评语", size="1", weight="medium", color=COLOR["text_muted"]),
            smart_markdown(c["comment"]),
            _experts_panel(c),
            _teacher_comment_section(c),
            rx.link(
                rx.button(
                    "查看该题全班分析 →",
                    size="1", variant="ghost", color_scheme="blue",
                ),
                href="/tasks/" + TaskState.current_task_id + "/questions/" + c["q_id"].to(str),
                underline="none",
            ),
            spacing="2",
            align="start",
            width="100%",
        ),
        size="2",
        width="100%",
    )


@rx.page(
    route="/tasks/[task_id]/results/[student_id]",
    title="学生详情 | SmarTAI",
    on_load=TaskStudentDetailState.on_mount,
)
def task_student_detail_page() -> rx.Component:
    return require_auth(
        with_layout(
            TaskState.task_name,
            task_stepper(),
            rx.cond(
                TaskState.viewed_student_exists,
                rx.vstack(
                    section_header(
                        TaskState.viewed_student_name,
                        TaskState.viewed_student_summary,
                    ),
                    rx.foreach(
                        TaskState.viewed_student_corrections,
                        _question_block,
                    ),
                    rx.link(
                        rx.button(
                            rx.icon("arrow-left", size=14), "返回结果总览",
                            variant="soft",
                            color_scheme="gray",
                        ),
                        href="/tasks/" + TaskState.current_task_id + "/results",
                        underline="none",
                    ),
                    spacing="3",
                    width="100%",
                ),
                empty_state(
                    "user-x",
                    "学生不存在",
                    "未在该任务结果中找到此学生。",
                    rx.link(
                        rx.button("返回结果", color_scheme="blue"),
                        href="/tasks/" + TaskState.current_task_id + "/results",
                        underline="none",
                    ),
                ),
            ),
        ),
        require_role="teacher",
    )
