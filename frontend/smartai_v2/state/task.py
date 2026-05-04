"""Task state — unified, task-centric state management for the v2 frontend.

Replaces the global IngestState (problem_data + student_data) and per-page
GradingState polling with a single coherent flow:

    create_task → upload problems → upload submissions → grade → results

Key design points:
  - `current_task_id` is persisted in browser localStorage so reload restores it.
  - Progress polling runs as a single `@rx.event(background=True)` event
    (`watch_active_job`). It polls `GET /tasks/{id}/state` every 1.5s while the
    task is in any active phase (extracting / parsing / grading) and stops once
    the task transitions to *_ready, graded, or error. Critically: the polling
    is keyed to the State, not to a page — switching pages does NOT cancel it.
  - File hashes for dedup live on the backend; the frontend just retries the
    same call and reads `status: already_running | already_done` from the
    response to update its UI.
"""
from __future__ import annotations

import asyncio
import datetime
import time
from typing import Any

import reflex as rx

try:
    import plotly.graph_objects as go
except ImportError:
    go = None  # type: ignore

from smartai_v2.api import tasks as tasks_api
from smartai_v2.api import analytics as analytics_api
from smartai_v2.api import kb as kb_api
from smartai_v2.api.client import APIError
from smartai_v2.components.markdown import format_math
from smartai_v2.state.auth import AuthState


ACTIVE_STATUSES = (
    "extracting_problems",
    "parsing_submissions",
    "grading",
)
NL_COOLDOWN_SEC = 30.0


