"""Text probes require real output; overlapping checks keep the latest start."""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.api import experts
from backend.config import settings
from backend.db.provider_repository import get_provider_config
from backend.llm.providers import LLMResponse, ProviderRequestError
from backend.llm.registry import _build_scoped_registry
from backend.tests.test_shared_model_presets import presets, own, OWNER, LUNA, FakeProvider
from backend.tests.test_shared_weekly_budget import metered, client, rows


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,code", [
    ({"text":""}, "empty_response"),
    ({"text":" \n "}, "empty_response"),
    ({"text":"", "status":"incomplete", "reason":"max_output_tokens"}, "response_truncated"),
    ({"text":"partial", "status":"incomplete", "reason":"max_output_tokens"}, "response_truncated"),
    ({"text":"", "output":[SimpleNamespace(content=[SimpleNamespace(type="refusal")])]}, "response_refused"),
])
async def test_weekly_probe_rejects_unusable_output_but_settles_real_usage(metered, monkeypatch, payload, code):
    provider, calls, state = metered
    state.update(payload)
    monkeypatch.setattr(settings, "openai_api_key", provider.config.api_key)
    monkeypatch.setattr(settings, "custom_provider_verification_cooldown_seconds", 0)
    provider.config.base_url = settings.openai_api_base
    with pytest.raises(HTTPException) as error:
        await experts.verify_provider(LUNA, OWNER, SimpleNamespace(get=lambda _:provider))
    expected = "expert_verification_" + code
    assert error.value.detail["code"] == expected
    current = next(c for c in _build_scoped_registry(OWNER).list_configs() if c["provider_id"] == LUNA)
    assert current["verification_status"] == "failed" and current["verification_error_code"] == expected
    assert len(calls) == 2 and len(rows()) == 1 and rows()[0].state == "settled"


@pytest.mark.asyncio
@pytest.mark.parametrize("text,reason", [("", None), ("partial", "length"), ("refusal", "refused")])
async def test_byok_probe_uses_the_same_output_checks(presets, monkeypatch, text, reason):
    personal = own()
    async def respond(self, messages, **kwargs):
        return LLMResponse(text, self.provider_id, self.model, 1, finish_reason=reason)
    monkeypatch.setattr(FakeProvider, "ainvoke", respond)
    with pytest.raises(HTTPException):
        await experts.verify_provider(personal.id, OWNER, _build_scoped_registry(OWNER))
    saved = get_provider_config(OWNER.id, personal.id, master_key=settings.provider_encryption_key)
    assert saved.verification_status == "failed" and saved.config.enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
@pytest.mark.parametrize("newer_fails", [False, True])
async def test_latest_started_probe_wins_after_default_cooldown(presets, monkeypatch, order, newer_fails):
    now = [100.0]
    monkeypatch.setattr(settings, "shared_pool_quota_mode", "daily_estimated")
    monkeypatch.setattr(settings, "custom_provider_verification_cooldown_seconds", 5)
    monkeypatch.setattr(experts, "_custom_probe_last_at", {})
    monkeypatch.setattr(experts, "time", SimpleNamespace(monotonic=lambda:now[0], time=time.time))
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    calls = []
    async def respond(self, messages, **kwargs):
        index = len(calls); calls.append(index); entered[index].set()
        await release[index].wait()
        if (index == 1) == newer_fails:
            raise ProviderRequestError("provider_auth_failed", status_code=401)
        return LLMResponse("OK", self.provider_id, self.model, 1)
    monkeypatch.setattr(FakeProvider, "ainvoke", respond)
    tasks = [asyncio.create_task(experts.verify_provider(LUNA, OWNER, _build_scoped_registry(OWNER)))]
    try:
        await asyncio.wait_for(entered[0].wait(), 2)
        now[0] += 6
        tasks.append(asyncio.create_task(experts.verify_provider(LUNA, OWNER, _build_scoped_registry(OWNER))))
        await asyncio.wait_for(entered[1].wait(), 2)
        results = [None, None]
        for index in order:
            release[index].set()
            results[index] = (await asyncio.gather(tasks[index], return_exceptions=True))[0]
        assert isinstance(results[0], HTTPException) and results[0].detail["code"] == "expert_verification_superseded"
        if newer_fails:
            assert isinstance(results[1], HTTPException) and results[1].detail["code"] == "expert_verification_auth_failed"
        else:
            assert results[1]["verification_status"] == "verified"
        current = next(c for c in _build_scoped_registry(OWNER).list_configs() if c["provider_id"] == LUNA)
        assert current["verification_status"] == ("failed" if newer_fails else "verified")
    finally:
        for event in release: event.set()
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_simultaneous_starts_have_unique_versions_with_a_stalled_clock(presets, monkeypatch):
    from backend.db import shared_image_repository as evidence
    monkeypatch.setattr(evidence, "time", SimpleNamespace(time=lambda:123.0))
    config = presets.get(LUNA).config
    def start(_):
        return evidence.shared_image_evidence(OWNER.id, config, create=True, text_probe=True, start_probe=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        snapshots = list(pool.map(start, range(8)))
    assert len({snapshot.updated_at for snapshot in snapshots}) == 8
    latest = max(snapshots, key=lambda s:s.updated_at)
    for snapshot in snapshots:
        assert evidence.set_shared_image_evidence(OWNER.id, snapshot, status="verified", checked_at=124,
            reason=None, text_probe=True) is (snapshot is latest)
