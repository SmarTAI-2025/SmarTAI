from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from backend.llm.endpoint_policy import (
    ProviderEndpointError,
    ResolvedEndpoint,
    _PinnedAsyncBackend,
    _LimitedSyncStream,
    _PinnedHTTPTransport,
    _PinnedSyncBackend,
    canonicalize_provider_base_url,
    normalize_provider_endpoint,
    provider_operation_url,
    resolve_public_endpoint,
)


def _public_resolver(hostname: str, port: int):
    assert port == 443
    return ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" https://Relay.Example.com/v1/ ", "https://relay.example.com/v1"),
        ("https://relay.example.com:443/openai/v1", "https://relay.example.com/openai/v1"),
        ("https://api.llm.ustc.edu.cn/v1", "https://api.llm.ustc.edu.cn/v1"),
        ("https://例子.测试/v1", "https://xn--fsqu00a.xn--0zwm56d/v1"),
    ],
)
def test_custom_base_url_canonicalization(value, expected):
    assert canonicalize_provider_base_url(value) == expected
    assert canonicalize_provider_base_url(expected) == expected


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("http://relay.example.com/v1", "provider_endpoint_https_required"),
        ("https://relay.example.com:8443/v1", "provider_endpoint_port_not_allowed"),
        ("https://user:pass@relay.example.com/v1", "provider_endpoint_invalid"),
        ("https://relay.example.com/v1?token=x", "provider_endpoint_invalid"),
        ("https://relay.example.com/v1#x", "provider_endpoint_invalid"),
        ("https://relay.example.com/v1/chat/completions", "provider_endpoint_invalid"),
        ("https://relay.example.com/v1/messages", "provider_endpoint_invalid"),
        ("https://relay.example.com/v1beta/models/x:generateContent", "provider_endpoint_invalid"),
        ("https://127.0.0.1/v1", "provider_endpoint_host_not_allowed"),
        ("https://2130706433/v1", "provider_endpoint_host_not_allowed"),
        ("https://127.1/v1", "provider_endpoint_host_not_allowed"),
        ("https://localhost/v1", "provider_endpoint_host_not_allowed"),
        ("https://service.local/v1", "provider_endpoint_host_not_allowed"),
        ("https://relay.example.com/v1/../admin", "provider_endpoint_invalid"),
        ("https://relay.example.com/v1%2fadmin", "provider_endpoint_invalid"),
        ("https://relay.example.com/v1%252fadmin", "provider_endpoint_invalid"),
        ("https://relay.example.com/%252e%252e/admin", "provider_endpoint_invalid"),
        ("https://relay.example.com//v1", "provider_endpoint_invalid"),
    ],
)
def test_custom_base_url_rejections(value, code):
    with pytest.raises(ProviderEndpointError) as exc_info:
        canonicalize_provider_base_url(value)
    assert exc_info.value.code == code


@pytest.mark.parametrize(
    "value",
    [
        "https://relay.example.com/v1?ignored",
        "https://relay.example.com/v1#ignored",
    ],
)
def test_query_and_fragment_are_rejected_even_when_empty_or_malformed(value):
    with pytest.raises(ProviderEndpointError) as exc_info:
        canonicalize_provider_base_url(value)
    assert exc_info.value.code == "provider_endpoint_invalid"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "100.64.0.1",
        "169.254.169.254",
        "192.0.2.1",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "2001:db8::1",
    ],
)
def test_non_public_dns_results_fail_closed(address):
    with pytest.raises(ProviderEndpointError) as exc_info:
        resolve_public_endpoint(
            "https://relay.example.com/v1",
            resolver=lambda hostname, port: ["93.184.216.34", address],
        )
    assert exc_info.value.code == "provider_endpoint_non_public_address"


