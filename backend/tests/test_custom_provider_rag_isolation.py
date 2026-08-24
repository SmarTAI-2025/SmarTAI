from __future__ import annotations

import numpy as np
import pytest

from backend.config import settings
from backend.llm.registry import ExpertRegistry
from backend.models import ProviderConfig
from backend.rag.embedder import (
    BM25Embedder,
    OpenAICompatibleEmbedder,
    pick_embedder,
)


@pytest.mark.parametrize(
    ("provider_type", "wire_protocol"),
    [
        ("openai", "openai_chat_completions"),
        ("zhipu", "openai_chat_completions"),
        ("gemini", "openai_chat_completions"),
    ],
)
@pytest.mark.asyncio
async def test_every_custom_route_stays_on_bm25_and_never_calls_embeddings(
    monkeypatch,
    provider_type,
    wire_protocol,
):
    monkeypatch.setattr(settings, "runtime_environment", "development")
    registry = ExpertRegistry(seed_from_settings=False)
    registry.register(
        ProviderConfig(
            provider_type=provider_type,
            api_key="private-key",
            model="relay-model",
            base_url="https://relay.example.com/v1",
            endpoint_identity="https://relay.example.com/v1",
            wire_protocol=wire_protocol,
        ),
        provider_id="custom",
    )

    def forbidden_embeddings(*_args, **_kwargs):
        raise AssertionError("custom routes must never construct /embeddings clients")

    import langchain_openai

    monkeypatch.setattr(langchain_openai, "OpenAIEmbeddings", forbidden_embeddings)
    embedder = pick_embedder(registry)
    assert isinstance(embedder, BM25Embedder)
    vectors = await embedder.embed(["Newton force", "Energy"])
    scores = await embedder.score(
        "Newton",
        vectors,
        chunk_texts=["Newton force", "Energy"],
    )
    assert vectors.shape == (2, 1)
    assert isinstance(scores, np.ndarray)


def test_dense_embedder_rejects_a_custom_base_url_as_defense_in_depth():
    with pytest.raises(ValueError, match="embedding_endpoint_not_official"):
        OpenAICompatibleEmbedder(
            api_key="private-key",
            base_url="https://relay.example.com/v1",
            model="text-embedding-3-small",
            provider_type="openai",
        )


def test_official_openai_route_remains_eligible_for_dense_embedding(monkeypatch):
    monkeypatch.setattr(settings, "runtime_environment", "development")
    registry = ExpertRegistry(seed_from_settings=False)
    registry.register(
        ProviderConfig(
            provider_type="openai",
            api_key="private-key",
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            endpoint_identity="https://api.openai.com/v1",
            wire_protocol="openai_chat_completions",
        ),
        provider_id="official",
    )

    embedder = pick_embedder(registry)
    assert isinstance(embedder, OpenAICompatibleEmbedder)
    assert embedder.base_url == "https://api.openai.com/v1"


def test_selected_custom_record_is_not_confused_with_same_model_official_record(
    monkeypatch,
):
    monkeypatch.setattr(settings, "runtime_environment", "development")
    registry = ExpertRegistry(seed_from_settings=False)
    for provider_id, base_url in (
        ("official", "https://api.openai.com/v1"),
        ("relay", "https://relay.example.com/v1"),
    ):
        registry.register(
            ProviderConfig(
                provider_type="openai",
                api_key=f"{provider_id}-key",
                model="same-model",
                base_url=base_url,
                endpoint_identity=base_url,
                wire_protocol="openai_chat_completions",
            ),
            provider_id=provider_id,
        )

    relay_only = registry.select(["relay"], primary_provider_id="relay")

    assert relay_only.list_enabled_configs() == []
    assert isinstance(pick_embedder(relay_only), BM25Embedder)
