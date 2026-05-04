"""Unit tests for multi-expert robustness:

- All experts blank → AllExpertsFailed → grading_agent produces a Correction
  with synthesis_method='all_failed' and a clean error comment.
- Partial failure (1 of 2) → degraded_to_single, only successful expert's
  comment is used; failed expert is preserved in expert_results.
- Synthesis JSON with raw LaTeX backslashes parses successfully.
- ExpertRegistry.list_configs() exposes provider_id + display_name +
  max_concurrent in the dict shape the frontend consumes.

Run:
    /opt/anaconda3/envs/smartai/bin/python -m pytest backend/tests/test_multi_expert_robustness.py -v
"""
from __future__ import annotations

import os

# Disable proxy before importing backend modules so settings.http_proxy is "".
os.environ["SMARTAI_HTTP_PROXY"] = ""
os.environ["SMARTAI_HTTPS_PROXY"] = ""

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.agents.grading_agent import _grade_single_answer
from backend.agents.multi_expert import (
    AllExpertsFailed,
    SynthesisOutput,
    _weighted_average_fallback,
    run_multi_expert,
)
from backend.models import (
    Correction,
    ExpertResult,
    ProblemInfo,
    ProviderConfig,
    StudentAnswerInfo,
)
from backend.tools.structured_llm import extract_and_parse_json


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _problem(qid: str = "q1") -> ProblemInfo:
    return ProblemInfo(
        q_id=qid, number="1", type="计算题", stem="2+2=?", criterion="Correctness.",
    )


def _answer(content: str = "4") -> StudentAnswerInfo:
    return StudentAnswerInfo(q_id="q1", number="1", type="计算题", content=content)


def _expert_result(provider: str, score: float, conf: float, comment: str) -> ExpertResult:
    return ExpertResult(
        provider=provider, score=score, max_score=10.0, confidence=conf, comment=comment,
    )


class _FakeProvider:
    """Stand-in for BaseProvider — pretends to be a registered provider."""

    def __init__(self, pid: str):
        self.provider_id = pid
        self.provider_type = pid.split(":", 1)[0]

    async def ainvoke(self, _messages):  # pragma: no cover — judge LLM not called in these tests
        raise RuntimeError("ainvoke should not be called in these tests")


class _FakeRegistry:
    def __init__(self, providers):
        self._providers = providers

    def list_available(self):
        return list(self._providers)


def _patch_skill_returning(monkeypatch, results_by_provider: dict[str, ExpertResult]):
    """Make get_skill_for_type return a skill whose grade() yields the
    pre-baked ExpertResult for the provider passed to its ctor."""

    class _CannedSkill:
        name = "Canned"
        problem_type = "any"

        def __init__(self, provider, **_kw):
            self.provider = provider

        async def grade(self, problem, answer, *, student_id=""):
            er = results_by_provider[self.provider.provider_id]
            return ExpertResult(
                provider=self.provider.provider_id,
                score=er.score,
                max_score=er.max_score,
                confidence=er.confidence,
                comment=er.comment,
            )

    import backend.agents.multi_expert as me

    monkeypatch.setattr(me, "get_skill_for_type", lambda _t: _CannedSkill)


# ─── 1. AllExpertsFailed path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_experts_failed_raises(monkeypatch):
    p1 = _FakeProvider("zhipu:glm-4.5-air")
    p2 = _FakeProvider("gemini:gemini-3-flash-preview")
    _patch_skill_returning(monkeypatch, {
        p1.provider_id: _expert_result(p1.provider_id, 0.0, 0.0, "Programming grading failed: 429"),
        p2.provider_id: _expert_result(p2.provider_id, 0.0, 0.0, "Calculation grading failed: timeout"),
    })

    with pytest.raises(AllExpertsFailed) as excinfo:
        await run_multi_expert(
            problem=_problem(),
            answer=_answer(),
            student_id="S1",
            registry=_FakeRegistry([p1, p2]),
        )
    assert len(excinfo.value.failures) == 2


@pytest.mark.asyncio
async def test_grading_agent_handles_all_failed(monkeypatch):
    """grading_agent._grade_single_answer should catch AllExpertsFailed and
    return a synthesis_method='all_failed' Correction with clean comment."""
    p1 = _FakeProvider("zhipu:glm-4.5-air")
    p2 = _FakeProvider("gemini:gemini-3-flash-preview")
    _patch_skill_returning(monkeypatch, {
        p1.provider_id: _expert_result(p1.provider_id, 0.0, 0.0, "boom 1"),
        p2.provider_id: _expert_result(p2.provider_id, 0.0, 0.0, "boom 2"),
    })

    correction = await _grade_single_answer(
        problem=_problem(),
        answer=_answer(),
        student_id="S1",
        registry=_FakeRegistry([p1, p2]),
    )
    assert correction.synthesis_method == "all_failed"
    assert correction.score == 0.0
    assert correction.confidence == 0.0
    assert "AI 专家批改失败" in correction.comment
    assert "zhipu:glm-4.5-air" in correction.comment
    assert "gemini:gemini-3-flash-preview" in correction.comment
    # both failures preserved for frontend accordion
    assert len(correction.expert_results) == 2


