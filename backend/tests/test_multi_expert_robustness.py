"""Unit tests for multi-expert robustness:

- All experts blank → AllExpertsFailed → grading_agent produces a Correction
  with synthesis_method='all_failed' and a clean error comment.
- Partial failure (1 of 2) → degraded_to_single, only successful expert's
  comment is used; failed expert is preserved in expert_results.
- Synthesis JSON with raw LaTeX backslashes parses successfully.
- ExpertRegistry.list_configs() exposes provider_id + display_name +
  max_concurrent in the dict shape the frontend consumes.

Run:
    python -m pytest backend/tests/test_multi_expert_robustness.py -v
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
                error_kind=er.error_kind,
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
    return a synthesis_method='all_failed' Correction with a safe user comment."""
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
    assert "请检查 BYOK 配置" in correction.comment
    assert "zhipu:glm-4.5-air" not in correction.comment
    assert "gemini:gemini-3-flash-preview" not in correction.comment
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


# ─── 2b. 2026-08-28 low-confidence merge policy ─────────────────────────────
#
# Model capabilities are uneven: Qwen cannot grade some free-response items
# while DeepSeek only grades big questions. When only some models can grade
# an item, the merged result must NOT be forced into the review queue by the
# weaker model's low confidence — ANY confident expert makes the result high
# confidence ("any high confidence wins").


@pytest.mark.asyncio
async def test_partial_failure_high_confidence_survivor_is_not_flagged(monkeypatch):
    p1 = _FakeProvider("zhipu:glm-4.5-air")
    p2 = _FakeProvider("gemini:gemini-3-flash-preview")
    _patch_skill_returning(monkeypatch, {
        p1.provider_id: _expert_result(p1.provider_id, 0.0, 0.0, "cannot grade this item"),
        p2.provider_id: _expert_result(p2.provider_id, 8.5, 0.9, "Looks correct."),
    })
    import backend.agents.multi_expert as me
    monkeypatch.setattr(me._settings, "confidence_threshold", 0.6, raising=False)

    correction = await run_multi_expert(
        problem=_problem(),
        answer=_answer(),
        student_id="S1",
        registry=_FakeRegistry([p1, p2]),
    )
    assert correction.synthesis_method == "degraded_to_single"
    assert correction.confidence == pytest.approx(0.9)
    # One confident model is enough: no forced human review.
    assert correction.requires_human_review is False
    assert correction.review_reasons == []


@pytest.mark.asyncio
async def test_partial_failure_low_confidence_survivor_is_flagged(monkeypatch):
    p1 = _FakeProvider("zhipu:glm-4.5-air")
    p2 = _FakeProvider("gemini:gemini-3-flash-preview")
    _patch_skill_returning(monkeypatch, {
        p1.provider_id: _expert_result(p1.provider_id, 0.0, 0.0, "cannot grade this item"),
        p2.provider_id: _expert_result(p2.provider_id, 4.0, 0.35, "unsure"),
    })
    import backend.agents.multi_expert as me
    monkeypatch.setattr(me._settings, "confidence_threshold", 0.6, raising=False)

    correction = await run_multi_expert(
        problem=_problem(),
        answer=_answer(),
        student_id="S1",
        registry=_FakeRegistry([p1, p2]),
    )
    assert correction.synthesis_method == "degraded_to_single"
    assert correction.requires_human_review is True
    assert "degraded_to_single" in correction.review_reasons


def test_weighted_average_confidence_uses_best_expert():
    successes = [
        _expert_result("weak:m", 8.0, 0.3, "weak but tried"),
        _expert_result("strong:m", 7.0, 0.95, "strong"),
    ]
    correction = _weighted_average_fallback(_problem(), successes)
    # The merged result is as confident as the best expert — a weak model's
    # self-doubt must not drag it below the review threshold.
    assert correction.confidence == pytest.approx(0.95)
    # The score is still confidence-weighted (strong expert dominates).
    assert correction.score == pytest.approx((8.0 * 0.3 + 7.0 * 0.95) / 1.25, abs=1e-9)


def _transient_result(provider: str, score: float = 0.0, conf: float = 0.0) -> ExpertResult:
    """The network/timeout blank result a skill returns after classify_skill_error."""
    return ExpertResult(
        provider=provider,
        score=score,
        max_score=10.0,
        confidence=conf,
        comment="🌐 该题暂未批改完成 — AI 服务出现网络/超时错误。请稍后重试。",
        error_kind="transient_llm",
    )


