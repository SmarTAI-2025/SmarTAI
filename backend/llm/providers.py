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
import logging
import random
import time
from abc import ABC, abstractmethod
from collections import deque
from typing import List, Optional, Dict, Any, Deque
from dataclasses import dataclass

from langchain_core.messages import BaseMessage

from backend.config import settings
from backend.models import ProviderConfig

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    content: str
    provider: str
    model: str
    duration_ms: float
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


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


class BaseProvider(ABC):
    """Abstract provider with async ainvoke interface."""

    provider_type: str = ""

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
                    self._client = self._build_client_sync()
        return self._client

    async def ainvoke(self, messages: List[BaseMessage]) -> LLMResponse:
        """Invoke the LLM. Default: native async. Gemini overrides this."""
        self._ensure_async_primitives()
        await self._rpm_limiter.acquire()
        async with self._semaphore:
            t0 = time.perf_counter()
            client = await self._get_client()
            try:
                response = await client.ainvoke(messages)
                content = response.content if hasattr(response, "content") else str(response)
                duration_ms = (time.perf_counter() - t0) * 1000
                logger.info(f"LLM call OK on {self.provider_id} in {duration_ms:.0f}ms ({len(content)} chars)")
                return LLMResponse(content=content, provider=self.provider_id, model=self.model, duration_ms=duration_ms)
            except Exception as e:
                duration_ms = (time.perf_counter() - t0) * 1000
                logger.warning(f"LLM call failed on {self.provider_id} after {duration_ms:.0f}ms: {e}")
                raise


# ─── Gemini ───────────────────────────────────────────────────────────────────

class GeminiProvider(BaseProvider):
    """
    Gemini provider with two modes:
      - Proxy mode (local): run_in_threadpool + per-call fresh client (parallel safe)
      - Direct mode (cloud): native ainvoke with shared client (faster)
    Auto-detected from settings.http_proxy.
    """
    provider_type = "gemini"

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
        return bool(settings.http_proxy)

    async def ainvoke(self, messages: List[BaseMessage]) -> LLMResponse:
        if not self._needs_proxy_mode:
            # Cloud mode: native async, shared client
            return await super().ainvoke(messages)

        # Local proxy mode: sync invoke in threadpool, fresh client per call
        self._ensure_async_primitives()
        await self._rpm_limiter.acquire()
        async with self._semaphore:
            t0 = time.perf_counter()
            try:
                from fastapi.concurrency import run_in_threadpool

                def _sync_call():
                    # Each thread gets its own client → no lock contention → true parallel
                    local_client = self._build_client_sync()
                    return local_client.invoke(messages)

                response = await run_in_threadpool(_sync_call)
                content = response.content if hasattr(response, "content") else str(response)
                duration_ms = (time.perf_counter() - t0) * 1000
                logger.info(f"LLM call OK on {self.provider_id} in {duration_ms:.0f}ms ({len(content)} chars)")
                return LLMResponse(content=content, provider=self.provider_id, model=self.model, duration_ms=duration_ms)
            except Exception as e:
                duration_ms = (time.perf_counter() - t0) * 1000
                logger.warning(f"LLM call failed on {self.provider_id} after {duration_ms:.0f}ms: {e}")
                raise


# ─── OpenAI / Zhipu / Anthropic ──────────────────────────────────────────────

class OpenAIProvider(BaseProvider):
    provider_type = "openai"

    def _build_client_sync(self) -> Any:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=self.model,
            temperature=0.0,
            timeout=settings.llm_timeout,
            max_retries=0,
            api_key=self.config.api_key,
            base_url=self.config.base_url or "https://api.openai.com/v1",
        )


class ZhipuProvider(BaseProvider):
    provider_type = "zhipu"

    def _build_client_sync(self) -> Any:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=self.model,
            temperature=0.0,
            timeout=settings.llm_timeout,
            max_retries=0,
            api_key=self.config.api_key,
            base_url=self.config.base_url or settings.zhipu_api_base,
        )


class AnthropicProvider(BaseProvider):
    provider_type = "anthropic"

    def _build_client_sync(self) -> Any:
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=self.model,
            temperature=0.0,
            timeout=settings.llm_timeout,
            max_retries=0,
            api_key=self.config.api_key,
        )


# ─── Factory ─────────────────────────────────────────────────────────────────

PROVIDER_CLASSES: Dict[str, type[BaseProvider]] = {
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
    "zhipu": ZhipuProvider,
    "anthropic": AnthropicProvider,
}


def build_provider(config: ProviderConfig) -> BaseProvider:
    provider_cls = PROVIDER_CLASSES.get(config.provider_type)
    if provider_cls is None:
        raise ValueError(f"Unknown provider type: {config.provider_type}. Supported: {list(PROVIDER_CLASSES.keys())}")
    return provider_cls(config)
