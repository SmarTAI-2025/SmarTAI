"""
LLM provider adapters with async interface.

Each provider implements: `ainvoke(messages) -> LLMResponse`.

Gemini proxy compatibility:
  The supported langchain-google-genai 4.x client uses Google's HTTP SDK.
  Keep the established per-call synchronous client path when an explicit
  SmarTAI proxy is configured, and native async otherwise. Do not reintroduce
  the removed legacy transport="rest" parameter or infer machine credentials.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import ssl
import time
from abc import ABC, abstractmethod
from collections import deque
from typing import List, Optional, Dict, Any, Deque, Callable
from dataclasses import dataclass

import httpx
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from backend.config import settings
from backend.llm.concurrency import (
    HARD_LIMIT, get_scheduler, initial_concurrency, quota_key, request_memory,
)
from backend.llm.endpoint_policy import (
    build_safe_provider_clients,
    effective_provider_base_url,
    is_user_defined_provider_endpoint,
    provider_operation_url,
)
from backend.llm.provider_catalog import effective_wire_protocol
from backend.llm.image_capability import is_explicit_image_rejection, explicitly_rejects_images
from backend.models import ProviderConfig

logger = logging.getLogger(__name__)




def _configured_proxy_url() -> Optional[str]:
    """Return the explicit proxy for HTTPS model APIs, if configured."""
    return settings.https_proxy.strip() or settings.http_proxy.strip() or None


def _build_httpx_clients(proxy_url: Optional[str]) -> tuple[Any, Any]:
    """Build sync/async clients without inheriting machine proxy variables."""
    import httpx

    kwargs: Dict[str, Any] = {
        "follow_redirects": True,
        "timeout": settings.llm_timeout,
        "trust_env": False,
    }
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return httpx.Client(**kwargs), httpx.AsyncClient(**kwargs)


def _build_provider_httpx_clients(
    config: ProviderConfig,
    *,
    provider_type: str,
    official_proxy_url: Optional[str],
) -> tuple[Any, Any]:
    if not is_user_defined_provider_endpoint(
        provider_type,
        config.base_url,
        config.wire_protocol,
    ):
        return _build_httpx_clients(official_proxy_url)
    if not settings.custom_provider_endpoints_available or not config.base_url:
        raise ValueError("custom_provider_endpoints_disabled")
    return build_safe_provider_clients(
        config.base_url,
        timeout_seconds=float(settings.llm_timeout),
        max_response_bytes=settings.custom_provider_max_response_bytes,
        allowed_target_url=provider_operation_url(
            config.base_url,
            effective_wire_protocol(config.provider_type, config.wire_protocol),
            model=config.model,
        ),
    )


@dataclass
class LLMResponse:
    content: str
    provider: str
    model: str
    duration_ms: float
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    finish_reason: Optional[str] = None
    refusal_code: Optional[str] = None


@dataclass
class VisionImage:
    data: bytes
    media_type: str
    filename: Optional[str] = None


class ProviderRequestError(RuntimeError):
    """Stable provider failure that never includes response bodies or secrets."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after


def _validate_output_limit(value: int | None) -> None:
    if value is not None and (type(value) is not int or not 1 <= value <= 32768):
        raise ProviderRequestError("provider_output_limit_invalid")


def _safe_finish_reason(value: Any) -> str | None:
    # Never preserve arbitrary upstream metadata in recognition evidence.
    if not isinstance(value, str):
        return None
    reason = value.lower()
    if reason in {"stop", "end_turn", "stop_sequence"}:
        return "stop"
    if reason in {"length", "max_tokens", "max_output_tokens"}:
        return "length"
    if reason in {"safety", "content_filter", "recitation", "refusal"}:
        return "refused"
    return "unknown"


