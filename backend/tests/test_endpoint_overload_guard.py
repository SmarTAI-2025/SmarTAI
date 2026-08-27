"""Tests for the shared-endpoint overload guard and 5xx gateway classification.

Covers the three mitigations for the campus-relay 503 storm:

  * B1 — relay 502/503/504 (and 429) are classified onto the long-wait
    rate-limit retry policy in ``structured_llm._classify_exception``, and the
    stable error-code strings map to question-level-retryable kinds in
    ``skills.base.classify_skill_error`` (previously a relay 503 surfaced as
    "provider_upstream_unavailable" — matching no keyword — and landed in the
    non-retryable "general" bucket);
  * B2 — ``_EndpointBreaker`` (per-endpoint, shared by every provider pointed
    at the same host) trips on an overload burst, freezes new calls for an
    escalating cooldown, and closes on the first success;
  * B3 — providers pointed at the same endpoint share one concurrency cap
    (``max_concurrent_llm_per_endpoint``) instead of multiplying it.

Run:
    python -m pytest backend/tests/test_endpoint_overload_guard.py -v
"""
from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

# Tests must not pick up the developer's proxy
os.environ["SMARTAI_HTTP_PROXY"] = ""
os.environ["SMARTAI_HTTPS_PROXY"] = ""

import httpx
import pytest

from backend.agents.grading_agent import _RETRYABLE_ERROR_KINDS
from backend.config import settings
from backend.llm import providers as llm_providers
from backend.llm.providers import (
    BaseProvider,
    ProviderRequestError,
    SafeRelayProvider,
    _EndpointBreaker,
    _is_overload_failure,
)
from backend.models import ProviderConfig
from backend.skills.base import classify_skill_error
from backend.tools.structured_llm import (
    PermanentLLMError,
    RateLimitError,
    TransientLLMError,
    _classify_exception,
)


@pytest.fixture(autouse=True)
def _clear_endpoint_state():
    """Endpoint guards are process-wide; isolate tests from each other."""
    llm_providers._ENDPOINT_BREAKERS.clear()
    llm_providers._ENDPOINT_SEMAPHORES.clear()
    yield
    llm_providers._ENDPOINT_BREAKERS.clear()
    llm_providers._ENDPOINT_SEMAPHORES.clear()


# ─── B1: retry-policy classification ─────────────────────────────────────────


class TestGatewayErrorClassification:
    def test_relay_503_uses_long_wait_rate_limit_policy(self):
        exc = ProviderRequestError("provider_upstream_unavailable", status_code=503)
        assert isinstance(_classify_exception(exc), RateLimitError)

    @pytest.mark.parametrize("status_code", [429, 502, 504])
    def test_relay_429_502_504_use_rate_limit_policy(self, status_code):
        exc = ProviderRequestError(
            "provider_upstream_unavailable", status_code=status_code
        )
        assert isinstance(_classify_exception(exc), RateLimitError)

    def test_relay_503_retry_after_hint_is_carried(self):
        exc = ProviderRequestError(
            "provider_upstream_unavailable", status_code=503, retry_after=45
        )
        classified = _classify_exception(exc)
        assert isinstance(classified, RateLimitError)
        assert classified.retry_after == 45

    def test_plain_500_stays_generic_transient(self):
        # 500 may be a durable app bug — keep the short 3-attempt policy.
        exc = ProviderRequestError("provider_upstream_unavailable", status_code=500)
        assert type(_classify_exception(exc)) is TransientLLMError

    @pytest.mark.parametrize("status_code", [401, 403, 404])
    def test_auth_and_not_found_are_permanent(self, status_code):
        exc = ProviderRequestError("provider_auth_failed", status_code=status_code)
        assert isinstance(_classify_exception(exc), PermanentLLMError)

    def test_textual_503_message_uses_rate_limit_policy(self):
        # httpx-style message without a structured status_code attribute.
        exc = RuntimeError("Server error '503 Service Unavailable' for url ...")
        assert isinstance(_classify_exception(exc), RateLimitError)

    def test_stable_code_string_without_status_is_transient(self):
        # The code string alone (no digits) is a network flake, not an
        # explicit rate-limit signal.
        exc = RuntimeError("provider_upstream_unavailable")
        assert type(_classify_exception(exc)) is TransientLLMError


class TestSkillLevelErrorKind:
    def test_relay_503_is_question_retryable(self):
        exc = ProviderRequestError("provider_upstream_unavailable", status_code=503)
        kind, _ = classify_skill_error(exc)
        assert kind == "transient_llm"
        assert kind in _RETRYABLE_ERROR_KINDS

    def test_relay_429_is_question_retryable(self):
        exc = ProviderRequestError("provider_rate_limited", status_code=429)
        kind, _ = classify_skill_error(exc)
        assert kind == "quota_exhausted"
        assert kind in _RETRYABLE_ERROR_KINDS

    def test_relay_timeout_stays_transient(self):
        kind, _ = classify_skill_error(ProviderRequestError("provider_timeout"))
        assert kind == "transient_llm"