@pytest.mark.asyncio
async def test_transient_network_failure_is_not_a_confidence_vote(monkeypatch):
    """2026-08-28 fine-tune: a network/timeout error (conf 0, transient_llm)
    is not a vote — the confident sibling fully determines the merged
    confidence, and the result is not flagged for review."""
    p1 = _FakeProvider("zhipu:glm-4.5-air")
    p2 = _FakeProvider("gemini:gemini-3-flash-preview")
    _patch_skill_returning(monkeypatch, {
        p1.provider_id: _transient_result(p1.provider_id),
        p2.provider_id: _expert_result(p2.provider_id, 8.0, 0.9, "Looks correct."),
    })
    import backend.agents.multi_expert as me
    monkeypatch.setattr(me._settings, "confidence_threshold", 0.6, raising=False)

    correction = await run_multi_expert(
        problem=_problem(),
        answer=_answer(),
        student_id="S1",
        registry=_FakeRegistry([p1, p2]),
    )
    assert correction.synthesis_method == "degraded_to_single"
    assert correction.confidence == pytest.approx(0.9)
    assert correction.requires_human_review is False
    assert correction.review_reasons == []
    # The failed expert stays visible for triage but cast no vote.
    assert [er.provider for er in correction.expert_results] == [
        p2.provider_id, p1.provider_id,
    ]
    assert correction.expert_results[1].error_kind == "transient_llm"


@pytest.mark.asyncio
async def test_transient_error_is_non_vote_even_with_spurious_confidence(monkeypatch):
    """Defensive: an infrastructure failure must stay non-voting even if a
    spurious non-zero confidence leaked into the result — it must not count
    as a second vote nor drag the merged confidence down."""
    p1 = _FakeProvider("zhipu:glm-4.5-air")
    p2 = _FakeProvider("gemini:gemini-3-flash-preview")
    _patch_skill_returning(monkeypatch, {
        p1.provider_id: _transient_result(p1.provider_id, score=4.0, conf=0.3),
        p2.provider_id: _expert_result(p2.provider_id, 8.0, 0.9, "Looks correct."),
    })
    import backend.agents.multi_expert as me
    monkeypatch.setattr(me._settings, "confidence_threshold", 0.6, raising=False)

    correction = await run_multi_expert(
        problem=_problem(),
        answer=_answer(),
        student_id="S1",
        registry=_FakeRegistry([p1, p2]),
    )
    # Only one real vote → degraded_to_single, not judge/weighted synthesis.
    assert correction.synthesis_method == "degraded_to_single"
    assert correction.confidence == pytest.approx(0.9)
    assert correction.requires_human_review is False
    assert correction.review_reasons == []


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
    pid = reg.register(
        cfg,
        verification_status="verified",
        last_checked_at=1_735_689_600.0,
    )
    items = reg.list_configs()
    matching = [i for i in items if i["provider_id"] == pid]
    assert len(matching) == 1
    item = matching[0]
    assert item["display_name"] == "GLM Air"
    assert item["max_concurrent"] == 5
    assert "api_key" not in item
    assert item["model"] == "glm-4.5-air"
    assert item["last_checked_at"] == "2025-01-01T00:00:00+00:00"
    assert item["verified_at"] == "2025-01-01T00:00:00+00:00"


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


# ─── 7. Question-level retry on transient all-failure ───────────────────────
#
# The per-LLM-call tenacity retry runs inside each attempt; when the WHOLE
# question still fails transiently (campus-relay hang across the fan-out),
# _grade_single_answer re-runs the question once with backoff before
# reporting "暂未批改" (题目识别与判卷问题记录 §4).


import asyncio as _asyncio


def _patch_sequenced_skill(monkeypatch, outcomes: list[dict]):
    """get_skill_for_type → a skill whose grade() returns outcomes[i] per
    call (cycling the last entry once exhausted)."""
    import backend.agents.multi_expert as me

    calls = {"n": 0}

    class _SequencedSkill:
        name = "Sequenced"
        problem_type = "any"

        def __init__(self, provider, **_kw):
            self.provider = provider

        async def grade(self, problem, answer, *, student_id=""):
            i = min(calls["n"], len(outcomes) - 1)
            calls["n"] += 1
            spec = outcomes[i]
            return ExpertResult(
                provider=self.provider.provider_id,
                score=spec.get("score", 0.0),
                max_score=spec.get("max_score", 10.0),
                confidence=spec.get("confidence", 0.0),
                comment=spec.get("comment", ""),
                error_kind=spec.get("error_kind"),
            )

    monkeypatch.setattr(me, "get_skill_for_type", lambda _t: _SequencedSkill)
    return calls


