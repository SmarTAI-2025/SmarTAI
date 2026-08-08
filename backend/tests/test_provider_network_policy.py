from __future__ import annotations

import httpx
import pytest

from backend.api.experts import _verification_error_code
from backend.config import Settings, configure_provider_proxy_environment, settings
from backend.llm import providers as provider_module
from backend.llm.providers import (
    AnthropicProvider,
    GeminiProvider,
    OpenAIProvider,
    ZhipuProvider,
    build_provider,
)
from backend.models import ProviderConfig


def _provider_config(provider_type: str, model: str) -> ProviderConfig:
    return ProviderConfig(provider_type=provider_type, api_key="test-key", model=model)


def test_proxy_defaults_do_not_inherit_machine_http_proxy(monkeypatch):
    monkeypatch.delenv("SMARTAI_HTTP_PROXY", raising=False)
    monkeypatch.delenv("SMARTAI_HTTPS_PROXY", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://machine-proxy.invalid:9999")

    config = Settings(_env_file=None)

    assert config.http_proxy == ""
    assert config.https_proxy == ""


def test_proxy_environment_uses_only_explicit_smartai_values():
    environ = {
        "HTTP_PROXY": "http://old.invalid:1",
        "HTTPS_PROXY": "http://old.invalid:1",
        "ALL_PROXY": "socks5://old.invalid:1",
        "http_proxy": "http://old-lower.invalid:1",
        "KEEP_ME": "yes",
    }
    config = Settings(
        _env_file=None,
        http_proxy="http://127.0.0.1:7897",
        https_proxy="",
    )

    configure_provider_proxy_environment(config, environ=environ)

    assert environ["HTTP_PROXY"] == "http://127.0.0.1:7897"
    assert environ["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert "ALL_PROXY" not in environ
    assert "http_proxy" not in environ
    assert environ["KEEP_ME"] == "yes"


def test_empty_proxy_configuration_clears_all_proxy_spellings():
    environ = {
        "HTTP_PROXY": "http://old.invalid:1",
        "HTTPS_PROXY": "http://old.invalid:1",
        "ALL_PROXY": "socks5://old.invalid:1",
        "http_proxy": "http://old.invalid:1",
        "https_proxy": "http://old.invalid:1",
        "all_proxy": "socks5://old.invalid:1",
    }

    configure_provider_proxy_environment(
        Settings(_env_file=None, http_proxy="", https_proxy=""),
        environ=environ,
    )

    assert environ == {}


@pytest.mark.parametrize("proxy_url", [None, "http://127.0.0.1:7897"])
def test_httpx_clients_never_inherit_environment_proxy(monkeypatch, proxy_url):
    captured: dict[str, dict] = {}

    def fake_sync_client(**kwargs):
        captured["sync"] = kwargs
        return object()

    def fake_async_client(**kwargs):
        captured["async"] = kwargs
        return object()

    monkeypatch.setattr(httpx, "Client", fake_sync_client)
    monkeypatch.setattr(httpx, "AsyncClient", fake_async_client)

    provider_module._build_httpx_clients(proxy_url)

    assert captured["sync"]["trust_env"] is False
    assert captured["async"]["trust_env"] is False
    if proxy_url is None:
        assert "proxy" not in captured["sync"]
        assert "proxy" not in captured["async"]
    else:
        assert captured["sync"]["proxy"] == proxy_url
        assert captured["async"]["proxy"] == proxy_url


def test_openai_uses_explicit_smartai_proxy(monkeypatch):
    import langchain_openai

    captured: dict[str, object] = {}

    def fake_clients(proxy_url):
        captured["proxy_url"] = proxy_url
        return "sync-client", "async-client"

    def fake_chat_openai(**kwargs):
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(settings, "http_proxy", "")
    monkeypatch.setattr(settings, "https_proxy", "http://127.0.0.1:7897")
    monkeypatch.setattr(provider_module, "_build_httpx_clients", fake_clients)
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", fake_chat_openai)

    OpenAIProvider(_provider_config("openai", "gpt-4o"))._build_client_sync()

    assert captured["proxy_url"] == "http://127.0.0.1:7897"
    assert captured["kwargs"]["http_client"] == "sync-client"
    assert captured["kwargs"]["http_async_client"] == "async-client"


def test_zhipu_always_builds_a_direct_client(monkeypatch):
    import langchain_openai

    captured: dict[str, object] = {}

    def fake_clients(proxy_url):
        captured["proxy_url"] = proxy_url
        return "sync-client", "async-client"

    def fake_chat_openai(**kwargs):
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(settings, "http_proxy", "http://127.0.0.1:7897")
    monkeypatch.setattr(settings, "https_proxy", "http://127.0.0.1:7897")
    monkeypatch.setattr(provider_module, "_build_httpx_clients", fake_clients)
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", fake_chat_openai)

    ZhipuProvider(_provider_config("zhipu", "glm-4.6v-flash"))._build_client_sync()

    assert captured["proxy_url"] is None
    assert captured["kwargs"]["http_client"] == "sync-client"
    assert captured["kwargs"]["http_async_client"] == "async-client"


@pytest.mark.parametrize(
    ("provider_type", "model", "expected_base_url"),
    [
        ("deepseek", "deepseek-v4-flash", "https://api.deepseek.com/v1"),
        ("moonshot", "kimi-k3", "https://api.moonshot.cn/v1"),
        ("qwen", "qwen-plus", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ],
)
def test_domestic_providers_always_build_a_direct_client(
    monkeypatch, provider_type, model, expected_base_url
):
    """DeepSeek/Moonshot/Qwen ignore the foreign-proxy setting and connect
    directly, exactly like Zhipu — so domestic models keep working at the same
    time as VPN-routed OpenAI/Gemini/Anthropic. They also default to the
    vendor's official OpenAI-compatible endpoint when no base_url is supplied."""
    import langchain_openai

    captured: dict[str, object] = {}

    def fake_clients(proxy_url):
        captured["proxy_url"] = proxy_url
        return "sync-client", "async-client"

    def fake_chat_openai(**kwargs):
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(settings, "http_proxy", "http://127.0.0.1:7897")
    monkeypatch.setattr(settings, "https_proxy", "http://127.0.0.1:7897")
    monkeypatch.setattr(provider_module, "_build_httpx_clients", fake_clients)
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", fake_chat_openai)

    provider = build_provider(_provider_config(provider_type, model))
    provider._build_client_sync()

    assert captured["proxy_url"] is None
    assert captured["kwargs"]["base_url"] == expected_base_url


def test_gemini_and_anthropic_see_explicit_proxy(monkeypatch):
    import langchain_anthropic

    captured: dict[str, object] = {}

    def fake_chat_anthropic(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(settings, "http_proxy", "")
    monkeypatch.setattr(settings, "https_proxy", "http://127.0.0.1:7897")
    monkeypatch.setattr(langchain_anthropic, "ChatAnthropic", fake_chat_anthropic)

    gemini = GeminiProvider(_provider_config("gemini", "gemini-2.5-flash"))
    anthropic = AnthropicProvider(_provider_config("anthropic", "claude-sonnet-4"))

    assert gemini._needs_proxy_mode is True
    anthropic._build_client_sync()
    assert captured["anthropic_proxy"] == "http://127.0.0.1:7897"


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("glm-4v-flash", True),
        ("glm-4.1v-thinking-flash", True),
        ("glm-4.6v", True),
        ("glm-4.6v-flash", True),
        ("glm-4.5-air", False),
        ("glm-4-plus", False),
    ],
)
def test_zhipu_vision_support_is_model_aware(model, expected):
    provider = ZhipuProvider(_provider_config("zhipu", model))

    assert provider.supports_vision is expected


def test_openai_api_connection_error_has_safe_connection_code():
    from openai import APIConnectionError

    error = APIConnectionError(request=httpx.Request("POST", "https://example.test"))

    assert _verification_error_code(error) == "expert_verification_connection_failed"


def test_openai_api_timeout_has_timeout_code():
    from openai import APITimeoutError

    error = APITimeoutError(request=httpx.Request("POST", "https://example.test"))

    assert _verification_error_code(error) == "expert_verification_timeout"
