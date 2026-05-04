"""Task setup — /tasks/[task_id]/setup — 批改任务配置页

This used to be the "do everything" page (upload problems + upload submissions
+ start grading). The user feedback was that the linear flow was unclear, so
Setup is now strictly the *configuration* step:

    Setup (config) → /upload_problems → /problems → /upload_submissions
        → /students → grading → /results

Configuration is held in TaskState (frontend-only mock for this commit). When
the backend gains a Task.config field, the `proceed_to_upload_problems` event
will POST/PUT it; until then the values just guide the UX.

See ROADMAP.md "Setup 配置项后端实现" for the integration TODO list.
"""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.auth_guard import require_auth
from smartai_v2.components.cards import info_card
from smartai_v2.components.forms import section_header
from smartai_v2.components.layout import with_layout
from smartai_v2.components.task_stepper import task_stepper
from smartai_v2.state.experts import ExpertsState
from smartai_v2.state.task import TaskState
from smartai_v2.theme import COLOR, RADIUS, SPACE


class TaskSetupState(rx.State):
    """Route param `task_id` is auto-injected."""

    @rx.event
    async def on_mount(self):
        tid = self.task_id
        if tid:
            return [
                TaskState.load_task(tid),
                TaskState.watch_active_job,
                # Pull the BYOK provider list so the Experts section can show
                # real options instead of a stub disabled select.
                ExpertsState.load,
                # Pull current KB docs for this task — backend is the source
                # of truth (it survives page navigation, frontend State doesn't).
                TaskState.load_kb,
            ]


# ─── Helper layout primitives ────────────────────────────────────────────────

def _config_section(title: str, *children: rx.Component) -> rx.Component:
    """Flat config section — heading + divider + content. No nested cards.

    Matches the "enterprise / less student-y" UI direction agreed on in the
    plan (Phase E).
    """
    return rx.vstack(
        rx.heading(title, size="4", weight="medium"),
        rx.divider(),
        rx.vstack(*children, spacing="3", width="100%", align="start"),
        spacing="3",
        width="100%",
        align="start",
        padding=f"{SPACE['md']} 0",
    )


def _field(label: str, control: rx.Component, hint: str = "") -> rx.Component:
    return rx.vstack(
        rx.text(label, size="2", weight="medium"),
        control,
        rx.cond(
            hint != "",
            rx.text(hint, size="1", color=COLOR["text_muted"]),
            rx.fragment(),
        ),
        spacing="1",
        width="100%",
        align="start",
    )


# ─── Section 1: 任务基础 ────────────────────────────────────────────────────

def _section_basics() -> rx.Component:
    return _config_section(
        "任务基础",
        _field(
            "学科语言",
            rx.select(
                ["auto", "zh", "en"],
                value=TaskState.config_subject_lang,
                on_change=TaskState.set_config_subject_lang,
                size="2",
                width="180px",
            ),
            "auto = 让 AI 根据题面自动判断",
        ),
        _field(
            "截止日期 (可选)",
            rx.input(
                type="date",
                value=TaskState.config_due_at,
                on_change=TaskState.set_config_due_at,
                size="2",
                width="200px",
            ),
            "用于学生提交侧的展示，不影响批改",
        ),
    )


# ─── Section 2: 标答 & 知识库 ────────────────────────────────────────────────


def _kb_doc_row(doc: rx.Var) -> rx.Component:
    """One row in the uploaded KB documents list."""
    return rx.hstack(
        rx.icon("file-text", size=16, color=COLOR["text_muted"]),
        rx.vstack(
            rx.text(doc["filename"].to_string(), size="2", weight="medium"),
            rx.text(
                doc["chunk_count"].to_string() + " 段",
                size="1",
                color=COLOR["text_muted"],
            ),
            spacing="0",
            align="start",
        ),
        rx.spacer(),
        rx.icon_button(
            rx.icon("trash-2", size=14),
            color_scheme="red",
            variant="ghost",
            size="1",
            on_click=lambda: TaskState.delete_kb_doc(doc["doc_id"].to_string()),
        ),
        spacing="2",
        align="center",
        width="100%",
        padding="6px 8px",
        border_radius=RADIUS["sm"],
        background=COLOR["primary_subtle"],
    )


