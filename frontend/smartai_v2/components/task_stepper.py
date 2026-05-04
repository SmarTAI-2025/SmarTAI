"""Task stepper — top-of-page workflow indicator (Setup → Upload → Review → Results).

Phase B (linear flow rework): Setup is now config-only; uploads have their own
pages; review (students) hosts the "start grading" action; results contains its
own sub-nav for overview / per-question / visualization.

Phase F (style): five visual states keyed on (status × is_active_route):
  - 未完成 + 不在该页 → 浅灰圈 outline
  - 未完成 + 在该页    → 蓝填充 + 蓝竖条 + 蓝字
  - 已完成 + 不在该页 → 绿圈 outline + 灰字
  - 已完成 + 在该页    → 蓝填充 + 蓝竖条 + 蓝字 (右下角小绿勾)
  - 进行中             → 蓝填充 + 蓝竖条 + spinner

The current-page (route) detection uses `rx.State.router.page.path` —
exposed through a State computed property; see TaskState.current_subpath.
"""
from __future__ import annotations

import reflex as rx

from smartai_v2.state.task import TaskState
from smartai_v2.theme import COLOR, RADIUS, SPACE


# (key, label, icon, route_suffix, route_prefix_match)
# `route_prefix_match` is the substring we test the URL's last segment(s) against
# to decide "is the user currently on this step's family of pages".
STEPS: list[tuple[str, str, str, str, tuple[str, ...]]] = [
    ("setup",       "配置",     "settings",      "setup",                ("setup",)),
    ("upload_p",    "上传题目", "file-text",     "upload_problems",      ("upload_problems",)),
    ("view_p",      "查看题目", "list-checks",   "problems",             ("problems",)),
    ("upload_s",    "上传作业", "file-archive",  "upload_submissions",   ("upload_submissions",)),
    ("review",      "审阅批改", "user-check",    "students",             ("students",)),
    ("results",     "查看结果", "bar-chart-3",   "results",              ("results", "visualization", "questions")),
]


def _is_complete_for_step(key: str, status) -> rx.Var:
    """phase-completed → green outline circle.

    Linear flow: each step's "completed" condition is "the next step's status
    has been reached or surpassed".
    """
    if key == "setup":
        # setup is a no-op (just config); consider it always completed once
        # any progress has been made.
        return (status == "extracting_problems") | status.to_string().contains("ready") | (status == "grading") | (status == "graded") | (status == "parsing_submissions")
    if key == "upload_p":
        return (status == "problems_ready") | (status == "parsing_submissions") | (status == "submissions_ready") | (status == "grading") | (status == "graded")
    if key == "view_p":
        # "查看题目" is implicitly complete once submissions are parsed (teacher has moved past it).
        return (status == "parsing_submissions") | (status == "submissions_ready") | (status == "grading") | (status == "graded")
    if key == "upload_s":
        return (status == "submissions_ready") | (status == "grading") | (status == "graded")
    if key == "review":
        return (status == "grading") | (status == "graded")
    if key == "results":
        return status == "graded"
    return False


def _is_in_progress_for_step(key: str, status) -> rx.Var:
    """phase-active (currently running backend job)."""
    if key == "upload_p":
        return status == "extracting_problems"
    if key == "upload_s":
        return status == "parsing_submissions"
    if key == "results":
        return status == "grading"
    return rx.Var.create(False)


def _is_active_route(prefix_match: tuple[str, ...], current_subpath) -> rx.Var:
    """is the user actually viewing a page that belongs to this step?"""
    # We OR-fold prefix_match — but Reflex Vars don't support multi-arg OR
    # cleanly, so we build a chained expression via match.
    # Implementation note: contains() works in Reflex via Var.to_string().contains.
    # We use a simple "current_subpath in prefix_match" computed inline.
    # Build the OR chain:
    expr = current_subpath == prefix_match[0]
    for m in prefix_match[1:]:
        expr = expr | (current_subpath == m)
    return expr


def _step(key: str, label: str, icon: str, route_suffix: str,
          prefix_match: tuple[str, ...],
          status, current_subpath, task_id) -> rx.Component:
    is_complete = _is_complete_for_step(key, status)
    is_in_progress = _is_in_progress_for_step(key, status)
    is_active = _is_active_route(prefix_match, current_subpath)

    # Decide the "circle" appearance from a 4-way priority:
    #   in_progress > active > complete > pending
    circle_bg = rx.cond(
        is_in_progress | is_active,
        COLOR["primary"],                         # solid blue fill
        rx.cond(
            is_complete,
            COLOR["surface"],                     # white fill, will get green outline
            COLOR["surface"],                     # white fill, gray outline
        ),
    )
    circle_border = rx.cond(
        is_in_progress | is_active,
        f"2px solid {COLOR['primary']}",
        rx.cond(
            is_complete,
            f"2px solid {COLOR['accent']}",       # green outline
            f"1px solid var(--sand-7)",           # gray outline
        ),
    )
    icon_color = rx.cond(
        is_in_progress | is_active,
        "white",
        rx.cond(is_complete, COLOR["accent"], COLOR["text_muted"]),
    )
    label_color = rx.cond(
        is_active | is_in_progress,
        COLOR["primary"],
        rx.cond(is_complete, COLOR["text"], COLOR["text_muted"]),
    )
    label_weight = rx.cond(is_active | is_in_progress, "bold", "medium")

    # Left vertical accent bar when this is the current page
    accent_bar = rx.box(
        width="3px",
        height="32px",
        background=rx.cond(is_active, COLOR["primary"], "transparent"),
        border_radius="2px",
        flex_shrink="0",
    )

    return rx.link(
        rx.hstack(
            accent_bar,
            rx.box(
                rx.cond(
                    is_in_progress,
                    rx.spinner(size="2"),
                    rx.icon(icon, size=18),
                ),
                background=circle_bg,
                border=circle_border,
                color=icon_color,
                padding=SPACE["sm"],
                border_radius=RADIUS["full"],
                flex_shrink="0",
                display="flex",
                align_items="center",
                justify_content="center",
                width="38px",
                height="38px",
                position="relative",
            ),
            rx.text(label, size="2", weight=label_weight, color=label_color),
            spacing="2",
            align="center",
            padding=f"{SPACE['xs']} {SPACE['sm']}",
            border_radius=RADIUS["sm"],
            _hover={"background": "var(--sand-3)"},
            cursor="pointer",
        ),
        href="/tasks/" + task_id + "/" + route_suffix,
        underline="none",
        color="inherit",
    )


def task_stepper() -> rx.Component:
    """Full-width stepper navigation."""
    return rx.hstack(
        *[
            _step(k, l, ic, sfx, pmatch,
                  TaskState.task_status, TaskState.current_subpath,
                  TaskState.current_task_id)
            for (k, l, ic, sfx, pmatch) in STEPS
        ],
        spacing="3",
        align="center",
        width="100%",
        padding=SPACE["md"],
        background=COLOR["surface"],
        border_radius=RADIUS["md"],
        border=f"1px solid {COLOR['border']}",
        flex_wrap="wrap",
    )
