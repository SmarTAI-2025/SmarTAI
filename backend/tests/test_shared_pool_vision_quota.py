from __future__ import annotations

import pytest

from backend.config import settings
from backend.llm import registry as registry_module
from backend.llm.providers import LLMResponse, VisionImage
from backend.llm.registry import SharedPoolLimitError, _GuardedSharedProvider


class _RecordingVisionProvider:
    provider_id = "fake:vision"
    provider_type = "fake"
    model = "vision"
    config = object()

    def __init__(self) -> None:
        self.vision_calls: list[tuple[str, list[VisionImage]]] = []

    async def ainvoke_vision(
        self,
        prompt: str,
        images: list[VisionImage],
    ) -> LLMResponse:
        self.vision_calls.append((prompt, images))
        return LLMResponse(
            content="recognized",
            provider=self.provider_id,
            model=self.model,
            duration_ms=1.0,
        )


@pytest.fixture(autouse=True)
def _isolated_shared_pool(monkeypatch):
    monkeypatch.setattr(
        registry_module,
        "_shared_pool_usage",
        registry_module._SharedPoolUsageLimiter(),
    )
    monkeypatch.setattr(settings, "shared_pool_enabled", True)
    monkeypatch.setattr(settings, "shared_pool_daily_request_limit", 1)
    monkeypatch.setattr(settings, "shared_pool_daily_estimated_token_limit", 100_000)


@pytest.mark.asyncio
async def test_guarded_shared_provider_charges_and_forwards_vision_call():
    provider = _RecordingVisionProvider()
    guarded = _GuardedSharedProvider(provider, "owner-vision")
    images = [VisionImage(data=b"image", media_type="image/png", filename="page.png")]

    response = await guarded.ainvoke_vision("Read the page", images)

    assert response.content == "recognized"
    assert provider.vision_calls == [("Read the page", images)]

    with pytest.raises(SharedPoolLimitError, match="shared_pool_daily_limit_reached"):
        await guarded.ainvoke_vision("Read it again", images)

    assert provider.vision_calls == [("Read the page", images)]


@pytest.mark.asyncio
async def test_guarded_shared_provider_kill_switch_blocks_vision_call(monkeypatch):
    monkeypatch.setattr(settings, "shared_pool_enabled", False)
    provider = _RecordingVisionProvider()
    guarded = _GuardedSharedProvider(provider, "owner-disabled")
    images = [VisionImage(data=b"image", media_type="image/png")]

    with pytest.raises(SharedPoolLimitError, match="shared_pool_disabled"):
        await guarded.ainvoke_vision("Read the page", images)

    assert provider.vision_calls == []


@pytest.mark.asyncio
async def test_guarded_shared_provider_applies_estimated_token_limit_to_vision(
    monkeypatch,
):
    monkeypatch.setattr(settings, "shared_pool_daily_request_limit", 10)
    monkeypatch.setattr(settings, "shared_pool_daily_estimated_token_limit", 1)
    provider = _RecordingVisionProvider()
    guarded = _GuardedSharedProvider(provider, "owner-token-limit")
    images = [VisionImage(data=b"image", media_type="image/png")]

    with pytest.raises(SharedPoolLimitError, match="shared_pool_daily_limit_reached"):
        await guarded.ainvoke_vision("Read the page", images)

    assert provider.vision_calls == []


@pytest.mark.asyncio
async def test_guarded_shared_provider_does_not_count_image_base64_as_text(monkeypatch):
    monkeypatch.setattr(settings, "shared_pool_daily_request_limit", 10)
    monkeypatch.setattr(settings, "shared_pool_daily_estimated_token_limit", 10_000)
    provider = _RecordingVisionProvider()
    guarded = _GuardedSharedProvider(provider, "owner-page-image")
    images = [VisionImage(data=b"x" * 450_000, media_type="image/png")]

    response = await guarded.ainvoke_vision("Read the handwritten page", images)

    assert response.content == "recognized"
    assert provider.vision_calls == [("Read the handwritten page", images)]