def _kb_upload_widget() -> rx.Component:
    """Reflex upload zone wired to TaskState.upload_kb_file.

    Constraints (mirror backend backend/rag/chunker.py and store.py):
      - 5 MB per file
      - PDF / MD / TXT / RST
      - 3 docs per task max (backend rejects further uploads with 409)
    """
    upload_id = "kb_upload"
    return rx.vstack(
        rx.upload.root(
            rx.vstack(
                rx.icon("cloud-upload", size=22, color=COLOR["primary"]),
                rx.text("拖拽或点击上传参考资料", size="2", weight="medium"),
                rx.text(
                    "PDF / MD / TXT  ·  ≤5MB  ·  本任务最多 3 份",
                    size="1",
                    color=COLOR["text_muted"],
                ),
                spacing="1",
                align="center",
                padding="14px",
            ),
            id=upload_id,
            multiple=False,
            accept={
                "application/pdf": [".pdf"],
                "text/plain": [".txt", ".md", ".markdown", ".rst"],
                "text/markdown": [".md", ".markdown"],
            },
            max_files=1,
            max_size=5 * 1024 * 1024,
            border=f"1px dashed {COLOR['border']}",
            border_radius=RADIUS["md"],
            width="100%",
            on_drop=TaskState.upload_kb_file(rx.upload_files(upload_id=upload_id)),
        ),
        rx.cond(
            TaskState.kb_uploading,
            rx.hstack(
                rx.spinner(size="2"),
                rx.text("正在分块 + 向量化…", size="1", color=COLOR["text_muted"]),
                spacing="2",
                align="center",
            ),
            rx.fragment(),
        ),
        rx.cond(
            TaskState.kb_docs.length() > 0,
            rx.vstack(
                rx.text("已索引文档", size="1", color=COLOR["text_muted"]),
                rx.foreach(TaskState.kb_docs, _kb_doc_row),
                spacing="2",
                width="100%",
                align="start",
            ),
            rx.fragment(),
        ),
        spacing="3",
        width="100%",
        align="start",
    )


def _section_kb() -> rx.Component:
    """Knowledge-base RAG section. Owned by the RAG plan.

    Note: the original "标答与知识库" section was split — reference-answer
    upload moved to _section_reference (this plan), keeping _section_kb
    focused on RAG only.
    """
    return _config_section(
        "知识库 RAG",
        rx.hstack(
            rx.switch(
                checked=TaskState.config_use_kb,
                on_change=TaskState.set_config_use_kb,
            ),
            rx.text("启用知识库 RAG 检索", size="2"),
            rx.badge(
                rx.cond(
                    ExpertsState.provider_count > 0,
                    "已就绪",
                    "需先在 /experts 添加 BYOK key",
                ),
                color_scheme=rx.cond(ExpertsState.provider_count > 0, "green", "amber"),
                variant="soft",
                size="1",
            ),
            spacing="2",
            align="center",
        ),
        rx.cond(
            TaskState.config_use_kb,
            rx.vstack(
                rx.callout(
                    "上传的资料会按 task 隔离存于内存,任务删除或后端重启后失效。"
                    "概念题/证明题在批改时会自动 top-k 检索后注入 prompt。",
                    icon="info",
                    color_scheme="blue",
                    size="1",
                ),
                _kb_upload_widget(),
                spacing="3",
                width="100%",
                align="start",
            ),
            rx.fragment(),
        ),
    )


# ─── Section 2b: 参考答案 + 测试用例（本 plan）─────────────────────────────

