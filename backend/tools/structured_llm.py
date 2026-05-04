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
    """Retryable error (rate limit, 5xx, timeout)."""


class PermanentLLMError(Exception):
    """Non-retryable error (4xx auth, bad request)."""


def _classify_exception(e: Exception) -> Exception:
    """Map provider exceptions to transient/permanent for retry logic."""
    msg = str(e).lower()
    if any(k in msg for k in ["rate limit", "429", "timeout", "connection", "5xx", "internal"]):
        return TransientLLMError(str(e))
    if any(k in msg for k in ["401", "403", "authentication", "unauthorized", "invalid api key"]):
        return PermanentLLMError(str(e))
    # Default: treat as transient (safer for flaky APIs)
    return TransientLLMError(str(e))


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

@retry(
    stop=stop_after_attempt(settings.llm_max_retries),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(TransientLLMError),
    reraise=True,
)
async def _ainvoke_with_retry(provider: BaseProvider, messages: List[BaseMessage]) -> LLMResponse:
    """Inner retry wrapper — exponential backoff, async-native."""
    try:
        return await provider.ainvoke(messages)
    except Exception as e:
        raise _classify_exception(e) from e


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
