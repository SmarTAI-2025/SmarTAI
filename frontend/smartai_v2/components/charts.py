"""Plotly chart helpers (returned via rx.plotly)."""
from __future__ import annotations

from typing import Iterable

try:
    import plotly.graph_objects as go
except Exception:
    go = None


CHART_PALETTE = ["#FFB3BA", "#FFDFBA", "#FFFFBA", "#BAFFC9", "#BAE1FF", "#E6E6FA"]


def score_distribution(scores: Iterable[float], bins: int = 10):
    if go is None:
        return None
    fig = go.Figure(data=[go.Histogram(x=list(scores), nbinsx=bins, marker_color=CHART_PALETTE[0], opacity=0.8)])
    fig.update_layout(
        margin=dict(l=20, r=20, t=20, b=20),
        height=300,
        plot_bgcolor="white",
        paper_bgcolor="white",
        colorway=CHART_PALETTE,
    )
    return fig


def grade_pie(grades: dict[str, int]):
    if go is None:
        return None
    labels = list(grades.keys())
    values = list(grades.values())
    fig = go.Figure(data=[go.Pie(labels=labels, values=values, hole=0.4, marker=dict(colors=CHART_PALETTE), opacity=0.9)])
    fig.update_layout(
        margin=dict(l=20, r=20, t=20, b=20),
        height=300,
        showlegend=True,
        plot_bgcolor="white",
        paper_bgcolor="white",
    )
    return fig


def question_bar(question_ids: list[str], avg_scores: list[float]):
    if go is None:
        return None
    fig = go.Figure(data=[go.Bar(x=question_ids, y=avg_scores, marker_color=CHART_PALETTE[3], opacity=0.8)])
    fig.update_layout(
        margin=dict(l=20, r=20, t=20, b=20),
        height=300,
        plot_bgcolor="white",
        paper_bgcolor="white",
        xaxis_title="Question",
        yaxis_title="Average Score",
    )
    return fig


def student_radar(categories: list[str], values: list[float], student_name: str = ""):
    if go is None:
        return None
    fig = go.Figure(data=go.Scatterpolar(
        r=values + [values[0] if values else 0],
        theta=categories + [categories[0] if categories else ""],
        fill="toself",
        name=student_name,
        line=dict(color="#FFB3BA"),
        fillcolor="rgba(255, 179, 186, 0.4)",
    ))
    fig.update_layout(
        margin=dict(l=20, r=20, t=40, b=20),
        height=320,
        polar=dict(radialaxis=dict(visible=True, range=[0, 100])),
        showlegend=False,
        plot_bgcolor="white",
        paper_bgcolor="white",
    )
    return fig