def _section_reference() -> rx.Component:
    """Reference-answer + programming test-case upload section.

    These uploads are *auxiliary* — they don't change task.status. The backend
    parses them with the LLM and merges per-q_id into problem_data; calculation
    / programming skills pick them up automatically on the next grade pass.

    See backend/skills/calculation.py for the SymPy-verification ladder and
    backend/skills/programming.py for the sandbox-cases ladder driven by these
    uploads.
    """
    return _config_section(
        "参考答案与测试用例",
        # ─── 标答 (计算题) ──────────────────────────────────────────────────
        rx.heading("标准答案", size="3", weight="medium"),
        rx.text(
            "用于计算题的 SymPy 自动验证；编程题不消费此项。"
            "未上传时 AI 会自行写 SymPy 代码生成参考值。",
            size="1",
            color=COLOR["text_muted"],
        ),
        rx.hstack(
            rx.switch(
                checked=TaskState.config_reference_in_problems,
                on_change=TaskState.set_config_reference_in_problems,
            ),
            rx.text("题目文件已包含标答（无需另传，再上传一次同份文件即可）", size="2"),
            spacing="2",
            align="center",
        ),
        rx.cond(
            TaskState.config_reference_in_problems,
            rx.callout(
                "重新上传题目文件到下方上传区即可 — AI 会从同一份文档里抽取答案部分（不会重复抓取题干）。",
                icon="info",
                color_scheme="blue",
                size="1",
            ),
            rx.fragment(),
        ),
        rx.upload.root(
            rx.vstack(
                rx.icon("cloud-upload", size=22, color=COLOR["primary"]),
                rx.text("拖拽或点击上传标答文件", size="2", weight="medium"),
                rx.text(
                    "PDF / MD / TXT  ·  全部题目放在同一份文件里",
                    size="1",
                    color=COLOR["text_muted"],
                ),
                spacing="1",
                align="center",
                padding="14px",
            ),
            id="reference-upload",
            multiple=False,
            accept={
                "application/pdf": [".pdf"],
                "text/plain": [".txt", ".md", ".markdown"],
                "text/markdown": [".md", ".markdown"],
            },
            max_files=1,
            border=f"1px dashed {COLOR['border']}",
            border_radius=RADIUS["md"],
            width="100%",
            on_drop=TaskState.upload_reference_file(
                rx.upload_files(upload_id="reference-upload")
            ),
        ),
        rx.cond(
            TaskState.reference_file_name != "",
            rx.hstack(
                rx.icon("circle-check", size=14),
                rx.text("已上传：", size="2", color=COLOR["text_muted"]),
                rx.text(TaskState.reference_file_name, size="2", weight="medium"),
                rx.spacer(),
                rx.button(
                    "清除显示",
                    variant="ghost",
                    size="1",
                    on_click=TaskState.clear_reference,
                ),
                spacing="2",
                align="center",
                width="100%",
                padding="6px 8px",
                border_radius=RADIUS["sm"],
                background=COLOR["primary_subtle"],
            ),
            rx.fragment(),
        ),
        # ─── 测试用例 (编程题) ────────────────────────────────────────────
        rx.divider(),
        rx.heading("编程题测试用例", size="3", weight="medium"),
        rx.text(
            "支持 JSON / Markdown / 自然语言 / 代码注释 — AI 会解析为 stdin/stdout 配对。"
            "未上传时：编程题先按关键词规则筛，再让 AI 生成 ≤8 个测试用例；"
            "题目过于复杂时跳过沙箱，评语会注明。",
            size="1",
            color=COLOR["text_muted"],
        ),
        rx.upload.root(
            rx.vstack(
                rx.icon("cloud-upload", size=22, color=COLOR["primary"]),
                rx.text("拖拽或点击上传测试用例", size="2", weight="medium"),
                rx.text(
                    "JSON / MD / TXT  ·  仅编程题消费此项",
                    size="1",
                    color=COLOR["text_muted"],
                ),
                spacing="1",
                align="center",
                padding="14px",
            ),
            id="test-cases-upload",
            multiple=False,
            accept={
                "application/json": [".json"],
                "application/pdf": [".pdf"],
                "text/plain": [".txt", ".md", ".markdown"],
                "text/markdown": [".md", ".markdown"],
            },
            max_files=1,
            border=f"1px dashed {COLOR['border']}",
            border_radius=RADIUS["md"],
            width="100%",
            on_drop=TaskState.upload_test_cases_file(
                rx.upload_files(upload_id="test-cases-upload")
            ),
        ),
        rx.cond(
            TaskState.test_cases_file_name != "",
            rx.hstack(
                rx.icon("circle-check", size=14),
                rx.text("已上传：", size="2", color=COLOR["text_muted"]),
                rx.text(TaskState.test_cases_file_name, size="2", weight="medium"),
                rx.spacer(),
                rx.button(
                    "清除显示",
                    variant="ghost",
                    size="1",
                    on_click=TaskState.clear_test_cases,
                ),
                spacing="2",
                align="center",
                width="100%",
                padding="6px 8px",
                border_radius=RADIUS["sm"],
                background=COLOR["primary_subtle"],
            ),
            rx.fragment(),
        ),
    )