def _fast_sleep(monkeypatch):
    """Question-level retry backoff must not slow the suite down."""
    monkeypatch.setattr(_asyncio, "sleep", AsyncMock(return_value=None))


def _set_item_retries(monkeypatch, n: int):
    from backend.config import settings

    monkeypatch.setattr(settings, "grading_item_max_retries", n)


@pytest.mark.asyncio
async def test_single_expert_transient_failure_retries_question(monkeypatch):
    """Single-provider mode: a blank transient result (no AllExpertsFailed)
    triggers exactly one extra question attempt, which succeeds."""
    p1 = _FakeProvider("ustc:qwen2.5")
    calls = _patch_sequenced_skill(monkeypatch, [
        {"score": 0.0, "confidence": 0.0, "error_kind": "transient_llm",
         "comment": "🌐 该题暂未批改完成 — AI 服务出现网络/超时错误。请稍后重试。"},
        {"score": 9.0, "confidence": 0.9, "comment": "Looks correct."},
    ])
    _fast_sleep(monkeypatch)
    _set_item_retries(monkeypatch, 1)

    correction = await _grade_single_answer(
        problem=_problem(), answer=_answer(), student_id="S1",
        registry=_FakeRegistry([p1]),
    )
    # One initial pass + one question-level retry.
    assert calls["n"] == 2
    # The retry produced a real score — the question was NOT left 暂未批改.
    assert correction.synthesis_method == "single"
    assert correction.score == pytest.approx(9.0)
    assert correction.confidence == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_all_experts_transient_failure_retries_question(monkeypatch):
    """Two providers both fail transiently → AllExpertsFailed → the whole
    question is re-attempted and succeeds on the second pass."""
    p1 = _FakeProvider("zhipu:glm-4.5-air")
    p2 = _FakeProvider("gemini:gemini-3-flash-preview")
    calls = _patch_sequenced_skill(monkeypatch, [
        {"score": 0.0, "confidence": 0.0, "error_kind": "transient_llm"},
        {"score": 0.0, "confidence": 0.0, "error_kind": "transient_llm"},
        {"score": 8.0, "confidence": 0.85, "comment": "retry-1 ok"},
        {"score": 8.5, "confidence": 0.9, "comment": "retry-2 ok"},
    ])
    _fast_sleep(monkeypatch)
    _set_item_retries(monkeypatch, 1)

    correction = await _grade_single_answer(
        problem=_problem(), answer=_answer(), student_id="S1",
        registry=_FakeRegistry([p1, p2]),
    )
    # First pass: 2 experts blank → AllExpertsFailed → retry. Second pass:
    # both experts succeed → judge/weighted synthesis, real score.
    assert calls["n"] == 4
    assert correction.confidence > 0
    assert correction.score > 0
    assert correction.synthesis_method in {"judge_agent", "weighted_average"}


@pytest.mark.asyncio
async def test_non_transient_failure_is_not_retried(monkeypatch):
    """parse_failed / general are deterministic: no question-level retry."""
    p1 = _FakeProvider("zhipu:glm-4.5-air")
    p2 = _FakeProvider("gemini:gemini-3-flash-preview")
    calls = _patch_sequenced_skill(monkeypatch, [
        {"score": 0.0, "confidence": 0.0, "error_kind": "parse_failed"},
        {"score": 0.0, "confidence": 0.0, "error_kind": "parse_failed"},
    ])
    _fast_sleep(monkeypatch)
    _set_item_retries(monkeypatch, 1)

    correction = await _grade_single_answer(
        problem=_problem(), answer=_answer(), student_id="S1",
        registry=_FakeRegistry([p1, p2]),
    )
    assert calls["n"] == 2  # exactly one pass, no retry
    assert correction.synthesis_method == "all_failed"
    assert correction.confidence == 0.0


@pytest.mark.asyncio
async def test_retry_disabled_by_zero_max_retries(monkeypatch):
    p1 = _FakeProvider("ustc:qwen2.5")
    calls = _patch_sequenced_skill(monkeypatch, [
        {"score": 0.0, "confidence": 0.0, "error_kind": "transient_llm"},
    ])
    _fast_sleep(monkeypatch)
    monkeypatch.setattr(
        "backend.agents.grading_agent._settings.grading_item_max_retries", 0,
    )

    correction = await _grade_single_answer(
        problem=_problem(), answer=_answer(), student_id="S1",
        registry=_FakeRegistry([p1]),
    )
    assert calls["n"] == 1
    assert correction.confidence == 0.0
