"""Per-question detail — /tasks/[task_id]/questions/[q_id]

Phase D fixes:
  D1: common_mistakes is pre-baked during grading, so first visit is hot.
  D2: load_question_detail clears state immediately to avoid Q1→Q2 residue.
  D3: Prev / Next question buttons (disabled at first / last).
  D4: All-student-responses table now has a "教师评语" column.
  D5: Back button goes to /results/by_question, not /results.
"""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.auth_guard import require_auth
from smartai_v2.components.cards import info_card, empty_state, stat_card
from smartai_v2.components.forms import section_header
from smartai_v2.components.layout import with_layout
from smartai_v2.components.markdown import smart_markdown
from smartai_v2.components.nl_query_box import nl_query_box
from smartai_v2.components.task_stepper import task_stepper
from smartai_v2.state.task import TaskState
from smartai_v2.theme import COLOR, RADIUS, SPACE


class TaskQuestionDetailState(rx.State):
    @rx.event
    async def on_mount(self):
        tid = self.task_id
        qid = self.q_id
        if tid and qid:
            return [
                TaskState.load_task(tid),
                TaskState.load_question_detail(qid),
            ]


def _confidence_cell(r) -> rx.Component:
    return rx.cond(
        r["low_confidence"],
        rx.badge(
            rx.icon("triangle-alert", size=10),
            "低 " + r["confidence_display"].to_string(),
            color_scheme="amber",
            variant="solid",
            size="1",
        ),
        rx.badge(
            "AI " + r["confidence_display"].to_string(),
            color_scheme="green",
            variant="soft",
            size="1",
        ),
    )


def _student_response_row(r: dict) -> rx.Component:
    return rx.table.row(
        rx.table.cell(rx.code(r["student_id"])),
        rx.table.cell(r["student_name"]),
        rx.table.cell(r["score"].to_string() + " / " + r["max_score"].to_string()),
        rx.table.cell(r["pct"].to_string() + "%"),
        rx.table.cell(_confidence_cell(r)),
        rx.table.cell(
            rx.box(
                smart_markdown(r["answer"]),
                max_width="280px",
                overflow="auto",
                max_height="120px",
            ),
        ),
        rx.table.cell(
            rx.box(
                smart_markdown(r["comment"]),
                max_width="240px",
                overflow="auto",
                max_height="120px",
            ),
        ),
        rx.table.cell(
            rx.box(
                rx.cond(
                    r["teacher_comment"] != "",
                    smart_markdown(r["teacher_comment"]),
                    rx.text("—", size="2", color=COLOR["text_muted"]),
                ),
                max_width="200px",
                overflow="auto",
                max_height="120px",
            ),
        ),
        rx.table.cell(
            rx.link(
                rx.button("详情", size="1", variant="soft", color_scheme="gray"),
                href="/tasks/" + TaskState.current_task_id + "/results/" + r["student_id"].to(str),
                underline="none",
            ),
        ),
    )


def _stats_row() -> rx.Component:
    return rx.grid(
        stat_card("学生数", TaskState.question_stats_n, "users", COLOR["primary"]),
        stat_card("均分", TaskState.question_stats_avg.to_string(), "trending-up", COLOR["accent"]),
        stat_card("通过率", TaskState.question_stats_pass_rate.to_string() + "%", "award", COLOR["accent"]),
        stat_card("最低 / 最高",
                  TaskState.question_stats_min.to_string() + " / " + TaskState.question_stats_max.to_string(),
                  "bar-chart-2", COLOR["warning"]),
        columns="4",
        spacing="4",
        width="100%",
    )


