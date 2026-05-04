"""Task students list — /tasks/[task_id]/students"""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.auth_guard import require_auth
from smartai_v2.components.cards import empty_state
from smartai_v2.components.forms import section_header
from smartai_v2.components.layout import with_layout
from smartai_v2.components.task_progress import task_progress_card
from smartai_v2.components.task_stepper import task_stepper
from smartai_v2.components.tables import data_table
from smartai_v2.state.task import TaskState
from smartai_v2.theme import COLOR, SPACE


class TaskStudentsState(rx.State):
    @rx.event
    async def on_mount(self):
        tid = self.task_id
        if tid:
            return [TaskState.load_task(tid), TaskState.watch_active_job]


def _student_row(s: dict) -> rx.Component:
    sid = s["stu_id"].to(str)
    return rx.table.row(
        rx.table.cell(rx.code(sid)),
        rx.table.cell(s["name"]),
        rx.table.cell(rx.code(s["answer_count"].to_string() + " 题")),
        rx.table.cell(
            rx.cond(
                s["flag_count"].to(int) > 0,
                rx.badge(
                    rx.icon("triangle-alert", size=10),
                    s["flag_count"].to_string() + " 个标记",
                    color_scheme="amber",
                    variant="solid",
                    size="1",
                ),
                rx.badge("正常", color_scheme="green", variant="soft", size="1"),
            ),
        ),
        rx.table.cell(
            rx.link(
                rx.button(
                    rx.icon("eye", size=12),
                    "查看",
                    size="1",
                    variant="soft",
                    color_scheme="gray",
                ),
                href="/tasks/" + TaskState.current_task_id + "/students/" + sid,
                underline="none",
            ),
        ),
    )


def _action_bar() -> rx.Component:
    can_grade = (
        (TaskState.student_count > 0)
        & (TaskState.task_status != "grading")
        & (TaskState.task_status != "parsing_submissions")
    )
    return rx.hstack(
        rx.link(
            rx.button(
                rx.icon("arrow-left", size=14),
                "重新上传作业",
                variant="soft",
                color_scheme="gray",
                size="3",
            ),
            href="/tasks/" + TaskState.current_task_id + "/upload_submissions",
            underline="none",
        ),
        rx.spacer(),
        rx.cond(
            TaskState.task_status == "graded",
            rx.link(
                rx.button(
                    "查看批改结果",
                    rx.icon("arrow-right", size=14),
                    size="3",
                    color_scheme="blue",
                ),
                href="/tasks/" + TaskState.current_task_id + "/results",
                underline="none",
            ),
            rx.button(
                rx.cond(
                    TaskState.task_status == "grading",
                    rx.spinner(size="1"),
                    rx.icon("zap", size=14),
                ),
                "开始批改",
                on_click=TaskState.start_grading,
                disabled=~can_grade,
                size="3",
                color_scheme="blue",
            ),
        ),
        width="100%",
        align="center",
        padding=f"{SPACE['md']} 0",
        border_top=f"1px solid {COLOR['border']}",
    )


@rx.page(
    route="/tasks/[task_id]/students",
    title="学生作答 | SmarTAI",
    on_load=TaskStudentsState.on_mount,
)
def task_students_page() -> rx.Component:
    return require_auth(
        with_layout(
            "学生作答",
            task_stepper(),
            section_header(
                "学生作答预览",
                "AI 已自动识别每位学生、按题分割。点击「查看」检查 AI 切分结果"
                + "（可编辑修正）。确认无误后点「开始批改」。",
            ),
            rx.cond(
                TaskState.student_list.length() > 0,
                rx.vstack(
                    rx.hstack(
                        rx.text("总计：", size="2"),
                        rx.text(TaskState.student_list.length().to_string(), size="2", weight="bold"),
                        rx.text("位学生", size="2"),
                        rx.spacer(),
                        width="100%",
                        align="center",
                    ),
                    data_table(
                        ["学号", "姓名", "答题数", "状态", "操作"],
                        TaskState.student_list,
                        _student_row,
                    ),
                    rx.cond(
                        TaskState.task_status == "grading",
                        task_progress_card(),
                        rx.fragment(),
                    ),
                    spacing="3",
                    width="100%",
                ),
                empty_state(
                    "inbox",
                    "暂无学生作答",
                    "请先上传学生作业压缩包。",
                    rx.link(
                        rx.button("去上传作业", color_scheme="blue"),
                        href="/tasks/" + TaskState.current_task_id + "/upload_submissions",
                        underline="none",
                    ),
                ),
            ),
            _action_bar(),
        ),
        require_role="teacher",
    )