class TaskState(rx.State):
    # ─── Task list & selection ────────────────────────────────────────────

    tasks: dict[str, dict[str, Any]] = {}                # task_id → lite metadata
    current_task_id: str = rx.LocalStorage("", name="smartai_current_task_id")

    # ─── Current task data (loaded on select) ─────────────────────────────

    current_task: dict[str, Any] = {}                    # full lite + problem_data + student_data
    current_problem_data: dict[str, dict[str, Any]] = {}
    current_student_data: dict[str, dict[str, Any]] = {}
    current_results: dict[str, Any] = {}

    # ─── Active sub-job progress ──────────────────────────────────────────

    progress: dict[str, Any] = {}                        # reporter snapshot
    polling: bool = False
    progress_phase: str = ""                             # "extracting" / "parsing" / "grading" / "done" / ...

    # ─── New task creation ────────────────────────────────────────────────

    new_task_name: str = ""

    # ─── NL Query ─────────────────────────────────────────────────────────

    nl_query_input: str = ""
    nl_query_mode: str = "filter"                        # "filter" / "summary" / "chart"
    nl_filter_student_ids: list[str] = []
    nl_filter_explanation: str = ""
    nl_summary_md: str = ""
    nl_chart_traces: list[dict[str, Any]] = []     # raw traces from LLM
    nl_chart_layout: dict[str, Any] = {}            # raw layout from LLM
    nl_chart_title: str = ""
    nl_chart_rationale: str = ""
    nl_loading: bool = False
    nl_error: str = ""
    nl_last_query_at: float = 0.0
    nl_cooldown_remaining: int = 0

    # ─── Per-question detail ──────────────────────────────────────────────

    question_detail: dict[str, Any] = {}                 # /analytics/{id}/per_question/{q_id} payload
    question_loading: bool = False

    # ─── Currently viewed student (for /tasks/[id]/results/[student_id]) ──

    current_student_id: str = ""

    # ─── Student filters (UI state) ───────────────────────────────────────

    search_query: str = ""
    sort_by: str = "score_desc"                          # "score_desc"|"score_asc"|"name"|"id"
    quick_filter: str = "all"                            # "all"|"failing"|"top10"|"flagged"

    # ─── Generic error/loading ────────────────────────────────────────────

    error: str = ""

    # ─── Problem editing (in-place edit of stem/criterion) ────────────────

    editing_q_id: str = ""                               # which question is being edited
    edit_stem_input: str = ""                            # raw textarea contents
    edit_criterion_input: str = ""

    # ─── Student answer editing (manual OCR / segmentation correction) ────
    # editing_answer_key = "" or f"{stu_id}::{q_id}"

    editing_answer_key: str = ""
    edit_answer_input: str = ""

    # ─── Teacher comments (per (student_id, q_id)) ────────────────────────
    # Keyed as f"{student_id}::{q_id}". Persisted to backend via
    # /tasks/{task_id}/comments. Pre-populated from current_results when
    # the task is loaded.

    teacher_comments: dict[str, str] = {}
    editing_comment_key: str = ""                        # which student/q comment is being edited
    edit_comment_input: str = ""

    # ─── Setup task config (frontend mock — backend接口待实现, see ROADMAP) ──
    # All `config_*` fields are persisted only on the frontend State for now.
    # When the backend Task model gains a `config` field these get plumbed
    # through `create_task` / `start_grading`. UI is fully wired in /setup.

    config_subject_lang: str = "auto"                    # auto / zh / en
    config_due_at: str = ""                              # YYYY-MM-DD or empty
    config_use_kb: bool = False
    config_selected_kb_ids: list[str] = []
    # Backend-mirrored KB docs for the current task — populated by load_kb /
    # upload_kb / delete_kb events. Each entry is the KBDoc.public() shape:
    # {doc_id, filename, sha256, chunk_count, uploaded_at}. Only meaningful
    # while config_use_kb is true.
    kb_docs: list[dict[str, Any]] = []
    kb_uploading: bool = False
    # ─── Reference answer + test cases (auxiliary uploads) ──────────────────
    # config_reference_in_problems = True means "the problems file ALREADY
    # contains the standard answers; re-feed the same bytes so the LLM can
    # split out the answer portion" — replaces the old config_has_reference
    # bool which was only a placeholder. The mirrored *_file_name fields are
    # populated from the backend Task.lite() snapshot in load_task().
    config_reference_in_problems: bool = False
    reference_file_name: str = ""
    test_cases_file_name: str = ""
    config_main_expert_id: str = ""
    config_aux_expert_ids: list[str] = []
    config_synthesis: str = "single"                     # single / weighted_average / judge_agent
    config_strictness: int = 50                          # 0..100
    config_partial_credit: bool = True
    # NOTE: SymPy verification and code sandbox are now ALWAYS-ON inside
    # CalculationSkill / ProgrammingSkill — the skill self-decides whether to
    # apply them based on problem type / language / complexity keywords.
    # The old config_enable_sympy / config_enable_code_sandbox toggles were
    # removed; users no longer need to choose.
    config_grading_notes: str = ""
    config_tone: str = "neutral"                         # encouraging / neutral / strict
    config_length: str = "medium"                        # short / medium / long
    config_suggest_corrections: bool = True
    config_comment_lang: str = "auto"                    # auto / zh / en
    config_low_conf_threshold: int = 60                  # 30..80, divided by 100 at apply time
    config_enable_judge: bool = False
    # Per-task override for backend `settings.multi_sample_n`. Default 1 keeps
    # cost matching the legacy behavior. Only meaningful when ≤ 1 BYOK expert
    # is enabled — with ≥ 2 experts the variance signal comes from the experts
    # themselves and this slider is hidden in the UI. The value is sent to
    # `POST /tasks/{id}/grade` only when ≥ 2 (the backend ignores 1).
    config_multi_sample_n: int = 1

    # ════════════════════════════════════════════════════════════════════════
    # Computed vars
    # ════════════════════════════════════════════════════════════════════════

    @rx.var
    def has_current_task(self) -> bool:
        return bool(self.current_task_id) and bool(self.current_task)

    @rx.var
    def current_subpath(self) -> str:
        """Last meaningful path segment under /tasks/[id]/ — used by task_stepper
        to highlight the page the user is currently viewing.

        Examples:
          /tasks/T_abc/setup                     → "setup"
          /tasks/T_abc/upload_problems           → "upload_problems"
          /tasks/T_abc/students/S001             → "students"
          /tasks/T_abc/results/by_question       → "results"
          /tasks/T_abc/questions/q3              → "questions"
          /tasks/T_abc/visualization             → "visualization"
        """
        path = (self.router.page.path or "") if hasattr(self, "router") else ""
        parts = [p for p in path.split("/") if p]
        # /tasks/[id]/X → X is parts[2]
        if len(parts) >= 3 and parts[0] == "tasks":
            return parts[2]
        return ""

    @rx.var
    def config_strictness_label(self) -> str:
        v = self.config_strictness
        if v < 25:
            return "宽松：注重思路、轻视小错"
        if v < 50:
            return "偏宽松：允许细节疏漏"
        if v < 75:
            return "偏严格：扣分点明确"
        return "严格：步骤、表达、规范全部计分"

    @rx.var
    def config_low_conf_threshold_display(self) -> str:
        return f"{self.config_low_conf_threshold / 100:.2f}"

    @rx.var
    def task_status(self) -> str:
        return self.current_task.get("status", "") if self.current_task else ""

    @rx.var
    def task_name(self) -> str:
        return self.current_task.get("name", "") if self.current_task else ""

    @rx.var
    def problem_count(self) -> int:
        return int(self.current_task.get("problem_count", 0) or 0) if self.current_task else 0

    @rx.var
    def student_count(self) -> int:
        return int(self.current_task.get("student_count", 0) or 0) if self.current_task else 0

    @rx.var
    def problem_file_name(self) -> str:
        return self.current_task.get("problem_file_name") or ""

    @rx.var
    def submission_file_name(self) -> str:
        return self.current_task.get("submission_file_name") or ""

    @rx.var
    def task_error(self) -> str:
        return self.current_task.get("error") or ""

    @rx.var
    def is_active(self) -> bool:
        return self.task_status in ACTIVE_STATUSES

    @rx.var
    def progress_pct(self) -> int:
        total = int(self.progress.get("total_students", 0) or 0)
        done = int(self.progress.get("completed_units", 0) or 0)
        if total <= 0:
            # When ingesting problems, total_students=0; show indeterminate fallback
            phase = self.progress.get("phase", "")
            return 50 if phase in ("extracting", "ingesting") else 0
        return min(100, int(done / total * 100))

    @rx.var
    def latest_message(self) -> str:
        msgs = self.progress.get("messages", []) or []
        if not msgs:
            return ""
        last = msgs[-1] if isinstance(msgs, list) else {}
        return last.get("message", "") if isinstance(last, dict) else ""

    @rx.var
    def task_list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for tid, t in self.tasks.items():
            entry = dict(t) if isinstance(t, dict) else {}
            entry["task_id"] = tid
            ts = entry.get("updated_at", entry.get("created_at", 0))
            try:
                entry["updated_at_fmt"] = (
                    datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
                    if float(ts) > 0 else "—"
                )
            except (TypeError, ValueError):
                entry["updated_at_fmt"] = "—"
            out.append(entry)
        out.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
        return out

    @rx.var
    def active_tasks(self) -> list[dict[str, Any]]:
        return [t for t in self.task_list if t.get("status") in ACTIVE_STATUSES + ("draft", "problems_ready", "submissions_ready", "error")]

    @rx.var
    def graded_tasks(self) -> list[dict[str, Any]]:
        return [t for t in self.task_list if t.get("status") == "graded"]

    @rx.var
    def problem_list(self) -> list[dict[str, Any]]:
        from smartai_v2.config import TYPE_EN_TO_CN
        out: list[dict[str, Any]] = []
        for q_id, q in self.current_problem_data.items():
            entry = dict(q) if isinstance(q, dict) else {}
            entry.setdefault("q_id", q_id)
            t = entry.get("type", "")
            entry["type"] = TYPE_EN_TO_CN.get(t, t)
            # Normalize LaTeX delimiters so KaTeX renders them
            entry["stem"] = format_math(entry.get("stem", ""))
            entry["criterion"] = format_math(entry.get("criterion", ""))
            # Keep raw versions for the edit textarea (so the teacher edits
            # the original LLM output rather than the math-normalized text)
            entry["stem_raw"] = q.get("stem", "") if isinstance(q, dict) else ""
            entry["criterion_raw"] = q.get("criterion", "") if isinstance(q, dict) else ""
            # Pre-split the criterion into a numbered "Rubric N" list. Grading
            # comments routinely cite "rubric 1 / 2 / 3" without a corresponding
            # marker on the criterion display, so the teacher can't tell which
            # bullet is which. We strip any pre-existing numbering / bullets the
            # LLM emitted so the new "Rubric N:" prefix is the sole numbering.
            entry["rubric_items"] = _split_criterion_to_rubric_items(
                q.get("criterion", "") if isinstance(q, dict) else ""
            )
            out.append(entry)
        out.sort(key=lambda x: x.get("number", x.get("q_id", "")))
        return out

    @rx.var
    def student_list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for sid, s in self.current_student_data.items():
            entry = dict(s) if isinstance(s, dict) else {}
            entry.setdefault("stu_id", sid)
            entry["name"] = s.get("stu_name", s.get("name", "Unknown"))
            ans = entry.get("stu_ans", entry.get("answers", {}))
            entry["answer_count"] = len(ans) if hasattr(ans, "__len__") else 0
            # Count flagged answers (recognition issues from ingest pipeline)
            flag_count = 0
            if isinstance(ans, list):
                for a in ans:
                    if isinstance(a, dict) and a.get("flag"):
                        flag_count += 1
            entry["flag_count"] = flag_count
            out.append(entry)
        out.sort(key=lambda x: x.get("stu_id", ""))
        return out

    # ─── Pre-grading student answer detail (for /tasks/[id]/students/[sid]) ─

    @rx.var
    def viewed_student_answers(self) -> list[dict[str, Any]]:
        """Parsed answers for the current student (pre-grading review).

        Joins each answer with the matching problem stem so the teacher can
        see the AI-segmented question + answer side-by-side and verify
        correctness before grading.
        """
        sid = self.current_student_id
        if not sid:
            return []
        student = self.current_student_data.get(sid, {}) if isinstance(self.current_student_data, dict) else {}
        if not student:
            return []
        answers = student.get("stu_ans", []) or student.get("answers", [])
        if not isinstance(answers, list):
            return []
        out = []
        for a in answers:
            entry = dict(a) if isinstance(a, dict) else {}
            qid = entry.get("q_id", "")
            problem = (self.current_problem_data or {}).get(qid, {}) if isinstance(self.current_problem_data, dict) else {}
            entry["stem"] = format_math(problem.get("stem", "") if isinstance(problem, dict) else "")
            # Keep the raw (un-formatted) content for the edit textarea so the
            # teacher edits the original LLM/OCR text rather than the
            # math-normalized version.
            raw_content = a.get("content", "") if isinstance(a, dict) else ""
            entry["content_raw"] = raw_content
            entry["content"] = format_math(raw_content or "")
            entry["number"] = entry.get("number") or (problem.get("number", "") if isinstance(problem, dict) else "")
            entry["type"] = entry.get("type") or (problem.get("type", "") if isinstance(problem, dict) else "")
            flags = entry.get("flag", [])
            if isinstance(flags, list):
                entry["has_flag"] = len(flags) > 0
                entry["flag_text"] = "; ".join(str(f) for f in flags)
            else:
                entry["has_flag"] = bool(flags)
                entry["flag_text"] = str(flags) if flags else ""
            out.append(entry)
        out.sort(key=lambda x: x.get("number", ""))
        return out

    @rx.var
    def viewed_student_meta(self) -> dict[str, Any]:
        sid = self.current_student_id
        if not sid:
            return {}
        s = self.current_student_data.get(sid, {}) if isinstance(self.current_student_data, dict) else {}
        if not s:
            return {}
        return {
            "stu_id": sid,
            "name": s.get("stu_name", s.get("name", "Unknown")),
            "answer_count": len(s.get("stu_ans", []) or s.get("answers", [])),
        }

    @rx.var
    def per_student_stats(self) -> list[dict[str, Any]]:
        """Per-student aggregate (id, name, total, max, pct, grade) — sorted desc."""
        students = self.current_results.get("results", []) or []
        if isinstance(students, dict):
            students = [students]
        out = []
        for s in students:
            corrections = s.get("corrections", []) or []
            total = sum(float(c.get("score", 0) or 0) for c in corrections)
            mx = sum(float(c.get("max_score", 0) or 0) for c in corrections)
            pct = (total / mx * 100) if mx > 0 else 0.0
            # Low-confidence count (any correction below 0.6 confidence)
            low_conf_count = 0
            for c in corrections:
                try:
                    if float(c.get("confidence", 1) or 1) < 0.6:
                        low_conf_count += 1
                except (TypeError, ValueError):
                    continue
            out.append({
                "id": str(s.get("student_id", "")),
                "name": str(s.get("student_name", "") or s.get("student_id", "")),
                "total": round(total, 2),
                "max": round(mx, 2),
                "pct": round(pct, 1),
                "grade": _grade_letter(pct),
                "low_conf_count": low_conf_count,
            })
        out.sort(key=lambda r: r["pct"], reverse=True)
        return out

    @rx.var
    def student_search_options(self) -> list[str]:
        """Combined search/dropdown options: 'id — name' strings.

        Used by the search bar's HTML datalist to provide autocomplete.
        """
        return [f"{s['id']} — {s['name']}" for s in self.per_student_stats]

    @rx.var
    def filtered_student_stats(self) -> list[dict[str, Any]]:
        """Apply search + sort + quick filter + NL filter on per_student_stats."""
        rows = list(self.per_student_stats)

        q = (self.search_query or "").strip().lower()
        if q:
            rows = [r for r in rows if q in str(r.get("id", "")).lower() or q in str(r.get("name", "")).lower()]

        if self.quick_filter == "failing":
            rows = [r for r in rows if r["pct"] < 60]
        elif self.quick_filter == "top10":
            keep_n = max(1, len(rows) // 10)
            rows = sorted(rows, key=lambda r: r["pct"], reverse=True)[:keep_n]
        elif self.quick_filter == "flagged":
            students_raw = self.current_results.get("results", []) or []
            flagged_ids = set()
            for s in students_raw:
                for a in (s.get("student_answers", []) or []):
                    if a.get("flag"):
                        flagged_ids.add(str(s.get("student_id", "")))
                        break
                for c in (s.get("corrections", []) or []):
                    try:
                        if float(c.get("confidence", 1) or 1) < 0.6:
                            flagged_ids.add(str(s.get("student_id", "")))
                            break
                    except (TypeError, ValueError):
                        continue
            rows = [r for r in rows if r["id"] in flagged_ids]

        if self.nl_filter_student_ids:
            allowed = set(self.nl_filter_student_ids)
            rows = [r for r in rows if r["id"] in allowed]

        if self.sort_by == "score_desc":
            rows.sort(key=lambda r: r["pct"], reverse=True)
        elif self.sort_by == "score_asc":
            rows.sort(key=lambda r: r["pct"])
        elif self.sort_by == "name":
            rows.sort(key=lambda r: r["name"])
        elif self.sort_by == "id":
            rows.sort(key=lambda r: r["id"])
        return rows

    @rx.var
    def nl_filter_active(self) -> bool:
        return len(self.nl_filter_student_ids) > 0

    @rx.var(cache=True)
    def nl_chart_figure(self) -> "go.Figure | None":
        """Build a plotly Figure from the stored traces+layout dicts."""
        if go is None or not self.nl_chart_traces:
            return None
        fig = go.Figure()
        for t in self.nl_chart_traces:
            tt = dict(t)
            ttype = tt.pop("type", "bar")
            try:
                if ttype == "bar":
                    fig.add_trace(go.Bar(**tt))
                elif ttype == "scatter":
                    fig.add_trace(go.Scatter(**tt))
                elif ttype == "pie":
                    fig.add_trace(go.Pie(**tt))
                elif ttype == "histogram":
                    fig.add_trace(go.Histogram(**tt))
                elif ttype == "box":
                    fig.add_trace(go.Box(**tt))
            except Exception:
                continue
        fig.update_layout(
            margin=dict(l=20, r=20, t=40, b=20),
            height=int(self.nl_chart_layout.get("height", 380) or 380),
            plot_bgcolor="white",
            paper_bgcolor="white",
            title=self.nl_chart_layout.get("title") or self.nl_chart_title or "",
            xaxis_title=self.nl_chart_layout.get("xaxis_title"),
            yaxis_title=self.nl_chart_layout.get("yaxis_title"),
            barmode=self.nl_chart_layout.get("barmode"),
        )
        return fig

    @rx.var
    def students_count(self) -> int:
        return len(self.per_student_stats)

    @rx.var
    def avg_score(self) -> float:
        scores = [s["pct"] for s in self.per_student_stats]
        return round(sum(scores) / len(scores), 1) if scores else 0.0

    @rx.var
    def pass_rate(self) -> float:
        scores = [s["pct"] for s in self.per_student_stats]
        if not scores:
            return 0.0
        return round(sum(1 for s in scores if s >= 60) / len(scores) * 100, 1)

    @rx.var
    def highest_score(self) -> float:
        scores = [s["pct"] for s in self.per_student_stats]
        return max(scores) if scores else 0.0

    @rx.var
    def lowest_score(self) -> float:
        scores = [s["pct"] for s in self.per_student_stats]
        return min(scores) if scores else 0.0

    @rx.var
    def question_detail_rows(self) -> list[dict[str, Any]]:
        rows = self.question_detail.get("rows", []) or []
        if not isinstance(rows, list):
            return []
        q_id = self.question_q_id
        out = []
        for r in rows:
            entry = dict(r) if isinstance(r, dict) else {}
            entry["answer"] = format_math(entry.get("answer", "") or "")
            entry["comment"] = format_math(entry.get("comment", "") or "")
            try:
                conf = float(entry.get("confidence", 1) or 1)
            except (TypeError, ValueError):
                conf = 1.0
            entry["confidence_display"] = round(conf, 2)
            entry["low_confidence"] = conf < 0.6
            # Pull teacher comment for this (student, q_id) pair
            sid = str(entry.get("student_id", ""))
            tc_key = f"{sid}::{q_id}"
            entry["teacher_comment"] = format_math(self.teacher_comments.get(tc_key, ""))
            out.append(entry)
        return out

    @rx.var
    def question_problem_number(self) -> str:
        p = self.question_detail.get("problem", {}) or {}
        return str(p.get("number", "")) if isinstance(p, dict) else ""

    @rx.var
    def question_problem_type(self) -> str:
        p = self.question_detail.get("problem", {}) or {}
        return str(p.get("type", "")) if isinstance(p, dict) else ""

    @rx.var
    def question_problem_stem(self) -> str:
        p = self.question_detail.get("problem", {}) or {}
        return format_math(str(p.get("stem", "")) if isinstance(p, dict) else "")

    @rx.var
    def question_problem_criterion(self) -> str:
        p = self.question_detail.get("problem", {}) or {}
        return format_math(str(p.get("criterion", "")) if isinstance(p, dict) else "")

    @rx.var
    def question_stats_n(self) -> int:
        s = self.question_detail.get("stats", {}) or {}
        return int(s.get("n", 0) or 0) if isinstance(s, dict) else 0

    @rx.var
    def question_stats_avg(self) -> float:
        s = self.question_detail.get("stats", {}) or {}
        return float(s.get("avg", 0) or 0) if isinstance(s, dict) else 0.0

    @rx.var
    def question_stats_max(self) -> float:
        s = self.question_detail.get("stats", {}) or {}
        return float(s.get("max", 0) or 0) if isinstance(s, dict) else 0.0

    @rx.var
    def question_stats_min(self) -> float:
        s = self.question_detail.get("stats", {}) or {}
        return float(s.get("min", 0) or 0) if isinstance(s, dict) else 0.0

    @rx.var
    def question_stats_pass_rate(self) -> float:
        s = self.question_detail.get("stats", {}) or {}
        return float(s.get("pass_rate", 0) or 0) if isinstance(s, dict) else 0.0

    @rx.var
    def question_common_mistakes(self) -> str:
        return format_math(str(self.question_detail.get("common_mistakes_md", "") or ""))

    @rx.var
    def question_q_id(self) -> str:
        return str(self.question_detail.get("q_id", "") or "")

    @rx.var
    def question_section_title(self) -> str:
        return f"Question {self.question_problem_number}"

    # ─── Prev / next navigation between deep-dive questions (D3) ──────────

    @rx.var
    def question_q_id_list(self) -> list[str]:
        """All q_ids in the order shown on the problem list (sorted by number)."""
        return [p["q_id"] for p in self.problem_list]

    @rx.var
    def question_prev_q_id(self) -> str:
        qids = self.question_q_id_list
        cur = self.question_q_id
        if not cur or cur not in qids:
            return ""
        i = qids.index(cur)
        return qids[i - 1] if i > 0 else ""

    @rx.var
    def question_next_q_id(self) -> str:
        qids = self.question_q_id_list
        cur = self.question_q_id
        if not cur or cur not in qids:
            return ""
        i = qids.index(cur)
        return qids[i + 1] if i < len(qids) - 1 else ""

    @rx.var
    def question_has_prev(self) -> bool:
        return self.question_prev_q_id != ""

    @rx.var
    def question_has_next(self) -> bool:
        return self.question_next_q_id != ""

    @rx.var
    def per_question_stats(self) -> list[dict[str, Any]]:
        students = self.current_results.get("results", []) or []
        if isinstance(students, dict):
            students = [students]
        agg: dict[str, dict[str, float]] = {}
        for s in students:
            for c in s.get("corrections", []) or []:
                q = str(c.get("q_id", ""))
                if not q:
                    continue
                bucket = agg.setdefault(q, {"sum": 0.0, "max_sum": 0.0, "count": 0.0})
                bucket["sum"] += float(c.get("score", 0) or 0)
                bucket["max_sum"] += float(c.get("max_score", 0) or 0)
                bucket["count"] += 1
        out = []
        for q, b in agg.items():
            avg = (b["sum"] / b["count"]) if b["count"] else 0.0
            mx = (b["max_sum"] / b["count"]) if b["count"] else 0.0
            out.append({
                "q_id": q,
                "avg": round(avg, 2),
                "max": round(mx, 2),
                "pct": round(avg / mx * 100, 1) if mx else 0.0,
                "count": int(b["count"]),
            })
        out.sort(key=lambda r: r["q_id"])
        return out

    # ════════════════════════════════════════════════════════════════════════
    # Events: list / select / create / delete
    # ════════════════════════════════════════════════════════════════════════

    @rx.event
    async def load_tasks(self):
        self.error = ""
        try:
            auth = await self.get_state(AuthState)
            data = await tasks_api.list_tasks(token=auth.token or None)
            self.tasks = data if isinstance(data, dict) else {}
        except APIError as e:
            self.error = e.message

    @rx.event
    async def load_task(self, task_id: str):
        """Load full task data (problem_data + student_data) into current_*."""
        self.error = ""
        if not task_id:
            return
        # Skip silently if auth hasn't hydrated yet — auth_guard will redirect to /login.
        # This prevents spurious 401s during initial page load.
        auth = await self.get_state(AuthState)
        if not auth.token:
            return
        try:
            data = await tasks_api.get_task(task_id, token=auth.token or None)
            self.current_task = data if isinstance(data, dict) else {}
            self.current_task_id = task_id
            self.current_problem_data = data.get("problem_data", {}) if isinstance(data, dict) else {}
            self.current_student_data = data.get("student_data", {}) if isinstance(data, dict) else {}
            # Mirror auxiliary upload state from the backend Task into local
            # display fields (so the Setup page can render the "已上传：…" badge
            # without having to dig into self.current_task on every render).
            if isinstance(data, dict):
                self.reference_file_name = data.get("reference_file_name") or ""
                self.test_cases_file_name = data.get("test_cases_file_name") or ""
            # If graded, also pull results
            if data.get("status") == "graded":
                try:
                    res = await tasks_api.get_task_result(task_id, token=auth.token or None)
                    self.current_results = res if isinstance(res, dict) else {}
                except APIError:
                    self.current_results = {}
                # Pull any saved teacher comments
                try:
                    tc_data = await tasks_api.list_teacher_comments(task_id, token=auth.token or None)
                    self.teacher_comments = tc_data.get("comments", {}) if isinstance(tc_data, dict) else {}
                except APIError:
                    self.teacher_comments = {}
            else:
                self.current_results = {}
                self.teacher_comments = {}
            # Reset NL filter on switch
            self.nl_filter_student_ids = []
            self.nl_filter_explanation = ""
            self.nl_summary_md = ""
            self.nl_chart_traces = []
            self.nl_chart_layout = {}
            self.nl_chart_title = ""
            self.nl_chart_rationale = ""
            self.search_query = ""
            self.quick_filter = "all"
            # Clear stale progress so the progress card from a prior task
            # doesn't bleed into the new selection.
            if data.get("status") not in ACTIVE_STATUSES:
                self.progress = {}
                self.progress_phase = ""
                self.polling = False
            # Kick off polling if active
            if data.get("status") in ACTIVE_STATUSES:
                return TaskState.watch_active_job
        except APIError as e:
            self.error = e.message
            return rx.toast.error(f"Failed to load task: {e.message}")

    @rx.event
    def set_new_task_name(self, name: str):
        self.new_task_name = name

    @rx.event
    async def create_task(self):
        name = (self.new_task_name or "").strip() or f"Task {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
        try:
            auth = await self.get_state(AuthState)
            data = await tasks_api.create_task(name, token=auth.token or None)
            tid = data.get("task_id")
            if not tid:
                return rx.toast.error("Failed to create task")
            self.tasks = {**self.tasks, tid: data}
            self.current_task_id = tid
            self.current_task = data
            self.current_problem_data = {}
            self.current_student_data = {}
            self.current_results = {}
            self.new_task_name = ""
            return [
                rx.toast.success(f"Created task: {name}"),
                rx.redirect(f"/tasks/{tid}/setup"),
            ]
        except APIError as e:
            return rx.toast.error(f"Failed: {e.message}")

    @rx.event
    async def rename_task(self, task_id: str, new_name: str):
        try:
            auth = await self.get_state(AuthState)
            data = await tasks_api.update_task(task_id, name=new_name, token=auth.token or None)
            self.tasks = {**self.tasks, task_id: data}
            if self.current_task_id == task_id:
                self.current_task = data
            return rx.toast.success("Task renamed")
        except APIError as e:
            return rx.toast.error(f"Failed: {e.message}")

    @rx.event
    async def delete_task(self, task_id: str):
        try:
            auth = await self.get_state(AuthState)
            await tasks_api.delete_task(task_id, token=auth.token or None)
            new = dict(self.tasks)
            new.pop(task_id, None)
            self.tasks = new
            if self.current_task_id == task_id:
                self.current_task_id = ""
                self.current_task = {}
                self.current_problem_data = {}
                self.current_student_data = {}
                self.current_results = {}
            return rx.toast.success("Task deleted")
        except APIError as e:
            return rx.toast.error(f"Failed: {e.message}")

    @rx.event
    def select_task(self, task_id: str):
        """Switch to a task without page navigation. Returns load_task event."""
        self.current_task_id = task_id
        return TaskState.load_task(task_id)

    # ════════════════════════════════════════════════════════════════════════
    # Setup config events (frontend-only — backend integration pending)
    # ════════════════════════════════════════════════════════════════════════

    @rx.event
    def set_config_subject_lang(self, v: str): self.config_subject_lang = v
    @rx.event
    def set_config_due_at(self, v: str): self.config_due_at = v
    @rx.event
    def set_config_use_kb(self, v: bool): self.config_use_kb = v
    @rx.event
    def set_config_reference_in_problems(self, v: bool): self.config_reference_in_problems = v
    @rx.event
    def set_config_main_expert(self, v: str): self.config_main_expert_id = v
    @rx.event
    def set_config_synthesis(self, v: str): self.config_synthesis = v
    @rx.event
    def set_config_strictness(self, v: list[float] | float):
        # rx.slider emits list[float]; defensively unwrap.
        self.config_strictness = int(v[0]) if isinstance(v, list) else int(v)
    @rx.event
    def set_config_partial_credit(self, v: bool): self.config_partial_credit = v
    # NOTE: set_config_enable_sympy / set_config_enable_code_sandbox were
    # removed — the corresponding skills self-decide. Setup page no longer
    # surfaces those toggles.
    @rx.event
    def set_config_grading_notes(self, v: str): self.config_grading_notes = v
    @rx.event
    def set_config_tone(self, v: str): self.config_tone = v
    @rx.event
    def set_config_length(self, v: str): self.config_length = v
    @rx.event
    def set_config_suggest_corrections(self, v: bool): self.config_suggest_corrections = v
    @rx.event
    def set_config_comment_lang(self, v: str): self.config_comment_lang = v
    @rx.event
    def set_config_low_conf_threshold(self, v: list[float] | float):
        self.config_low_conf_threshold = int(v[0]) if isinstance(v, list) else int(v)
    @rx.event
    def set_config_enable_judge(self, v: bool): self.config_enable_judge = v
    @rx.event
    def set_config_multi_sample_n(self, v: list[float] | float):
        # Slider emits list[float]; defensively unwrap and clamp to [1, 5].
        n = int(v[0]) if isinstance(v, list) else int(v)
        self.config_multi_sample_n = max(1, min(5, n))

    @rx.event
    def proceed_to_upload_problems(self):
        """Setup → upload_problems. Validates required config (currently none required;
        all fields have sensible defaults). When backend gains a config endpoint,
        also POST/PUT the config dict here.
        """
        if not self.current_task_id:
            return rx.toast.error("No task selected")
        return rx.redirect(f"/tasks/{self.current_task_id}/upload_problems")

    # ════════════════════════════════════════════════════════════════════════
    # Events: file upload (problems / submissions)
    # ════════════════════════════════════════════════════════════════════════

    @rx.event
    async def upload_problem_file(self, files: list[rx.UploadFile]):
        if not self.current_task_id:
            return rx.toast.error("No task selected")
        if not files:
            return rx.toast.error("No file selected")
        f = files[0]
        try:
            content = await f.read()
            auth = await self.get_state(AuthState)
            data = await tasks_api.extract_problems(
                self.current_task_id, f.name, content,
                f.content_type or "application/octet-stream",
                token=auth.token or None,
            )
            status_str = data.get("status", "")
            if status_str == "already_running":
                return rx.toast.info("Already processing this file. Please wait.")
            if status_str == "already_done":
                return rx.toast.info("Already extracted (same file). Choose a different file to re-run.")
            # status_str == "started"
            return [
                rx.toast.success("Extraction started"),
                TaskState.load_task(self.current_task_id),
                TaskState.watch_active_job,
            ]
        except APIError as e:
            return rx.toast.error(f"Upload failed: {e.message}")

    @rx.event
    async def upload_submission_archive(self, files: list[rx.UploadFile]):
        if not self.current_task_id:
            return rx.toast.error("No task selected")
        if not files:
            return rx.toast.error("No file selected")
        f = files[0]
        try:
            content = await f.read()
            auth = await self.get_state(AuthState)
            data = await tasks_api.parse_submissions(
                self.current_task_id, f.name, content,
                f.content_type or "application/octet-stream",
                token=auth.token or None,
            )
            status_str = data.get("status", "")
            if status_str == "already_running":
                return rx.toast.info("Already processing this archive.")
            if status_str == "already_done":
                return rx.toast.info("Already parsed (same archive). Choose a different file to re-run.")
            return [
                rx.toast.success("Parse started"),
                TaskState.load_task(self.current_task_id),
                TaskState.watch_active_job,
            ]
        except APIError as e:
            return rx.toast.error(f"Upload failed: {e.message}")

    # ─── Reference / test-case uploads (auxiliary) ──────────────────────────
    # These do NOT advance task.status. The backend parses the doc with the
    # LLM in a fire-and-forget background task and merges the result into
    # problem_data[q_id]. We just kick off the upload and reload the task.

    @rx.event
    async def upload_reference_file(self, files: list[rx.UploadFile]):
        if not self.current_task_id:
            return rx.toast.error("No task selected")
        if not files:
            return rx.toast.error("No file selected")
        f = files[0]
        try:
            content = await f.read()
            auth = await self.get_state(AuthState)
            data = await tasks_api.upload_reference(
                self.current_task_id, f.name, content,
                f.content_type or "application/octet-stream",
                token=auth.token or None,
            )
            status_str = data.get("status", "")
            if status_str == "already_running":
                return rx.toast.info("Already parsing this reference file.")
            if status_str == "already_done":
                return rx.toast.info("Reference already parsed (same file).")
            return [
                rx.toast.success("Reference upload started — will appear shortly"),
                TaskState.load_task(self.current_task_id),
            ]
        except APIError as e:
            return rx.toast.error(f"Upload failed: {e.message}")

    @rx.event
    async def upload_test_cases_file(self, files: list[rx.UploadFile]):
        if not self.current_task_id:
            return rx.toast.error("No task selected")
        if not files:
            return rx.toast.error("No file selected")
        f = files[0]
        try:
            content = await f.read()
            auth = await self.get_state(AuthState)
            data = await tasks_api.upload_test_cases(
                self.current_task_id, f.name, content,
                f.content_type or "application/octet-stream",
                token=auth.token or None,
            )
            status_str = data.get("status", "")
            if status_str == "already_running":
                return rx.toast.info("Already parsing this test-case file.")
            if status_str == "already_done":
                return rx.toast.info("Test cases already parsed (same file).")
            return [
                rx.toast.success("Test cases upload started — will appear shortly"),
                TaskState.load_task(self.current_task_id),
            ]
        except APIError as e:
            return rx.toast.error(f"Upload failed: {e.message}")

    @rx.event
    def clear_reference(self):
        # Visual-only clear; uploading a different file will overwrite the
        # backend hash. We don't expose a backend DELETE endpoint to keep the
        # surface small.
        self.reference_file_name = ""
        return rx.toast.info("已清除显示（重新上传新文件即覆盖后端记录）")

    @rx.event
    def clear_test_cases(self):
        self.test_cases_file_name = ""
        return rx.toast.info("已清除显示（重新上传新文件即覆盖后端记录）")

    @rx.event
    async def start_grading(self):
        if not self.current_task_id:
            return rx.toast.error("No task selected")
        try:
            auth = await self.get_state(AuthState)
            data = await tasks_api.start_grading(
                self.current_task_id,
                multi_sample_n=self.config_multi_sample_n,
                token=auth.token or None,
            )
            status_str = data.get("status", "")
            if status_str == "already_running":
                return rx.toast.info("Already grading")
            if status_str == "already_done":
                return rx.toast.info("Already graded")
            return [
                rx.toast.success("Grading started"),
                TaskState.load_task(self.current_task_id),
                TaskState.watch_active_job,
            ]
        except APIError as e:
            return rx.toast.error(f"Failed: {e.message}")

    # ════════════════════════════════════════════════════════════════════════
    # Events: task-scoped knowledge base (RAG MVP)
    # ════════════════════════════════════════════════════════════════════════

    @rx.event
    async def load_kb(self):
        """Pull current KB docs from backend; called on Setup page mount."""
        if not self.current_task_id:
            return
        try:
            auth = await self.get_state(AuthState)
            data = await kb_api.list_kb(self.current_task_id, token=auth.token or None)
            self.kb_docs = list(data.get("docs", []) or [])
        except APIError as e:
            # KB endpoint may 404/503 if backend hasn't enabled the retriever
            # — silently fall back to empty list rather than spamming toasts.
            self.kb_docs = []
            self.error = e.message

    @rx.event
    async def upload_kb_file(self, files: list[rx.UploadFile]):
        """Upload one PDF / MD / TXT to the current task's KB index."""
        if not self.current_task_id:
            return rx.toast.error("No task selected")
        if not files:
            return rx.toast.error("No file selected")
        f = files[0]
        # Soft-check before posting; backend re-enforces the 5MB cap and may
        # also 413 on raw bytes if the user bypasses the UI.
        try:
            content = await f.read()
        except Exception as e:
            return rx.toast.error(f"Could not read file: {e}")
        if len(content) > 5 * 1024 * 1024:
            return rx.toast.error("File too large (5 MB max for KB uploads)")

        self.kb_uploading = True
        try:
            auth = await self.get_state(AuthState)
            data = await kb_api.upload_kb(
                self.current_task_id, f.name, content,
                f.content_type or "application/octet-stream",
                token=auth.token or None,
            )
            self.kb_uploading = False
            status_str = data.get("status", "")
            if status_str == "already_done":
                return [
                    rx.toast.info(f"已存在相同内容的 KB:{data.get('filename', '')}"),
                    TaskState.load_kb,
                ]
            chunk_count = data.get("chunk_count", 0)
            embedder = data.get("embedder", "")
            return [
                rx.toast.success(
                    f"已索引 {chunk_count} 段 (embedder: {embedder})"
                    if embedder else f"已索引 {chunk_count} 段"
                ),
                TaskState.load_kb,
            ]
        except APIError as e:
            self.kb_uploading = False
            return rx.toast.error(f"KB 上传失败: {e.message}")

    @rx.event
    async def delete_kb_doc(self, doc_id: str):
        """Remove one KB doc from the task's index."""
        if not self.current_task_id or not doc_id:
            return
        try:
            auth = await self.get_state(AuthState)
            await kb_api.delete_kb(
                self.current_task_id, doc_id, token=auth.token or None,
            )
            return [
                rx.toast.success("已删除"),
                TaskState.load_kb,
            ]
        except APIError as e:
            return rx.toast.error(f"删除失败: {e.message}")

    # ════════════════════════════════════════════════════════════════════════
    # Background polling — single source of truth for progress
    # ════════════════════════════════════════════════════════════════════════

    @rx.event(background=True)
    async def watch_active_job(self):
        """Poll task state until terminal, then yield toast + redirect.

        CRITICAL: This is a `background=True` event. In Reflex, background
        events MUST `yield` events to dispatch them — `return rx.toast(...)`
        from a background event silently no-ops. That's why earlier the user
        never saw a completion toast.
        """
        async with self:
            self.polling = True
            tid = self.current_task_id
        if not tid:
            async with self:
                self.polling = False
            return
        try:
            auth = await self.get_state(AuthState)
            token = auth.token or None
        except Exception:
            token = None

        # Auth not hydrated yet — abort silently to avoid 401 spam during
        # the brief window between page load and LocalStorage rehydration.
        if not token:
            async with self:
                self.polling = False
            return

        terminal_status = ""
        terminal_full: dict | None = None
        terminal_results: dict | None = None
        error_msg = ""

        while True:
            try:
                snap = await tasks_api.get_task_state(tid, token=token)
            except APIError as e:
                async with self:
                    self.polling = False
                # 401 means auth dropped — abort silently so the user isn't
                # spammed with a toast. auth_guard handles re-login.
                if e.status == 401:
                    return
                error_msg = e.message
                terminal_status = "error"
                break

            status_now = snap.get("status", "")
            is_terminal = status_now not in ACTIVE_STATUSES

            # Pre-fetch terminal data BEFORE locking state, so we don't hold
            # the state lock during slow API calls (which would block UI updates).
            if is_terminal:
                if status_now in ("problems_ready", "submissions_ready", "graded"):
                    try:
                        terminal_full = await tasks_api.get_task(tid, token=token)
                    except APIError:
                        terminal_full = None
                if status_now == "graded":
                    try:
                        terminal_results = await tasks_api.get_task_result(tid, token=token)
                    except APIError:
                        terminal_results = None
                    # Also pull teacher comments so they're available immediately
                    try:
                        tc_data = await tasks_api.list_teacher_comments(tid, token=token)
                    except APIError:
                        tc_data = None
                else:
                    tc_data = None
            else:
                tc_data = None

            async with self:
                # Keep the snapshot up-to-date
                self.current_task = snap
                self.progress = snap.get("progress") or {}
                self.progress_phase = (snap.get("progress") or {}).get("phase", "") if snap.get("progress") else ""
                # Refresh task list metadata (so dashboard reflects updated state)
                if isinstance(self.tasks.get(tid), dict):
                    new_tasks = dict(self.tasks)
                    new_tasks[tid] = {
                        **new_tasks.get(tid, {}),
                        "status": snap.get("status"),
                        "updated_at": snap.get("updated_at"),
                        "problem_count": snap.get("problem_count"),
                        "student_count": snap.get("student_count"),
                    }
                    self.tasks = new_tasks

                if is_terminal:
                    self.polling = False
                    # Clear progress so the progress card disappears cleanly.
                    self.progress = {}
                    self.progress_phase = ""

                    if terminal_full is not None:
                        self.current_task = terminal_full
                        self.current_problem_data = terminal_full.get("problem_data", {}) if isinstance(terminal_full, dict) else {}
                        self.current_student_data = terminal_full.get("student_data", {}) if isinstance(terminal_full, dict) else {}
                    if terminal_results is not None:
                        self.current_results = terminal_results
                    if isinstance(tc_data, dict):
                        self.teacher_comments = tc_data.get("comments", {})
                    terminal_status = status_now
                    break
            await asyncio.sleep(1.5)

        # Outside `async with self:` — yield (NOT return) so Reflex actually
        # dispatches the event. background events use generator semantics.
        # CRITICAL: Toast + redirect MUST be yielded as a single batch. Yielding
        # them on separate lines lets Reflex unmount the current page before the
        # toast renders, so the user sees nothing — the original "no completion
        # notification" symptom users reported. A single yielded list dispatches
        # both events atomically.
        if terminal_status == "problems_ready":
            yield [
                rx.toast.success("题目识别完成 ✓"),
                rx.redirect(f"/tasks/{tid}/problems"),
            ]
        elif terminal_status == "submissions_ready":
            yield [
                rx.toast.success("作业识别完成 ✓"),
                rx.redirect(f"/tasks/{tid}/students"),
            ]
        elif terminal_status == "graded":
            yield [
                rx.toast.success("批改完成 ✓ — 正在打开结果页"),
                rx.redirect(f"/tasks/{tid}/results"),
            ]
        elif terminal_status == "error":
            yield rx.toast.error(
                "任务失败：" + (error_msg or self.current_task.get("error", "unknown error"))
            )

    # ════════════════════════════════════════════════════════════════════════
    # Filters / NL Query
    # ════════════════════════════════════════════════════════════════════════

    @rx.event
    def set_search_query(self, q: str):
        # Datalist option values come back as the full "ID — Name" string when
        # the user picks from the dropdown. Strip the suffix so the matcher
        # (which compares against id and name independently) still hits.
        if " — " in q:
            q = q.split(" — ", 1)[0]
        self.search_query = q

    @rx.event
    def set_sort_by(self, s: str):
        self.sort_by = s

    @rx.event
    def set_quick_filter(self, f: str):
        self.quick_filter = f

    @rx.event
    def clear_nl_filter(self):
        self.nl_filter_student_ids = []
        self.nl_filter_explanation = ""

    @rx.event
    def set_nl_query_input(self, v: str):
        self.nl_query_input = v

    @rx.event
    def set_nl_query_mode(self, m: str):
        self.nl_query_mode = m

    @rx.event
    async def submit_nl_query(self):
        if not self.current_task_id:
            return rx.toast.error("No task selected")
        if not (self.nl_query_input or "").strip():
            return rx.toast.error("Enter a question first")

        # Client-side cooldown
        elapsed = time.time() - self.nl_last_query_at
        if elapsed < NL_COOLDOWN_SEC:
            wait = round(NL_COOLDOWN_SEC - elapsed, 1)
            return rx.toast.error(f"Please wait {wait}s before asking again.")

        self.nl_loading = True
        self.nl_error = ""
        try:
            auth = await self.get_state(AuthState)
            data = await analytics_api.nl_query(
                self.current_task_id,
                self.nl_query_input.strip(),
                self.nl_query_mode,
                token=auth.token or None,
            )
            mode = data.get("mode", self.nl_query_mode)
            if mode == "filter":
                self.nl_filter_student_ids = data.get("student_ids", []) or []
                self.nl_filter_explanation = data.get("explanation", "")
                msg = f"Filtered to {len(self.nl_filter_student_ids)} students"
            elif mode == "summary":
                self.nl_summary_md = format_math(data.get("markdown", ""))
                msg = "Summary generated"
            elif mode == "chart":
                self.nl_chart_traces = [_clean_dict(t) for t in (data.get("traces", []) or [])]
                self.nl_chart_layout = _clean_dict(data.get("layout", {}) or {})
                self.nl_chart_title = data.get("title", "Chart")
                self.nl_chart_rationale = data.get("rationale", "")
                msg = "Chart generated"
            else:
                msg = "Done"

            self.nl_last_query_at = time.time()
            self.nl_loading = False
            return rx.toast.success(msg)
        except APIError as e:
            self.nl_loading = False
            self.nl_error = e.message
            return rx.toast.error(f"Failed: {e.message}")

    # ════════════════════════════════════════════════════════════════════════
    # Problem editing (in-place stem / criterion edit)
    # ════════════════════════════════════════════════════════════════════════

    @rx.event
    def start_problem_edit(self, q_id: str, stem_raw: str, criterion_raw: str):
        """Open the inline editor for a problem with the raw (un-formatted) text
        pre-filled, so the teacher edits the original LLM output rather than the
        math-rendered version.
        """
        self.editing_q_id = q_id
        self.edit_stem_input = stem_raw or ""
        self.edit_criterion_input = criterion_raw or ""

    @rx.event
    def cancel_problem_edit(self):
        self.editing_q_id = ""
        self.edit_stem_input = ""
        self.edit_criterion_input = ""

    @rx.event
    def set_edit_stem_input(self, v: str):
        self.edit_stem_input = v

    @rx.event
    def set_edit_criterion_input(self, v: str):
        self.edit_criterion_input = v

    @rx.event
    async def save_problem_edit(self, q_id: str):
        if not self.current_task_id or not q_id:
            return
        try:
            auth = await self.get_state(AuthState)
            data = await tasks_api.update_problem(
                self.current_task_id, q_id,
                stem=self.edit_stem_input,
                criterion=self.edit_criterion_input,
                token=auth.token or None,
            )
            updated = data.get("problem", {}) if isinstance(data, dict) else {}
            # Merge into current_problem_data; problem_list re-formats math on read
            if updated:
                new_pd = dict(self.current_problem_data)
                new_pd[q_id] = updated
                self.current_problem_data = new_pd
            self.editing_q_id = ""
            self.edit_stem_input = ""
            self.edit_criterion_input = ""
            return rx.toast.success("题目已更新")
        except APIError as e:
            return rx.toast.error(f"保存失败：{e.message}")

    # ════════════════════════════════════════════════════════════════════════
    # Student answer editing (manual OCR / segmentation correction)
    # ════════════════════════════════════════════════════════════════════════

    @rx.event
    def start_answer_edit(self, q_id: str, content_raw: str):
        sid = self.current_student_id
        if not sid or not q_id:
            return
        self.editing_answer_key = f"{sid}::{q_id}"
        self.edit_answer_input = content_raw or ""

    @rx.event
    def cancel_answer_edit(self):
        self.editing_answer_key = ""
        self.edit_answer_input = ""

    @rx.event
    def set_edit_answer_input(self, v: str):
        self.edit_answer_input = v

    @rx.event
    async def save_answer_edit(self, q_id: str):
        sid = self.current_student_id
        if not self.current_task_id or not sid or not q_id:
            return
        try:
            auth = await self.get_state(AuthState)
            data = await tasks_api.update_student_answer(
                self.current_task_id, sid, q_id,
                content=self.edit_answer_input,
                token=auth.token or None,
            )
            updated = data.get("answer", {}) if isinstance(data, dict) else {}
            # Patch current_student_data so viewed_student_answers re-renders
            if updated:
                student = dict(self.current_student_data.get(sid, {}))
                answers = list(student.get("stu_ans", []) or [])
                for i, a in enumerate(answers):
                    if isinstance(a, dict) and a.get("q_id") == q_id:
                        answers[i] = updated
                        break
                student["stu_ans"] = answers
                new_sd = dict(self.current_student_data)
                new_sd[sid] = student
                self.current_student_data = new_sd
            self.editing_answer_key = ""
            self.edit_answer_input = ""
            return rx.toast.success("作答已更新")
        except APIError as e:
            return rx.toast.error(f"保存失败：{e.message}")

    # ════════════════════════════════════════════════════════════════════════
    # Teacher comments (manual annotation on AI corrections)
    # ════════════════════════════════════════════════════════════════════════

    @rx.event
    def start_teacher_comment(self, q_id: str, current_value: str):
        """Open the inline editor for a teacher comment, prefilled with whatever
        was already saved for this (student, q_id) pair (empty if none).
        """
        sid = self.current_student_id
        if not sid or not q_id:
            return
        self.editing_comment_key = f"{sid}::{q_id}"
        self.edit_comment_input = current_value or ""

    @rx.event
    def cancel_teacher_comment(self):
        self.editing_comment_key = ""
        self.edit_comment_input = ""

    @rx.event
    def set_edit_comment_input(self, v: str):
        self.edit_comment_input = v

    @rx.event
    async def save_teacher_comment(self, q_id: str):
        sid = self.current_student_id
        if not self.current_task_id or not sid or not q_id:
            return
        key = f"{sid}::{q_id}"
        try:
            auth = await self.get_state(AuthState)
            data = await tasks_api.set_teacher_comment(
                self.current_task_id, sid, q_id,
                self.edit_comment_input,
                token=auth.token or None,
            )
            saved = data.get("teacher_comment", "") if isinstance(data, dict) else ""
            new_tc = dict(self.teacher_comments)
            if saved:
                new_tc[key] = saved
            else:
                new_tc.pop(key, None)
            self.teacher_comments = new_tc
            self.editing_comment_key = ""
            self.edit_comment_input = ""
            return rx.toast.success("Teacher comment saved")
        except APIError as e:
            return rx.toast.error(f"Save failed: {e.message}")

    # ════════════════════════════════════════════════════════════════════════
    # Per-question detail
    # ════════════════════════════════════════════════════════════════════════

    @rx.event
    async def load_question_detail(self, q_id: str):
        if not self.current_task_id or not q_id:
            return
        # IMMEDIATELY clear previous question's data + raise loading flag.
        # Without this, switching from Q1 to Q2 leaves Q1 content on screen
        # for the duration of the API call (the "Q2 button shows Q1" bug).
        self.question_loading = True
        self.question_detail = {}
        try:
            auth = await self.get_state(AuthState)
            data = await analytics_api.per_question(
                self.current_task_id, q_id, token=auth.token or None,
            )
            self.question_detail = data if isinstance(data, dict) else {}
            self.question_loading = False
        except APIError as e:
            self.question_loading = False
            return rx.toast.error(f"Failed: {e.message}")

    @rx.event
    async def regenerate_common_mistakes(self, q_id: str):
        if not self.current_task_id or not q_id:
            return
        try:
            auth = await self.get_state(AuthState)
            await analytics_api.reset_per_question_cache(
                self.current_task_id, q_id, token=auth.token or None,
            )
        except APIError:
            pass
        return TaskState.load_question_detail(q_id)

    @rx.event
    def set_current_student_id(self, sid: str):
        self.current_student_id = (sid or "").strip('"').strip()

    @rx.var
    def viewed_student_record(self) -> dict[str, Any]:
        sid = self.current_student_id
        if not sid:
            return {}
        students = self.current_results.get("results", []) or []
        if isinstance(students, dict):
            students = [students]
        for s in students:
            if str(s.get("student_id", "")) == sid:
                return s
        return {}

    @rx.var
    def viewed_student_id(self) -> str:
        return str(self.viewed_student_record.get("student_id", "") or "")

    @rx.var
    def viewed_student_name(self) -> str:
        return str(self.viewed_student_record.get("student_name", "") or "")

    @rx.var
    def viewed_student_total(self) -> float:
        rec = self.viewed_student_record
        corrections = rec.get("corrections", []) or []
        return round(sum(float(c.get("score", 0) or 0) for c in corrections), 2)

    @rx.var
    def viewed_student_max(self) -> float:
        rec = self.viewed_student_record
        corrections = rec.get("corrections", []) or []
        return round(sum(float(c.get("max_score", 0) or 0) for c in corrections), 2)

    @rx.var
    def viewed_student_pct(self) -> float:
        mx = self.viewed_student_max
        return round(self.viewed_student_total / mx * 100, 1) if mx else 0.0

    @rx.var
    def viewed_student_summary(self) -> str:
        return (
            f"ID: {self.viewed_student_id} · Total: "
            f"{self.viewed_student_total} / {self.viewed_student_max} "
            f"({self.viewed_student_pct}%)"
        )

    @rx.var
    def viewed_student_corrections(self) -> list[dict[str, Any]]:
        rec = self.viewed_student_record
        corrections = rec.get("corrections", []) or []
        answers = rec.get("student_answers", []) or []
        ans_by_qid = {a.get("q_id", ""): a.get("content", "") for a in answers}
        out = []
        for c in corrections:
            entry = dict(c) if isinstance(c, dict) else {}
            qid = entry.get("q_id", "")
            entry["answer_content"] = format_math(ans_by_qid.get(qid, ""))
            entry["comment"] = format_math(entry.get("comment", "") or "")
            # Pull stem from problem_data so the detail view can show the question
            problem = (self.current_problem_data or {}).get(qid, {}) if isinstance(self.current_problem_data, dict) else {}
            entry["stem"] = format_math(problem.get("stem", "") if isinstance(problem, dict) else "")
            # Confidence flag: anything below 0.6 is "needs review"
            try:
                conf = float(entry.get("confidence", 1) or 1)
            except (TypeError, ValueError):
                conf = 1.0
            entry["low_confidence"] = conf < 0.6
            entry["confidence_display"] = round(conf, 2)
            # ── Per-expert breakdown for the "各专家详情" accordion ──────────
            # We expose: provider, display label (provider_id), score, max,
            # confidence, comment, failed (confidence==0 → blank/failed).
            experts_summary: list[dict[str, Any]] = []
            for er in (entry.get("expert_results") or []):
                if not isinstance(er, dict):
                    continue
                try:
                    ec = float(er.get("confidence", 0) or 0)
                except (TypeError, ValueError):
                    ec = 0.0
                experts_summary.append({
                    "provider": str(er.get("provider", "?")),
                    "score": float(er.get("score", 0) or 0),
                    "max_score": float(er.get("max_score", 10) or 10),
                    "confidence": round(ec, 2),
                    "comment": format_math(str(er.get("comment", "") or "")),
                    "failed": ec <= 0.0,
                })
            entry["experts_summary"] = experts_summary
            entry["experts_count"] = len(experts_summary)
            entry["synthesis_method"] = str(entry.get("synthesis_method") or "single")
            entry["all_failed"] = entry["synthesis_method"] == "all_failed"
            entry["degraded"] = entry["synthesis_method"] == "degraded_to_single"
            # Hide the "各专家详情" accordion when there's only one expert
            # AND it succeeded — the per-expert panel would just duplicate
            # the AI 评语 already shown above. Keep it visible whenever the
            # outcome was degraded / all-failed (teachers need to see why).
            entry["show_experts_panel"] = (
                entry["all_failed"]
                or entry["degraded"]
                or entry["experts_count"] > 1
            )
            # Teacher comment (read-back from local override store)
            tc_key = self.current_student_id + "::" + str(qid)
            entry["teacher_comment"] = self.teacher_comments.get(tc_key, "")
            out.append(entry)
        return out

    @rx.var
    def viewed_student_exists(self) -> bool:
        return bool(self.viewed_student_record)

    # ─── Plotly figures (built-in charts for visualization page) ──────────

    @rx.var(cache=True)
    def fig_score_distribution(self) -> "go.Figure | None":
        if go is None:
            return None
        scores = [s["pct"] for s in self.per_student_stats]
        if not scores:
            return None
        fig = go.Figure(data=[go.Histogram(
            x=scores, nbinsx=10,
            marker_line_color="white", marker_line_width=1,
        )])
        fig.update_layout(
            margin=dict(l=20, r=20, t=20, b=20),
            height=320, plot_bgcolor="white", paper_bgcolor="white",
            xaxis_title="Score %", yaxis_title="Students", bargap=0.05,
        )
        return fig

    @rx.var(cache=True)
    def fig_grade_pie(self) -> "go.Figure | None":
        if go is None:
            return None
        per_student = self.per_student_stats
        if not per_student:
            return None
        buckets = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
        for s in per_student:
            buckets[s["grade"]] = buckets.get(s["grade"], 0) + 1
        fig = go.Figure(data=[go.Pie(
            labels=list(buckets.keys()),
            values=list(buckets.values()),
            hole=0.45,
            textinfo="label+percent",
        )])
        fig.update_layout(
            margin=dict(l=20, r=20, t=20, b=20),
            height=320, plot_bgcolor="white", paper_bgcolor="white",
            showlegend=True,
        )
        return fig

    @rx.var(cache=True)
    def fig_per_question(self) -> "go.Figure | None":
        if go is None:
            return None
        stats = self.per_question_stats
        if not stats:
            return None
        q_ids = [s["q_id"] for s in stats]
        avgs = [s["avg"] for s in stats]
        maxes = [s["max"] for s in stats]
        fig = go.Figure(data=[
            go.Bar(name="Max", x=q_ids, y=maxes, opacity=0.5),
            go.Bar(name="Average", x=q_ids, y=avgs, opacity=1.0),
        ])
        fig.update_layout(
            barmode="overlay",
            margin=dict(l=20, r=20, t=20, b=20),
            height=320, plot_bgcolor="white", paper_bgcolor="white",
            xaxis_title="Question", yaxis_title="Score",
        )
        return fig


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _grade_letter(pct: float) -> str:
    if pct >= 90:
        return "A"
    if pct >= 80:
        return "B"
    if pct >= 70:
        return "C"
    if pct >= 60:
        return "D"
    return "F"


def _split_criterion_to_rubric_items(criterion: str) -> list[dict[str, str]]:
    """Split a freeform criterion string into ordered Rubric items.

    Why: grading comments cite ``rubric 1 / 2 / 3`` (the LLM judge enumerates
    the criterion bullets). The criterion field is plain markdown the extract-
    LLM emitted, often as a bulleted or numbered list — but with inconsistent
    markers. Without explicit ``Rubric N`` labels in the display the teacher
    can't tell which standard the comment is referring to.

    We strip any leading list markers (``1.``, ``1)``, ``-``, ``•``, etc.) so
    the renderer can prefix its own ``Rubric N`` consistently. If no markers
    are detected we fall back to splitting on blank lines or sentences.

    Each returned item is a `{index, text}` dict so it serializes cleanly into
    the Reflex Var system. The text retains LaTeX delimiters which the
    smart_markdown component then normalizes for KaTeX.
    """
    import re as _re

    if not criterion or not isinstance(criterion, str):
        return []

    text = criterion.strip()
    if not text:
        return []

    lines = [ln.rstrip() for ln in text.split("\n")]
    # Detect a list-marker prefix on each non-empty line. The marker can be:
    #   1. / 1) / (1) / （1）/ 1、    — numeric (half- or full-width parens)
    #   - / * / • / ●               — bullet
    marker_re = _re.compile(
        r"^\s*(?:[\(（]\s*\d+\s*[\)）]|\d+\s*[\.\)、])\s*|^\s*[-*•●▪○]\s+"
    )
    list_lines = [ln for ln in lines if marker_re.match(ln)]

    if list_lines and len(list_lines) >= 2:
        # Treat each list-marker line as a rubric. Subsequent non-marker
        # lines (continuations) are joined into the previous bullet.
        items: list[str] = []
        for ln in lines:
            if not ln.strip():
                continue
            if marker_re.match(ln):
                items.append(marker_re.sub("", ln, count=1).strip())
            else:
                if items:
                    items[-1] = (items[-1] + " " + ln.strip()).strip()
                else:
                    items.append(ln.strip())
    else:
        # No list markers detected. Fall back to blank-line paragraphs.
        paragraphs = [p.strip() for p in _re.split(r"\n\s*\n", text) if p.strip()]
        if len(paragraphs) >= 2:
            items = paragraphs
        else:
            # Single block — try splitting on Chinese/English sentence ends.
            # Only do this if it produces ≥ 2 reasonably-long pieces; otherwise
            # treat the whole criterion as one rubric (rather than a misleading
            # 1-item enumeration that suggests there's only one standard).
            sentences = [s.strip() for s in _re.split(r"(?<=[。；;])\s*", text) if s.strip()]
            items = sentences if len([s for s in sentences if len(s) > 4]) >= 2 else [text]

    return [
        {"index": str(i + 1), "text": format_math(t)}
        for i, t in enumerate(items)
        if t.strip()
    ]


def _clean_dict(d: Any) -> dict:
    """Remove None values from a dict so plotly doesn't choke."""
    if not isinstance(d, dict):
        return d
    return {k: v for k, v in d.items() if v is not None}