@pytest.mark.parametrize(
    "addresses",
    [
        ["93.184.216.34"],
        ["2606:2800:220:1:248:1893:25c8:1946"],
        ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"],
    ],
)
def test_public_ipv4_ipv6_and_dual_stack_dns_results_are_accepted(addresses):
    endpoint = resolve_public_endpoint(
        "https://relay.example.com/v1",
        resolver=lambda hostname, port: addresses,
    )
    assert endpoint.hostname == "relay.example.com"
    assert endpoint.addresses == tuple(addresses)


def test_dns_failure_and_empty_result_fail_closed():
    for resolver in (
        lambda hostname, port: [],
        lambda hostname, port: (_ for _ in ()).throw(OSError("dns")),
    ):
        with pytest.raises(ProviderEndpointError) as exc_info:
            resolve_public_endpoint("https://relay.example.com/v1", resolver=resolver)
        assert exc_info.value.code == "provider_endpoint_dns_failed"


def test_deepseek_accepts_ustc_as_a_generic_user_defined_endpoint():
    base_url, identity = normalize_provider_endpoint(
        "deepseek", "https://api.llm.ustc.edu.cn/v1"
    )
    assert base_url == identity == "https://api.llm.ustc.edu.cn/v1"


def test_legacy_ustc_special_allowlist_is_absent():
    source = (
        Path(__file__).resolve().parents[1] / "api" / "experts.py"
    ).read_text(encoding="utf-8")

    assert "_APPROVED_PROXY_HOSTS" not in source
    assert "api.llm.ustc.edu.cn" not in source


@pytest.mark.parametrize(
    "value",
    ["https://api.deepseek.com", "https://api.deepseek.com/v1"],
)
def test_official_deepseek_accepts_only_its_origin_or_canonical_base(value):
    base_url, identity = normalize_provider_endpoint("deepseek", value)
    assert base_url == identity == "https://api.deepseek.com/v1"


def test_deepseek_accepts_any_policy_compliant_service_root_path():
    base_url, identity = normalize_provider_endpoint(
        "deepseek", "https://api.deepseek.com/proxy/v1"
    )
    assert base_url == identity == "https://api.deepseek.com/proxy/v1"


@pytest.mark.parametrize(
    ("provider_type", "protocol"),
    [
        ("gemini", "gemini_generate_content"),
        ("anthropic", "anthropic_messages"),
        ("gemini", "openai_chat_completions"),
        ("anthropic", "gemini_generate_content"),
    ],
)
def test_native_and_cross_protocol_relays_use_the_same_generic_url_policy(
    provider_type,
    protocol,
):
    base_url, identity = normalize_provider_endpoint(
        provider_type,
        "https://relay.example.com/v1",
        protocol,
    )
    assert base_url == identity == "https://relay.example.com/v1"


def test_cross_protocol_override_requires_a_custom_url():
    with pytest.raises(ProviderEndpointError) as exc_info:
        normalize_provider_endpoint(
            "gemini",
            None,
            "openai_chat_completions",
        )
    assert exc_info.value.code == "provider_wire_protocol_requires_custom_endpoint"


@pytest.mark.parametrize(
    ("base_url", "protocol", "model", "expected"),
    [
        (
            "https://relay.example.com/v1",
            "openai_chat_completions",
            "model",
            "https://relay.example.com/v1/chat/completions",
        ),
        (
            "https://relay.example.com/v1",
            "anthropic_messages",
            "model",
            "https://relay.example.com/v1/messages",
        ),
        (
            "https://relay.example.com/anthropic",
            "anthropic_messages",
            "model",
            "https://relay.example.com/anthropic/v1/messages",
        ),
        (
            "https://relay.example.com/v1beta",
            "gemini_generate_content",
            "model/name",
            "https://relay.example.com/v1beta/models/model%2Fname:generateContent",
        ),
    ],
)
def test_protocol_operation_url_is_joined_once(base_url, protocol, model, expected):
    assert provider_operation_url(base_url, protocol, model=model) == expected


class _SyncBackend:
    def __init__(self):
        self.hosts = []

    def connect_tcp(self, host, port, **kwargs):
        self.hosts.append(host)
        return object()

    def sleep(self, seconds):
        return None


