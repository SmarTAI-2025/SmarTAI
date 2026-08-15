from __future__ import annotations

import ssl

import httpx
import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from backend.api.experts import _verification_error_code
from backend.config import Settings, configure_provider_proxy_environment, settings
from backend.llm import providers as provider_module
from backend.llm.providers import (
    AnthropicProvider,
    GeminiProvider,
    OpenAIProvider,
    ProviderRequestError,
    SafeRelayProvider,
    VisionImage,
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


def test_deepseek_relay_uses_only_the_pinned_safe_clients(monkeypatch):
    captured: dict[str, object] = {}

    def fake_safe_clients(base_url, **kwargs):
        captured["base_url"] = base_url
        captured["client_kwargs"] = kwargs
        return "safe-sync", "safe-async"

    monkeypatch.setattr(settings, "runtime_environment", "development")
    monkeypatch.setattr(provider_module, "build_safe_provider_clients", fake_safe_clients)
    monkeypatch.setattr(
        provider_module,
        "_build_httpx_clients",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("custom providers must not use the ordinary client")
        ),
    )
    provider = build_provider(ProviderConfig(
        provider_type="deepseek",
        api_key="test-key",
        model="relay-model",
        base_url="https://relay.example.com/v1",
        endpoint_identity="https://relay.example.com/v1",
    ))

    provider._build_client_sync()

    assert captured["base_url"] == "https://relay.example.com/v1"
    assert captured["client_kwargs"]["allowed_target_url"] == (
        "https://relay.example.com/v1/chat/completions"
    )
    assert provider._safe_sync_client == "safe-sync"
    assert provider._safe_async_client == "safe-async"
    assert provider.supports_vision is False
    assert provider.can_encode_vision is True


