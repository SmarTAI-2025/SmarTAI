"""
Unified structured LLM call — replaces 4 copies of JSON parsing logic scattered
across backend/correct/*.py.

Strategy (in order):
  1. Try provider.with_structured_output(PydanticModel) — cleanest, native.
  2. Fall back to text generation + robust JSON extraction & repair.
  3. Async retry with tenacity (only on rate-limit / transient errors).

All modules (skills, agents) should call this — never duplicate JSON parsing.
"""
from __future__ import annotations

import json
import logging
import random
import re
from typing import Type, TypeVar, Optional, List, Dict, Any

from pydantic import BaseModel, ValidationError
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from backend.config import settings
from backend.llm.providers import BaseProvider, LLMResponse

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


# ─── Exceptions ──────────────────────────────────────────────────────────────

class TransientLLMError(Exception):
    """Retryable error (timeout, 5xx, generic transient)."""


class RateLimitError(TransientLLMError):
    """Retryable rate-limit / quota error.

    Carries the server-suggested wait (`retry_after` seconds) when the provider
    response includes one (Gemini's `retryDelay: '23s'` field, or the standard
    `Retry-After` header on OpenAI / Anthropic). The retry decorator honors it
    instead of guessing with exponential backoff.

    If no hint was returned, `retry_after` stays None and we fall back to a
    conservative fixed wait (`settings.llm_rate_limit_max_wait // 2`).
    """

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentLLMError(Exception):
    """Non-retryable error (4xx auth, bad request)."""


# Patterns we use to extract the retry-after hint from the raw error message.
# Gemini surfaces "Please retry in 23.377528861s" AND a structured
# `retryDelay: '23s'` (Google RetryInfo proto). OpenAI / Anthropic return a
# `Retry-After: 23` HTTP header that langchain folds into the exception text.
_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry\s*[-_]?after['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)", re.IGNORECASE),
    re.compile(r"retrydelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)\s*s", re.IGNORECASE),
    re.compile(r"please\s+retry\s+in\s+(\d+(?:\.\d+)?)\s*s", re.IGNORECASE),
)


def _extract_retry_after(msg: str) -> Optional[float]:
    """Pull a retry-after hint (seconds) out of an LLM error message.

    Returns None when no hint is detected — caller then falls back to a fixed
    cooldown so the loop still makes progress (we'd rather over-wait than
    burn through retries while the quota window hasn't reset).
    """
    for pat in _RETRY_AFTER_PATTERNS:
        m = pat.search(msg)
        if m:
            try:
                v = float(m.group(1))
                if v > 0:
                    return v
            except (ValueError, IndexError):
                continue
    return None


def _classify_exception(e: Exception) -> Exception:
    """Map provider exceptions to transient/permanent for retry logic.

    Rate-limit / quota errors get a dedicated `RateLimitError` carrying the
    server-suggested wait so the retry wait function can honor it precisely
    (Gemini commonly suggests 20-40s, far beyond our exponential cap).
    """
    msg = str(e)
    lower = msg.lower()

    # Auth/permission errors — never retry.
    if any(k in lower for k in ["401", "403", "authentication", "unauthorized", "invalid api key"]):
        return PermanentLLMError(msg)

    # Quota / rate limit — retryable but with server-provided wait when available.
    if (
        "429" in lower
        or "rate limit" in lower
        or "quota" in lower
        or "resourceexhausted" in lower
        or "resource_exhausted" in lower
    ):
        return RateLimitError(msg, retry_after=_extract_retry_after(msg))

    # Generic transient (timeout / 5xx / connection) — retryable.
    if any(k in lower for k in ["timeout", "connection", "5xx", "internal", "503", "502", "504"]):
        return TransientLLMError(msg)

    # Default: treat as transient (safer for flaky APIs).
    return TransientLLMError(msg)


# ─── JSON repair (preserves behavior of backend/dependencies.py) ─────────────

def _fix_incomplete_json(json_str: str) -> str:
    """Best-effort repair of truncated/malformed JSON from LLMs."""
    open_braces = json_str.count("{")
    close_braces = json_str.count("}")
    open_brackets = json_str.count("[")
    close_brackets = json_str.count("]")

    fixed = json_str
    while close_braces < open_braces:
        fixed += "}"
        close_braces += 1
    while close_brackets < open_brackets:
        fixed += "]"
        close_brackets += 1

    quote_count = fixed.count('"')
    if quote_count % 2 != 0:
        fixed += '"'

    fixed = fixed.strip()
    if fixed.startswith("{") and not fixed.endswith("}"):
        fixed += "}"
    elif fixed.startswith("[") and not fixed.endswith("]"):
        fixed += "]"
    return fixed


def format_math_and_quotes(text: str) -> str:
    if not isinstance(text, str):
        return text
    # Fix standard LaTeX delimiters to Reflex-compatible math markers
    text = re.sub(r'\\\[(.*?)\\\]', r'$$\1$$', text, flags=re.DOTALL)
    text = re.sub(r'\\\((.*?)\\\)', r'$\1$', text, flags=re.DOTALL)
    # Strip literal quotes hallucinated by LLM
    text = text.strip('"').strip("'")
    return text