class _AsyncBackend:
    def __init__(self):
        self.hosts = []

    async def connect_tcp(self, host, port, **kwargs):
        self.hosts.append(host)
        return object()

    async def sleep(self, seconds):
        return None


def test_sync_network_backend_connects_only_to_approved_ip():
    endpoint = ResolvedEndpoint(
        "https://relay.example.com/v1", "relay.example.com", ("93.184.216.34",)
    )
    backend = _SyncBackend()
    pinned = _PinnedSyncBackend(endpoint, backend=backend)

    pinned.connect_tcp("relay.example.com", 443)
    assert backend.hosts == ["93.184.216.34"]
    with pytest.raises(ProviderEndpointError):
        pinned.connect_tcp("other.example.com", 443)


@pytest.mark.asyncio
async def test_async_network_backend_connects_only_to_approved_ip():
    endpoint = ResolvedEndpoint(
        "https://relay.example.com/v1", "relay.example.com", ("93.184.216.34",)
    )
    backend = _AsyncBackend()
    pinned = _PinnedAsyncBackend(endpoint, backend=backend)

    await pinned.connect_tcp("relay.example.com", 443)
    assert backend.hosts == ["93.184.216.34"]


def test_rebinding_is_rechecked_before_connect():
    calls = 0

    def rebinding_resolver(hostname, port):
        nonlocal calls
        calls += 1
        return ["93.184.216.34"] if calls == 1 else ["127.0.0.1"]

    endpoint = resolve_public_endpoint(
        "https://relay.example.com/v1", resolver=rebinding_resolver
    )
    backend = _SyncBackend()
    pinned = _PinnedSyncBackend(
        endpoint,
        backend=backend,
        resolver=rebinding_resolver,
        revalidate=True,
    )

    with pytest.raises(ProviderEndpointError) as exc_info:
        pinned.connect_tcp("relay.example.com", 443)
    assert exc_info.value.code == "provider_endpoint_non_public_address"
    assert backend.hosts == []


def test_public_dns_rotation_uses_only_the_freshly_approved_connection_set():
    calls = 0

    def rotating_resolver(hostname, port):
        nonlocal calls
        calls += 1
        return ["93.184.216.34"] if calls == 1 else ["1.1.1.1"]

    endpoint = resolve_public_endpoint(
        "https://relay.example.com/v1", resolver=rotating_resolver
    )
    backend = _SyncBackend()
    pinned = _PinnedSyncBackend(
        endpoint,
        backend=backend,
        resolver=rotating_resolver,
        revalidate=True,
    )

    pinned.connect_tcp("relay.example.com", 443)
    assert backend.hosts == ["1.1.1.1"]


def test_pinned_transport_keeps_original_hostname_for_tls_source_contract():
    source = (
        Path(__file__).resolve().parents[1] / "llm" / "endpoint_policy.py"
    ).read_text(encoding="utf-8")
    assert "ssl.create_default_context()" in source
    assert "trust_env=False" in source
    assert "follow_redirects=False" in source
    assert "address,\n                    port" in source
    assert 'server_hostname": sni_hostname' not in source
    assert 'host.lower().rstrip(".") != endpoint.hostname' in source


class _BytesStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed = True


def test_response_stream_is_bounded():
    raw = _BytesStream([b"1234", b"5678"])
    stream = _LimitedSyncStream(raw, max_bytes=6)

    iterator = iter(stream)
    assert next(iterator) == b"1234"
    with pytest.raises(ProviderEndpointError) as exc_info:
        next(iterator)
    assert exc_info.value.code == "provider_endpoint_response_too_large"
    assert raw.closed is True