# ─── B2: endpoint breaker ────────────────────────────────────────────────────

def _fast_breaker() -> _EndpointBreaker:
    return _EndpointBreaker(
        "https://relay.test",
        window=5.0,
        threshold=6,
        base_cooldown=0.2,
        max_cooldown=1.6,
    )


class TestEndpointBreaker:
    def test_stays_closed_below_threshold(self):
        breaker = _fast_breaker()
        for _ in range(5):
            breaker.record_failure()
        assert not breaker.is_open

    def test_opens_on_burst_and_freezes_new_calls(self):
        breaker = _fast_breaker()
        for _ in range(6):
            breaker.record_failure()
        assert breaker.is_open
        t0 = time.monotonic()
        asyncio.run(breaker.before_call())
        # Waits out the 0.2s cooldown (plus per-slice jitter) before returning.
        assert time.monotonic() - t0 >= 0.15

    def test_closes_on_first_success(self):
        breaker = _fast_breaker()
        for _ in range(5):
            breaker.record_failure()
        breaker.record_success()
        for _ in range(5):
            breaker.record_failure()
        assert not breaker.is_open

    def test_cooldown_escalates_on_repeated_trips(self):
        breaker = _fast_breaker()
        for _ in range(6):
            breaker.record_failure()
        first_cooldown = breaker._open_until - time.monotonic()
        assert first_cooldown > 0.1  # ~0.2s first trip
        time.sleep(0.45)  # let the first cooldown expire
        for _ in range(6):
            breaker.record_failure()
        second_cooldown = breaker._open_until - time.monotonic()
        assert second_cooldown > first_cooldown + 0.1  # ~0.4s second trip

    def test_window_slides_away_old_failures(self):
        breaker = _EndpointBreaker(
            "https://relay.test", window=0.3, threshold=6,
            base_cooldown=0.2, max_cooldown=0.8,
        )
        for _ in range(3):
            breaker.record_failure()
        time.sleep(0.45)
        for _ in range(3):
            breaker.record_failure()
        assert not breaker.is_open


class TestIsOverloadFailure:
    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (ProviderRequestError("provider_upstream_unavailable", status_code=503), True),
            (ProviderRequestError("provider_upstream_unavailable", status_code=500), True),
            (ProviderRequestError("provider_timeout"), True),
            (ProviderRequestError("provider_unreachable"), True),
            # Deterministic / per-key failures must not feed the breaker.
            (ProviderRequestError("provider_auth_failed", status_code=401), False),
            (ProviderRequestError("provider_rate_limited", status_code=429), False),
            (ProviderRequestError("provider_model_or_endpoint_not_found", status_code=404), False),
            (ProviderRequestError("provider_response_invalid"), False),
            (httpx.ConnectError("boom"), True),
            (httpx.ConnectTimeout("slow"), True),
            (RuntimeError("local bug"), False),
        ],
    )
    def test_classification(self, exc, expected):
        assert _is_overload_failure(exc) is expected


class _FakeRelayResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        if self.status_code < 400:
            # qwen defaults to the openai_chat_completions wire protocol.
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}
        raise ValueError("no body")


class _FakeRelayClient:
    def __init__(self, status_codes):
        self.status_codes = list(status_codes)
        self.calls = 0

    async def post(self, url, headers=None, json=None):
        self.calls += 1
        return _FakeRelayResponse(self.status_codes.pop(0))


