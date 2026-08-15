from __future__ import annotations

from backend.config import settings
from backend.llm.registry import ExpertRegistry


def test_seed_from_settings_registers_domestic_providers(monkeypatch):
    """DeepSeek/Moonshot/Qwen must env-seed into the shared pool when their API
    keys are set. This is the path that makes domestic models usable on
    deployments that rely on the platform/shared pool instead of per-user BYOK
    — it was the missing piece behind "DeepSeek 还不能用" after the BYOK-only
    provider classes landed."""
    monkeypatch.setattr(settings, "shared_pool_enabled", True)
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-deepseek")
    monkeypatch.setattr(settings, "deepseek_api_base", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr(settings, "moonshot_api_key", "sk-moonshot")
    monkeypatch.setattr(settings, "moonshot_api_base", "https://api.moonshot.cn/v1")
    monkeypatch.setattr(settings, "moonshot_model", "kimi-k3")
    monkeypatch.setattr(settings, "qwen_api_key", "sk-qwen")
    monkeypatch.setattr(settings, "qwen_api_base", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setattr(settings, "qwen_model", "qwen-plus")
    # Don't seed the foreign providers in this check.
    for foreign in ("gemini_api_key", "openai_api_key", "zhipu_api_key", "anthropic_api_key"):
        monkeypatch.setattr(settings, foreign, "")

    registry = ExpertRegistry(seed_from_settings=True)

    configs = {c["provider_type"]: c for c in registry.list_configs()}
    assert {"deepseek", "moonshot", "qwen"} <= set(configs)
    assert configs["deepseek"]["model"] == "deepseek-v4-flash"
    assert (
        configs["qwen"]["base_url"]
        == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