class _FakeRelayClient:
    def __init__(self, response_payload, status_code=200):
        self.response_payload = response_payload
        self.status_code = status_code
        self.calls = []

    async def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return httpx.Response(
            self.status_code,
            json=self.response_payload,
            request=httpx.Request("POST", url),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_type", "wire_protocol", "expected_path", "response_payload"),
    [
        (
            "deepseek",
            "openai_chat_completions",
            "/v1/chat/completions",
            {
                "choices": [{"message": {"content": "openai ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        ),
        (
            "anthropic",
            "anthropic_messages",
            "/v1/messages",
            {
                "content": [{"type": "text", "text": "anthropic ok"}],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        ),
        (
            "gemini",
            "gemini_generate_content",
            "/v1beta/models/test-model:generateContent",
            {
                "candidates": [{"content": {"parts": [{"text": "gemini ok"}]}}],
                "usageMetadata": {
                    "promptTokenCount": 3,
                    "candidatesTokenCount": 2,
                },
            },
        ),
    ],
)
async def test_each_relay_protocol_sends_text_and_image_once_without_key_in_url_or_body(
    monkeypatch,
    provider_type,
    wire_protocol,
    expected_path,
    response_payload,
):
    monkeypatch.setattr(settings, "runtime_environment", "development")
    provider = build_provider(ProviderConfig(
        provider_type=provider_type,
        api_key="test-key",
        model="test-model",
        base_url="https://relay.example.com/v1" if wire_protocol != "gemini_generate_content" else "https://relay.example.com",
        endpoint_identity="https://relay.example.com/v1" if wire_protocol != "gemini_generate_content" else "https://relay.example.com",
        wire_protocol=wire_protocol,
    ))
    assert isinstance(provider, SafeRelayProvider)
    fake = _FakeRelayClient(response_payload)
    provider._safe_async_client = fake

    text_response = await provider.ainvoke([HumanMessage(content="hello")])
    response = await provider.ainvoke_vision(
        "Read this image",
        [VisionImage(data=b"image-bytes", media_type="image/png")],
    )

    assert text_response.content.endswith("ok")
    assert response.content.endswith("ok")
    assert len(fake.calls) == 2
    text_call, call = fake.calls
    assert text_call["url"] == call["url"]
    assert call["url"].endswith(expected_path)
    assert "?" not in call["url"]
    assert "test-key" not in str(call["json"])
    if wire_protocol == "openai_chat_completions":
        assert call["headers"]["authorization"] == "Bearer test-key"
        assert text_call["json"]["messages"][0]["content"] == "hello"
        image = call["json"]["messages"][0]["content"][1]
        assert image["type"] == "image_url"
    elif wire_protocol == "anthropic_messages":
        assert call["headers"]["x-api-key"] == "test-key"
        assert text_call["json"]["messages"][0]["content"][0]["text"] == "hello"
        image = call["json"]["messages"][0]["content"][1]
        assert image["source"]["type"] == "base64"
    else:
        assert call["headers"]["x-goog-api-key"] == "test-key"
        assert text_call["json"]["contents"][0]["parts"][0]["text"] == "hello"
        image = call["json"]["contents"][0]["parts"][1]
        assert image["inlineData"]["mimeType"] == "image/png"


@pytest.mark.asyncio
async def test_cross_protocol_override_uses_only_the_selected_wire_protocol(monkeypatch):
    monkeypatch.setattr(settings, "runtime_environment", "development")
    provider = build_provider(ProviderConfig(
        provider_type="gemini",
        api_key="test-key",
        model="relay-model",
        base_url="https://relay.example.com/v1",
        endpoint_identity="https://relay.example.com/v1",
        wire_protocol="openai_chat_completions",
    ))
    fake = _FakeRelayClient({
        "choices": [{"message": {"content": "ok"}}],
        "usage": {},
    })
    provider._safe_async_client = fake

    await provider.ainvoke([
        SystemMessage(content="system"),
        HumanMessage(content="hello"),
    ])

    assert len(fake.calls) == 1
    assert fake.calls[0]["url"] == "https://relay.example.com/v1/chat/completions"
    assert fake.calls[0]["json"]["messages"][0]["role"] == "system"
    assert "x-goog-api-key" not in fake.calls[0]["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (401, "provider_auth_failed"),
        (404, "provider_model_or_endpoint_not_found"),
        (429, "provider_rate_limited"),
        (500, "provider_upstream_unavailable"),
        (400, "provider_request_rejected"),
    ],
)
async def test_relay_http_failures_expose_only_stable_codes(
    monkeypatch,
    status_code,
    expected_code,
):
    monkeypatch.setattr(settings, "runtime_environment", "development")
    provider = build_provider(ProviderConfig(
        provider_type="deepseek",
        api_key="secret-must-not-leak",
        model="relay-model",
        base_url="https://relay.example.com/v1",
        wire_protocol="openai_chat_completions",
    ))
    provider._safe_async_client = _FakeRelayClient(
        {"raw": "secret-must-not-leak"},
        status_code=status_code,
    )

    with pytest.raises(ProviderRequestError) as exc_info:
        await provider.ainvoke([HumanMessage(content="hello")])

    assert exc_info.value.code == expected_code
    assert str(exc_info.value) == expected_code
    assert "secret-must-not-leak" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_relay_tls_failure_preserves_a_specific_safe_error_code(monkeypatch):
    monkeypatch.setattr(settings, "runtime_environment", "development")
    provider = build_provider(ProviderConfig(
        provider_type="deepseek",
        api_key="secret-must-not-leak",
        model="relay-model",
        base_url="https://relay.example.com/v1",
        wire_protocol="openai_chat_completions",
    ))

    class TLSFailureClient:
        async def post(self, url, *, headers, json):
            try:
                raise ssl.SSLCertVerificationError("certificate verify failed")
            except ssl.SSLError as cause:
                raise httpx.ConnectError(
                    "TLS connection failed",
                    request=httpx.Request("POST", url),
                ) from cause

    provider._safe_async_client = TLSFailureClient()

    with pytest.raises(ProviderRequestError) as exc_info:
        await provider.ainvoke([HumanMessage(content="hello")])

    assert exc_info.value.code == "provider_endpoint_tls_failed"
    assert "relay.example.com" not in str(exc_info.value)
    assert "secret-must-not-leak" not in str(exc_info.value)


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
