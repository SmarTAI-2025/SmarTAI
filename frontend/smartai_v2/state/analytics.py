"""Analytics state — derives Plotly figures + per-student stats from a grading job's results.

Decoupled from GradingState so users can analyze ANY completed job (including ones
they didn't just run). Loads /ai_grading/grade_result/{id} and /ai_grading/all_history
on demand.
"""
from __future__ import annotations

from typing import Any

import reflex as rx

from smartai_v2.api import grading as grading_api
from smartai_v2.api.client import APIError
from smartai_v2.state.auth import AuthState
from smartai_v2.theme import MACARON_COLORS

try:
    import plotly.graph_objects as go
except ImportError:
    go = None  # type: ignore


PALETTE = MACARON_COLORS



class AnalyticsState(rx.State):
    selected_job_id: str = ""
    available_jobs: dict[str, dict[str, Any]] = {}     # job_id → metadata
    results: dict[str, Any] = {}                       # raw /grade_result/{id} response
    loading: bool = False
    error: str = ""

    # ─── Loaders ──────────────────────────────────────────────────────────

    @rx.event
    async def load_available_jobs(self):
        try:
            auth = await self.get_state(AuthState)
            data = await grading_api.all_history(token=auth.token or None)
            self.available_jobs = data if isinstance(data, dict) else {}
            # Auto-select the most recent if nothing chosen
            if not self.selected_job_id and self.available_jobs:
                jids = list(self.available_jobs.keys())
                if jids:
                    self.selected_job_id = jids[0]
                    return AnalyticsState.load_results
        except APIError as e:
            self.error = e.message

    @rx.event
    async def select_job(self, job_id: str):
        self.selected_job_id = job_id
        return AnalyticsState.load_results

    @rx.event
    async def load_results(self):
        if not self.selected_job_id:
            return
        self.loading = True
        self.error = ""
        try:
            auth = await self.get_state(AuthState)
            data = await grading_api.get_result(self.selected_job_id, token=auth.token or None)
            self.results = data if isinstance(data, dict) else {}
        except APIError as e:
            self.error = e.message
        self.loading = False

    # ─── Derived data ─────────────────────────────────────────────────────

    @rx.var(cache=True)
    def has_results(self) -> bool:
        return bool(self._raw_students())

    @rx.var(cache=True)
    def students_count(self) -> int:
        return len(self._raw_students())

    @rx.var(cache=True)
    def per_student_stats(self) -> list[dict[str, Any]]:
        """[{"id","name","total","max","pct","grade"}, ...] sorted by pct desc."""
        out = []
        for s in self._raw_students():
            corrections = s.get("corrections", []) or []
            total = sum(float(c.get("score", 0) or 0) for c in corrections)
            mx = sum(float(c.get("max_score", 0) or 0) for c in corrections)
            pct = (total / mx * 100) if mx > 0 else 0.0
            out.append({
                "id": str(s.get("student_id", "")),
                "name": str(s.get("student_name", "") or s.get("student_id", "")),
                "total": round(total, 2),
                "max": round(mx, 2),
                "pct": round(pct, 1),
                "grade": _grade_letter(pct),
            })
        out.sort(key=lambda r: r["pct"], reverse=True)
        return out

    @rx.var(cache=True)
    def total_scores_pct(self) -> list[float]:
        return [s["pct"] for s in self.per_student_stats]

    @rx.var(cache=True)
    def avg_score(self) -> float:
        scores = self.total_scores_pct
        return round(sum(scores) / len(scores), 1) if scores else 0.0

    @rx.var(cache=True)
    def pass_rate(self) -> float:
        scores = self.total_scores_pct
        if not scores:
            return 0.0
        passed = sum(1 for s in scores if s >= 60)
        return round(passed / len(scores) * 100, 1)

    @rx.var(cache=True)
    def highest_score(self) -> float:
        scores = self.total_scores_pct
        return max(scores) if scores else 0.0

    @rx.var(cache=True)
    def lowest_score(self) -> float:
        scores = self.total_scores_pct
        return min(scores) if scores else 0.0

    @rx.var(cache=True)
    def grade_buckets(self) -> dict[str, int]:
        out: dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
        for pct in self.total_scores_pct:
            out[_grade_letter(pct)] += 1
        return out

    @rx.var(cache=True)
    def per_question_stats(self) -> list[dict[str, Any]]:
        """[{"q_id","avg","max","count"}, ...]"""
        agg: dict[str, dict[str, float]] = {}
        for s in self._raw_students():
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

    @rx.var(cache=True)
    def confidence_values(self) -> list[float]:
        out = []
        for s in self._raw_students():
            for c in s.get("corrections", []) or []:
                conf = c.get("confidence")
                if conf is None:
                    continue
                try:
                    out.append(float(conf))
                except (TypeError, ValueError):
                    continue
        return out

    @rx.var(cache=True)
    def avg_confidence(self) -> float:
        v = self.confidence_values
        return round(sum(v) / len(v), 2) if v else 0.0

    @rx.var(cache=True)
    def low_confidence_count(self) -> int:
        return sum(1 for c in self.confidence_values if c < 0.6)

    # ─── Plotly figures ───────────────────────────────────────────────────

    @rx.var(cache=True)
    def fig_score_distribution(self) -> go.Figure | None:
        if go is None or not self.has_results:
            return None
        fig = go.Figure(data=[go.Histogram(
            x=self.total_scores_pct,
            nbinsx=10,
            marker_color=PALETTE[0],
            marker_line_color="white",
            marker_line_width=1,
        )])
        fig.update_layout(
            margin=dict(l=20, r=20, t=20, b=20),
            height=320,
            plot_bgcolor="white",
            paper_bgcolor="white",
            xaxis_title="Score %",
            yaxis_title="Students",
            bargap=0.05,
        )
        return fig

    @rx.var(cache=True)
    def fig_grade_pie(self) -> go.Figure | None:
        if go is None or not self.has_results:
            return None
        buckets = self.grade_buckets
        labels = list(buckets.keys())
        values = list(buckets.values())
        fig = go.Figure(data=[go.Pie(
            labels=labels,
            values=values,
            hole=0.45,
            marker=dict(colors=PALETTE[: len(labels)]),
            textinfo="label+percent",
        )])
        fig.update_layout(
            margin=dict(l=20, r=20, t=20, b=20),
            height=320,
            plot_bgcolor="white",
            paper_bgcolor="white",
            showlegend=True,
        )
        return fig

    @rx.var(cache=True)
    def fig_per_question(self) -> go.Figure | None:
        if go is None or not self.has_results:
            return None
        stats = self.per_question_stats
        if not stats:
            return None
        q_ids = [s["q_id"] for s in stats]
        avgs = [s["avg"] for s in stats]
        maxes = [s["max"] for s in stats]
        fig = go.Figure(data=[
            go.Bar(name="Max", x=q_ids, y=maxes, marker_color=PALETTE[2], opacity=0.5),
            go.Bar(name="Average", x=q_ids, y=avgs, marker_color=PALETTE[0], opacity=1.0),
        ])
        fig.update_layout(
            barmode="overlay",
            margin=dict(l=20, r=20, t=20, b=20),
            height=320,
            plot_bgcolor="white",
            paper_bgcolor="white",
            xaxis_title="Question",
            yaxis_title="Score",
        )
        return fig

    @rx.var(cache=True)
    def fig_confidence_dist(self) -> go.Figure | None:
        if go is None or not self.has_results:
            return None
        confs = self.confidence_values
        if not confs:
            return None
        fig = go.Figure(data=[go.Histogram(
            x=confs,
            nbinsx=10,
            marker_color=PALETTE[3],
            marker_line_color="white",
            marker_line_width=1,
        )])
        fig.update_layout(
            margin=dict(l=20, r=20, t=20, b=20),
            height=320,
            plot_bgcolor="white",
            paper_bgcolor="white",
            xaxis_title="Confidence (0-1)",
            yaxis_title="Corrections",
            bargap=0.05,
        )
        return fig

    @rx.var(cache=True)
    def fig_top_student_radar(self) -> go.Figure | None:
        if go is None or not self.has_results:
            return None
        stats = self.per_student_stats
        if not stats:
            return None
        top = stats[0]
        # find that student's per-question scores
        students_raw = self._raw_students()
        target = next((s for s in students_raw if str(s.get("student_id", "")) == top["id"]), None)
        if not target:
            return None
        corrections = target.get("corrections", []) or []
        if not corrections:
            return None
        categories = [str(c.get("q_id", "")) for c in corrections]
        values: list[float] = []
        for c in corrections:
            score = float(c.get("score", 0) or 0)
            mx = float(c.get("max_score", 0) or 0)
            values.append(score / mx * 100 if mx else 0.0)
        # Close the polygon
        categories_closed = categories + [categories[0]] if categories else []
        values_closed = values + [values[0]] if values else []
        fig = go.Figure(data=go.Scatterpolar(
            r=values_closed,
            theta=categories_closed,
            fill="toself",
            line=dict(color=PALETTE[0]),
            name=top["name"],
        ))
        fig.update_layout(
            margin=dict(l=20, r=20, t=40, b=20),
            height=320,
            polar=dict(radialaxis=dict(visible=True, range=[0, 100])),
            showlegend=False,
            plot_bgcolor="white",
            paper_bgcolor="white",
            title=f"Top: {top['name']} ({top['pct']}%)",
        )
        return fig

    # ─── Helpers ──────────────────────────────────────────────────────────

    def _raw_students(self) -> list[dict[str, Any]]:
        """Normalize results dict into a list of student records."""
        if not self.results:
            return []
        # Batch grade_all → {"results": [{...}, ...]}
        if "results" in self.results and isinstance(self.results["results"], list):
            return self.results["results"]
        # Single grade_student → top-level has corrections
        if "corrections" in self.results:
            return [self.results]
        return []


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
