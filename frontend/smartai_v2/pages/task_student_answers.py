"""Per-student parsed answer detail — /tasks/[task_id]/students/[student_id]

Shows the AI-segmented student answers (one card per question) with the
question stem, the parsed answer, and any recognition flags. The teacher
can edit any answer in-place (Phase C of the redesign) — fixes for AI
OCR / segmentation errors.

This is the "verify before grading" view — separate from
`task_student_detail.py` which shows post-grading scores + comments.
"""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.auth_guard import require_auth
from smartai_v2.components.cards import empty_state
from smartai_v2.components.forms import section_header
from smartai_v2.components.layout import with_layout
from smartai_v2.components.markdown import smart_markdown
from smartai_v2.components.task_stepper import task_stepper
from smartai_v2.state.task import TaskState
from smartai_v2.theme import COLOR, RADIUS, SPACE


class TaskStudentAnswersState(rx.State):
    """Route params `task_id`, `student_id` are auto-injected."""

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


def _answer_card(a: dict) -> rx.Component:
    edit_key = TaskState.current_student_id + "::" + a["q_id"].to(str)
    is_editing = TaskState.editing_answer_key == edit_key
    return rx.card(
        rx.vstack(
            rx.hstack(
                rx.heading("Q" + a["number"].to(str), size="3"),
                rx.badge(a["type"], variant="soft", size="1"),
                rx.spacer(),
                rx.cond(
                    a["has_flag"],
                    rx.tooltip(
                        rx.badge(
                            rx.icon("triangle-alert", size=12),
                            "识别标记",
                            color_scheme="amber",
                            variant="solid",
                            size="1",
                        ),
                        content=a["flag_text"],
                    ),
                    rx.fragment(),
                ),
                rx.cond(
                    is_editing,
                    rx.hstack(
                        rx.button(
                            rx.icon("save", size=12), "保存",
                            on_click=lambda: TaskState.save_answer_edit(a["q_id"]),
                            size="1",
                            color_scheme="blue",
                        ),
                        rx.button(
                            rx.icon("x", size=12), "取消",
                            on_click=TaskState.cancel_answer_edit,
                            size="1",
                            variant="soft",
                            color_scheme="gray",
                        ),
                        spacing="1",
                    ),
                    rx.button(
                        rx.icon("pencil", size=12), "编辑",
                        on_click=lambda: TaskState.start_answer_edit(a["q_id"], a["content_raw"]),
                        size="1",
                        variant="soft",
                        color_scheme="gray",
                    ),
                ),
                width="100%",
                align="center",
            ),
            rx.cond(
                a["stem"] != "",
                rx.box(
                    rx.text("题干", size="1", weight="medium", color=COLOR["text_muted"]),
                    smart_markdown(a["stem"]),
                    width="100%",
                ),
                rx.fragment(),
            ),
            rx.text("识别答案", size="1", weight="medium", color=COLOR["text_muted"]),
            rx.cond(
                is_editing,
                rx.text_area(
                    value=TaskState.edit_answer_input,
                    on_change=TaskState.set_edit_answer_input,
                    rows="6",
                    width="100%",
                    placeholder="编辑识别后的作答（保留 LaTeX 分隔符 \\(...\\) 或 \\[...\\]）",
                ),
                rx.box(
                    rx.cond(
                        a["content"] != "",
                        smart_markdown(a["content"]),
                        rx.text("(空)", size="2", color=COLOR["text_muted"], font_style="italic"),
                    ),
                    padding="12px",
                    background=COLOR["bg_paper"],
                    border_radius=RADIUS["sm"],
                    width="100%",
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
                rx.icon("arrow-left", size=14), "返回学生列表",
                variant="soft",
                color_scheme="gray",
                size="3",
            ),
            href="/tasks/" + TaskState.current_task_id + "/students",
            underline="none",
        ),
        rx.spacer(),
        rx.cond(
            TaskState.task_status == "graded",
            rx.link(
                rx.button(
                    "查看批改详情",
                    rx.icon("arrow-right", size=14),
                    color_scheme="blue",
                    size="3",
                ),
                href="/tasks/" + TaskState.current_task_id + "/results/" + TaskState.current_student_id,
                underline="none",
            ),
            rx.fragment(),
        ),
        width="100%",
        align="center",
        padding=f"{SPACE['md']} 0",
        border_top=f"1px solid {COLOR['border']}",
    )


@rx.page(
    route="/tasks/[task_id]/students/[student_id]",
    title="学生作答详情 | SmarTAI",
    on_load=TaskStudentAnswersState.on_mount,
)
def task_student_answers_page() -> rx.Component:
    return require_auth(
        with_layout(
            "学生作答详情",
            task_stepper(),
            rx.cond(
                TaskState.viewed_student_meta.get("stu_id", "").to(str) != "",
                rx.vstack(
                    section_header(
                        "学生：" + TaskState.viewed_student_meta.get("name", "").to(str),
                        "学号：" + TaskState.viewed_student_meta.get("stu_id", "").to(str)
                        + " · 共 " + TaskState.viewed_student_meta.get("answer_count", 0).to_string()
                        + " 道题。逐题检查 AI 切分，发现错误可点「编辑」修正后再批改。",
                    ),
                    rx.cond(
                        TaskState.viewed_student_answers.length() > 0,
                        rx.vstack(
                            rx.foreach(TaskState.viewed_student_answers, _answer_card),
                            spacing="3",
                            width="100%",
                        ),
                        empty_state(
                            "inbox",
                            "暂无识别结果",
                            "该学生没有任何已识别的作答。",
                        ),
                    ),
                    _action_bar(),
                    spacing="3",
                    width="100%",
                ),
                empty_state(
                    "user-x",
                    "学生不存在",
                    "找不到该学生的作答记录。",
                    rx.link(
                        rx.button("返回学生列表", color_scheme="blue"),
                        href="/tasks/" + TaskState.current_task_id + "/students",
                        underline="none",
                    ),
                ),
            ),
        ),
        require_role="teacher",
    )
