"""
LLM provider adapters with async interface.

Each provider implements: `ainvoke(messages) -> LLMResponse`.

IMPORTANT — Gemini proxy issue:
  langchain-google-genai's ainvoke() uses gRPC async client internally,
  which ignores HTTP_PROXY. The sync invoke() correctly uses REST transport
  with proxy. Therefore when a proxy is configured (local dev behind GFW),
  GeminiProvider uses run_in_threadpool with a per-call client factory for
  parallel safety. When no proxy (cloud deployment), it uses native ainvoke.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import random
import re
import ssl
import time
from abc import ABC, abstractmethod
from collections import deque
from typing import List, Optional, Dict, Any, Deque
from dataclasses import dataclass

import httpx
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from backend.config import settings
from backend.llm.endpoint_policy import (
    build_safe_provider_clients,
    effective_provider_base_url,
    is_user_defined_provider_endpoint,
    provider_operation_url,
)
from backend.llm.provider_catalog import effective_wire_protocol
from backend.models import ProviderConfig

logger = logging.getLogger(__name__)


_ZHIPU_VISION_MODEL_PATTERN = re.compile(
    r"^glm-\d+(?:\.\d+)?v(?:-|$)", re.IGNORECASE
)


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


class _RPMLimiter:
    """Sliding-window per-minute rate limiter (token bucket flavor).

    Tracks timestamps of the last `rpm` successful starts. If a new acquire
    would exceed the cap inside the trailing 60s window, sleeps until the
    oldest timestamp falls out of the window.

    Thread-of-asyncio safety: a single asyncio.Lock serializes the window
    bookkeeping; the sleep itself is awaited *while holding the lock* so that
    concurrent waiters queue cleanly (each one re-checks the window after its
    sleep). This makes the limiter strictly FIFO and prevents thundering-herd
    on quota reset.

    A small randomized jitter (50-250ms) is added on top of the computed wait
    so multiple workers that wake at the same instant don't slam the API in a
    synchronized burst.
    """

    def __init__(self, rpm: int, provider_id: str):
        self.rpm = max(0, int(rpm))
        self.provider_id = provider_id
        self._window: Deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.rpm <= 0:
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                cutoff = now - 60.0
                while self._window and self._window[0] < cutoff:
                    self._window.popleft()
                if len(self._window) < self.rpm:
                    self._window.append(now)
                    return
                # Window full — wait until the oldest call falls out.
                wait = self._window[0] + 60.0 - now + random.uniform(0.05, 0.25)
                logger.info(
                    f"RPM limiter [{self.provider_id}] full ({len(self._window)}/"
                    f"{self.rpm} in last 60s) — sleeping {wait:.2f}s before next call"
                )
                await asyncio.sleep(wait)


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
_ENDPOINT_SEMAPHORES: Dict[str, tuple[asyncio.Semaphore, int]] = {}


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
    return seconds if seconds > 0 else None


class BaseProvider(ABC):
    """Abstract provider with async ainvoke interface."""

    provider_type: str = ""
    supports_vision: bool = False

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.model = config.model
        self._semaphore = None
        self._client = None
        self._client_lock = None
        self._rpm_limiter: Optional[_RPMLimiter] = None

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

    def _endpoint_semaphore(self) -> asyncio.Semaphore:
        """Shared concurrency cap for every provider on this endpoint.

        Kept separate from the per-provider semaphore: N configs pointing at
        one relay must not multiply its in-flight calls N-fold.
        """
        key = endpoint_key(self.config)
        limit = max(1, int(settings.max_concurrent_llm_per_endpoint))
        entry = _ENDPOINT_SEMAPHORES.get(key)
        if entry is None or entry[1] != limit:
            entry = (asyncio.Semaphore(limit), limit)
            _ENDPOINT_SEMAPHORES[key] = entry
        return entry[0]

    @abstractmethod
    def _build_client_sync(self) -> Any:
        """Build a LangChain client. Must be callable from any thread."""
        ...

    def _ensure_async_primitives(self) -> None:
        if self._semaphore is None:
            limit = max(1, getattr(self.config, "max_concurrent", None) or settings.max_concurrent_llm_per_provider)
            self._semaphore = asyncio.Semaphore(limit)
            logger.debug(f"Provider {self.provider_id} concurrency capped at {limit}")
        if self._client_lock is None:
            self._client_lock = asyncio.Lock()
        if self._rpm_limiter is None:
            rpm = max(0, int(getattr(self.config, "rpm", 0) or 0))
            self._rpm_limiter = _RPMLimiter(rpm, self.provider_id)
            if rpm > 0:
                logger.debug(f"Provider {self.provider_id} RPM capped at {rpm}/min")

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

    async def ainvoke(self, messages: List[BaseMessage]) -> LLMResponse:
        """Invoke the LLM. Default: native async. Gemini overrides this."""
        self._ensure_async_primitives()
        # Breaker before RPM limiter: a call frozen by the cooldown must not
        # burn this key's per-minute window while doing nothing.
        await self._endpoint_breaker().before_call()
        await self._rpm_limiter.acquire()
        async with self._endpoint_semaphore(), self._semaphore:
            t0 = time.perf_counter()
            client = await self._get_client()
            try:
                response = await client.ainvoke(messages)
                self._endpoint_breaker().record_success()
                content = response.content if hasattr(response, "content") else str(response)
                duration_ms = (time.perf_counter() - t0) * 1000
                logger.info(f"LLM call OK on {self.provider_id} in {duration_ms:.0f}ms ({len(content)} chars)")
                return LLMResponse(content=content, provider=self.provider_id, model=self.model, duration_ms=duration_ms)
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

    async def ainvoke_vision(self, prompt: str, images: List[VisionImage]) -> LLMResponse:
        """Invoke a vision-capable model with text prompt plus one or more images."""
        if not self.supports_vision:
            raise NotImplementedError(f"{self.provider_id} does not support vision input.")
        if not images:
            raise ValueError("ainvoke_vision requires at least one image.")
        return await self.ainvoke(_build_vision_messages(prompt, images))


# ─── Gemini ───────────────────────────────────────────────────────────────────

class GeminiProvider(BaseProvider):
    """
    Gemini provider with two modes:
      - Proxy mode (local): run_in_threadpool + per-call fresh client (parallel safe)
      - Direct mode (cloud): native ainvoke with shared client (faster)
    Auto-detected from SmarTAI's explicit proxy settings.
    """
    provider_type = "gemini"
    supports_vision = True

    def _build_client_sync(self) -> Any:
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=self.model,
            temperature=0.0,
            transport="rest",
            timeout=settings.llm_timeout,
            max_retries=0,
            google_api_key=self.config.api_key,
        )

    @property
    def _needs_proxy_mode(self) -> bool:
        return _configured_proxy_url() is not None

    async def ainvoke(self, messages: List[BaseMessage]) -> LLMResponse:
        if not self._needs_proxy_mode:
            # Cloud mode: native async, shared client
            return await super().ainvoke(messages)

        # Local proxy mode: sync invoke in threadpool, fresh client per call
        self._ensure_async_primitives()
        await self._endpoint_breaker().before_call()
        await self._rpm_limiter.acquire()
        async with self._endpoint_semaphore(), self._semaphore:
            t0 = time.perf_counter()
            try:
                from fastapi.concurrency import run_in_threadpool

                def _sync_call():
                    # Each thread gets its own client → no lock contention → true parallel
                    local_client = self._build_client_sync()
                    return local_client.invoke(messages)

                response = await run_in_threadpool(_sync_call)
                self._endpoint_breaker().record_success()
                content = response.content if hasattr(response, "content") else str(response)
                duration_ms = (time.perf_counter() - t0) * 1000
                logger.info(f"LLM call OK on {self.provider_id} in {duration_ms:.0f}ms ({len(content)} chars)")
                return LLMResponse(content=content, provider=self.provider_id, model=self.model, duration_ms=duration_ms)
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
    supports_vision = True

    def _build_client_sync(self) -> Any:
        from langchain_openai import ChatOpenAI
        http_client, http_async_client = _build_provider_httpx_clients(
            self.config,
            provider_type=self.provider_type,
            official_proxy_url=_configured_proxy_url(),
        )

        return ChatOpenAI(
            model=self.model,
            temperature=0.0,
            timeout=settings.llm_timeout,
            max_retries=0,
            api_key=self.config.api_key,
            base_url=self.config.base_url or "https://api.openai.com/v1",
            http_client=http_client,
            http_async_client=http_async_client,
        )


class ZhipuProvider(BaseProvider):
    provider_type = "zhipu"

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.supports_vision = bool(
            _ZHIPU_VISION_MODEL_PATTERN.match(self.model.strip())
        )

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
            temperature=0.0,
            timeout=settings.llm_timeout,
            max_retries=0,
            api_key=self.config.api_key,
            base_url=self.config.base_url or settings.zhipu_api_base,
            http_client=http_client,
            http_async_client=http_async_client,
        )


class AnthropicProvider(BaseProvider):
    provider_type = "anthropic"
    supports_vision = True

    def _build_client_sync(self) -> Any:
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=self.model,
            temperature=0.0,
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
# Text models stay supports_vision=False — never advertise a domestic text model
# as OCR (launch plan 上线前 08); vision GLM is handled by ZhipuProvider above.


class _DomesticOpenAICompatibleProvider(BaseProvider):
    """OpenAI-compatible domestic provider that always connects directly."""

    _default_base_url: str = ""

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.supports_vision = False

    def _build_client_sync(self) -> Any:
        from langchain_openai import ChatOpenAI
        http_client, http_async_client = _build_provider_httpx_clients(
            self.config,
            provider_type=self.provider_type,
            official_proxy_url=None,
        )
        return ChatOpenAI(
            model=self.model,
            temperature=0.0,
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
    return {"model": model, "messages": output, "temperature": 0}


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
        "temperature": 0,
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
        "generationConfig": {"temperature": 0},
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
        # Preserve today's routing behavior. PR-C will let users explicitly
        # choose a stage model and then rely on the real provider response.
        self.supports_vision = (
            config.provider_type in {"openai", "gemini", "anthropic"}
            or (
                config.provider_type == "zhipu"
                and bool(_ZHIPU_VISION_MODEL_PATTERN.match(config.model.strip()))
            )
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
    ) -> tuple[dict[str, str], dict[str, Any]]:
        headers = {"content-type": "application/json"}
        if self.wire_protocol == "openai_chat_completions":
            headers["authorization"] = f"Bearer {self.config.api_key}"
            payload = _openai_payload(messages, self.model)
        elif self.wire_protocol == "anthropic_messages":
            headers.update({
                "x-api-key": self.config.api_key,
                "anthropic-version": "2023-06-01",
            })
            payload = _anthropic_payload(messages, self.model)
        else:
            headers["x-goog-api-key"] = self.config.api_key
            payload = _gemini_payload(messages, self.model)
        return headers, payload

    def _parse_response(self, payload: Any, duration_ms: float) -> LLMResponse:
        try:
            if self.wire_protocol == "openai_chat_completions":
                content = payload["choices"][0]["message"]["content"]
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
                content = "".join(
                    str(item.get("text", ""))
                    for item in payload["content"]
                    if isinstance(item, dict) and item.get("type") == "text"
                )
                usage = payload.get("usage", {})
                input_tokens = usage.get("input_tokens")
                output_tokens = usage.get("output_tokens")
            else:
                parts = payload["candidates"][0]["content"]["parts"]
                content = "".join(
                    str(item.get("text", ""))
                    for item in parts
                    if isinstance(item, dict)
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
            input_tokens=input_tokens if isinstance(input_tokens, int) else None,
            output_tokens=output_tokens if isinstance(output_tokens, int) else None,
        )

    async def _relay_call(self, messages: List[BaseMessage]) -> LLMResponse:
        started = time.perf_counter()
        client = await self._relay_client()
        headers, payload = self._request_parts(messages)
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
            raise ProviderRequestError(
                _response_error_code(response.status_code),
                status_code=response.status_code,
                retry_after=_parse_retry_after_header(
                    response.headers.get("retry-after")
                ),
            )
        # A 200 response means the endpoint answered — recovery signal for the
        # breaker, even if the body later turns out unparseable.
        self._endpoint_breaker().record_success()
        try:
            response_payload = response.json()
        except ValueError as exc:
            raise ProviderRequestError("provider_response_invalid") from exc
        duration_ms = (time.perf_counter() - started) * 1000
        return self._parse_response(response_payload, duration_ms)

    async def ainvoke(self, messages: List[BaseMessage]) -> LLMResponse:
        self._ensure_async_primitives()
        await self._endpoint_breaker().before_call()
        await self._rpm_limiter.acquire()
        async with self._endpoint_semaphore(), self._semaphore:
            try:
                return await self._relay_call(messages)
            except Exception as e:
                if _is_overload_failure(e):
                    self._endpoint_breaker().record_failure()
                raise

    async def ainvoke_vision(
        self,
        prompt: str,
        images: List[VisionImage],
    ) -> LLMResponse:
        if not images:
            raise ValueError("ainvoke_vision requires at least one image.")
        return await self.ainvoke(_build_vision_messages(prompt, images))


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
