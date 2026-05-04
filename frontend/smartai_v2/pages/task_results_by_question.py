"""Per-question results — /tasks/[task_id]/results/by_question

Lists every problem with its class-level statistics, and lets the teacher
click into a single question for the deep AI breakdown
(`/tasks/[task_id]/questions/[q_id]`).
"""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.auth_guard import require_auth
from smartai_v2.components.cards import info_card, empty_state, stat_card
from smartai_v2.components.forms import section_header
from smartai_v2.components.layout import with_layout
from smartai_v2.components.task_stepper import task_stepper
from smartai_v2.pages.task_results import results_subnav
from smartai_v2.state.task import TaskState
from smartai_v2.theme import COLOR


class TaskResultsByQuestionState(rx.State):
    @rx.event
    async def on_mount(self):
        tid = self.task_id
        if tid:
            return [TaskState.load_task(tid)]


def _q_row(q: dict) -> rx.Component:
    return rx.table.row(
        rx.table.cell(rx.code(q["q_id"])),
        rx.table.cell(q["avg"].to_string() + " / " + q["max"].to_string()),
        rx.table.cell(q["pct"].to_string() + "%"),
        rx.table.cell(q["count"].to_string()),
        rx.table.cell(
            rx.link(
                rx.button(
                    rx.icon("microscope", size=12),
                    "深入分析",
                    size="1",
                    variant="soft",
                    color_scheme="blue",
                ),
                href="/tasks/" + TaskState.current_task_id + "/questions/" + q["q_id"].to(str),
                underline="none",
            ),
        ),
    )


def _summary_stats() -> rx.Component:
    return rx.grid(
        stat_card("学生数", TaskState.students_count, "users", COLOR["primary"]),
        stat_card("题目数", TaskState.problem_count, "list-checks", COLOR["accent"]),
        stat_card("平均分", TaskState.avg_score.to_string() + "%", "trending-up", COLOR["accent"]),
        stat_card("通过率", TaskState.pass_rate.to_string() + "%", "award", COLOR["warning"]),
        columns="4",
        spacing="4",
        width="100%",
    )


@rx.page(
    route="/tasks/[task_id]/results/by_question",
    title="按题分析 | SmarTAI",
    on_load=TaskResultsByQuestionState.on_mount,
)
def task_results_by_question_page() -> rx.Component:
    return require_auth(
        with_layout(
            TaskState.task_name,
            task_stepper(),
            results_subnav("by_question"),
            section_header(
                "按题分析",
                "每道题的全班得分情况。点「深入分析」进入题目详情：AI 易错点 / "
                + "全班作答 / 教师评语 / 单题 AI 问答。",
            ),
            rx.cond(
                TaskState.task_status == "graded",
                rx.vstack(
                    _summary_stats(),
                    info_card(
                        rx.cond(
                            TaskState.per_question_stats.length() > 0,
                            rx.table.root(
                                rx.table.header(
                                    rx.table.row(
                                        rx.table.column_header_cell("题目"),
                                        rx.table.column_header_cell("均分 / 满分"),
                                        rx.table.column_header_cell("百分比"),
                                        rx.table.column_header_cell("作答数"),
                                        rx.table.column_header_cell(""),
                                    ),
                                ),
                                rx.table.body(
                                    rx.foreach(TaskState.per_question_stats, _q_row),
                                ),
                                variant="surface",
                                width="100%",
                            ),
                            empty_state("inbox", "暂无题目统计", "请先完成批改。"),
                        ),
                        title="题目列表",
                        flat=True,
                    ),
                    spacing="4",
                    width="100%",
                ),
                empty_state(
                    "clock",
                    "尚未批改",
                    "请先完成批改流程。",
                    rx.link(
                        rx.button("去配置", color_scheme="blue"),
                        href="/tasks/" + TaskState.current_task_id + "/setup",
                        underline="none",
                    ),
                ),
            ),
        ),
        require_role="teacher",
    )