def _prev_next_nav() -> rx.Component:
    """Prev / Next buttons. Both disabled if no list yet (defensive)."""
    return rx.hstack(
        rx.link(
            rx.button(
                rx.icon("chevron-left", size=14),
                "上一题",
                variant="soft",
                color_scheme="gray",
                size="2",
                disabled=~TaskState.question_has_prev,
            ),
            href="/tasks/" + TaskState.current_task_id + "/questions/" + TaskState.question_prev_q_id,
            underline="none",
            # When disabled, also block the navigation (hard block via CSS)
            pointer_events=rx.cond(TaskState.question_has_prev, "auto", "none"),
        ),
        rx.spacer(),
        rx.text(
            "Q" + TaskState.question_problem_number,
            size="2", weight="bold", color=COLOR["text_muted"],
        ),
        rx.spacer(),
        rx.link(
            rx.button(
                "下一题",
                rx.icon("chevron-right", size=14),
                variant="soft",
                color_scheme="gray",
                size="2",
                disabled=~TaskState.question_has_next,
            ),
            href="/tasks/" + TaskState.current_task_id + "/questions/" + TaskState.question_next_q_id,
            underline="none",
            pointer_events=rx.cond(TaskState.question_has_next, "auto", "none"),
        ),
        width="100%",
        align="center",
    )


@rx.page(
    route="/tasks/[task_id]/questions/[q_id]",
    title="题目详情 | SmarTAI",
    on_load=TaskQuestionDetailState.on_mount,
)
def task_question_detail_page() -> rx.Component:
    return require_auth(
        with_layout(
            TaskState.task_name,
            task_stepper(),
            _prev_next_nav(),
            rx.cond(
                TaskState.question_loading,
                rx.center(
                    rx.vstack(
                        rx.spinner(size="3"),
                        rx.text("AI 正在分析这道题…", size="2", color=COLOR["text_muted"]),
                        spacing="2", align="center",
                    ),
                    padding="48px", width="100%",
                ),
                rx.cond(
                    TaskState.question_q_id != "",
                    rx.vstack(
                        section_header(
                            TaskState.question_section_title,
                            TaskState.question_problem_type,
                        ),
                        info_card(
                            smart_markdown(TaskState.question_problem_stem),
                            title="题干",
                        ),
                        info_card(
                            smart_markdown(TaskState.question_problem_criterion),
                            title="评分标准",
                        ),
                        _stats_row(),
                        info_card(
                            rx.cond(
                                TaskState.question_common_mistakes != "",
                                smart_markdown(TaskState.question_common_mistakes),
                                rx.text(
                                    "AI 易错点尚未生成。点「重新生成」可手动触发。",
                                    size="2", color=COLOR["text_muted"],
                                ),
                            ),
                            rx.button(
                                rx.icon("refresh-cw", size=12),
                                "重新生成",
                                on_click=lambda: TaskState.regenerate_common_mistakes(TaskState.question_q_id),
                                size="1",
                                variant="soft",
                                color_scheme="gray",
                            ),
                            title="AI 易错点分析",
                        ),
                        info_card(
                            rx.text(
                                "针对这道题问 SmarTAI 任何问题（全班作答情况、易错原因、典型错误等）。",
                                size="1", color=COLOR["text_muted"],
                            ),
                            nl_query_box(embed_chart=True, embed_summary=True),
                            title="问 SmarTAI",
                        ),
                        info_card(
                            rx.table.root(
                                rx.table.header(
                                    rx.table.row(
                                        rx.table.column_header_cell("学号"),
                                        rx.table.column_header_cell("姓名"),
                                        rx.table.column_header_cell("得分"),
                                        rx.table.column_header_cell("百分比"),
                                        rx.table.column_header_cell("置信度"),
                                        rx.table.column_header_cell("作答"),
                                        rx.table.column_header_cell("AI 评语"),
                                        rx.table.column_header_cell("教师评语"),
                                        rx.table.column_header_cell(""),
                                    ),
                                ),
                                rx.table.body(
                                    rx.foreach(
                                        TaskState.question_detail_rows,
                                        _student_response_row,
                                    ),
                                ),
                                variant="surface",
                                width="100%",
                            ),
                            title="全班作答详情",
                        ),
                        rx.link(
                            rx.button(
                                rx.icon("arrow-left", size=14),
                                "返回题目列表",
                                variant="soft",
                                color_scheme="gray",
                            ),
                            href="/tasks/" + TaskState.current_task_id + "/results/by_question",
                            underline="none",
                        ),
                        spacing="3",
                        width="100%",
                    ),
                    empty_state("inbox", "题目无数据", "未在该任务中找到此题。"),
                ),
            ),
        ),
        require_role="teacher",
    )