def _clean_strings(data: Any) -> Any:
    """Recursively clean strings in dicts/lists: strip literal quotes, format math."""
    if isinstance(data, dict):
        return {k: _clean_strings(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_clean_strings(v) for v in data]
    elif isinstance(data, str):
        return format_math_and_quotes(data)
    return data

def _escape_latex_backslashes(s: str) -> str:
    """Double up backslashes that aren't part of a valid JSON escape.

    LLM judges synthesizing math feedback routinely emit raw LaTeX inside JSON
    string values: ``"comment": "$F = \\overline{C} + \\bar{D}$"``. The token
    ``\\o`` is not a valid JSON escape, and json.loads rejects it. We need to
    convert each "lone" backslash into ``\\\\`` so JSON parses it as a literal
    backslash.

    JSON officially recognizes ``\\"``, ``\\\\``, ``\\/``, ``\\b``, ``\\f``,
    ``\\n``, ``\\r``, ``\\t``, ``\\uXXXX``. We deliberately EXCLUDE ``\\b`` and
    ``\\f`` from the safe set because LaTeX commands ``\\bar``, ``\\beta``,
    ``\\frac``, ``\\forall`` collide with them and are vastly more common in
    grading feedback than literal backspace / form-feed control chars (which
    no LLM ever intentionally embeds in a comment). So we keep ``\\"``,
    ``\\\\``, ``\\/``, ``\\n``, ``\\r``, ``\\t``, ``\\u`` as legit JSON
    escapes and double every other backslash.

    Naive ``s.replace("\\", "\\\\")`` would *double* already-correct escapes.
    The negative-lookahead regex preserves them.
    """
    return re.sub(r'\\(?!["\\/nrtu])', r'\\\\', s)


def _normalize_inline_newlines(s: str) -> str:
    """Replace literal newlines/tabs that appear *inside* JSON string values.

    JSON forbids unescaped control chars in strings, but LLMs often pretty-
    print multi-line comments. We rewrite the bare LF/CR/TAB that occur
    between a `"` and the next unescaped `"`. Single-pass state machine.
    """
    out = []
    in_str = False
    escaped = False
    for ch in s:
        if not in_str:
            out.append(ch)
            if ch == '"':
                in_str = True
            continue
        # inside string
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            out.append(ch)
            in_str = False
            continue
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    return "".join(out)


def _extract_balanced_json(s: str) -> Optional[str]:
    """Find the first balanced JSON object/array in `s`, walking string literals
    correctly so that braces inside quoted strings don't shift the depth.

    This is more robust than the previous greedy ``\\{.*\\}`` regex which would
    happily swallow a trailing duplicate ``}`` (e.g. ``{"x": "y"}}``) emitted
    by some LLMs and produce un-parseable JSON. We stop at the first close
    brace that brings depth back to 0, ignoring everything after.
    """
    n = len(s)
    i = 0
    while i < n:
        ch = s[i]
        if ch == "{" or ch == "[":
            open_ch = ch
            close_ch = "}" if open_ch == "{" else "]"
            depth = 0
            in_str = False
            esc = False
            j = i
            while j < n:
                c = s[j]
                if esc:
                    esc = False
                elif c == "\\" and in_str:
                    esc = True
                elif c == '"':
                    in_str = not in_str
                elif not in_str:
                    if c == open_ch:
                        depth += 1
                    elif c == close_ch:
                        depth -= 1
                        if depth == 0:
                            return s[i : j + 1]
                j += 1
            # Unbalanced — return what we have; downstream repair fns can pad.
            return s[i:]
        i += 1
    return None


def extract_and_parse_json(raw: str, model: Type[T]) -> T:
    """
    Robustly extract JSON from LLM text output and validate against a Pydantic model.
    Handles: markdown code fences, leading prose, backslash escape issues, truncation,
    LaTeX-style backslashes inside string values, literal newlines inside strings,
    AND trailing duplicate close braces (e.g. ``{"a": "b"}}``).
    """
    # 1. Strip markdown code fences if present
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw.strip(), flags=re.MULTILINE)

    # 2. Find first BALANCED JSON object/array. We deliberately don't use
    # ``re.search(r"\{.*\}", ...)`` because it's greedy and will absorb a
    # trailing stray ``}`` the LLM appended after the real object close.
    json_str = _extract_balanced_json(cleaned)
    if json_str is None:
        raise ValueError(f"No JSON found in LLM output. First 200 chars: {raw[:200]}")

    # 3. Try repair attempts in escalating order. Attempt list intentionally
    # composes transforms — `latex+newlines` is the realistic LLM math case
    # (e.g. comment = "F = \overline{C}\n step 2: ..."); previous code only
    # tried double-everything which butchers already-valid escapes.
    for attempt_desc, transform in [
        ("direct", lambda s: s),
        ("latex_backslashes", _escape_latex_backslashes),
        ("normalize_newlines", _normalize_inline_newlines),
        ("latex+newlines", lambda s: _normalize_inline_newlines(_escape_latex_backslashes(s))),
        ("escape_all_backslashes", lambda s: s.replace("\\", "\\\\")),
        ("remove_trailing_commas", lambda s: re.sub(r",(\s*[}\]])", r"\1", s)),
        ("fix_incomplete", _fix_incomplete_json),
    ]:
        try:
            candidate = transform(json_str)
            candidate_dict = json.loads(candidate)
            cleaned_dict = _clean_strings(candidate_dict)
            return model.model_validate(cleaned_dict)
        except (ValidationError, json.JSONDecodeError) as e:
            logger.debug(f"JSON parse attempt '{attempt_desc}' failed: {e}")
            continue

    # 4. All attempts failed — raise with full context
    raise ValueError(
        f"Could not parse LLM output as {model.__name__}. Raw output first 500 chars: {raw[:500]}"
    )