class TestRelayBreakerIntegration:
    """A 503 storm through SafeRelayProvider.ainvoke trips the breaker."""

    @pytest.mark.asyncio
    async def test_503_storm_opens_breaker_and_freezes_next_call(self):
        provider = SafeRelayProvider(
            ProviderConfig(
                provider_type="qwen",
                api_key="test-key",
                model="test-model",
                base_url="https://relay.example.com",
                endpoint_identity="https://relay.example.com",
            )
        )
        # Pre-seed a fast breaker for this endpoint so the test does not
        # wait out the real 30s cooldown.
        fast = _EndpointBreaker(
            "https://relay.example.com",
            window=10.0, threshold=6, base_cooldown=0.1, max_cooldown=0.4,
        )
        llm_providers._ENDPOINT_BREAKERS[fast.endpoint] = fast
        client = _FakeRelayClient([503] * 8)

        async def _fake_relay_client():
            return client

        provider._relay_client = _fake_relay_client

        from langchain_core.messages import HumanMessage

        for _ in range(6):
            with pytest.raises(ProviderRequestError):
                await provider.ainvoke([HumanMessage(content="hi")])
        assert fast.is_open
        assert client.calls == 6

        # The next call must wait out the cooldown before even attempting.
        t0 = time.monotonic()
        with pytest.raises(ProviderRequestError):
            await provider.ainvoke([HumanMessage(content="hi")])
        assert time.monotonic() - t0 >= 0.05
        assert client.calls == 7

    @pytest.mark.asyncio
    async def test_success_after_storm_closes_breaker(self):
        provider = SafeRelayProvider(
            ProviderConfig(
                provider_type="qwen",
                api_key="test-key",
                model="test-model",
                base_url="https://relay.example.com",
                endpoint_identity="https://relay.example.com",
            )
        )
        fast = _EndpointBreaker(
            "https://relay.example.com",
            window=10.0, threshold=6, base_cooldown=0.1, max_cooldown=0.4,
        )
        llm_providers._ENDPOINT_BREAKERS[fast.endpoint] = fast
        # 5 failures, one success, 5 failures: must NOT open (success reset).
        client = _FakeRelayClient(
            [503, 503, 503, 503, 503, 200, 503, 503, 503, 503, 503]
        )

        async def _fake_relay_client():
            return client

        provider._relay_client = _fake_relay_client

        from langchain_core.messages import HumanMessage

        for _ in range(5):
            with pytest.raises(ProviderRequestError):
                await provider.ainvoke([HumanMessage(content="hi")])
        assert not fast.is_open  # 5 failures < threshold
        response = await provider.ainvoke([HumanMessage(content="hi")])
        assert response.content == "ok"
        for _ in range(5):
            with pytest.raises(ProviderRequestError):
                await provider.ainvoke([HumanMessage(content="hi")])
        assert not fast.is_open  # success reset the window


# ─── B3: endpoint-shared semaphore ───────────────────────────────────────────

class _CountingClient:
    """Fake langchain client that records peak in-flight calls."""

    def __init__(self, tracker: dict, hold: float = 0.05):
        self.tracker = tracker
        self.hold = hold

    async def ainvoke(self, messages):
        self.tracker["in_flight"] += 1
        self.tracker["max_in_flight"] = max(
            self.tracker["max_in_flight"], self.tracker["in_flight"]
        )
        try:
            await asyncio.sleep(self.hold)
        finally:
            self.tracker["in_flight"] -= 1
        return SimpleNamespace(content="ok")


class _DirectProvider(BaseProvider):
    """Concrete provider for tests: pre-seeded fake client, no network."""

    provider_type = "qwen"

    def __init__(self, config, client):
        super().__init__(config)
        self._client = client

    def _build_client_sync(self):
        # Never reached in tests: _client is pre-seeded, so _get_client
        # short-circuits before building.
        return self._client


class TestEndpointSharedSemaphore:
    @pytest.mark.asyncio
    async def test_same_endpoint_shares_one_concurrency_cap(self, monkeypatch):
        monkeypatch.setattr(settings, "max_concurrent_llm_per_endpoint", 2)
        tracker = {"in_flight": 0, "max_in_flight": 0}
        p1 = _DirectProvider(
            ProviderConfig(provider_type="qwen", api_key="k1", model="m1"),
            _CountingClient(tracker),
        )
        p2 = _DirectProvider(
            ProviderConfig(provider_type="qwen", api_key="k2", model="m2"),
            _CountingClient(tracker),
        )
        # Both point at the official qwen endpoint (base_url None) → same
        # endpoint key. Per-provider caps are 5 each, so without the shared
        # cap all 4 concurrent calls could run at once.
        await asyncio.gather(
            p1.ainvoke([]), p2.ainvoke([]), p1.ainvoke([]), p2.ainvoke([])
        )
        assert tracker["max_in_flight"] == 2

    @pytest.mark.asyncio
    async def test_different_endpoints_do_not_share_cap(self, monkeypatch):
        monkeypatch.setattr(settings, "max_concurrent_llm_per_endpoint", 2)
        tracker = {"in_flight": 0, "max_in_flight": 0}
        providers = [
            _DirectProvider(
                ProviderConfig(provider_type="qwen", api_key="k", model="m"),
                _CountingClient(tracker),
            ),
            _DirectProvider(
                ProviderConfig(provider_type="deepseek", api_key="k", model="m"),
                _CountingClient(tracker),
            ),
        ]
        # Distinct endpoint keys ("qwen:default" vs "deepseek:default") →
        # 2 in flight per endpoint, 4 overall.
        await asyncio.gather(
            providers[0].ainvoke([]), providers[1].ainvoke([]),
            providers[0].ainvoke([]), providers[1].ainvoke([]),
        )
        assert tracker["max_in_flight"] == 4