def _response_metadata(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage_metadata", None) or {}
    metadata = getattr(response, "response_metadata", None) or {}
    if not isinstance(usage, dict):
        usage = {}
    if not isinstance(metadata, dict):
        metadata = {}
    tokens = {
        name: value if type(value := usage.get(name)) is int and value >= 0 else None
        for name in ("input_tokens", "output_tokens")
    }
    additional = getattr(response, "additional_kwargs", None)
    refused = isinstance(additional, dict) and bool(additional.get("refusal"))
    raw_reason = metadata.get("finish_reason", metadata.get("stop_reason"))
    reason = "refused" if refused else _safe_finish_reason(raw_reason)
    return {**tokens, "finish_reason": reason,
            **({"refusal_code": _refusal_code(raw_reason)} if reason == "refused" else {})}


def _refusal_code(reason: Any) -> str:
    # Only fixed codes cross the API boundary, never arbitrary upstream text.
    return "provider_recitation_blocked" if str(reason).lower() == "recitation" else "provider_content_blocked"


def _response_text(content: Any) -> str:
    """Read visible SDK text blocks, excluding non-visible reasoning metadata."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, dict) and block.get("type") in {"thinking", "redacted_thinking", "reasoning"}:
                continue
            else:
                raise ProviderRequestError("provider_response_invalid")
        return "".join(parts)
    raise ProviderRequestError("provider_response_invalid")


def _image_data_url(image: VisionImage) -> str:
    encoded = base64.b64encode(image.data).decode("ascii")
    return f"data:{image.media_type};base64,{encoded}"


def _build_vision_messages(prompt: str, images: List[VisionImage]) -> List[BaseMessage]:
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image in images:
        content.append({
            "type": "image_url",
            "image_url": {"url": _image_data_url(image)},
        })
    return [HumanMessage(content=content)]


# ─── Per-endpoint overload guard ─────────────────────────────────────────────
# One host (e.g. the USTC campus relay) can front several provider configs.
# When that host starts returning 5xx, independent per-provider retries turn
# every in-flight concurrency slot into a synchronized hammer that prolongs
# the very overload causing the failures.  The breaker counts overload
# failures across ALL providers hitting the same endpoint and, once the
# sliding-window threshold trips, freezes new calls for a cooldown that
# doubles per consecutive trip (30s → 60s → 120s → 240s, capped at 300s).
# A successful call closes the breaker, so a recovering endpoint unlocks on
# its own.

_ENDPOINT_BREAKERS: Dict[str, "_EndpointBreaker"] = {}


def endpoint_key(config: ProviderConfig) -> str:
    """Guard key shared by every provider pointed at the same relay host."""
    return (
        getattr(config, "endpoint_identity", None)
        or config.base_url
        or f"{config.provider_type}:default"
    )


def _is_overload_failure(exc: BaseException) -> bool:
    """True when a failure signals endpoint overload, not a local/config bug.

    Deterministic failures (auth, model-not-found, TLS policy, endpoint
    policy) must NOT feed the breaker: retrying them is pointless and
    freezing the endpoint would punish unrelated, healthy traffic.
    """
    if isinstance(exc, ProviderRequestError):
        if exc.code in {"provider_timeout", "provider_unreachable"}:
            return True
        # Relay 5xx (status_code carried on the error) IS endpoint overload;
        # 4xx codes (auth, rate-limit, model-not-found) are per-key or
        # deterministic and must not feed the breaker.
        status = getattr(exc, "status_code", None)
        return isinstance(status, int) and status >= 500
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status >= 500
    # google.api_core exceptions expose the HTTP status as an int `code`.
    code = getattr(exc, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        return code >= 500
    # LangChain/OpenAI SDK connection-level failures (no HTTP response at all).
    return type(exc).__name__ in {
        "APITimeoutError", "APIConnectionError",
        "ConnectError", "ConnectTimeout", "ReadError", "ReadTimeout",
    }


class _EndpointBreaker:
    """Sliding-window overload counter with an escalating cooldown."""

    def __init__(
        self,
        endpoint: str,
        *,
        window: float = 30.0,
        threshold: int = 6,
        base_cooldown: float = 30.0,
        max_cooldown: float = 300.0,
    ) -> None:
        self.endpoint = endpoint
        self._window = window
        self._threshold = threshold
        self._base_cooldown = base_cooldown
        self._max_cooldown = max_cooldown
        self._failures: Deque[float] = deque()
        self._open_until: float = 0.0
        self._last_failure_at: float = -float("inf")
        self._consecutive_trips = 0

    @property
    def is_open(self) -> bool:
        return time.monotonic() < self._open_until

    def record_success(self) -> None:
        if self._consecutive_trips:
            logger.info(
                "Endpoint overload breaker CLOSED for %s after a successful call",
                self.endpoint,
            )
        self._consecutive_trips = 0
        self._failures.clear()

    def record_failure(self) -> None:
        now = time.monotonic()
        self._last_failure_at = now
        cutoff = now - self._window
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()
        self._failures.append(now)
        if len(self._failures) >= self._threshold and not self.is_open:
            self._consecutive_trips = min(self._consecutive_trips + 1, 4)
            cooldown = min(
                self._max_cooldown,
                self._base_cooldown * (2 ** (self._consecutive_trips - 1)),
            )
            self._open_until = now + cooldown
            self._failures.clear()
            logger.warning(
                "Endpoint overload breaker OPEN for %s: %d overload failures "
                "in last %.0fs — freezing new calls for %.0fs (trip %d)",
                self.endpoint, self._threshold, self._window, cooldown,
                self._consecutive_trips,
            )

    async def before_call(self) -> None:
        """Wait out the cooldown while the breaker is open.

        Each waiter sleeps in ≤2s slices plus 0.5-1.5s of random padding, so
        calls release staggered (no thundering herd) and a fresh trip that
        extended the cooldown is observed by the next slice.
        """
        logged = False
        while True:
            remaining = self._open_until - time.monotonic()
            if remaining <= 0:
                return
            if not logged:
                logger.warning(
                    "Endpoint overload breaker: holding new call to %s "
                    "(~%.0fs of cooldown left)",
                    self.endpoint, remaining,
                )
                logged = True
            await asyncio.sleep(min(remaining, 2.0) + random.uniform(0.5, 1.5))


def _parse_retry_after_header(value: Optional[str]) -> Optional[float]:
    """Parse a `Retry-After: <seconds>` header; ignore the HTTP-date form."""
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if 0 < seconds < float("inf") else None


class BaseProvider(ABC):
    """Abstract provider with async ainvoke interface."""

    provider_type: str = ""
    _image_rejection_recorder: Callable[[], object] | None = None
    @property
    def supports_vision(self) -> bool | None:
        # Capability evidence, never inferred from brand, protocol or model ID.
        state = self.config.image_capability_status
        return True if state == "passed" else False if state == "unsupported" else None

    @property
    def can_attempt_vision(self) -> bool:
        return self.config.image_capability_status != "unsupported"

    def _record_image_rejection(self):
        self.config.image_capability_status = "unsupported"
        recorder = getattr(self, "_image_rejection_recorder", None)
        if recorder is not None:
            try:
                recorder()
            except Exception:
                logger.warning("Unable to persist image rejection evidence")


    def __init__(self, config: ProviderConfig):
        self.config = config
        self.model = config.model
        self._client = None
        self._client_lock = None

    @property
    def provider_id(self) -> str:
        return f"{self.provider_type}:{self.model}"

    def _endpoint_breaker(self) -> _EndpointBreaker:
        key = endpoint_key(self.config)
        breaker = _ENDPOINT_BREAKERS.get(key)
        if breaker is None:
            breaker = _EndpointBreaker(key)
            _ENDPOINT_BREAKERS[key] = breaker
        return breaker

    @abstractmethod
    def _build_client_sync(self) -> Any:
        """Build a LangChain client. Must be callable from any thread."""
        ...

    def _ensure_async_primitives(self) -> None:
        if self._client_lock is None:
            self._client_lock = asyncio.Lock()

    @property
    def effective_concurrency(self) -> int:
        rpm = max(0, int(self.config.rpm or 0))
        try:
            limit = get_scheduler().limit(quota_key(self.config, endpoint_key(self.config)), rpm)
        except RuntimeError:
            limit = initial_concurrency(rpm)
        return min(HARD_LIMIT, limit, max(1, int(settings.max_concurrent_llm_per_provider)))

    def _call_capacity(self, messages: List[BaseMessage]):
        scheduler = get_scheduler()
        return scheduler.lease(
            key=quota_key(self.config, endpoint_key(self.config)),
            rpm=max(0, int(self.config.rpm or 0)),
            owner=self.config.scheduling_owner or "anonymous",
            endpoint=endpoint_key(self.config),
            endpoint_limit=min(HARD_LIMIT, max(1, int(settings.max_concurrent_llm_per_endpoint)),
                               max(1, int(settings.max_concurrent_llm_per_provider))),
            memory=request_memory(messages),
            ready=lambda: not self._endpoint_breaker().is_open,
        )

    def _record_duration(self, duration_ms: float) -> None:
        get_scheduler().record_success(
            quota_key(self.config, endpoint_key(self.config)),
            max(0, int(self.config.rpm or 0)), duration_ms / 1000.0,
            allow_growth=time.monotonic() - self._endpoint_breaker()._last_failure_at >= 60.0,
        )

    def _record_concurrency_overload(self, retry_after: float | None = None) -> None:
        get_scheduler().record_overload(
            quota_key(self.config, endpoint_key(self.config)),
            max(0, int(self.config.rpm or 0)), retry_after,
        )

    async def _get_client(self) -> Any:
        """Get or create a shared client (for native async providers)."""
        self._ensure_async_primitives()
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    if is_user_defined_provider_endpoint(
                        self.config.provider_type,
                        self.config.base_url,
                        self.config.wire_protocol,
                    ):
                        self._client = await asyncio.to_thread(
                            self._build_client_sync
                        )
                    else:
                        self._client = self._build_client_sync()
        return self._client

    def _generation_kwargs(self, max_output_tokens: int | None) -> dict[str, Any]:
        _validate_output_limit(max_output_tokens)
        return {} if max_output_tokens is None else {"max_tokens": max_output_tokens}

    async def ainvoke(self, messages: List[BaseMessage], *, max_output_tokens: int | None = None) -> LLMResponse:
        """Invoke the LLM. Default: native async. Gemini overrides this."""
        generation_kwargs = self._generation_kwargs(max_output_tokens)
        self._ensure_async_primitives()
        client = await self._get_client()
        # Admission combines actual-start RPM, quota concurrency and resources;
        # queued calls do not reserve minute quota or occupy capacity slots.
        await self._endpoint_breaker().before_call()
        async with self._call_capacity(messages):
            t0 = time.perf_counter()
            try:
                response = await client.ainvoke(messages, **generation_kwargs)
                self._endpoint_breaker().record_success()
                content = _response_text(response.content if hasattr(response, "content") else response)
                duration_ms = (time.perf_counter() - t0) * 1000
                self._record_duration(duration_ms)
                logger.info(f"LLM call OK on {self.provider_id} in {duration_ms:.0f}ms ({len(content)} chars)")
                return LLMResponse(content=content, provider=self.provider_id, model=self.model,
                                   duration_ms=duration_ms, **_response_metadata(response))
            except Exception as e:
                if _is_overload_failure(e):
                    self._endpoint_breaker().record_failure()
                duration_ms = (time.perf_counter() - t0) * 1000
                logger.warning(
                    "LLM call failed on %s after %.0fms; exception_type=%s",
                    self.provider_id,
                    duration_ms,
                    type(e).__name__,
                )
                raise

    async def ainvoke_vision(self, prompt: str, images: List[VisionImage], *, max_output_tokens: int | None = None) -> LLMResponse:
        """Invoke a vision-capable model with text prompt plus one or more images."""
        if not self.can_attempt_vision:
            raise ProviderRequestError("provider_vision_not_supported", status_code=400)
        if not images:
            raise ValueError("ainvoke_vision requires at least one image.")
        _validate_output_limit(max_output_tokens)
        options = {} if max_output_tokens is None else {"max_output_tokens": max_output_tokens}
        try:
            return await self.ainvoke(_build_vision_messages(prompt, images), **options)
        except Exception as exc:
            if is_explicit_image_rejection(exc):
                self._record_image_rejection()
                raise ProviderRequestError("provider_vision_not_supported", status_code=400) from exc
            raise


# ─── Gemini ───────────────────────────────────────────────────────────────────

class GeminiProvider(BaseProvider):
    """
    Gemini provider with two modes:
      - Proxy mode (local): run_in_threadpool + per-call fresh client (parallel safe)
      - Direct mode (cloud): native ainvoke with shared client (faster)
    Auto-detected from SmarTAI's explicit proxy settings.
    """
    provider_type = "gemini"

    def _build_client_sync(self) -> Any:
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=self.model,
            # Use the model's sampling default (Gemini 3 recommends 1.0).
            temperature=None,
            timeout=settings.llm_timeout,
            max_retries=0,
            google_api_key=self.config.api_key,
        )

    @property
    def _needs_proxy_mode(self) -> bool:
        return _configured_proxy_url() is not None

    def _generation_kwargs(self, max_output_tokens: int | None) -> dict[str, Any]:
        _validate_output_limit(max_output_tokens)
        return {} if max_output_tokens is None else {
            "generation_config": {"max_output_tokens": max_output_tokens}
        }

    async def ainvoke(self, messages: List[BaseMessage], *, max_output_tokens: int | None = None) -> LLMResponse:
        generation_kwargs = self._generation_kwargs(max_output_tokens)
        if not self._needs_proxy_mode:
            # Cloud mode: native async, shared client
            return await super().ainvoke(messages, max_output_tokens=max_output_tokens)

        # Local proxy mode: sync invoke in threadpool, fresh client per call
        async with get_scheduler().proxy_slots:
            return await self._invoke_proxy(messages, generation_kwargs)

    async def _invoke_proxy(self, messages: List[BaseMessage], generation_kwargs: dict[str, Any]) -> LLMResponse:
        from anyio import to_thread

        # Prepare before RPM admission; at most 50 per-call clients can wait.
        # Preparation uses the default pool, separate from the 50 SDK workers.
        local_client = await to_thread.run_sync(self._build_client_sync)
        self._ensure_async_primitives()
        await self._endpoint_breaker().before_call()
        async with self._call_capacity(messages):
            t0 = time.perf_counter()
            try:
                def _sync_call():
                    started = time.perf_counter()
                    response = local_client.invoke(messages, **generation_kwargs)
                    return response, (time.perf_counter() - started) * 1000

                # Cancellation cannot stop a running sync SDK thread. Keep its
                # capacity leases until it drains; never start replacement work.
                call = asyncio.create_task(to_thread.run_sync(
                    _sync_call, limiter=get_scheduler().thread_limiter,
                ))
                try:
                    response, sdk_duration_ms = await asyncio.shield(call)
                except asyncio.CancelledError:
                    while not call.done():
                        try:
                            await asyncio.shield(call)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    if not call.cancelled():
                        call.exception()
                    raise
                self._endpoint_breaker().record_success()
                content = _response_text(response.content if hasattr(response, "content") else response)
                duration_ms = sdk_duration_ms
                self._record_duration(duration_ms)
                logger.info(f"LLM call OK on {self.provider_id} in {duration_ms:.0f}ms ({len(content)} chars)")
                return LLMResponse(content=content, provider=self.provider_id, model=self.model,
                                   duration_ms=duration_ms, **_response_metadata(response))
            except Exception as e:
                if _is_overload_failure(e):
                    self._endpoint_breaker().record_failure()
                duration_ms = (time.perf_counter() - t0) * 1000
                logger.warning(
                    "LLM call failed on %s after %.0fms; exception_type=%s",
                    self.provider_id,
                    duration_ms,
                    type(e).__name__,
                )
                raise


# ─── OpenAI / Zhipu / Anthropic ──────────────────────────────────────────────

class OpenAIProvider(BaseProvider):
    provider_type = "openai"

    def _generation_kwargs(self, max_output_tokens: int | None) -> dict[str, Any]:
        _validate_output_limit(max_output_tokens)
        return {} if max_output_tokens is None else {"max_completion_tokens": max_output_tokens}

    def _build_client_sync(self) -> Any:
        from langchain_openai import ChatOpenAI
        http_client, http_async_client = _build_provider_httpx_clients(
            self.config,
            provider_type=self.provider_type,
            official_proxy_url=_configured_proxy_url(),
        )

        return ChatOpenAI(
            model=self.model,
            # Reasoning models reject temperature; retain their reasoning default.
            temperature=None,
            use_responses_api=False,
            timeout=settings.llm_timeout,
            max_retries=0,
            api_key=self.config.api_key,
            base_url=self.config.base_url or "https://api.openai.com/v1",
            http_client=http_client,
            http_async_client=http_async_client,
        )


class ZhipuProvider(BaseProvider):
    provider_type = "zhipu"

    def _generation_kwargs(self, max_output_tokens: int | None) -> dict[str, Any]:
        return _compatible_generation_kwargs(max_output_tokens)

    async def ainvoke(self, messages: List[BaseMessage], *, max_output_tokens: int | None = None) -> LLMResponse:
        try:
            return await super().ainvoke(messages, max_output_tokens=max_output_tokens)
        except Exception as exc:
            body = getattr(exc, "body", None)
            detail = body.get("error", body) if isinstance(body, dict) else {}
            if getattr(exc, "status_code", None) == 429 and isinstance(detail, dict) and str(detail.get("code")) == "1305":
                headers = getattr(getattr(exc, "response", None), "headers", {})
                retry_after = _parse_retry_after_header(headers.get("retry-after"))
                self._record_concurrency_overload(retry_after)
                raise ProviderRequestError(
                    "provider_overloaded", status_code=429, retry_after=retry_after,
                ) from None
            raise

    def __init__(self, config: ProviderConfig):
        super().__init__(config)

    def _build_client_sync(self) -> Any:
        from langchain_openai import ChatOpenAI
        # proxy=None is insufficient because httpx would still inherit
        # HTTP(S)_PROXY. Zhipu must use a dedicated direct client.
        http_client, http_async_client = _build_provider_httpx_clients(
            self.config,
            provider_type=self.provider_type,
            official_proxy_url=None,
        )

        return ChatOpenAI(
            model=self.model,
            temperature=None,
            use_responses_api=False,
            timeout=settings.llm_timeout,
            max_retries=0,
            api_key=self.config.api_key,
            base_url=self.config.base_url or settings.zhipu_api_base,
            http_client=http_client,
            http_async_client=http_async_client,
        )


class AnthropicProvider(BaseProvider):
    provider_type = "anthropic"

    def _build_client_sync(self) -> Any:
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=self.model,
            # Recent Claude models reject non-default sampling parameters.
            temperature=None,
            timeout=settings.llm_timeout,
            max_retries=0,
            api_key=self.config.api_key,
            anthropic_proxy=_configured_proxy_url(),
        )


# ─── Domestic OpenAI-compatible providers (direct, no proxy) ─────────────────
# DeepSeek / Moonshot (Kimi) / Qwen expose OpenAI-compatible endpoints that are
# reachable from mainland China without a VPN. Like Zhipu, they must ALWAYS
# build a direct httpx client (proxy=None) so a SMARTAI_HTTPS_PROXY configured
# for foreign providers (OpenAI/Gemini/Anthropic) is never applied to them.
# That is what lets domestic and foreign models coexist when a proxy is set.
# Visual capability is established by image evidence, independently of brand.
# Transport adapters still encode images for unverified configurations.
# Historical provider descriptions below do not determine visual capability.
# Never silently omit image blocks
# as OCR (launch plan 上线前 08); vision GLM is handled by ZhipuProvider above.


def _compatible_generation_kwargs(max_output_tokens: int | None) -> dict[str, Any]:
    _validate_output_limit(max_output_tokens)
    # ChatOpenAI rewrites max_tokens to OpenAI's max_completion_tokens.
    # These vendors document max_tokens; extra_body preserves the wire field.
    return {} if max_output_tokens is None else {"extra_body": {"max_tokens": max_output_tokens}}


class _DomesticOpenAICompatibleProvider(BaseProvider):
    """OpenAI-compatible domestic provider that always connects directly."""

    _default_base_url: str = ""

    def _generation_kwargs(self, max_output_tokens: int | None) -> dict[str, Any]:
        return _compatible_generation_kwargs(max_output_tokens)

    def __init__(self, config: ProviderConfig):
        super().__init__(config)

    def _build_client_sync(self) -> Any:
        from langchain_openai import ChatOpenAI
        http_client, http_async_client = _build_provider_httpx_clients(
            self.config,
            provider_type=self.provider_type,
            official_proxy_url=None,
        )
        return ChatOpenAI(
            model=self.model,
            # Kimi fixes sampling per model; preserve all vendors' defaults.
            temperature=None,
            use_responses_api=False,
            # Qwen's thinking-only/open-weight models require streaming.
            # LangChain assembles the stream into the usual AIMessage.
            streaming=self.provider_type == "qwen",
            stream_usage=self.provider_type == "qwen",
            timeout=settings.llm_timeout,
            max_retries=0,
            api_key=self.config.api_key,
            base_url=self.config.base_url or self._default_base_url,
            http_client=http_client,
            http_async_client=http_async_client,
        )


class DeepSeekProvider(_DomesticOpenAICompatibleProvider):
    provider_type = "deepseek"
    _default_base_url = "https://api.deepseek.com/v1"


class MoonshotProvider(_DomesticOpenAICompatibleProvider):
    provider_type = "moonshot"
    _default_base_url = "https://api.moonshot.cn/v1"


class QwenProvider(_DomesticOpenAICompatibleProvider):
    provider_type = "qwen"
    _default_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"


# ─── User-defined endpoint protocols ────────────────────────────────────────

def _message_role(message: BaseMessage) -> str:
    if isinstance(message, SystemMessage):
        return "system"
    role = getattr(message, "type", "human")
    if role in {"ai", "assistant"}:
        return "assistant"
    return "user"


def _data_url_parts(value: str) -> tuple[str, str]:
    match = re.fullmatch(r"data:([^;,]+);base64,([A-Za-z0-9+/=]+)", value)
    if match is None:
        raise ProviderRequestError("provider_image_payload_invalid")
    return match.group(1), match.group(2)


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]
    blocks: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            blocks.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            blocks.append({"type": "text", "text": str(item)})
            continue
        if item.get("type") == "text":
            blocks.append({"type": "text", "text": str(item.get("text", ""))})
            continue
        if item.get("type") == "image_url":
            image_url = item.get("image_url")
            value = image_url.get("url") if isinstance(image_url, dict) else image_url
            if not isinstance(value, str):
                raise ProviderRequestError("provider_image_payload_invalid")
            media_type, data = _data_url_parts(value)
            blocks.append({
                "type": "image",
                "media_type": media_type,
                "data": data,
                "data_url": value,
            })
            continue
        raise ProviderRequestError("provider_message_payload_not_supported")
    return blocks


def _openai_payload(messages: List[BaseMessage], model: str) -> dict[str, Any]:
    output: list[dict[str, Any]] = []
    for message in messages:
        content: str | list[dict[str, Any]]
        blocks = _content_blocks(message.content)
        if len(blocks) == 1 and blocks[0]["type"] == "text":
            content = blocks[0]["text"]
        else:
            content = []
            for block in blocks:
                if block["type"] == "text":
                    content.append({"type": "text", "text": block["text"]})
                else:
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": block["data_url"]},
                    })
        output.append({"role": _message_role(message), "content": content})
    return {"model": model, "messages": output}


def _anthropic_payload(messages: List[BaseMessage], model: str) -> dict[str, Any]:
    system_parts: list[str] = []
    output: list[dict[str, Any]] = []
    for message in messages:
        blocks = _content_blocks(message.content)
        if isinstance(message, SystemMessage):
            system_parts.extend(
                block["text"] for block in blocks if block["type"] == "text"
            )
            continue
        content: list[dict[str, Any]] = []
        for block in blocks:
            if block["type"] == "text":
                content.append({"type": "text", "text": block["text"]})
            else:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": block["media_type"],
                        "data": block["data"],
                    },
                })
        output.append({"role": _message_role(message), "content": content})
    payload: dict[str, Any] = {
        "model": model,
        "messages": output,
        "max_tokens": 4096,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    return payload


def _gemini_payload(messages: List[BaseMessage], model: str) -> dict[str, Any]:
    system_parts: list[dict[str, str]] = []
    contents: list[dict[str, Any]] = []
    for message in messages:
        blocks = _content_blocks(message.content)
        parts: list[dict[str, Any]] = []
        for block in blocks:
            if block["type"] == "text":
                parts.append({"text": block["text"]})
            else:
                parts.append({
                    "inlineData": {
                        "mimeType": block["media_type"],
                        "data": block["data"],
                    }
                })
        if isinstance(message, SystemMessage):
            system_parts.extend(parts)
        else:
            contents.append({
                "role": "model" if _message_role(message) == "assistant" else "user",
                "parts": parts,
            })
    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {},
    }
    if system_parts:
        payload["systemInstruction"] = {"parts": system_parts}
    return payload


def _response_error_code(status_code: int) -> str:
    if status_code in {401, 403}:
        return "provider_auth_failed"
    if status_code == 404:
        return "provider_model_or_endpoint_not_found"
    if status_code == 429:
        return "provider_rate_limited"
    if status_code >= 500:
        return "provider_upstream_unavailable"
    return "provider_request_rejected"


def _transport_error_code(exc: BaseException) -> str:
    """Preserve a certificate failure without exposing its raw diagnostics."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return "provider_endpoint_tls_failed"
        current = current.__cause__ or current.__context__
    return "provider_unreachable"


def _assemble_chat_stream(body: str) -> dict[str, Any]:
    """Assemble a bounded Qwen SSE response without exposing reasoning text.

    The safe transport enforces the existing response-byte and timeout limits.
    An interrupted stream must never be accepted as a complete recognition.
    """
    chunks: list[str] = []
    usage: dict[str, Any] = {}
    finish_reason = None
    done = False
    try:
        for event in re.split(r"\r?\n\r?\n", body):
            data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
            if not data:
                continue
            if data == "[DONE]":
                done = True
                break
            chunk = json.loads(data)
            if chunk.get("error"):
                raise ValueError("stream error")
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            for choice in chunk.get("choices", []):
                if choice.get("index", 0) != 0:
                    continue
                text = choice.get("delta", {}).get("content")
                if text is not None:
                    if not isinstance(text, str):
                        raise ValueError("invalid content")
                    chunks.append(text)
                finish_reason = choice.get("finish_reason") or finish_reason
        if not done or finish_reason is None:
            raise ValueError("incomplete stream")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ProviderRequestError("provider_response_invalid") from exc
    return {"choices": [{"message": {"content": "".join(chunks)}, "finish_reason": finish_reason}], "usage": usage}


class SafeRelayProvider(BaseProvider):
    """One owner-scoped custom endpoint using exactly one selected protocol."""

    can_encode_vision = True

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.provider_type = config.provider_type
        self.wire_protocol = effective_wire_protocol(
            config.provider_type,
            config.wire_protocol,
        )
        self._safe_sync_client: httpx.Client | None = None
        self._safe_async_client: httpx.AsyncClient | None = None
        self._target_url = provider_operation_url(
            effective_provider_base_url(config.provider_type, config.base_url),
            self.wire_protocol,
            model=config.model,
        )

    def _build_client_sync(self) -> Any:
        timeout_seconds = (
            float(settings.llm_timeout)
            if not settings.custom_provider_timeout_seconds
            else min(
                float(settings.llm_timeout),
                float(settings.custom_provider_timeout_seconds),
            )
        )
        sync_client, async_client = build_safe_provider_clients(
            effective_provider_base_url(
                self.config.provider_type,
                self.config.base_url,
            ),
            timeout_seconds=timeout_seconds,
            max_response_bytes=settings.custom_provider_max_response_bytes,
            allowed_target_url=self._target_url,
        )
        self._safe_sync_client = sync_client
        self._safe_async_client = async_client
        return async_client

    async def _relay_client(self) -> httpx.AsyncClient:
        self._ensure_async_primitives()
        if self._safe_async_client is None:
            async with self._client_lock:
                if self._safe_async_client is None:
                    await asyncio.to_thread(self._build_client_sync)
        assert self._safe_async_client is not None
        return self._safe_async_client

    def _request_parts(
        self,
        messages: List[BaseMessage],
        *, max_output_tokens: int | None = None,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        _validate_output_limit(max_output_tokens)
        headers = {"content-type": "application/json"}
        if self.wire_protocol == "openai_chat_completions":
            headers["authorization"] = f"Bearer {self.config.api_key}"
            payload = _openai_payload(messages, self.model)
            if self.provider_type == "qwen":
                payload.update(stream=True, stream_options={"include_usage": True})
        elif self.wire_protocol == "anthropic_messages":
            headers.update({
                "x-api-key": self.config.api_key,
                "anthropic-version": "2023-06-01",
            })
            payload = _anthropic_payload(messages, self.model)
        else:
            headers["x-goog-api-key"] = self.config.api_key
            payload = _gemini_payload(messages, self.model)
        if max_output_tokens is not None:
            if self.wire_protocol == "gemini_generate_content":
                payload["generationConfig"]["maxOutputTokens"] = max_output_tokens
            elif self.wire_protocol == "openai_chat_completions" and self.provider_type == "openai":
                payload["max_completion_tokens"] = max_output_tokens
            else:
                payload["max_tokens"] = max_output_tokens
        return headers, payload

    def _parse_response(self, payload: Any, duration_ms: float) -> LLMResponse:
        try:
            if self.wire_protocol == "openai_chat_completions":
                finish_reason = payload["choices"][0].get("finish_reason")
                if payload["choices"][0]["message"].get("refusal"):
                    finish_reason = "refusal"
                content = payload["choices"][0]["message"]["content"]
                if content is None and _safe_finish_reason(finish_reason) == "refused":
                    content = ""
                if isinstance(content, list):
                    content = "".join(
                        str(item.get("text", ""))
                        for item in content
                        if isinstance(item, dict)
                    )
                usage = payload.get("usage", {})
                input_tokens = usage.get("prompt_tokens")
                output_tokens = usage.get("completion_tokens")
            elif self.wire_protocol == "anthropic_messages":
                finish_reason = payload.get("stop_reason")
                content = "".join(
                    str(item.get("text", ""))
                    for item in payload["content"]
                    if isinstance(item, dict) and item.get("type") == "text"
                )
                usage = payload.get("usage", {})
                input_tokens = usage.get("input_tokens")
                output_tokens = usage.get("output_tokens")
            else:
                finish_reason = payload["candidates"][0].get("finishReason")
                candidate = payload["candidates"][0]
                # A blocked Gemini response legitimately omits content/parts.
                parts = (candidate.get("content") or {}).get("parts", []) if _safe_finish_reason(finish_reason) == "refused" else candidate["content"]["parts"]
                content = "".join(
                    str(item.get("text", ""))
                    for item in parts
                    if isinstance(item, dict) and not item.get("thought")
                )
                usage = payload.get("usageMetadata", {})
                input_tokens = usage.get("promptTokenCount")
                output_tokens = usage.get("candidatesTokenCount")
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderRequestError("provider_response_invalid") from exc
        if not isinstance(content, str):
            raise ProviderRequestError("provider_response_invalid")
        return LLMResponse(
            content=content,
            provider=self.provider_id,
            model=self.model,
            duration_ms=duration_ms,
            input_tokens=input_tokens if type(input_tokens) is int and input_tokens >= 0 else None,
            output_tokens=output_tokens if type(output_tokens) is int and output_tokens >= 0 else None,
            finish_reason=_safe_finish_reason(finish_reason),
            refusal_code=_refusal_code(finish_reason) if _safe_finish_reason(finish_reason) == "refused" else None,
        )

    async def _relay_call(self, messages: List[BaseMessage], *, max_output_tokens: int | None = None) -> LLMResponse:
        started = time.perf_counter()
        client = await self._relay_client()
        headers, payload = self._request_parts(messages, max_output_tokens=max_output_tokens)
        try:
            response = await client.post(
                self._target_url,
                headers=headers,
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise ProviderRequestError("provider_timeout") from exc
        except httpx.TransportError as exc:
            raise ProviderRequestError(_transport_error_code(exc)) from exc
        if response.status_code >= 400:
            code = _response_error_code(response.status_code)
            has_images = any(isinstance(m.content, list) and any(
                isinstance(block, dict) and block.get("type") == "image_url"
                for block in m.content) for m in messages)
            if has_images:
                try:
                    if explicitly_rejects_images(response.status_code, response.json()):
                        code = "provider_vision_not_supported"
                except ValueError:
                    pass
            if self.provider_type == "zhipu" and response.status_code == 429:
                try:
                    body = response.json()
                    detail = body.get("error", body) if isinstance(body, dict) else {}
                    if isinstance(detail, dict) and str(detail.get("code")) == "1305":
                        code = "provider_overloaded"
                except ValueError:
                    pass
            raise ProviderRequestError(
                code,
                status_code=response.status_code,
                retry_after=_parse_retry_after_header(
                    response.headers.get("retry-after")
                ),
            )
        # A 200 response means the endpoint answered — recovery signal for the
        # breaker, even if the body later turns out unparseable.
        self._endpoint_breaker().record_success()
        try:
            response_payload = (
                _assemble_chat_stream(response.text)
                if payload.get("stream") else response.json()
            )
        except ValueError as exc:
            raise ProviderRequestError("provider_response_invalid") from exc
        duration_ms = (time.perf_counter() - started) * 1000
        return self._parse_response(response_payload, duration_ms)

    async def ainvoke(self, messages: List[BaseMessage], *, max_output_tokens: int | None = None) -> LLMResponse:
        _validate_output_limit(max_output_tokens)
        self._ensure_async_primitives()
        # DNS validation/client construction must finish before minute quota
        # admission; a delayed cold start must not accumulate a send burst.
        await self._relay_client()
        await self._endpoint_breaker().before_call()
        async with self._call_capacity(messages):
            try:
                options = {} if max_output_tokens is None else {"max_output_tokens": max_output_tokens}
                response = await self._relay_call(messages, **options)
                self._record_duration(response.duration_ms)
                return response
            except Exception as e:
                if isinstance(e, ProviderRequestError) and e.code == "provider_overloaded":
                    self._record_concurrency_overload(e.retry_after)
                if _is_overload_failure(e):
                    self._endpoint_breaker().record_failure()
                raise

    async def ainvoke_vision(self, prompt: str, images: List[VisionImage], *, max_output_tokens: int | None = None) -> LLMResponse:
        return await super().ainvoke_vision(prompt, images, max_output_tokens=max_output_tokens)


# ─── Factory ─────────────────────────────────────────────────────────────────

PROVIDER_CLASSES: Dict[str, type[BaseProvider]] = {
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
    "zhipu": ZhipuProvider,
    "anthropic": AnthropicProvider,
    "deepseek": DeepSeekProvider,
    "moonshot": MoonshotProvider,
    "qwen": QwenProvider,
}


def build_provider(config: ProviderConfig) -> BaseProvider:
    protocol = effective_wire_protocol(config.provider_type, config.wire_protocol)
    normalized_config = config.model_copy(update={"wire_protocol": protocol})
    if is_user_defined_provider_endpoint(
        normalized_config.provider_type,
        normalized_config.base_url,
        protocol,
    ):
        if not settings.custom_provider_endpoints_available:
            raise ValueError("custom_provider_endpoints_disabled")
        return SafeRelayProvider(normalized_config)
    provider_cls = PROVIDER_CLASSES.get(config.provider_type)
    if provider_cls is None:
        raise ValueError(f"Unknown provider type: {config.provider_type}. Supported: {list(PROVIDER_CLASSES.keys())}")
    return provider_cls(normalized_config)