# ─── Section 3: AI 专家 ─────────────────────────────────────────────────────


def _section_experts() -> rx.Component:
    """Live BYOK-aware expert selection.

    Behavior:
      - Empty registry → callout pointing at /experts; both selects locked.
      - 1 provider → main expert auto-fills, synthesis locked to "single".
      - ≥2 providers → user can pick which is the "main" (UI hint only;
        backend grades with all enabled providers in parallel via
        run_multi_expert) and choose synthesis method.

    Important: backend grading currently always runs the full
    registry.list_available() set — this UI controls only the user's mental
    model + which experts they intend to enable on the /experts page. The
    full task-config persistence is a separate backlog item (see ROADMAP).
    """
    return _config_section(
        "AI 专家选择",
        rx.cond(
            ExpertsState.provider_count == 0,
            rx.callout(
                rx.hstack(
                    rx.text("尚未配置任何 BYOK 专家。"),
                    rx.link(
                        "前往 /experts 添加 →",
                        href="/experts",
                        color=COLOR["primary"],
                    ),
                    spacing="2",
                    align="center",
                ),
                icon="triangle-alert",
                color_scheme="amber",
                size="1",
            ),
            rx.fragment(),
        ),
        _field(
            "主专家 (Expert)",
            rx.select.root(
                rx.select.trigger(placeholder="选择一个专家"),
                rx.select.content(
                    rx.foreach(
                        ExpertsState.providers,
                        lambda p: rx.select.item(
                            p["display_name"].to_string(),
                            value=p["provider_id"].to_string(),
                        ),
                    ),
                ),
                value=TaskState.config_main_expert_id,
                on_change=TaskState.set_config_main_expert,
                disabled=ExpertsState.provider_count == 0,
                size="2",
            ),
            "下拉项来自 /experts 页注册的 BYOK key（显示自定义名称）",
        ),
        _field(
            "综合方法",
            rx.vstack(
                rx.select(
                    ["single", "weighted_average", "judge_agent"],
                    value=rx.cond(
                        ExpertsState.provider_count > 1,
                        TaskState.config_synthesis,
                        "single",
                    ),
                    on_change=TaskState.set_config_synthesis,
                    size="2",
                    width="220px",
                    disabled=ExpertsState.provider_count <= 1,
                ),
                rx.unordered_list(
                    rx.list_item(
                        rx.text(
                            rx.code("single"),
                            " — 单专家直出，最快、最便宜。",
                            size="1",
                            color=COLOR["text_muted"],
                        ),
                    ),
                    rx.list_item(
                        rx.text(
                            rx.code("weighted_average"),
                            " — 各专家分数按 confidence 加权平均，无额外 LLM 调用；评语会列出每个专家的理由。",
                            size="1",
                            color=COLOR["text_muted"],
                        ),
                    ),
                    rx.list_item(
                        rx.text(
                            rx.code("judge_agent"),
                            " — 多一次 LLM 合成专家意见 + 写综合评语，质量最好但成本翻倍；解析失败时自动降级为 weighted_average。",
                            size="1",
                            color=COLOR["text_muted"],
                        ),
                    ),
                    margin_top="6px",
                ),
                spacing="2",
                align="start",
            ),
            "需配置 ≥2 个启用的 BYOK 专家时才解锁 weighted_average / judge_agent",
        ),
    )


