"""
Multi-expert agent: fans out a single grading task to N provider-specific
skill instances in parallel, then synthesizes the results.

Flow:
  User has N configured providers (via ExpertRegistry)
  → GradingAgent selects a skill by problem type
  → MultiExpertAgent wraps the skill
    → asyncio.gather: skill.grade(provider=A), skill.grade(provider=B), ...
    → SynthesisAgent aggregates → final Correction (expert_results preserved)

If only 1 provider is available AND `settings.multi_sample_n > 1`, falls back
to **multi-sample** mode: the single provider is run N times in parallel, and
the resulting samples are aggregated with weighted average (cheaper & more
deterministic than the LLM judge — the judge cannot meaningfully arbitrate
between samples of the same model). This gives us a variance signal
(Indecisiveness Score) even in single-provider deployments.

Robustness:
  - A skill returning a `_blank_result` (explicit error_kind) is treated as a
    failed expert. We split results into successes / failures and synthesize
    only on successes:
      * len(successes) == 0 → raise AllExpertsFailed (caller renders error)
      * len(successes) == 1 → degraded_to_single (no judge call, clean comment)
      * len(successes) >= 2 → deterministic weighted score, optional judge explanation
  - Infrastructure failures are non-voting (2026-08-28): a network/timeout
    (transient_llm) or quota (quota_exhausted) error means the expert never
    graded the item, so its confidence — 0, or a spurious partial value —
    never enters the merge math ("不参与置信度计算").
  - Confidence policy (2026-08-28): model capabilities are uneven, so ANY
    confident expert makes the merged result high confidence — merged
    confidence is floored at the best expert's confidence, and
    degraded_to_single forces human review only when the surviving sample
    itself is below the low-confidence threshold.
  - failures are still preserved in `expert_results` so the frontend can show
    *why* each one failed, but their comment text never bleeds into the main
    synthesized comment.

P0 fairness signals (added 2026-05-03):
  - **Indecisiveness Score (IS)**: normalized std-dev of expert/sample scores.
    Computed in `_compute_review_signal` from the same successes that drive
    the synthesized score. None when only one sample is available.
  - **Minority Veto**: even when IS stays under threshold, a single rogue
    expert/sample whose score deviates from the median by > 30% of max_score
    (configurable) flips `requires_human_review=true`. Catches cases like
    "two experts say 9/10, one says 4/10" that average out to a clean-looking
    7.3 but really demand a teacher's eye.
  Both signals land on `Correction.is_score / requires_human_review /
  review_reasons` and the pipeline never blocks on them — they are advisory
  flags for the frontend to surface.
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Tuple, TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.config import settings as _settings
from backend.models import Correction, ExpertResult, ProblemInfo, StudentAnswerInfo, StepScore, TaskGradingSetup
from backend.llm.providers import BaseProvider
from backend.llm.registry import ExpertRegistry
from backend.skills.base import (
    GradingSkill,
    authoritative_score_instruction,
    build_system_prompt,
    get_skill_for_type,
    normalize_expert_result,
    normalize_score_scale,
)
from backend.tools.structured_llm import structured_llm_call
from backend.services.grading_scores import round_ai_score

if TYPE_CHECKING:
    from backend.progress.tracker import ProgressReporter

logger = logging.getLogger(__name__)


# ─── Synthesis output ─────────────────────────────────────────────────────────

class SynthesisOutput(BaseModel):
    score: float = Field(ge=0, allow_inf_nan=False)
    max_score: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    comment: str = Field(description="Synthesized feedback incorporating all experts")
    steps: List[dict] = Field(default_factory=list)


# Explicit daily exhaustion must stop retries even alongside transient failures.
_ERROR_KIND_PRIORITY = ("general", "parse_failed", "transient_llm", "quota_exhausted", "daily_quota_exhausted")


def dominant_error_kind(failures: List[ExpertResult]) -> str:
    """Most "actionable" error kind across a set of failed expert results.

    Preserve explicit daily exhaustion so the caller cannot mistake it for
    a short-lived limit and automatically replay the whole item.
    """
    best_rank = -1
    best = "general"
    for er in failures:
        kind = er.error_kind or "general"
        try:
            rank = _ERROR_KIND_PRIORITY.index(kind)
        except ValueError:
            rank = 0
        if rank > best_rank:
            best_rank = rank
            best = kind
    return best


def _is_successful_expert(result: ExpertResult) -> bool:
    """True when the expert result counts as a real grading vote.

    Explicit failures never vote, even with spurious confidence. A valid
    score with no usable confidence can still take part in an equal average.
    """
    return not result.error_kind


class AllExpertsFailed(Exception):
    """Raised when every expert returned a blank/failed result.

    Caller (grading_agent) catches this and produces a Correction with
    synthesis_method='all_failed' (or 'quota_exhausted' when at least one
    failure was a rate-limit) so the frontend can show a clear error UI
    instead of a 0-score Correction with mixed error text.

    `dominant_kind` is the most "actionable" error kind across the failures —
    we surface quota_exhausted preferentially because it's the only one the
    teacher can fix immediately (wait & retry, or raise RPM cap).
    """

    def __init__(self, failures: List[ExpertResult]):
        self.failures = failures
        self.dominant_kind = dominant_error_kind(failures)
        summary = "; ".join(
            f"{er.provider}: {(er.comment or 'unknown error').strip()[:160]}"
            for er in failures
        )
        super().__init__(
            f"All {len(failures)} experts failed (dominant_kind={self.dominant_kind}). {summary}"
        )


# ─── Multi-expert fan-out ─────────────────────────────────────────────────────

async def run_multi_expert(
    *,
    problem: ProblemInfo,
    answer: StudentAnswerInfo,
    student_id: str,
    registry: ExpertRegistry,
    reporter: Optional["ProgressReporter"] = None,
    language: str = "en",
    task_id: Optional[str] = None,
    multi_sample_n: Optional[int] = None,
    aggregation_method: Optional[str] = None,
    grading_setup: Optional[TaskGradingSetup] = None,
) -> Correction:
    """
    Run the grading skill across all available experts in parallel.

    Returns a Correction with expert_results populated and P0 fairness flags
    (`is_score`, `requires_human_review`, `review_reasons`) attached when ≥ 2
    samples were produced.

    Modes:
      * len(providers) ≥ 2  → classic multi-expert (one sample per provider)
      * len(providers) == 1 AND effective_n > 1 → multi-sample
        (the same provider is run N times so we can compute IS in
        single-provider deployments). Synthesis uses weighted-average rather
        than the LLM judge — the judge is the same model and cannot
        meaningfully arbitrate between its own samples.
      * len(providers) == 1 AND effective_n == 1 → single-shot
        (legacy path; no IS possible).

    `task_id` is passed down to each skill instance so KB retrieval can scope
    to the current task. None means no task scope (global / NoOp retriever).

    `multi_sample_n` is a per-call override for the global
    `settings.multi_sample_n`. None (default) → use the global setting. Set to
    ≥ 2 by the task_setup UI to opt a single important task into multi-sample
    mode without changing the global default. Ignored when ≥ 2 providers are
    configured (variance comes from the experts themselves).
    """
    providers = registry.list_available()
    provider_ids = {}
    if grading_setup is not None and callable(getattr(registry, "get", None)):
        selected = [(key, registry.get(key)) for key in grading_setup.selected_provider_ids]
        if any(provider is None for _, provider in selected):
            raise ValueError("A selected grading provider is unavailable")
        providers = [provider for _, provider in selected]
        provider_ids = {id(provider): key for key, provider in selected}

    if not providers:
        raise ValueError(
            "No LLM providers configured. "
            "Add API keys via POST /experts/keys or set GEMINI_API_KEY etc. in the environment."
        )

    skill_cls = get_skill_for_type(problem.type)

    # Resolve effective sample count: per-call override > settings default.
    # Pull settings at call time so dev hot-reloads pick up changes.
    if multi_sample_n is not None:
        effective_n = max(1, int(multi_sample_n))
        n_source = "task_override"
    else:
        effective_n = max(1, int(getattr(_settings, "multi_sample_n", 1)))
        n_source = "global_default"

    # ── Build the list of samples to run ──────────────────────────────────
    # Each entry is (provider, sample_idx). sample_idx > 0 only matters for
    # multi-sample disambiguation in expert_results.
    if len(providers) >= 2:
        samples: List[Tuple[BaseProvider, int]] = [(p, 0) for p in providers]
        mode = "multi_expert"
    elif effective_n > 1:
        samples = [(providers[0], i + 1) for i in range(effective_n)]
        mode = "multi_sample"
    else:
        samples = [(providers[0], 0)]
        mode = "single"

    # ── Single-shot fast path (legacy: 1 provider, effective_n == 1) ──────
    if mode == "single":
        provider = providers[0]
        from backend.services.grading_checkpoints import current_checkpoints
        checkpoints = current_checkpoints()
        provider_id = provider_ids.get(id(provider), provider.provider_id)
        skill = skill_cls(
            provider, reporter=reporter, language=language, task_id=task_id,
            grading_setup=grading_setup,
        )
        from backend.progress.grading_activity import observe_expert
        expert_result = checkpoints.get(student_id, problem.q_id, provider_id, 0) if checkpoints else None
        if expert_result is None:
            async with observe_expert(reporter, provider, samples) as activity:
                expert_result = normalize_expert_result(
                    await skill.grade(problem, answer, student_id=student_id),
                    problem.max_score,
                )
                expert_result.provider_id = provider_id
                if checkpoints:
                    checkpoints.save_expert(student_id, problem.q_id, provider_id, 0, expert_result)
                if activity is not None:
                    activity["successful"] = _is_successful_expert(expert_result)
        if not _is_successful_expert(expert_result):
            raise AllExpertsFailed([expert_result])
        expert_result.provider_id = provider_ids.get(id(provider), provider.provider_id)
        # is_score is None (one sample → no variance signal); review flag stays
        # off here. The caller applies the shared numeric confidence policy.
        return _expert_to_correction(
            problem.q_id,
            problem.type,
            expert_result,
            synthesis_method="single",
        )

    # ── Fan-out (multi-expert or multi-sample) ────────────────────────────
    logger.info(
        f"{mode} fan-out: {len(samples)} samples for q_id={problem.q_id}, "
        f"type={problem.type}, skill={skill_cls.name}, "
        f"effective_n={effective_n} source={n_source}"
    )

    async def _run_one(provider: BaseProvider, sample_idx: int) -> ExpertResult:
        from backend.services.grading_checkpoints import current_checkpoints
        checkpoints = current_checkpoints()
        provider_id = provider_ids.get(id(provider), provider.provider_id)
        skill = skill_cls(
            provider, reporter=reporter, language=language, task_id=task_id,
            grading_setup=grading_setup,
        )
        from backend.progress.grading_activity import observe_expert
        result = checkpoints.get(student_id, problem.q_id, provider_id, sample_idx) if checkpoints else None
        if result is None:
            async with observe_expert(reporter, provider, samples) as activity:
                result = normalize_expert_result(
                    await skill.grade(problem, answer, student_id=student_id), problem.max_score)
                result.provider_id = provider_id
                if sample_idx > 0 and result.provider and "#sample" not in result.provider:
                    result.provider = f"{result.provider}#sample{sample_idx}"
                if checkpoints:
                    checkpoints.save_expert(student_id, problem.q_id, provider_id, sample_idx, result)
                if activity is not None:
                    activity["successful"] = _is_successful_expert(result)
        result.provider_id = provider_ids.get(id(provider), provider.provider_id)
        # Tag duplicate provider IDs in multi-sample mode so the UI / logs can
        # tell them apart. We do this AFTER the skill returns so any internal
        # logging in the skill still reports the real provider_id.
        if sample_idx > 0 and result.provider and "#sample" not in result.provider:
            result.provider = f"{result.provider}#sample{sample_idx}"
        return result

    t0 = time.perf_counter()
    results: List[ExpertResult] = await asyncio.gather(
        *[_run_one(p, idx) for p, idx in samples],
        return_exceptions=False,  # Let individual skill error handling produce _blank_result
    )
    fan_out_ms = (time.perf_counter() - t0) * 1000
    logger.info(f"{mode} fan-out completed in {fan_out_ms:.0f}ms, {len(results)} results")

    # ── Split successes / failures ────────────────────────────────────────
    # All explicit failures are non-voting even if
    # a spurious non-zero confidence leaked through — see
    # _is_successful_expert. Their results stay in `failures` so the
    # frontend can still show why that expert did not grade the item.
    successes = [er for er in results if _is_successful_expert(er)]
    failures = [er for er in results if not _is_successful_expert(er)]

    if not successes:
        # Every expert/sample failed — let caller produce an explicit
        # error Correction with synthesis_method='all_failed'.
        raise AllExpertsFailed(failures)

    if len(successes) == 1:
        # Degraded path: only one sample produced a real answer. No IS
        # possible. The failed siblings remain in expert_results.
        logger.info(
            f"{mode} degraded_to_single for q_id={problem.q_id}: "
            f"{len(failures)} of {len(results)} samples failed"
        )
        correction = _expert_to_correction(
            problem.q_id,
            problem.type,
            successes[0],
            synthesis_method="degraded_to_single",
            extra_experts=failures,
        )
        # Failed siblings stay visible, but a usable survivor is not a failed
        # grade. Only confidence below .25 requires confirmation.
        from backend.domain.confidence_policy import confidence_requires_review
        if confidence_requires_review(successes[0].confidence):
            correction.requires_human_review = True
            correction.review_reasons = ["degraded_to_single"]
        return correction

    # ── Synthesis (≥ 2 successes) ─────────────────────────────────────────
    if mode == "multi_sample":
        # Same provider sampled N times — the LLM judge cannot meaningfully
        # arbitrate between its own outputs. Weighted-average is cheaper and
        # gives the IS signal a clean number to anchor on.
        correction = _weighted_average_fallback(problem, successes, grading_setup=grading_setup)
        correction.synthesis_method = "multi_sample"
    elif aggregation_method == "weighted_average":
        correction = _weighted_average_fallback(problem, successes, grading_setup=grading_setup)
    else:
        primary_provider = None
        pick_default = getattr(registry, "pick_default", None)
        if callable(pick_default):
            primary_provider = pick_default()
        if reporter:
            await reporter._emit_message(
                f"{student_id}/{problem.q_id}: synthesize_experts_started"
            )
        try:
            correction = await _synthesize(
                problem=problem,
                expert_results=successes,
                synthesis_provider=primary_provider or providers[0],
                language=language,
                grading_setup=grading_setup,
            )
        finally:
            if reporter:
                await reporter._emit_message(
                    f"{student_id}/{problem.q_id}: synthesize_experts_finished"
                )

    # 2026-08-28 policy ("any high confidence wins"): the merged result must
    # not be less confident than its most confident contributing expert. A
    # weaker model's self-doubt (Qwen on an item it cannot grade) must not
    # drag a confident sibling's verdict into the low-confidence review
    # queue — genuine disagreement is already covered by the fairness
    # signals computed below (IS / minority veto).
    best_expert_confidence = max(er.confidence for er in successes)
    if correction.confidence < best_expert_confidence:
        correction.confidence = round(best_expert_confidence, 2)

    # Re-attach failures so the frontend can show why they failed.
    if failures:
        correction.expert_results = list(correction.expert_results) + failures

    # ── P0 fairness signals: Indecisiveness Score + Minority Veto ─────────
    is_score, requires_review, reasons = _compute_review_signal(successes)
    correction.is_score = is_score
    correction.requires_human_review = requires_review
    correction.review_reasons = reasons
    if requires_review:
        is_str = f"{is_score:.3f}" if is_score is not None else "n/a"
        logger.info(
            f"q_id={problem.q_id} flagged for review (mode={mode}): "
            f"IS={is_str}, reasons={reasons}, "
            f"sample_scores={[round(er.score, 2) for er in successes]}"
        )

    return correction


# ─── Indecisiveness Score + Minority Veto ─────────────────────────────────────

def _compute_review_signal(
    successes: List[ExpertResult],
) -> Tuple[Optional[float], bool, List[str]]:
    """Compute the P0 fairness signals from a set of successful expert/sample results.

    Returns ``(is_score, requires_human_review, review_reasons)``.

    - **is_score**: ``std(scores) / max_score`` across the samples. Population
      std (divide by N, not N-1) so 2 samples don't blow up. ``None`` when
      < 2 samples — variance estimation is meaningless on one data point.
    - **requires_human_review**: True if EITHER ``is_score > settings.is_threshold``
      OR any score deviates from the median by > ``settings.minority_veto_deviation
      * max_score``. The minority-veto half catches "two agree, one strongly
      disagrees" cases that an averaged score would hide.
    - **review_reasons**: stable string IDs (``high_indecisiveness`` /
      ``minority_veto``) so the frontend can localize without parsing.

    The thresholds live on `backend.config.settings` so they can be tuned per
    deployment without code changes (default: IS 0.15, minority 0.30 of max).
    """
    if len(successes) < 2:
        return None, False, []

    # Defensive: skills should agree on max_score for the same problem, but if
    # they disagree we use the largest as the normalization anchor — gives the
    # most generous (lowest-IS) reading rather than artificially inflating it.
    max_score = max(er.max_score for er in successes) or 10.0
    scores = [er.score for er in successes]

    # Population std-dev — handles N=2 cleanly.
    n = len(scores)
    mean = sum(scores) / n
    variance = sum((s - mean) ** 2 for s in scores) / n
    std = variance ** 0.5
    is_score = std / max_score if max_score > 0 else 0.0

    reasons: List[str] = []
    if is_score > _settings.is_threshold:
        reasons.append("high_indecisiveness")

    # Minority-veto: any sample > deviation_pct away from median.
    sorted_scores = sorted(scores)
    if n % 2 == 1:
        median = sorted_scores[n // 2]
    else:
        median = (sorted_scores[n // 2 - 1] + sorted_scores[n // 2]) / 2

    threshold_abs = _settings.minority_veto_deviation * max_score
    if any(abs(s - median) > threshold_abs for s in scores):
        if "minority_veto" not in reasons:
            reasons.append("minority_veto")

    requires_review = len(reasons) > 0
    return is_score, requires_review, reasons


# ─── Synthesis logic ──────────────────────────────────────────────────────────

async def _synthesize(
    problem: ProblemInfo,
    expert_results: List[ExpertResult],
    synthesis_provider: BaseProvider,
    language: str = "en",
    grading_setup: Optional[TaskGradingSetup] = None,
) -> Correction:
    """
    Combine multiple expert results into a final Correction.

    The score is deterministic; the judge only synthesizes the explanation.
    Falls back to the original expert explanations if the LLM call fails.
    """
    aggregate = _weighted_average_fallback(problem, expert_results, grading_setup=grading_setup)
    # Build summary of expert opinions for the judge
    expert_summary_parts = []
    for i, er in enumerate(expert_results, 1):
        expert_summary_parts.append(
            f"Expert {i} ({er.provider}):\n"
            f"  Score: {er.score}/{er.max_score}\n"
            f"  Confidence: {er.confidence:.2f}\n"
            f"  Comment: {er.comment}\n"
        )
    expert_summary = "\n".join(expert_summary_parts)

    system_prompt = build_system_prompt(
        "You are a senior teaching coordinator synthesizing multiple expert opinions "
        "on a student's answer. Produce a fair, well-reasoned final grade while "
        "respecting the supplied rubric."
        + authoritative_score_instruction(problem.max_score),
        language,
        grading_setup,
    )
    user_prompt = (
        f"Problem type: {problem.type}\n"
        f"Problem: {problem.stem}\n\n"
        f"Expert opinions:\n{expert_summary}\n\n"
        f"Rubric: {problem.criterion}\n\n"
        f"Authoritative maximum score: {problem.max_score:g}\n\n"
        f"The final score is fixed at {aggregate.score:g}/{problem.max_score:g} by the configured expert weights. "
        "Explain these expert opinions without changing that score. If experts disagree, explain why in the comment. "
        "Return JSON with: score, max_score, confidence, comment, steps."
    )

    try:
        result, _ = await structured_llm_call(
            synthesis_provider,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output_model=SynthesisOutput,
        )

        step_scores = []
        for s in result.steps:
            if isinstance(s, dict):
                step_scores.append(StepScore(
                    step_no=s.get("step_no", len(step_scores) + 1),
                    desc=s.get("desc", ""),
                    is_correct=bool(s.get("is_correct", True)),
                    score=float(s.get("score", 0.0)),
                ))

        normalized_score, normalized_steps = normalize_score_scale(
            score=result.score,
            reported_max_score=result.max_score,
            authoritative_max_score=problem.max_score,
            steps=step_scores,
        )

        return Correction(
            q_id=problem.q_id,
            type=problem.type,
            score=aggregate.score,
            max_score=problem.max_score,
            confidence=max(0.0, min(result.confidence, 1.0)),
            comment=result.comment,
            steps=normalized_steps if normalized_score == aggregate.score else [],
            expert_results=aggregate.expert_results,
            synthesis_method="judge_agent",
        )

    except Exception as e:
        logger.warning(
            "Synthesis LLM call failed for q_id=%s; exception_type=%s; "
            "falling back to weighted_average",
            problem.q_id,
            type(e).__name__,
        )
        return _weighted_average_fallback(problem, expert_results, grading_setup=grading_setup)


def _weighted_average_fallback(
    problem: ProblemInfo,
    expert_results: List[ExpertResult],
    *,
    grading_setup: Optional[TaskGradingSetup] = None,
) -> Correction:
    """Custom weights > confidence weights > equal weights, using valid votes.

    Failed experts remain available as evidence but never affect the average.
    Round once after aggregation; original expert scores remain unchanged.
    """
    failures = [er for er in expert_results if not _is_successful_expert(er)]
    expert_results = [er for er in expert_results if _is_successful_expert(er)]
    if not expert_results:
        raise AllExpertsFailed(failures)
    custom = grading_setup.expert_weights if grading_setup else {}
    weights = [custom.get(er.provider_id or er.provider.split("#sample", 1)[0], er.confidence) for er in expert_results]
    if not any(weights):
        weights = [1.0] * len(expert_results)
    weights = [Decimal(str(weight)) for weight in weights]
    weighted_score = sum(Decimal(str(er.score)) * weight for er, weight in zip(expert_results, weights)) / sum(weights)
    max_score = problem.max_score
    # 2026-08-28 policy: the merged result is as confident as the BEST
    # contributing expert. Averaging confidences let a weak model's low
    # self-confidence flag the whole verdict for review even when a sibling
    # model was confident ("any high confidence wins").
    best_confidence = max(er.confidence for er in expert_results)

    if len(expert_results) == 1:
        comment = expert_results[0].comment
    else:
        parts = [
            f"【{er.provider}（{er.score:.1f}/{er.max_score:.0f}, conf={er.confidence:.2f}）】\n{er.comment}"
            for er in expert_results
        ]
        comment = "\n\n———\n\n".join(parts)

    return Correction(
        q_id=problem.q_id,
        type=problem.type,
        score=min(max(float(weighted_score.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)), 0.0), problem.max_score),
        max_score=max_score,
        confidence=round(best_confidence, 2),
        comment=comment,
        steps=[],
        expert_results=expert_results + failures,
        synthesis_method="weighted_average",
    )


def _expert_to_correction(
    q_id: str,
    q_type: str,
    expert: ExpertResult,
    synthesis_method: str = "single",
    extra_experts: Optional[List[ExpertResult]] = None,
) -> Correction:
    """Convert a single ExpertResult to a Correction.

    `extra_experts` (failed siblings under degraded_to_single) are appended to
    `expert_results` so the frontend can still display them in the per-expert
    accordion, but they do NOT contribute to score/comment.
    """
    expert_results = [expert] + list(extra_experts or [])
    return Correction(
        q_id=q_id,
        type=q_type,
        score=round_ai_score(expert.score, expert.max_score),
        max_score=expert.max_score,
        confidence=expert.confidence,
        comment=expert.comment,
        steps=expert.steps,
        hits=expert.hits,
        logs=expert.logs,
        expert_results=expert_results,
        synthesis_method=synthesis_method,
    )
