from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.llm.providers import BaseProvider, LLMResponse, VisionImage
from backend.llm.registry import ExpertRegistry
from backend.models import ProviderConfig
from backend.skills.ocr_ingest import LLMVisionOCRSkill, OCRImage


class CapturingVisionProvider(BaseProvider):
    provider_type = "openai"
    supports_vision = True

    def __init__(self):
        super().__init__(ProviderConfig(provider_type="openai", api_key="test", model="gpt-4o"))
        self.messages = None

    def _build_client_sync(self):
        raise AssertionError("ainvoke_vision test should not build a real client")

    async def ainvoke(self, messages):
        self.messages = messages
        return LLMResponse(
            content="ok",
            provider=self.provider_id,
            model=self.model,
            duration_ms=1.0,
            input_tokens=12,
            output_tokens=3,
        )


class TextOnlyProvider(BaseProvider):
    provider_type = "zhipu"
    supports_vision = False

    def __init__(self):
        super().__init__(ProviderConfig(provider_type="zhipu", api_key="test", model="glm-4.5-air"))

    def _build_client_sync(self):
        raise AssertionError("not used")


@pytest.mark.asyncio
async def test_base_provider_ainvoke_vision_builds_data_url_blocks():
    provider = CapturingVisionProvider()

    response = await provider.ainvoke_vision(
        "transcribe",
        [VisionImage(data=b"abc", media_type="image/png", filename="page.png")],
    )

    assert response.content == "ok"
    message = provider.messages[0]
    assert message.content[0] == {"type": "text", "text": "transcribe"}
    assert message.content[1]["type"] == "image_url"
    assert message.content[1]["image_url"]["url"] == "data:image/png;base64,YWJj"


@pytest.mark.asyncio
async def test_text_only_provider_rejects_vision():
    provider = TextOnlyProvider()

    with pytest.raises(NotImplementedError):
        await provider.ainvoke_vision(
            "transcribe",
            [VisionImage(data=b"abc", media_type="image/png", filename="page.png")],
        )


def test_registry_pick_vision_prefers_preferred_when_supported():
    registry = ExpertRegistry()
    registry._providers.clear()
    registry._configs.clear()

    vision = MagicMock()
    vision.provider_id = "openai:gpt-4o"
    vision.supports_vision = True
    text = MagicMock()
    text.provider_id = "zhipu:glm"
    text.supports_vision = False

    registry._providers[vision.provider_id] = vision
    registry._configs[vision.provider_id] = ProviderConfig(provider_type="openai", api_key="k", model="gpt-4o")
    registry._providers[text.provider_id] = text
    registry._configs[text.provider_id] = ProviderConfig(provider_type="zhipu", api_key="k", model="glm-4.5-air")

    assert registry.pick_vision(vision) is vision
    assert registry.pick_vision(text) is vision


@pytest.mark.asyncio
async def test_ocr_skill_preserves_provider_response_metadata():
    skill = LLMVisionOCRSkill(CapturingVisionProvider())

    result = await skill.recognize_images(
        [OCRImage(data=b"abc", media_type="image/png", label="page.png")],
        "problems",
    )

    assert result.text == "ok"
    assert result.provider == "openai:gpt-4o"
    assert result.model == "gpt-4o"
    assert result.duration_ms == 1.0
    assert result.input_tokens == 12
    assert result.output_tokens == 3