# ─── The unified call ────────────────────────────────────────────────────────


def _retry_wait(retry_state) -> float:
    """tenacity wait callable.

    - On a `RateLimitError` carrying `retry_after`: sleep exactly that long
      (clamped to `settings.llm_rate_limit_max_wait`), plus 0.5-2s jitter so
      concurrent waiters don't synchronize.
    - On a `RateLimitError` *without* a hint: sleep half of the max wait —
      conservatively waits for the quota window to roll rather than hammering.
    - On any other transient: exponential backoff (1, 2, 4 … capped at 30s).
    """
    max_wait = float(settings.llm_rate_limit_max_wait)

    outcome = retry_state.outcome
    exc = outcome.exception() if (outcome and outcome.failed) else None

    if isinstance(exc, RateLimitError):
        if exc.retry_after is not None:
            base = min(max_wait, float(exc.retry_after))
        else:
            base = max_wait / 2.0
        jitter = random.uniform(0.5, 2.0)
        wait = min(max_wait, base + jitter)
        logger.info(
            f"Rate-limit retry: sleeping {wait:.1f}s "
            f"(server hint={exc.retry_after}, attempt={retry_state.attempt_number})"
        )
        return wait

    # Generic transient: exponential 1, 2, 4, 8, ... cap 30s
    n = max(1, retry_state.attempt_number)
    return float(min(30, 2 ** (n - 1)))


def _retry_stop(retry_state) -> bool:
    """Stop condition: rate-limit failures get a separate (larger) budget.

    The two budgets stack so a quota burst that gradually clears doesn't share
    its retries with unrelated transient flakes. Returns True once the budget
    for the *current* exception kind is exhausted.
    """
    outcome = retry_state.outcome
    exc = outcome.exception() if (outcome and outcome.failed) else None
    if isinstance(exc, RateLimitError):
        limit = settings.llm_max_retries + settings.llm_rate_limit_max_retries
    else:
        limit = settings.llm_max_retries
    return retry_state.attempt_number >= max(1, limit)


@retry(
    stop=_retry_stop,
    wait=_retry_wait,
    retry=retry_if_exception_type(TransientLLMError),  # RateLimitError subclasses this
    reraise=True,
)
async def _ainvoke_with_retry(provider: BaseProvider, messages: List[BaseMessage]) -> LLMResponse:
    """Inner retry wrapper — honors retry-after hints, async-native."""
    try:
        return await provider.ainvoke(messages)
    except Exception as e:
        raise _classify_exception(e) from e


# Public alias for callers outside this module. Ingest agent / future helpers
# that issue raw provider.ainvoke calls must route through this so they share
# the same transient/rate-limit retry + classification policy as the skills.
ainvoke_with_retry = _ainvoke_with_retry


async def structured_llm_call(
    provider: BaseProvider,
    *,
    system_prompt: str,
    user_prompt: str,
    output_model: Type[T],
    use_native_structured_output: bool = True,
) -> tuple[T, LLMResponse]:
    """
    Single entry point for all structured LLM calls.

    Args:
        provider: Which LLM provider to use (ExpertRegistry gives you one).
        system_prompt: System message text.
        user_prompt: User message text.
        output_model: Pydantic model class to parse response into.
        use_native_structured_output: Try provider's native structured output
            (.with_structured_output) first. Falls back to text+regex on failure.

    Returns:
        (parsed_model, raw_llm_response). The raw response is preserved for
        ExpertResult.raw_output traceability.
    """
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]

    # Always use text generation + JSON extraction.
    # Native structured output (.with_structured_output) is skipped because:
    #   1. Gemini's ainvoke uses gRPC which ignores HTTP_PROXY
    #   2. Not all providers support it equally
    #   3. Text + parse is more portable and debuggable
    raw_response = await _ainvoke_with_retry(provider, messages)
    parsed = extract_and_parse_json(raw_response.content, output_model)
    return parsed, raw_response