@pytest.mark.parametrize("status_code", [301, 302, 307, 308])
def test_custom_transport_blocks_redirect_without_following(monkeypatch, status_code):
    endpoint = ResolvedEndpoint(
        "https://relay.example.com/v1", "relay.example.com", ("93.184.216.34",)
    )
    transport = _PinnedHTTPTransport(endpoint)
    response = httpx.Response(
        status_code, headers={"location": "https://127.0.0.1/"}
    )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, request: response)
    with pytest.raises(ProviderEndpointError) as exc_info:
        transport.handle_request(httpx.Request("POST", endpoint.canonical_url))
    assert exc_info.value.code == "provider_endpoint_redirect_blocked"
    assert response.is_closed


def test_custom_transport_allows_only_post_and_requests_identity_encoding(monkeypatch):
    endpoint = ResolvedEndpoint(
        "https://relay.example.com/v1", "relay.example.com", ("93.184.216.34",)
    )
    transport = _PinnedHTTPTransport(endpoint)
    captured = {}

    def fake_handle_request(_self, request):
        captured["accept_encoding"] = request.headers["accept-encoding"]
        return httpx.Response(200, stream=_BytesStream([b"{}"]), request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake_handle_request)
    request = httpx.Request("POST", endpoint.canonical_url)
    response = transport.handle_request(request)

    assert captured["accept_encoding"] == "identity"
    assert b"".join(response.iter_bytes()) == b"{}"
    with pytest.raises(ProviderEndpointError) as exc_info:
        transport.handle_request(httpx.Request("GET", endpoint.canonical_url))
    assert exc_info.value.code == "provider_endpoint_protocol_mismatch"


@pytest.mark.parametrize(
    "url",
    [
        "https://relay.example.com/v1",
        "https://relay.example.com/v1/chat/completions?key=x",
        "https://other.example.com/v1/chat/completions",
        "https://relay.example.com/v1/messages",
    ],
)
def test_custom_transport_rejects_every_target_except_the_selected_operation(
    monkeypatch,
    url,
):
    endpoint = ResolvedEndpoint(
        "https://relay.example.com/v1",
        "relay.example.com",
        ("93.184.216.34",),
    )
    allowed = "https://relay.example.com/v1/chat/completions"
    transport = _PinnedHTTPTransport(endpoint, allowed_target_url=allowed)
    monkeypatch.setattr(
        httpx.HTTPTransport,
        "handle_request",
        lambda _self, request: httpx.Response(
            200,
            stream=_BytesStream([b"{}"]),
            request=request,
        ),
    )

    with pytest.raises(ProviderEndpointError) as exc_info:
        transport.handle_request(httpx.Request("POST", url))
    assert exc_info.value.code == "provider_endpoint_protocol_mismatch"


def test_custom_transport_allows_exact_selected_operation(monkeypatch):
    endpoint = ResolvedEndpoint(
        "https://relay.example.com/v1",
        "relay.example.com",
        ("93.184.216.34",),
    )
    allowed = "https://relay.example.com/v1/chat/completions"
    transport = _PinnedHTTPTransport(endpoint, allowed_target_url=allowed)
    monkeypatch.setattr(
        httpx.HTTPTransport,
        "handle_request",
        lambda _self, request: httpx.Response(
            200,
            stream=_BytesStream([b"{}"]),
            request=request,
        ),
    )

    response = transport.handle_request(httpx.Request("POST", allowed))
    assert b"".join(response.iter_bytes()) == b"{}"


def test_custom_transport_rejects_compressed_response(monkeypatch):
    endpoint = ResolvedEndpoint(
        "https://relay.example.com/v1", "relay.example.com", ("93.184.216.34",)
    )
    transport = _PinnedHTTPTransport(endpoint)
    response = httpx.Response(
        200,
        headers={"content-encoding": "gzip"},
        stream=_BytesStream([b"compressed"]),
    )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda *_: response)
    with pytest.raises(ProviderEndpointError) as exc_info:
        transport.handle_request(httpx.Request("POST", endpoint.canonical_url))
    assert exc_info.value.code == "provider_endpoint_protocol_mismatch"
    assert response.is_closed
