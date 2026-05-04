"""Task results — /tasks/[task_id]/results — student list with filters/sort + NL.

The results section is split into 3 sub-views (each its own route) so the
teacher follows a natural drill-down: overview → per-question → visualization.
The sub-navigation bar (`results_subnav`) appears at the top of each.

Phase E UI: removed nested info_card wrappers around the filter bar, the NL
query box, and the student table. The page is now flatter — heading +
divider + content — to read as enterprise-grade rather than student project.
"""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.auth_guard import require_auth
from smartai_v2.components.cards import info_card, empty_state, stat_card
from smartai_v2.components.forms import section_header
from smartai_v2.components.layout import with_layout
from smartai_v2.components.nl_query_box import nl_query_box
from smartai_v2.components.student_filter_bar import student_filter_bar
from smartai_v2.components.task_stepper import task_stepper
from smartai_v2.state.task import TaskState
from smartai_v2.theme import COLOR, RADIUS, SPACE


# ─── Results sub-navigation ──────────────────────────────────────────────────
# (key, label, icon, route_suffix)
_SUBNAV: list[tuple[str, str, str, str]] = [
    ("overview",    "总览",       "layout-grid",  "results"),
    ("by_question", "按题分析",   "list-checks",  "results/by_question"),
    ("visualize",   "可视化",     "pie-chart",    "visualization"),
]


def _subnav_item(key: str, label: str, icon: str, route_suffix: str, active_key: str) -> rx.Component:
    is_active = active_key == key
    return rx.link(
        rx.hstack(
            rx.icon(icon, size=14),
            rx.text(label, size="2", weight=rx.cond(is_active, "bold", "medium")),
            padding=f"{SPACE['xs']} {SPACE['md']}",
            border_radius=RADIUS["sm"],
            background=rx.cond(is_active, COLOR["primary_subtle"], "transparent"),
            color=rx.cond(is_active, COLOR["primary"], COLOR["text_muted"]),
            _hover={"background": COLOR["primary_subtle"], "color": COLOR["primary"]},
            spacing="2",
            align="center",
        ),
        href="/tasks/" + TaskState.current_task_id + "/" + route_suffix,
        underline="none",
        color="inherit",
    )


def results_subnav(active_key: str) -> rx.Component:
    """Tab-style nav rendered at the top of every results sub-page."""
    return rx.hstack(
        *[_subnav_item(k, l, ic, sfx, active_key) for (k, l, ic, sfx) in _SUBNAV],
        spacing="1",
        align="center",
        padding=SPACE["xs"],
        background=COLOR["surface"],
        border_radius=RADIUS["md"],
        border=f"1px solid {COLOR['border']}",
        width="fit-content",
    )


class TaskResultsState(rx.State):
    @rx.event
    async def on_mount(self):
        tid = self.task_id
        if tid:
            return [TaskState.load_task(tid)]


def _grade_badge(grade) -> rx.Component:
    return rx.match(
        grade,
        ("A", rx.badge("A", color_scheme="green", variant="solid")),
        ("B", rx.badge("B", color_scheme="blue", variant="solid")),
        ("C", rx.badge("C", color_scheme="amber", variant="solid")),
        ("D", rx.badge("D", color_scheme="orange", variant="solid")),
        ("F", rx.badge("F", color_scheme="red", variant="solid")),
        rx.badge(grade, variant="soft"),
    )


def _student_row(idx_item) -> rx.Component:
    idx = idx_item[0]
    s = idx_item[1]
    return rx.table.row(
        rx.table.cell(rx.text((idx + 1).to_string(), weight="bold")),
        rx.table.cell(rx.code(s["id"])),
        rx.table.cell(s["name"]),
        rx.table.cell(s["total"].to_string() + " / " + s["max"].to_string()),
        rx.table.cell(s["pct"].to_string() + "%"),
        rx.table.cell(_grade_badge(s["grade"])),
        rx.table.cell(
            rx.cond(
                s["low_conf_count"].to(int) > 0,
                rx.tooltip(
                    rx.badge(
                        rx.icon("triangle-alert", size=10),
                        s["low_conf_count"].to_string(),
                        color_scheme="amber",
                        variant="solid",
                        size="1",
                    ),
                    content="部分题目 AI 置信度低，建议人工复核。",
                ),
                rx.badge("正常", color_scheme="green", variant="soft", size="1"),
            ),
        ),
        rx.table.cell(
            rx.link(
                rx.button(
                    rx.icon("eye", size=12), "详情",
                    variant="soft", color_scheme="gray", size="1",
                ),
                href="/tasks/" + TaskState.current_task_id + "/results/" + s["id"].to(str),
                underline="none",
            ),
        ),
    )


def _summary_stats() -> rx.Component:
    return rx.grid(
        stat_card("学生数", TaskState.students_count, "users", COLOR["primary"]),
        stat_card("平均分", TaskState.avg_score.to_string() + "%", "trending-up", COLOR["accent"]),
        stat_card("通过率", TaskState.pass_rate.to_string() + "%", "award", COLOR["accent"]),
        stat_card("最高分", TaskState.highest_score.to_string() + "%", "star", COLOR["warning"]),
        columns="4",
        spacing="4",
        width="100%",
    )


@rx.page(
    route="/tasks/[task_id]/results",
    title="批改结果 | SmarTAI",
    on_load=TaskResultsState.on_mount,
)
def task_results_page() -> rx.Component:
    return require_auth(
        with_layout(
            TaskState.task_name,
            task_stepper(),
            results_subnav("overview"),
            section_header(
                "批改结果总览",
                "学生分数与 AI 评语，支持搜索 / 排序 / 自然语言筛选。点学生行查看详细反馈，"
                + "或切换到「按题分析」从题目维度看全班作答。",
            ),
            rx.cond(
                TaskState.task_status == "graded",
                rx.vstack(
                    _summary_stats(),
                    # Filter bar — flat (no card wrapper)
                    info_card(student_filter_bar(), title="筛选", flat=True),
                    # NL query box — flat
                    info_card(nl_query_box(embed_chart=False, embed_summary=True), flat=True),
                    # Student table — flat (the table itself has its own border)
                    info_card(
                        rx.cond(
                            TaskState.filtered_student_stats.length() > 0,
                            rx.table.root(
                                rx.table.header(
                                    rx.table.row(
                                        rx.table.column_header_cell("排名"),
                                        rx.table.column_header_cell("学号"),
                                        rx.table.column_header_cell("姓名"),
                                        rx.table.column_header_cell("得分"),
                                        rx.table.column_header_cell("百分比"),
                                        rx.table.column_header_cell("等级"),
                                        rx.table.column_header_cell(
                                            rx.tooltip(
                                                rx.hstack(
                                                    rx.text("低置信题数", size="2"),
                                                    rx.icon("info", size=12, color=COLOR["text_muted"]),
                                                    spacing="1",
                                                    align="center",
                                                ),
                                                content=(
                                                    "该学生中 AI 置信度 < 0.6 的题数。"
                                                    "置信度本身是 0~1 的小数（越高越可信），"
                                                    "可在「批改设置 → 质量保证」里调阈值。"
                                                ),
                                            ),
                                        ),
                                        rx.table.column_header_cell("操作"),
                                    ),
                                ),
                                rx.table.body(
                                    rx.foreach(
                                        TaskState.filtered_student_stats,
                                        lambda s, i: _student_row((i, s)),
                                    ),
                                ),
                                variant="surface",
                                width="100%",
                            ),
                            empty_state("filter-x", "无匹配学生",
                                        "尝试清除筛选 / 自然语言条件。"),
                        ),
                        title="学生列表",
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