# ─── Section 4: 批改风格 ─────────────────────────────────────────────────────

def _section_grading_style() -> rx.Component:
    return _config_section(
        "批改风格",
        _field(
            "严格度",
            rx.vstack(
                rx.slider(
                    default_value=[50],
                    min_=0,
                    max=100,
                    step=5,
                    on_change=TaskState.set_config_strictness,
                    width="100%",
                ),
                rx.hstack(
                    rx.text("0 (宽松)", size="1", color=COLOR["text_muted"]),
                    rx.spacer(),
                    rx.badge(
                        TaskState.config_strictness.to_string() + " · "
                        + TaskState.config_strictness_label,
                        color_scheme="blue",
                        variant="soft",
                    ),
                    rx.spacer(),
                    rx.text("100 (严格)", size="1", color=COLOR["text_muted"]),
                    width="100%",
                    align="center",
                ),
                spacing="2",
                width="100%",
            ),
        ),
        rx.hstack(
            rx.switch(checked=TaskState.config_partial_credit, on_change=TaskState.set_config_partial_credit),
            rx.text("允许部分分（按步骤给分）", size="2"),
            spacing="2",
        ),
        # SymPy verification + code sandbox are NOT user-toggleable any more —
        # CalculationSkill / ProgrammingSkill self-decide based on problem
        # type, language detection, and complexity-keyword scan. Each comment
        # carries a metadata footer (`（SymPy 验证：…）` / `（沙箱测评：…）`)
        # so teachers can see what actually happened. See
        # backend/skills/calculation.py and backend/skills/programming.py.
        _field(
            "批改注意事项 (自由文本)",
            rx.text_area(
                placeholder="例：请重点检查推导步骤；忽略书写规范；某概念允许使用同义词…",
                value=TaskState.config_grading_notes,
                on_change=TaskState.set_config_grading_notes,
                rows="4",
                width="100%",
                max_length=500,
            ),
            "最多 500 字，会拼接到 AI 批改提示词",
        ),
    )


# ─── Section 5: 反馈风格 ─────────────────────────────────────────────────────

def _section_feedback_style() -> rx.Component:
    return _config_section(
        "反馈风格",
        _field(
            "评语语气",
            rx.select(
                ["encouraging", "neutral", "strict"],
                value=TaskState.config_tone,
                on_change=TaskState.set_config_tone,
                size="2",
                width="180px",
            ),
        ),
        _field(
            "评语长度",
            rx.select(
                ["short", "medium", "long"],
                value=TaskState.config_length,
                on_change=TaskState.set_config_length,
                size="2",
                width="180px",
            ),
        ),
        rx.hstack(
            rx.switch(
                checked=TaskState.config_suggest_corrections,
                on_change=TaskState.set_config_suggest_corrections,
            ),
            rx.text("给出改错建议", size="2"),
            spacing="2",
        ),
        _field(
            "评语语言",
            rx.select(
                ["auto", "zh", "en"],
                value=TaskState.config_comment_lang,
                on_change=TaskState.set_config_comment_lang,
                size="2",
                width="180px",
            ),
            "auto = 跟随题目语言",
        ),
    )


# ─── Section 6: 质量保证 ─────────────────────────────────────────────────────