# ─── 2. Partial failure → degraded_to_single ─────────────────────────────────


@pytest.mark.asyncio
async def test_partial_failure_degrades_to_single(monkeypatch):
    p1 = _FakeProvider("zhipu:glm-4.5-air")
    p2 = _FakeProvider("gemini:gemini-3-flash-preview")
    _patch_skill_returning(monkeypatch, {
        p1.provider_id: _expert_result(p1.provider_id, 0.0, 0.0, "Programming grading failed: 429"),
        p2.provider_id: _expert_result(p2.provider_id, 8.5, 0.9, "Looks correct."),
    })

    correction = await run_multi_expert(
        problem=_problem(),
        answer=_answer(),
        student_id="S1",
        registry=_FakeRegistry([p1, p2]),
    )
    assert correction.synthesis_method == "degraded_to_single"
    assert correction.score == pytest.approx(8.5)
    # Comment is the surviving expert's, NOT polluted with the failure text
    assert correction.comment == "Looks correct."
    assert "Programming grading failed" not in correction.comment
    # Both experts (success + failure) preserved for the frontend accordion
    pids = sorted(er.provider for er in correction.expert_results)
    assert pids == sorted([p1.provider_id, p2.provider_id])


# ─── 3. Weighted average no longer leaks failed experts ──────────────────────


def test_weighted_average_only_sees_successes_in_comment():
    successes = [
        _expert_result("a:m", 8.0, 0.9, "A says 8."),
        _expert_result("b:m", 7.0, 0.8, "B says 7."),
    ]
    correction = _weighted_average_fallback(_problem(), successes)
    assert correction.synthesis_method == "weighted_average"
    # Both successes appear; no error text leaked
    assert "A says 8" in correction.comment
    assert "B says 7" in correction.comment
    assert "failed" not in correction.comment.lower()


# ─── 4. JSON LaTeX backslash repair ──────────────────────────────────────────


def test_synthesis_json_with_latex_backslashes():
    raw = (
        "```json\n"
        "{\n"
        '  "score": 6.0,\n'
        '  "max_score": 10.0,\n'
        '  "confidence": 0.85,\n'
        '  "comment": "F = \\overline{C} + \\bar{D}; partial credit awarded.",\n'
        '  "steps": []\n'
        "}\n"
        "```"
    )
    parsed = extract_and_parse_json(raw, SynthesisOutput)
    assert parsed.score == 6.0
    assert "overline" in parsed.comment
    assert "bar" in parsed.comment


def test_synthesis_json_with_inline_newlines():
    raw = (
        "```json\n"
        "{\n"
        '  "score": 5,\n'
        '  "max_score": 10,\n'
        '  "confidence": 0.7,\n'
        '  "comment": "line1\nline2\nline3",\n'
        '  "steps": []\n'
        "}\n"
        "```"
    )
    parsed = extract_and_parse_json(raw, SynthesisOutput)
    assert "line1" in parsed.comment
    assert "line3" in parsed.comment


# ─── 5. Registry list_configs exposes provider_id ────────────────────────────


def test_list_configs_exposes_provider_id_and_display_name():
    from backend.llm.registry import ExpertRegistry

    reg = ExpertRegistry()
    # Pretend no env keys (we don't want network calls)
    reg._providers.clear()
    reg._configs.clear()

    cfg = ProviderConfig(
        provider_type="zhipu",
        api_key="dummy",
        model="glm-4.5-air",
        display_name="GLM Air",
        max_concurrent=5,
    )
    pid = reg.register(cfg)
    items = reg.list_configs()
    matching = [i for i in items if i["provider_id"] == pid]
    assert len(matching) == 1
    item = matching[0]
    assert item["display_name"] == "GLM Air"
    assert item["max_concurrent"] == 5
    assert item["api_key"] == "***"
    assert item["model"] == "glm-4.5-air"


# ─── 6. Per-call multi_sample_n override (plan: hyssop-paper-jaybird) ────────
#
# `run_multi_expert` and `grade_batch` accept an optional `multi_sample_n`
# kwarg that overrides `settings.multi_sample_n` for THIS call only. This is
# what powers the per-task "单专家多采样次数" slider on the task_setup page —
# a teacher can opt one important task into 3× sampling without changing the
# global default that all other tasks share.


