"""Upload problems — /tasks/[task_id]/upload_problems

Single-purpose upload page for the assignment file. Belongs in the linear
flow Setup → Upload Problems → Problems → Upload Submissions → Students →
grading → Results.

On extract completion, `watch_active_job` (state/task.py) yields a redirect
to /problems automatically.
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


PROB_UPLOAD_ID = "task_prob_upload"


class TaskUploadProblemsState(rx.State):
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
                "返回配置",
                variant="soft",
                color_scheme="gray",
                size="3",
            ),
            href="/tasks/" + TaskState.current_task_id + "/setup",
            underline="none",
        ),
        rx.spacer(),
        # Mirror the "查看作答" button on the submissions upload page: once
        # problems have been extracted, surface a direct way into the preview
        # so the teacher doesn't have to wait for the auto-redirect or hit
        # the stepper. Hidden until problem_count > 0.
        rx.cond(
            TaskState.problem_count > 0,
            rx.link(
                rx.button(
                    "查看题目",
                    rx.icon("arrow-right", size=14),
                    size="3",
                    color_scheme="blue",
                ),
                href="/tasks/" + TaskState.current_task_id + "/problems",
                underline="none",
            ),
            rx.fragment(),
        ),
        width="100%",
        align="center",
        padding=f"{SPACE['md']} 0",
        border_top=f"1px solid {COLOR['border']}",
    )


def _problem_upload_card() -> rx.Component:
    can_extract = (
        (rx.selected_files(PROB_UPLOAD_ID).length() > 0)
        & (TaskState.task_status != "extracting_problems")
    )
    has_problems = TaskState.problem_count > 0
    return rx.vstack(
        rx.text(
            "支持 .txt / .pdf / .docx。AI 会自动识别题号、题型、题干、评分要点。",
            size="2",
            color=COLOR["text_muted"],
        ),
        rx.cond(
            has_problems & (TaskState.problem_file_name != ""),
            rx.callout(
                rx.hstack(
                    rx.text("当前已加载：", size="1"),
                    rx.text(TaskState.problem_file_name, size="1", weight="bold"),
                    rx.text("·", size="1"),
                    rx.text(TaskState.problem_count.to_string(), size="1", weight="bold"),
                    rx.text("道题", size="1"),
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
                rx.icon("upload", size=28, color=COLOR["primary"]),
                rx.text("拖拽或点击选择文件", size="2"),
                rx.text(".txt / .pdf / .docx", size="1", color=COLOR["text_muted"]),
                spacing="2",
                align="center",
            ),
            id=PROB_UPLOAD_ID,
            multiple=False,
            accept={
                "text/plain": [".txt"],
                "application/pdf": [".pdf"],
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [".docx"],
            },
            max_files=1,
            border=f"2px dashed {COLOR['border']}",
            border_radius=RADIUS["md"],
            padding=SPACE["lg"],
            width="100%",
            background=COLOR["surface"],
            _hover={"border_color": COLOR["primary"]},
        ),
        rx.cond(
            rx.selected_files(PROB_UPLOAD_ID).length() > 0,
            rx.foreach(
                rx.selected_files(PROB_UPLOAD_ID),
                lambda fname: rx.hstack(rx.icon("file", size=14), rx.text(fname, size="2"), spacing="2"),
            ),
            rx.fragment(),
        ),
        rx.hstack(
            rx.button(
                rx.cond(
                    TaskState.task_status == "extracting_problems",
                    rx.spinner(size="1"),
                    rx.icon("send", size=14),
                ),
                "确认并开始识别",
                on_click=TaskState.upload_problem_file(rx.upload_files(upload_id=PROB_UPLOAD_ID)),
                disabled=~can_extract,
                size="3",
                color_scheme="blue",
            ),
            rx.cond(
                TaskState.task_status == "extracting_problems",
                rx.text("AI 正在识别题目，请耐心等待…完成后会自动跳转到题目预览页", size="2", color=COLOR["text_muted"]),
                rx.fragment(),
            ),
            spacing="3",
            align="center",
        ),
        rx.cond(
            TaskState.task_status == "extracting_problems",
            task_progress_card(),
            rx.fragment(),
        ),
        spacing="3",
        width="100%",
        align="start",
    )


@rx.page(
    route="/tasks/[task_id]/upload_problems",
    title="上传题目 | SmarTAI",
    on_load=TaskUploadProblemsState.on_mount,
)
def task_upload_problems_page() -> rx.Component:
    return require_auth(
        with_layout(
            "上传题目",
            task_stepper(),
            section_header(
                "上传题目文件",
                "AI 会自动识别题目分割、题型、题干和评分标准。识别完成后会自动跳到题目预览页面。",
            ),
            _problem_upload_card(),
            _action_bar(),
        ),
        require_role="teacher",
    )