def _section_qa() -> rx.Component:
    return _config_section(
        "质量保证",
        _field(
            "低置信度阈值",
            rx.vstack(
                rx.slider(
                    default_value=[60],
                    min_=30,
                    max=80,
                    step=5,
                    on_change=TaskState.set_config_low_conf_threshold,
                    width="100%",
                ),
                rx.hstack(
                    rx.text("0.30", size="1", color=COLOR["text_muted"]),
                    rx.spacer(),
                    rx.badge(
                        "当前: " + TaskState.config_low_conf_threshold_display,
                        color_scheme="blue", variant="soft",
                    ),
                    rx.spacer(),
                    rx.text("0.80", size="1", color=COLOR["text_muted"]),
                    width="100%",
                ),
                spacing="2",
                width="100%",
            ),
            "AI 置信度低于此值会标 ⚠ 提示老师人工复核。置信度范围 0~1（越高越可信）；"
            "结果总览页显示的「低置信题数」即每位学生中超过该阈值的题目数。",
        ),
        # ─── 单专家多采样 (per-task override for settings.multi_sample_n) ───
        # Only meaningful when ≤ 1 BYOK expert is configured — with ≥ 2
        # experts the variance signal comes from the experts themselves.
        # When ≥ 2 we replace the slider with an explanatory callout so the
        # teacher knows the field is a no-op for this task.
        _field(
            "单专家多采样次数",
            rx.cond(
                ExpertsState.provider_count <= 1,
                rx.vstack(
                    rx.slider(
                        default_value=[1],
                        min_=1,
                        max=5,
                        step=1,
                        on_change=TaskState.set_config_multi_sample_n,
                        width="100%",
                    ),
                    rx.hstack(
                        rx.text("1 (省钱)", size="1", color=COLOR["text_muted"]),
                        rx.spacer(),
                        rx.badge(
                            "当前: " + TaskState.config_multi_sample_n.to_string() + "×",
                            color_scheme="blue", variant="soft",
                        ),
                        rx.spacer(),
                        rx.text("5 (公平最强)", size="1", color=COLOR["text_muted"]),
                        width="100%",
                    ),
                    spacing="2",
                    width="100%",
                ),
                rx.callout(
                    "已配置 ≥ 2 个 BYOK 专家 — 自动启用 IS / Minority Veto，无需多采样。",
                    icon="info",
                    color_scheme="blue",
                    size="1",
                ),
            ),
            "仅当只配置 1 个专家时启用：每题在该专家上独立跑 N 次，"
            "用分数方差作为 Indecisiveness Score；> 0.15 触发人工复核标记。"
            "成本随 N 线性放大；普通作业建议 1，重要任务（期末考）建议 3。",
        ),
        rx.hstack(
            rx.switch(checked=TaskState.config_enable_judge, on_change=TaskState.set_config_enable_judge),
            rx.text("启用 Judge Agent 二次校验 (需配辅助专家)", size="2"),
            spacing="2",
        ),
    )


# ─── Action bar ──────────────────────────────────────────────────────────────

def _action_bar() -> rx.Component:
    return rx.hstack(
        rx.button(
            "存为草稿",
            on_click=rx.toast.info("已保存当前配置 (前端 only)"),
            variant="soft",
            color_scheme="gray",
            size="3",
        ),
        rx.spacer(),
        rx.button(
            "开始批改流程",
            rx.icon("arrow-right", size=14),
            on_click=TaskState.proceed_to_upload_problems,
            size="3",
            color_scheme="blue",
        ),
        width="100%",
        align="center",
        padding=f"{SPACE['md']} 0",
        border_top=f"1px solid {COLOR['border']}",
    )


# ─── Page ────────────────────────────────────────────────────────────────────

@rx.page(
    route="/tasks/[task_id]/setup",
    title="批改配置 | SmarTAI",
    on_load=TaskSetupState.on_mount,
)
def task_setup_page() -> rx.Component:
    return require_auth(
        with_layout(
            "批改配置",
            task_stepper(),
            section_header(
                TaskState.task_name,
                "为本次批改配置参考资料、专家、风格与质量门槛。完成后进入下一步上传题目。",
            ),
            _section_basics(),
            _section_reference(),
            _section_kb(),
            _section_experts(),
            _section_grading_style(),
            _section_feedback_style(),
            _section_qa(),
            _action_bar(),
        ),
        require_role="teacher",
    )
