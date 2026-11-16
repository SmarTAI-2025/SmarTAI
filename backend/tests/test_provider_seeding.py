from __future__ import annotations

from backend.config import settings
from backend.llm.registry import ExpertRegistry


def test_demo_does_not_seed_other_providers_when_gemini_is_missing(monkeypatch):
    """Configured domestic keys cannot become a fallback for Gemini-only Demo."""
    monkeypatch.setattr(settings, "shared_pool_enabled", True)
    monkeypatch.setattr(settings, "frontier_demo_enabled", True)
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-deepseek")
    monkeypatch.setattr(settings, "deepseek_api_base", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr(settings, "moonshot_api_key", "sk-moonshot")
    monkeypatch.setattr(settings, "moonshot_api_base", "https://api.moonshot.cn/v1")
    monkeypatch.setattr(settings, "moonshot_model", "kimi-k3")
    monkeypatch.setattr(settings, "qwen_api_key", "sk-qwen")
    monkeypatch.setattr(settings, "qwen_api_base", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setattr(settings, "qwen_model", "qwen-plus")
    # No Gemini is configured; other server credentials must stay unused.
    for foreign in ("gemini_api_key", "openai_api_key", "zhipu_api_key", "anthropic_api_key"):
        monkeypatch.setattr(settings, foreign, "")

    registry = ExpertRegistry(seed_from_settings=True, frontier_demo=True)

    assert registry.list_configs() == []
    assert registry.pick_default() is None
