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
import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Type, TypeVar, Optional, List, Dict, Any, Callable

from pydantic import BaseModel, ValidationError
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from backend.config import settings
from backend.llm.providers import BaseProvider, LLMResponse, ProviderRequestError
from backend.llm.endpoint_policy import ProviderEndpointError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

MATH_MARKDOWN_SYSTEM_INSTRUCTION = (
    "\n\nMARKDOWN MATH CONTRACT: In every prose field, wrap inline mathematics in "
    "`$...$` and display mathematics in `$$...$$`. Never leave LaTeX commands "
    "such as `\\int`, `\\mu`, or `\\times` bare. Do not add math delimiters "
    "inside source code, code blocks, test input, or expected output. Inside JSON, "
    "escape a TeX backslash exactly once and encode each line break exactly once; "
    "the decoded field must contain one backslash per TeX command and real newlines, "
    "not the visible characters `\\n`. Never use triple-dollar delimiters."
)

class StructuredOutputBoundsError(ValueError):
    """The provider returned valid JSON whose fields exceed safe bounds."""


class StructuredOutputInvalidError(ValueError):
    """Malformed model output, with no source content in the error message."""

    code = "provider_response_invalid"


@dataclass
class _AttemptBudget:
    used: int = 0
    waited: float = 0


_attempt_budget: ContextVar[_AttemptBudget | None] = ContextVar("llm_attempt_budget", default=None)


@contextmanager
def llm_attempt_budget():
    """Share the three-attempt ceiling across initial output and format repair."""
    if _attempt_budget.get() is not None:
        yield
        return
    token = _attempt_budget.set(_AttemptBudget())
    try:
        yield
    finally:
        _attempt_budget.reset(token)


# ─── Exceptions ──────────────────────────────────────────────────────────────