@pytest.mark.asyncio
async def test_run_multi_expert_respects_per_call_multi_sample_n(monkeypatch):
    """Single provider + per-call multi_sample_n=3 → 3 sample fan-out, regardless
    of the global setting. multi_sample_n=1 (or None) goes through the legacy
    single-shot path. Settings is forced to 1 to prove the override drives the
    behavior, not the global default."""
    import backend.agents.multi_expert as me
    monkeypatch.setattr(me._settings, "multi_sample_n", 1, raising=False)

    p1 = _FakeProvider("zhipu:glm-4.5-air")

    # Track how many times grade() is called so we know the fan-out fired.
    call_count = {"n": 0}

    class _CountingSkill:
        name = "Counting"
        problem_type = "any"

        def __init__(self, provider, **_kw):
            self.provider = provider

        async def grade(self, problem, answer, *, student_id=""):
            call_count["n"] += 1
            return ExpertResult(
                provider=self.provider.provider_id,
                score=8.0,
                max_score=10.0,
                confidence=0.9,
                comment="ok",
            )

    monkeypatch.setattr(me, "get_skill_for_type", lambda _t: _CountingSkill)

    # ── multi_sample_n=3 → 3 calls, multi_sample mode, IS computed ────────
    correction = await run_multi_expert(
        problem=_problem(),
        answer=_answer(),
        student_id="S1",
        registry=_FakeRegistry([p1]),
        multi_sample_n=3,
    )
    assert call_count["n"] == 3
    assert correction.synthesis_method == "multi_sample"
    # 3 successes with identical scores → IS == 0.0 (no review flag)
    assert correction.is_score == pytest.approx(0.0, abs=1e-9)
    assert correction.requires_human_review is False
    # 3 expert_results preserved with #sample tags so the UI can disambiguate
    pids = [er.provider for er in correction.expert_results]
    assert all("#sample" in pid for pid in pids)

    # ── multi_sample_n=1 → single shot, no fan-out ─────────────────────────
    call_count["n"] = 0
    correction = await run_multi_expert(
        problem=_problem(),
        answer=_answer(),
        student_id="S1",
        registry=_FakeRegistry([p1]),
        multi_sample_n=1,
    )
    assert call_count["n"] == 1
    assert correction.synthesis_method == "single"
    assert correction.is_score is None  # no variance possible from 1 sample

    # ── multi_sample_n=None → fall back to settings (which we forced to 1) ─
    call_count["n"] = 0
    correction = await run_multi_expert(
        problem=_problem(),
        answer=_answer(),
        student_id="S1",
        registry=_FakeRegistry([p1]),
        multi_sample_n=None,
    )
    assert call_count["n"] == 1
    assert correction.synthesis_method == "single"


@pytest.mark.asyncio
async def test_grade_batch_threads_multi_sample_n(monkeypatch):
    """grade_batch must thread multi_sample_n down through grade_student and
    _grade_single_answer to run_multi_expert. We force settings to 1 then call
    grade_batch with multi_sample_n=3 and check the skill was invoked 3 times
    per (student × question)."""
    from backend.agents.grading_agent import grade_batch
    import backend.agents.multi_expert as me
    monkeypatch.setattr(me._settings, "multi_sample_n", 1, raising=False)

    p1 = _FakeProvider("zhipu:glm-4.5-air")
    call_count = {"n": 0}

    class _CountingSkill:
        name = "Counting"
        problem_type = "any"

        def __init__(self, provider, **_kw):
            self.provider = provider

        async def grade(self, problem, answer, *, student_id=""):
            call_count["n"] += 1
            return ExpertResult(
                provider=self.provider.provider_id,
                score=7.5,
                max_score=10.0,
                confidence=0.85,
                comment="ok",
            )

    monkeypatch.setattr(me, "get_skill_for_type", lambda _t: _CountingSkill)

    student_store = {
        "S1": {
            "stu_id": "S1",
            "stu_name": "Alice",
            "stu_ans": [
                {"q_id": "q1", "number": "1", "type": "计算题", "content": "4"},
            ],
        },
    }
    problem_store = {
        "q1": {
            "q_id": "q1", "number": "1", "type": "计算题",
            "stem": "2+2=?", "criterion": "Correctness.",
        },
    }

    results = await grade_batch(
        student_store=student_store,
        problem_store=problem_store,
        registry=_FakeRegistry([p1]),
        multi_sample_n=3,
    )

    # 1 student × 1 question × 3 samples → 3 grade() calls
    assert call_count["n"] == 3
    assert len(results) == 1
    correction = results[0]["corrections"][0]
    assert correction.synthesis_method == "multi_sample"
