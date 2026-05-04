"""Upload submissions — /tasks/[task_id]/upload_submissions

Single-purpose upload page for the student-answer archive. Sits between
/problems and /students in the linear flow.

On parse completion, `watch_active_job` (state/task.py) yields a redirect
to /students automatically.
"""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.auth_guard import require_auth
from smartai_v2.components.cards import info_card
from smartai_v2.components.forms import section_header
from smartai_v2.components.layout import with_layout
from smartai_v2.components.task_progress import task_progress_card
from smartai_v2.components.task_stepper import task_stepper
from smartai_v2.state.task import TaskState
from smartai_v2.theme import COLOR, RADIUS, SPACE


HW_UPLOAD_ID = "task_hw_upload"


class TaskUploadSubmissionsState(rx.State):
    @rx.event
    async def on_mount(self):
        tid = self.task_id
        if tid:
            return [
                TaskState.load_task(tid),
                TaskState.watch_active_job,
            ]


def _action_bar() -> rx.Component:
    return rx.hstack(
        rx.link(
            rx.button(
                rx.icon("arrow-left", size=14),
                "返回题目",
                variant="soft",
                color_scheme="gray",
                size="3",
            ),
            href="/tasks/" + TaskState.current_task_id + "/problems",
            underline="none",
        ),
        rx.spacer(),
        rx.cond(
            TaskState.student_count > 0,
            rx.link(
                rx.button(
                    "查看作答",
                    rx.icon("arrow-right", size=14),
                    size="3",
                    color_scheme="blue",
                ),
                href="/tasks/" + TaskState.current_task_id + "/students",
                underline="none",
            ),
            rx.fragment(),
        ),
        width="100%",
        align="center",
        padding=f"{SPACE['md']} 0",
        border_top=f"1px solid {COLOR['border']}",
    )


def _submission_upload_card() -> rx.Component:
    has_problems = TaskState.problem_count > 0
    can_parse = (
        has_problems
        & (rx.selected_files(HW_UPLOAD_ID).length() > 0)
        & (TaskState.task_status != "parsing_submissions")
        & (TaskState.task_status != "extracting_problems")
    )
    has_subs = TaskState.student_count > 0
    return rx.vstack(
        rx.text(
            "支持压缩包格式 .zip / .rar / .7z / .tar.gz / .bz2。每位学生的作答文件可以是 .txt / .pdf / .docx / .ipynb 等。",
            size="2",
            color=COLOR["text_muted"],
        ),
        rx.cond(
            ~has_problems,
            rx.callout(
                "请先上传题目，再上传学生作业。",
                icon="info",
                color_scheme="amber",
                size="1",
            ),
            rx.fragment(),
        ),
        rx.cond(
            has_subs & (TaskState.submission_file_name != ""),
            rx.callout(
                rx.hstack(
                    rx.text("当前已加载：", size="1"),
                    rx.text(TaskState.submission_file_name, size="1", weight="bold"),
                    rx.text("·", size="1"),
                    rx.text(TaskState.student_count.to_string(), size="1", weight="bold"),
                    rx.text("位学生", size="1"),
                    spacing="1",
                ),
                icon="check",
                color_scheme="green",
                size="1",
            ),
            rx.fragment(),
        ),
        rx.upload(
            rx.vstack(
                rx.icon("file-archive", size=28, color=COLOR["primary"]),
                rx.text("拖拽压缩包或点击选择", size="2"),
                rx.text(".zip / .rar / .7z / .tar.gz / .bz2", size="1", color=COLOR["text_muted"]),
                spacing="2",
                align="center",
            ),
            id=HW_UPLOAD_ID,
            multiple=False,
            max_files=1,
            disabled=~has_problems,
            border=f"2px dashed {COLOR['border']}",
            border_radius=RADIUS["md"],
            padding=SPACE["lg"],
            width="100%",
            background=COLOR["surface"],
            _hover={"border_color": COLOR["primary"]},
        ),
        rx.cond(
            rx.selected_files(HW_UPLOAD_ID).length() > 0,
            rx.foreach(
                rx.selected_files(HW_UPLOAD_ID),
                lambda fname: rx.hstack(rx.icon("file", size=14), rx.text(fname, size="2"), spacing="2"),
            ),
            rx.fragment(),
        ),
        rx.hstack(
            rx.button(
                rx.cond(
                    TaskState.task_status == "parsing_submissions",
                    rx.spinner(size="1"),
                    rx.icon("send", size=14),
                ),
                "确认并开始识别",
                on_click=TaskState.upload_submission_archive(rx.upload_files(upload_id=HW_UPLOAD_ID)),
                disabled=~can_parse,
                size="3",
                color_scheme="blue",
            ),
            rx.cond(
                TaskState.task_status == "parsing_submissions",
                rx.text("AI 正在解析每位学生的作答…完成后会自动跳到作答预览页", size="2", color=COLOR["text_muted"]),
                rx.fragment(),
            ),
            spacing="3",
            align="center",
        ),
        rx.cond(
            TaskState.task_status == "parsing_submissions",
            task_progress_card(),
            rx.fragment(),
        ),
        spacing="3",
        width="100%",
        align="start",
    )


@rx.page(
    route="/tasks/[task_id]/upload_submissions",
    title="上传学生作业 | SmarTAI",
    on_load=TaskUploadSubmissionsState.on_mount,
)
def task_upload_submissions_page() -> rx.Component:
    return require_auth(
        with_layout(
            "上传学生作业",
            task_stepper(),
            section_header(
                "上传学生作业压缩包",
                "AI 会自动识别每位学生、按题分割每个人的作答。完成后会自动跳到学生预览页。",
            ),
            _submission_upload_card(),
            _action_bar(),
        ),
        require_role="teacher",
    )