class TransientLLMError(Exception):
    """Retryable error (timeout, 5xx, generic transient)."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class RateLimitError(TransientLLMError):
    """Retryable rate-limit / quota error.

    Carries the server-suggested wait (`retry_after` seconds) when the provider
    response includes one (Gemini's `retryDelay: '23s'` field, or the standard
    `Retry-After` header on OpenAI / Anthropic). The retry decorator honors it
    instead of guessing with exponential backoff.

    If no hint was returned, `retry_after` stays None and we fall back to a
    conservative 65-second cooldown plus jitter.
    """

class PermanentLLMError(Exception):
    """Non-retryable error (4xx auth, bad request)."""


class DailyQuotaError(PermanentLLMError):
    code = "provider_daily_quota_exceeded"

    def __init__(self):
        super().__init__(self.code)


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


def _provider_retry_after(exc: Exception) -> float | None:
    value = getattr(exc, "retry_after", None)
    if value is None:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", {}) or {}
        value = headers.get("retry-after") or headers.get("Retry-After")
    if value is not None:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            try:
                seconds = parsedate_to_datetime(str(value)).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                seconds = 0
        if math.isfinite(seconds) and seconds > 0:
            return seconds
    return _extract_retry_after(str(exc))


def _block_exhausted_provider(provider, code: str) -> None:
    if code in {"provider_daily_quota_exceeded", "provider_quota_exceeded"}:
        from backend.services.execution_control import current_execution
        control = current_execution()
        if control is not None:
            control.block(provider, code)


def _classify_exception(e: Exception) -> Exception:
    """Map provider exceptions to transient/permanent for retry logic.

    Rate-limit / quota errors get a dedicated `RateLimitError` carrying the
    server-suggested wait so the retry wait function can honor it precisely
    (Gemini commonly suggests 20-40s, far beyond our exponential cap).

    Exceptions carrying an exact HTTP status code (SafeRelayProvider's
    ProviderRequestError, openai's APIStatusError) are classified on the code
    itself — keyword-matching a stable error-code string such as
    "provider_upstream_unavailable" would miss every branch below and fall
    into the default 3-attempt transient path.
    """
    from backend.llm.provider_limits import exhausted_quota_code
    exhausted = exhausted_quota_code(e)
    if exhausted:
        return DailyQuotaError() if exhausted == "provider_daily_quota_exceeded" else PermanentLLMError(exhausted)
    if getattr(e, "retryable", True) is False:
        return PermanentLLMError("non_retryable_provider_limit")
    if isinstance(e, ProviderRequestError) and e.code in {"provider_recitation_blocked", "provider_content_blocked"}:
        return PermanentLLMError(e.code)

    # Endpoint safety-policy rejections (non-public address, host not allowed,
    # DNS/config policy) are deterministic configuration/environment errors:
    # retrying with backoff merely burns several attempts plus 1s/2s/4s waits
    # before the same refusal repeats. Fail fast so the caller surfaces the
    # concrete code instead of appearing to hang on a slow model call.
    if isinstance(e, ProviderEndpointError):
        code = str(e).strip()
        if code in {
            "provider_endpoint_non_public_address",
            "provider_endpoint_host_not_allowed",
            "provider_endpoint_https_required",
            "provider_endpoint_port_not_allowed",
            "provider_endpoint_invalid",
            "provider_base_url_not_allowed",
            "provider_wire_protocol_not_supported",
            "provider_wire_protocol_requires_custom_endpoint",
            "provider_model_invalid",
            "provider_endpoint_protocol_mismatch",
            "provider_endpoint_redirect_blocked",
        }:
            return PermanentLLMError(code)

    msg = str(e)
    lower = msg.lower()

    # Structured status codes win over keyword guessing: the stable
    # error-code strings ("provider_upstream_unavailable", …) carry no digits,
    # so without this branch a relay 503 would fall into the default path.
    status_code = getattr(e, "status_code", None)
    if isinstance(status_code, int):
        if status_code in {401, 403, 404}:
            # Deterministic auth / routing failures — retrying only delays
            # the same refusal.
            return PermanentLLMError(msg)
        if status_code in {500, 502, 503, 504}:
            return TransientLLMError(msg, retry_after=_provider_retry_after(e))
        if status_code == 429:
            retry_after = _provider_retry_after(e)
            return RateLimitError(
                msg, retry_after=retry_after or _extract_retry_after(msg)
            )

    # Auth/permission errors — never retry.
    if any(k in lower for k in ["401", "403", "authentication", "unauthorized", "invalid api key"]):
        return PermanentLLMError(msg)

    # Quota / rate limit — retryable with long waits.
    if (
        "429" in lower
        or "rate limit" in lower
        or "rate_limited" in lower
        or "quota" in lower
        or "resourceexhausted" in lower
        or "resource_exhausted" in lower
    ):
        return RateLimitError(msg, retry_after=_provider_retry_after(e))

    # Generic transient (timeout / 5xx / connection) — retryable.
    if any(k in lower for k in ["timeout", "timed out", "connection", "5xx", "internal", "upstream_unavailable"]):
        return TransientLLMError(msg, retry_after=_provider_retry_after(e))

    # Default: treat as transient (safer for flaky APIs).
    return TransientLLMError(msg, retry_after=_provider_retry_after(e))


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


_PROTECTED_MARKDOWN_RE = re.compile(
    r"(```[\s\S]*?```|`[^`\n]*`|\$\$[\s\S]*?\$\$|(?<!\\)\$(?:\\.|[^$\n])+\$)"
)
_LEADING_MATH_RE = re.compile(
    r"^\\(?:int|sum|prod|lim|frac|dfrac|tfrac|sqrt|ker|operatorname|lVert|Vert)(?![A-Za-z])"
)
_BARE_MATH_ENVIRONMENT_RE = re.compile(
    r"\\begin\{(?P<environment>align\*?|aligned|alignat\*?|alignedat|gather\*?|gathered|"
    r"equation\*?|array|[pbBvV]?matrix\*?|cases)\}[\s\S]*?\\end\{(?P=environment)\}"
)
_SOURCE_CODE_START_RE = re.compile(
    r"^\s*(?:def|class|async\s+def|import|from\s+\S+\s+import|function|const|let|var|"
    r"public|private|protected|#include)\b",
    re.MULTILINE,
)
_PROSE_WORD_RE = re.compile(
    r"\b(?:show|find|prove|where|when|then|and|or|with|for|use|return|assume|explain)\b",
    re.IGNORECASE,
)
_DEGREE_EXPRESSION_RE = re.compile(
    r"(?<![\w$])(?P<base>[+-]?(?:\d+(?:\.\d+)?|[A-Za-z]))\s*\^\s*(?P<command>\\(?:circ|degree))\b"
)
_LATEX_ATOM_RE = re.compile(
    r"(?:"
    r"\\(?:d?frac|tfrac)\s*\{[^{}\n]*\}\s*\{[^{}\n]*\}"
    r"|\\sqrt(?:\s*\[[^\]\n]*\])?\s*\{[^{}\n]*\}"
    r"|\\(?:text|mathrm|mathbf|mathit|operatorname)\s*\{[^{}\n]*\}(?:\s*[_^](?:\{[^{}\n]*\}|[A-Za-z0-9]))*"
    r"|\\(?:int|sum|prod|lim|ker|rank|sin|cos|tan|log|ln|exp|det|max|min|"
    r"alpha|beta|gamma|delta|epsilon|theta|lambda|mu|nu|pi|rho|sigma|tau|phi|psi|omega|"
    r"infty|partial|nabla|ell|lVert|rVert|Vert)"
    r"(?:\s*[_^](?:\{[^{}\n]*\}|[A-Za-z0-9]))*(?:\s*\([^()\n]*\))?"
    r"|\\(?:times|cdot|div|pm|mp|leq?|geq?|neq|approx|equiv|in|notin|subseteq|supseteq|to|mapsto)"
    r")"
)
_DOUBLE_ESCAPED_LATEX_RE = re.compile(
    r"\\\\(?=(?:int|sum|prod|lim|frac|dfrac|tfrac|sqrt|ker|rank|sin|cos|tan|log|ln|exp|det|max|min|"
    r"alpha|beta|gamma|delta|epsilon|theta|lambda|mu|nu|pi|rho|sigma|tau|phi|psi|omega|"
    r"infty|partial|nabla|ell|lVert|rVert|Vert|text|mathrm|mathbf|mathit|operatorname|"
    r"left|right|begin|end|times|cdot|div|pm|mp|leq?|geq?|neq|approx|equiv|in|notin|"
    r"subseteq|supseteq|to|mapsto|circ|star|langle|rangle|dots|ldots|cdots|iota|mid|forall|exists)(?![A-Za-z]))"
)
_OVERESCAPED_NEWLINE_RE = re.compile(
    r"\\{1,2}n(?!(?:u|abla|eq|e|otin|i|exists|eg|ot|ewcommand|ewline|ewpage|olimits|onumber)\b)"
)


def format_math_and_quotes(text: str) -> str:
    if not isinstance(text, str):
        return text
    # Structured output occasionally contains an unfenced implementation in a
    # generic text field. Never reinterpret source-code escape sequences as
    # mathematics; code-specific fields are also excluded in `_clean_strings`.
    if _SOURCE_CODE_START_RE.match(text):
        return text
    text = _normalize_overescaped_markdown(text)
    # Normalize standard LaTeX delimiters for the Markdown math renderer.
    text = re.sub(r'\\\[(.*?)\\\]', r'$$\1$$', text, flags=re.DOTALL)
    text = re.sub(r'\\\((.*?)\\\)', r'$\1$', text, flags=re.DOTALL)
    # Strip literal quotes hallucinated by LLM
    text = text.strip('"').strip("'")
    return _wrap_bare_latex(text)


def _normalize_overescaped_markdown(text: str) -> str:
    """Repair presentation-only double escaping without changing semantics.

    Some models correctly return JSON but double-escape the *contents* of a
    prose field. After JSON decoding that leaves visible ``\\n`` separators and
    two backslashes before TeX commands, which Markdown/KaTeX cannot interpret.
    This pass is deliberately narrow: it only decodes separator-shaped newlines
    and a fixed allowlist of TeX commands. Code/test fields never call it.
    """
    parts = re.split(r"(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|(?<![A-Za-z0-9_])[A-Za-z]:\\[^\s]*)", text)
    return "".join(part if index % 2 else _normalize_prose_escapes(part)
                   for index, part in enumerate(parts))


def _normalize_prose_escapes(text: str) -> str:
    # Inside math, n-prefixed commands are TeX (including commands outside our
    # common-command allowlist), not prose separators.
    parts = re.split(r"(\$\$[\s\S]*?\$\$|\$[^$\n]*\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\))", text)
    text = "".join(part if index % 2 else _OVERESCAPED_NEWLINE_RE.sub(
        "\n", re.sub(r"\\{1,2}r\\{1,2}n", "\n", part)) for index, part in enumerate(parts))
    text = _DOUBLE_ESCAPED_LATEX_RE.sub(lambda _match: "\\", text)
    text = re.sub(r"\\\\(?=[\[\]()])", lambda _match: "\\", text)
    text = re.sub(r"(?<!\$)\${3,}(?!\$)", "$$", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def _wrap_bare_latex(text: str) -> str:
    """Add Markdown math delimiters around common bare LaTeX conservatively.

    Prompting remains the first line of defence. This deterministic pass keeps
    an occasional non-compliant model response renderable without touching
    fenced/inline code or expressions that already carry math delimiters.
    """
    parts = _PROTECTED_MARKDOWN_RE.split(text)
    return "".join(
        part if not part or part.startswith(("$", "`")) else _wrap_bare_latex_segment(part)
        for part in parts
    )


def _wrap_bare_latex_segment(segment: str) -> str:
    # A multi-line environment is one math expression, not a series of atoms.
    output = []
    offset = 0
    for match in _BARE_MATH_ENVIRONMENT_RE.finditer(segment):
        output.extend(_wrap_bare_latex_line(line) for line in segment[offset:match.start()].splitlines(keepends=True))
        output.append("\n$$\n" + match.group(0) + "\n$$\n")
        offset = match.end()
    output.extend(_wrap_bare_latex_line(line) for line in segment[offset:].splitlines(keepends=True))
    return "".join(output)


def _wrap_bare_latex_line(line: str) -> str:
    newline = "\n" if line.endswith("\n") else ""
    body = line[:-1] if newline else line
    leading = body[: len(body) - len(body.lstrip())]
    trailing = body[len(body.rstrip()):]
    core = body.strip()
    if not core or "\\" not in core:
        return line

    # Formula-only lines from PDF extraction are common. Wrap the complete
    # expression so integral bounds, operands, and equality chains stay intact.
    if _LEADING_MATH_RE.match(core) and not _PROSE_WORD_RE.search(core):
        punctuation = core[-1] if core[-1:] in {".", ",", ";", ":"} else ""
        expression = core[:-1] if punctuation else core
        return f"{leading}${expression}${punctuation}{trailing}{newline}"

    # Inline degree notation needs its numeric/symbolic base inside the same
    # math span; other known commands can safely render as individual atoms.
    core = _DEGREE_EXPRESSION_RE.sub(
        lambda match: f"${match.group('base')}^{match.group('command')}$",
        core,
    )
    inline_parts = _PROTECTED_MARKDOWN_RE.split(core)
    normalized = "".join(
        part if not part or part.startswith(("$", "`")) else _LATEX_ATOM_RE.sub(lambda match: f"${match.group(0)}$", part)
        for part in inline_parts
    )
    return f"{leading}{normalized}{trailing}{newline}"


_SOURCE_CODE_FIELD_NAMES = frozenset(
    {
        "code",
        "solution_code",
        "reference_code",
        "student_code",
    }
)
_CODE_FIELD_NAMES = frozenset(
    {
        *_SOURCE_CODE_FIELD_NAMES,
        "input",
        "expected_output",
        "expected_return",
    }
)


def normalize_code_line_breaks(text: str) -> str:
    """Decode model-escaped code line separators outside string literals.

    Structured providers normally turn JSON ``\\n`` escapes into real newlines.
    Some models escape the code contents a second time, leaving visible ``\\n``
    text after JSON decoding.  A global replacement would corrupt legitimate
    string/regex literals such as ``print("\\\\n")``.  This small scanner only
    decodes one- or two-backslash CR/LF separators while outside quoted source
    strings, preserving every character inside single, double, triple, and
    backtick-delimited strings.
    """
    if "\\n" not in text and "\\r" not in text:
        return text

    output: list[str] = []
    index = 0
    delimiter = ""
    while index < len(text):
        if delimiter:
            if text.startswith(delimiter, index):
                output.append(delimiter)
                index += len(delimiter)
                delimiter = ""
                continue
            if text[index] == "\\" and index + 1 < len(text):
                output.append(text[index:index + 2])
                index += 2
                continue
            output.append(text[index])
            index += 1
            continue

        character = text[index]
        if character in {"'", '"', "`"}:
            delimiter = (
                character * 3
                if character != "`" and text.startswith(character * 3, index)
                else character
            )
            output.append(delimiter)
            index += len(delimiter)
            continue

        escaped_line_break_end = _escaped_code_line_break_end(text, index)
        if escaped_line_break_end is not None:
            output.append("\n")
            index = escaped_line_break_end
            continue

        output.append(character)
        index += 1

    return "".join(output)


def _escaped_code_line_break_end(text: str, index: int) -> Optional[int]:
    if text[index] != "\\":
        return None
    cursor = index
    while cursor < len(text) and text[cursor] == "\\":
        cursor += 1
    if cursor - index not in {1, 2} or cursor >= len(text):
        return None
    if text[cursor] == "n":
        return cursor + 1
    if text[cursor] != "r":
        return None

    newline_slashes = cursor + 1
    newline_marker = newline_slashes
    while newline_marker < len(text) and text[newline_marker] == "\\":
        newline_marker += 1
    if newline_marker - newline_slashes not in {1, 2}:
        return None
    return newline_marker + 1 if newline_marker < len(text) and text[newline_marker] == "n" else None


def _clean_strings(data: Any, field_name: Optional[str] = None) -> Any:
    """Recursively clean strings in dicts/lists: strip literal quotes, format math."""
    if isinstance(data, dict):
        return {
            k: _clean_strings(
                v, field_name="solution_code" if k == "text_value" and data.get("target") == "solution_code" else k
            )
            for k, v in data.items()
        }
    elif isinstance(data, list):
        return [_clean_strings(v, field_name=field_name) for v in data]
    elif isinstance(data, str):
        if field_name in _SOURCE_CODE_FIELD_NAMES:
            return normalize_code_line_breaks(data)
        if field_name in _CODE_FIELD_NAMES:
            return data
        return format_math_and_quotes(data)
    return data


_JSON_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"', re.DOTALL)
_JSON_KEY_SEPARATOR_RE = re.compile(r"\s*:")
_RAW_MATH_SPAN_RE = re.compile(r"(```[\s\S]*?```|`[^`]*`|\$\$[\s\S]*?\$\$|(?<!\\)\$(?!\$)[^$]*\$)")
_JSON_TEX_COLLISION_RE = re.compile(
    r"\\(?:[\\\"/]|(?:n(?:eq?|u|abla|ot(?:in)?|subseteq)|"
    r"t(?:imes|heta|au|ext|frac|o)|r(?:angle|ight|ho|Vert)|"
    r"f(?:rac|orall)|b(?:ar|eta|egin|inom))(?![A-Za-z]))"
)


def _protect_json_math_escapes(source: str) -> str:
    """Preserve raw TeX that is also a valid JSON control escape, before decoding.

    In a math span, raw ``\\neq`` otherwise silently becomes LF + ``eq``.
    Consume valid pairs as units and leave code/test fields and Markdown code
    spans untouched. Unknown/ambiguous prose escapes keep normal JSON meaning.
    """
    field_name = ""
    has_code_candidate = bool(re.search(r'"target"\s*:\s*"solution_code"', source))

    def repair_token(match: re.Match) -> str:
        nonlocal field_name
        token = match.group(0)
        if _JSON_KEY_SEPARATOR_RE.match(source, match.end()):
            field_name = token[1:-1]
            return token
        if (
            field_name in _CODE_FIELD_NAMES
            or (field_name == "text_value" and has_code_candidate)
            or _SOURCE_CODE_START_RE.match(token[1:-1])
        ):
            return token

        def repair_math(span: re.Match) -> str:
            value = span.group(0)
            if value.startswith("`"):
                return value
            def preserve_command(escape: re.Match) -> str:
                token = escape.group(0)
                if len(token) == 2 and token[1] in '\\"/':
                    return token
                return "\\" + token

            return _JSON_TEX_COLLISION_RE.sub(
                preserve_command, value,
            )

        return _RAW_MATH_SPAN_RE.sub(repair_math, token)

    return _JSON_STRING_RE.sub(repair_token, source)

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
    Consume valid escape pairs together so the second slash is never repaired
    again when a response mixes correctly escaped and raw TeX commands.
    """
    out = []
    index = 0
    while index < len(s):
        char = s[index]
        if char == "\\":
            if index + 1 < len(s) and s[index + 1] in '"\\/nrtu':
                out.append(s[index:index + 2])
                index += 2
                continue
            out.append("\\\\")
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _collapse_duplicate_json_colons(s: str) -> str:
    """Repair repeated key separators, never punctuation inside string values."""
    out = []
    in_string = escaped = after_colon = False
    for char in s:
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            after_colon = False
        elif char == ":":
            if after_colon:
                continue
            after_colon = True
        elif not char.isspace():
            after_colon = False
        out.append(char)
    return "".join(out)


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


def _normalize_markdown_json_keys(source: str) -> str:
    """Remove presentation-only bullets/backticks from object keys.

    Some models wrap JSON property names as Markdown list items. Touch only
    keys immediately following an object opener or comma, outside string
    values. Never alter bullets, backticks, math or code inside user content.
    """
    key = re.compile(r"(?:[-*][ \t]+)?(?:`([A-Za-z_][A-Za-z0-9_]*)`|([A-Za-z_][A-Za-z0-9_]*))[ \t]*:")
    result: list[str] = []
    in_string = escaped = False
    previous = ""
    index = 0
    while index < len(source):
        char = source[index]
        if not in_string and previous in {"{", ","}:
            match = key.match(source, index)
            if match:
                result.append(json.dumps(match.group(1) or match.group(2)) + ":")
                index = match.end()
                previous = ":"
                continue
        result.append(char)
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        if not char.isspace():
            previous = char
        index += 1
    return "".join(result)


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
        raise StructuredOutputInvalidError("The model returned no structured JSON result.")
    json_str = _protect_json_math_escapes(_normalize_markdown_json_keys(json_str))

    # 3. Try repair attempts in escalating order. Attempt list intentionally
    # composes transforms — `latex+newlines` is the realistic LLM math case
    # (e.g. comment = "F = \overline{C}\n step 2: ..."); previous code only
    # tried double-everything which butchers already-valid escapes.
    bounds_error: ValidationError | None = None
    for attempt_desc, transform in [
        ("direct", lambda s: s),
        ("latex_backslashes", _escape_latex_backslashes),
        ("normalize_newlines", _normalize_inline_newlines),
        ("latex+newlines", lambda s: _normalize_inline_newlines(_escape_latex_backslashes(s))),
        ("duplicate_separator", lambda s: _collapse_duplicate_json_colons(
            _normalize_inline_newlines(_escape_latex_backslashes(s)))),
        ("escape_all_backslashes", lambda s: s.replace("\\", "\\\\")),
        ("remove_trailing_commas", lambda s: re.sub(r",(\s*[}\]])", r"\1", s)),
        ("fix_incomplete", _fix_incomplete_json),
    ]:
        try:
            candidate = transform(json_str)
            candidate_dict = json.loads(candidate)
            cleaned_dict = _clean_strings(candidate_dict)
            return model.model_validate(cleaned_dict)
        except ValidationError as e:
            if any(
                error.get("type") in {"string_too_long", "too_long"}
                or "safe total size" in str(error.get("msg") or "").lower()
                for error in e.errors()
            ):
                bounds_error = e
            logger.debug(f"JSON parse attempt '{attempt_desc}' failed: {e}")
            continue
        except json.JSONDecodeError as e:
            logger.debug(f"JSON parse attempt '{attempt_desc}' failed: {e}")
            continue

    # 4. All attempts failed — raise with full context
    if bounds_error is not None:
        raise StructuredOutputBoundsError(
            f"Structured output for {model.__name__} exceeds safe field bounds."
        ) from bounds_error
    raise StructuredOutputInvalidError(f"The model result does not match {model.__name__}.")


# ─── The unified call ────────────────────────────────────────────────────────


def _retry_wait(retry_state) -> float:
    """tenacity wait callable.

    - On a rate limit: wait at least 65 seconds and at least Retry-After,
      plus 0.5-2s jitter. The stop predicate enforces the wait budget.
    - On any other transient: exponential backoff (1, 2, 4 … capped at 30s),
      or a longer Retry-After hint. A 5xx hint does not make it a rate limit.
    """
    outcome = retry_state.outcome
    exc = outcome.exception() if (outcome and outcome.failed) else None

    if isinstance(exc, RateLimitError):
        if exc.retry_after is not None:
            base = max(float(settings.llm_rate_limit_retry_seconds), float(exc.retry_after))
        else:
            base = float(settings.llm_rate_limit_retry_seconds)
        jitter = random.uniform(0.5, 2.0)
        # Never retry earlier than Retry-After. The stop predicate rejects a
        # delay beyond the budget instead of silently truncating that hint.
        return base + jitter

    # Generic transient: exponential 1, 2, 4, 8, ... cap 30s
    n = max(1, retry_state.attempt_number)
    return max(float(min(30, 2 ** (n - 1))), float(getattr(exc, "retry_after", None) or 0))


def _retry_provider(retry_state):
    first = retry_state.args[0] if retry_state.args else None
    return getattr(first, "__self__", None) or first


def _retry_limit() -> int:
    return min(3, max(1, int(settings.llm_max_retries)))


def _retry_stop(retry_state) -> bool:
    """One budget, including the first attempt; never stack retry allowances."""
    outcome = retry_state.outcome
    exc = outcome.exception() if (outcome and outcome.failed) else None
    delay = float(getattr(retry_state, "upcoming_sleep", 0))
    budget = _attempt_budget.get()
    stop = (
        retry_state.attempt_number >= _retry_limit()
        or (budget is not None and budget.used >= _retry_limit())
        or (budget.waited if budget is not None else retry_state.idle_for) + delay > float(settings.llm_retry_wait_budget_seconds)
        or ((isinstance(exc, RateLimitError) or getattr(exc, "retry_after", None) is not None)
            and delay > float(settings.llm_rate_limit_max_wait))
    )
    if isinstance(exc, RateLimitError):
        from backend.services.execution_control import current_execution, provider_key
        control = current_execution()
        if control is not None:
            provider = _retry_provider(retry_state)
            key = provider_key(provider)
            control.rate_failures[key] = control.rate_failures.get(key, 0) + 1
            stop = stop or control.rate_failures[key] >= _retry_limit()
            if stop:
                control.block(provider, "provider_rate_limited")
    return stop


async def _before_retry_attempt(retry_state) -> None:
    from backend.services.execution_control import current_execution
    control = current_execution()
    if control is not None:
        await control.check(_retry_provider(retry_state))
    budget = _attempt_budget.get()
    if budget is not None:
        if budget.used >= _retry_limit():
            raise StructuredOutputInvalidError("The structured response attempt limit was reached.")
        budget.used += 1


async def _before_retry_sleep(retry_state) -> None:
    budget = _attempt_budget.get()
    if budget is not None:
        budget.waited += float(retry_state.next_action.sleep)
    exc = retry_state.outcome.exception()
    if not isinstance(exc, RateLimitError):
        return
    seconds = float(retry_state.next_action.sleep)
    next_attempt = (budget.used if budget is not None else retry_state.attempt_number) + 1
    logger.info("Rate-limit retry: waiting %.1fs; attempt=%s/%s", seconds,
                next_attempt, _retry_limit())
    from backend.services.execution_control import current_execution
    control = current_execution()
    if control is not None:
        from backend.services.execution_control import provider_key
        provider = _retry_provider(retry_state)
        await control.waiting(provider, max(next_attempt, control.rate_failures.get(provider_key(provider), 0) + 1),
                              _retry_limit(), seconds)


@retry(
    stop=_retry_stop,
    wait=_retry_wait,
    retry=retry_if_exception_type(TransientLLMError),  # RateLimitError subclasses this
    before=_before_retry_attempt,
    before_sleep=_before_retry_sleep,
    reraise=True,
)
async def _ainvoke_with_retry(provider: BaseProvider, messages: List[BaseMessage]) -> LLMResponse:
    """Inner retry wrapper — honors retry-after hints, async-native."""
    try:
        response = await provider.ainvoke(messages)
        if getattr(response, "finish_reason", None) == "refused":
            raise ProviderRequestError(getattr(response, "refusal_code", None) or "provider_content_blocked", status_code=422)
        return response
    except Exception as e:
        classified = _classify_exception(e)
        _block_exhausted_provider(provider, str(classified))
        raise classified from e


# Public alias for callers outside this module. Ingest agent / future helpers
# that issue raw provider.ainvoke calls must route through this so they share
# the same transient/rate-limit retry + classification policy as the skills.
ainvoke_with_retry = _ainvoke_with_retry


@retry(stop=_retry_stop, wait=_retry_wait, retry=retry_if_exception_type(RateLimitError),
       before=_before_retry_attempt, before_sleep=_before_retry_sleep, reraise=True)
async def _invoke_with_rate_retry(invoke, *args, **kwargs):
    """Retry explicit rejected 429s only; never replay an uncertain vision call."""
    try:
        return await invoke(*args, **kwargs)
    except Exception as exc:
        from backend.services.background_errors import classify_background_error
        code = classify_background_error(exc, "provider_request_failed")
        _block_exhausted_provider(getattr(invoke, "__self__", invoke), code)
        if code == "provider_daily_quota_exceeded":
            raise DailyQuotaError() from exc
        if code == "provider_rate_limited":
            raise RateLimitError("provider_rate_limited", retry_after=_provider_retry_after(exc)) from exc
        raise


async def ainvoke_vision_with_rate_retry(provider, prompt, images, *, max_output_tokens):
    return await _invoke_with_rate_retry(provider.ainvoke_vision, prompt, images, max_output_tokens=max_output_tokens)


async def ainvoke_with_rate_retry(provider, messages):
    return await _invoke_with_rate_retry(provider.ainvoke, messages)


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
    messages = [
        SystemMessage(content=system_prompt + MATH_MARKDOWN_SYSTEM_INSTRUCTION),
        HumanMessage(content=user_prompt),
    ]

    # Always use text generation + JSON extraction.
    # Native structured output (.with_structured_output) is skipped because:
    #   1. Gemini's ainvoke uses gRPC which ignores HTTP_PROXY
    #   2. Not all providers support it equally
    #   3. Text + parse is more portable and debuggable
    with llm_attempt_budget():
        raw_response = await _ainvoke_with_retry(provider, messages)
        parsed, raw_response = await parse_with_format_repair(provider, messages, raw_response, output_model)
    return parsed, raw_response


async def parse_with_format_repair(provider, messages, raw_response, output_model, *, invoke=None, validate_output: Callable[[T], None] | None = None):
    """One schema-guided repair, shared by every provider; never invent a score."""
    try:
        parsed = extract_and_parse_json(raw_response.content, output_model)
        if validate_output is not None:
            validate_output(parsed)
        return parsed, raw_response
    except StructuredOutputInvalidError:
        budget = _attempt_budget.get()
        if budget is not None and budget.used >= _retry_limit():
            raise
        # Regenerate from the original evidence. Do not replay malformed output
        # as instructions, expose it in logs, or loop indefinitely.
        repair = HumanMessage(content=(
            "Your previous response could not be parsed or did not satisfy the required schema. "
            "Return one complete JSON value matching this schema, with all required fields. "
            "Use only the original evidence and the same grading/recognition rules. "
            "Do not fill missing student work or invent a score to satisfy validation. "
            "Encode each newline once. No commentary or Markdown fences.\n"
            + json.dumps(output_model.model_json_schema(), ensure_ascii=False)))
        corrected = await (invoke or _ainvoke_with_retry)(provider, [*messages, repair])
        from dataclasses import replace
        corrected = replace(corrected, duration_ms=raw_response.duration_ms + corrected.duration_ms,
            input_tokens=None if raw_response.input_tokens is None or corrected.input_tokens is None else raw_response.input_tokens + corrected.input_tokens,
            output_tokens=None if raw_response.output_tokens is None or corrected.output_tokens is None else raw_response.output_tokens + corrected.output_tokens)
        parsed = extract_and_parse_json(corrected.content, output_model)
        if validate_output is not None:
            validate_output(parsed)
        return parsed, corrected
