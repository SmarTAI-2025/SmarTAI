"""Task visualization — /tasks/[task_id]/visualization"""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.auth_guard import require_auth
from smartai_v2.components.cards import info_card, empty_state, stat_card
from smartai_v2.components.forms import section_header
from smartai_v2.components.layout import with_layout
from smartai_v2.components.nl_query_box import nl_query_box
from smartai_v2.components.task_stepper import task_stepper
from smartai_v2.pages.task_results import results_subnav
from smartai_v2.state.task import TaskState
from smartai_v2.theme import COLOR


class TaskVisualizationState(rx.State):
    @rx.event
    async def on_mount(self):
        tid = self.task_id
        if tid:
            return [TaskState.load_task(tid)]


def _stats_row() -> rx.Component:
    return rx.grid(
        stat_card("学生数", TaskState.students_count, "users", COLOR["primary"]),
        stat_card("平均分", TaskState.avg_score.to_string() + "%", "trending-up", COLOR["accent"]),
        stat_card("通过率", TaskState.pass_rate.to_string() + "%", "award", COLOR["accent"]),
        stat_card("最高分", TaskState.highest_score.to_string() + "%", "star", COLOR["warning"]),
        columns="4",
        spacing="4",
        width="100%",
    )


def _charts_grid() -> rx.Component:
    return rx.grid(
        info_card(
            rx.cond(
                TaskState.fig_score_distribution,
                rx.plotly(data=TaskState.fig_score_distribution, height="320px"),
                rx.text("无数据", size="1", color=COLOR["text_muted"]),
            ),
            title="分数分布 (% 每位学生)",
        ),
        info_card(
            rx.cond(
                TaskState.fig_grade_pie,
                rx.plotly(data=TaskState.fig_grade_pie, height="320px"),
                rx.text("无数据", size="1", color=COLOR["text_muted"]),
            ),
            title="等级分布 (A/B/C/D/F)",
        ),
        info_card(
            rx.cond(
                TaskState.fig_per_question,
                rx.plotly(data=TaskState.fig_per_question, height="320px"),
                rx.text("无数据", size="1", color=COLOR["text_muted"]),
            ),
            title="逐题平均分 vs 满分",
        ),
        columns="2",
        spacing="4",
        width="100%",
    )


@rx.page(
    route="/tasks/[task_id]/visualization",
    title="可视化 | SmarTAI",
    on_load=TaskVisualizationState.on_mount,
)
def task_visualization_page() -> rx.Component:
    return require_auth(
        with_layout(
            TaskState.task_name,
            task_stepper(),
            results_subnav("visualize"),
            section_header(
                "可视化",
                "内置图表 + AI 自然语言生成自定义图。",
            ),
            rx.cond(
                TaskState.task_status == "graded",
                rx.vstack(
                    _stats_row(),
                    _charts_grid(),
                    info_card(nl_query_box(embed_chart=True, embed_summary=True), flat=True),
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
