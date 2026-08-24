"""Network policy and pinned transport for user-defined model endpoints."""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpcore
import httpx

from backend.llm.provider_catalog import (
    PROVIDER_CATALOG_BY_TYPE,
    WIRE_PROTOCOLS,
    effective_wire_protocol,
)


Resolver = Callable[[str, int], Iterable[str]]

OFFICIAL_PROVIDER_BASE_URLS = {
    provider_type: entry.default_base_url
    for provider_type, entry in PROVIDER_CATALOG_BY_TYPE.items()
}
EDITABLE_OPENAI_COMPATIBLE_PROVIDER_TYPES = frozenset(
    provider_type
    for provider_type, entry in PROVIDER_CATALOG_BY_TYPE.items()
    if entry.wire_protocol == "openai_chat_completions"
)
CUSTOM_BASE_URL_PROVIDER_TYPES = frozenset(
    provider_type
    for provider_type, entry in PROVIDER_CATALOG_BY_TYPE.items()
    if entry.custom_base_url_supported
)
OFFICIAL_PROVIDER_ENDPOINT_IDENTITIES = dict(OFFICIAL_PROVIDER_BASE_URLS)

_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class ProviderEndpointError(ValueError):
    """Safe endpoint-policy error whose code may be returned to clients."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ResolvedEndpoint:
    canonical_url: str
    hostname: str
    addresses: tuple[str, ...]


def canonicalize_provider_base_url(value: str) -> str:
    """Return one stable HTTPS Base URL or raise a safe policy error."""
    raw = value.strip()
    if (
        not raw
        or len(raw) > 512
        or any(ord(character) < 32 for character in raw)
        or "?" in raw
        or "#" in raw
    ):
        raise ProviderEndpointError("provider_endpoint_invalid")
    if "\\" in raw:
        raise ProviderEndpointError("provider_endpoint_invalid")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise ProviderEndpointError("provider_endpoint_invalid") from exc
    if parsed.scheme.lower() != "https":
        raise ProviderEndpointError("provider_endpoint_https_required")
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderEndpointError("provider_endpoint_invalid")
    if port not in {None, 443}:
        raise ProviderEndpointError("provider_endpoint_port_not_allowed")

    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ProviderEndpointError("provider_endpoint_invalid") from exc
    if (
        not hostname
        or hostname.endswith(".")
        or hostname == "localhost"
        or hostname.endswith((".localhost", ".local", ".internal"))
        or "." not in hostname
        or all(character.isdigit() or character == "." for character in hostname)
        or len(hostname) > 253
        or any(not _DOMAIN_LABEL.fullmatch(label) for label in hostname.split("."))
    ):
        raise ProviderEndpointError("provider_endpoint_host_not_allowed")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ProviderEndpointError("provider_endpoint_host_not_allowed")

    raw_segments = parsed.path.split("/")
    normalized_segments: list[str] = []
    for raw_segment in raw_segments:
        segment = unquote(raw_segment)
        if (
            segment in {".", ".."}
            or "/" in segment
            or "\\" in segment
            or "%" in segment
            or any(ord(character) < 32 for character in segment)
        ):
            raise ProviderEndpointError("provider_endpoint_invalid")
        normalized_segments.append(quote(segment, safe="!$&'()*+,;=:@~-._"))
    if any(not segment for segment in normalized_segments[1:-1]):
        raise ProviderEndpointError("provider_endpoint_invalid")
    path = "/".join(normalized_segments).rstrip("/")
    if path and not path.startswith("/"):
        path = "/" + path
    lowered_path = unquote(path).lower()
    if (
        lowered_path.endswith("/chat/completions")
        or lowered_path.endswith("/messages")
        or lowered_path.endswith(":generatecontent")
    ):
        raise ProviderEndpointError("provider_endpoint_invalid")
    return urlunsplit(("https", hostname, path, "", ""))


def normalize_provider_endpoint(
    provider_type: str,
    base_url: str | None,
    wire_protocol: str | None = None,
) -> tuple[str | None, str]:
    """Normalize a provider URL and return (stored URL, endpoint identity).

    Every implemented provider may use its official default or a policy-safe
    public HTTPS relay. Protocol selection is independent from the model brand,
    but a cross-protocol override is only allowed with a custom endpoint.
    """
    value = base_url.strip() if base_url else ""
    entry = PROVIDER_CATALOG_BY_TYPE.get(provider_type)
    if entry is None:
        raise ProviderEndpointError("provider_base_url_not_allowed")
    try:
        protocol = effective_wire_protocol(provider_type, wire_protocol)
    except ValueError as exc:
        raise ProviderEndpointError("provider_wire_protocol_not_supported") from exc
    identity = entry.default_base_url
    if value:
        canonical = canonicalize_provider_base_url(value)
        expected = entry.default_base_url
        expected_parts = urlsplit(expected)
        expected_origin = urlunsplit((
            expected_parts.scheme,
            expected_parts.netloc,
            "",
            "",
            "",
        ))
        is_official = canonical in {expected_origin, expected}
        if is_official:
            if protocol != entry.wire_protocol:
                raise ProviderEndpointError(
                    "provider_wire_protocol_requires_custom_endpoint"
                )
            return expected, identity
        return canonical, canonical
    if protocol != entry.wire_protocol:
        raise ProviderEndpointError("provider_wire_protocol_requires_custom_endpoint")
    return None, identity


def is_user_defined_provider_endpoint(
    provider_type: str,
    base_url: str | None,
    wire_protocol: str | None = None,
) -> bool:
    """Return whether a valid provider route differs from its official default.

    Invalid routes must propagate their safe policy error instead of being
    mistaken for an official endpoint by callers that choose the transport.
    """
    entry = PROVIDER_CATALOG_BY_TYPE.get(provider_type)
    if entry is None:
        return False
    _, identity = normalize_provider_endpoint(
        provider_type,
        base_url,
        wire_protocol,
    )
    protocol = effective_wire_protocol(provider_type, wire_protocol)
    return protocol != entry.wire_protocol or identity != entry.default_base_url


def provider_operation_url(
    base_url: str,
    wire_protocol: str,
    *,
    model: str,
) -> str:
    """Join one reviewed Base URL to the only operation allowed per protocol."""
    if wire_protocol not in WIRE_PROTOCOLS:
        raise ProviderEndpointError("provider_wire_protocol_not_supported")
    canonical = canonicalize_provider_base_url(base_url)
    path = urlsplit(canonical).path.rstrip("/")
    if wire_protocol == "openai_chat_completions":
        suffix = "/chat/completions"
    elif wire_protocol == "anthropic_messages":
        suffix = "/messages" if path.lower().endswith("/v1") else "/v1/messages"
    else:
        clean_model = model.strip()
        if not clean_model or any(ord(ch) < 32 for ch in clean_model):
            raise ProviderEndpointError("provider_model_invalid")
        encoded_model = quote(clean_model, safe="-._~")
        suffix = (
            f"/models/{encoded_model}:generateContent"
            if path.lower().endswith("/v1beta")
            else f"/v1beta/models/{encoded_model}:generateContent"
        )
    return f"{canonical}{suffix}"


def effective_provider_base_url(provider_type: str, base_url: str | None) -> str:
    entry = PROVIDER_CATALOG_BY_TYPE.get(provider_type)
    if entry is None:
        raise ProviderEndpointError("provider_base_url_not_allowed")
    return base_url or entry.default_base_url


def resolve_public_endpoint(
    base_url: str,
    *,
    resolver: Resolver | None = None,
) -> ResolvedEndpoint:
    """Resolve every address and reject the endpoint if any is non-public."""
    canonical = canonicalize_provider_base_url(base_url)
    hostname = urlsplit(canonical).hostname
    assert hostname is not None
    try:
        raw_addresses = tuple((resolver or _system_resolver)(hostname, 443))
    except ProviderEndpointError:
        raise
    except (OSError, TimeoutError, UnicodeError) as exc:
        raise ProviderEndpointError("provider_endpoint_dns_failed") from exc
    addresses = tuple(dict.fromkeys(raw_addresses))
    if not addresses:
        raise ProviderEndpointError("provider_endpoint_dns_failed")
    for raw_address in addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise ProviderEndpointError("provider_endpoint_dns_failed") from exc
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if (
            not address.is_global
            or address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ProviderEndpointError("provider_endpoint_non_public_address")
    return ResolvedEndpoint(canonical, hostname, addresses)


def _system_resolver(hostname: str, port: int) -> tuple[str, ...]:
    try:
        results = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise ProviderEndpointError("provider_endpoint_dns_failed") from exc
    return tuple(item[4][0] for item in results)


def _resolve_for_connection(
    endpoint: ResolvedEndpoint,
    resolver: Resolver | None,
) -> ResolvedEndpoint:
    """Re-resolve at socket time and return only the currently approved set.

    Public CDN addresses may rotate normally between calls.  The safety
    boundary is that every address in the fresh answer is globally routable
    and that the socket connects only to that fresh answer, not that DNS must
    remain byte-for-byte equal to the answer observed when the client object
    was created.
    """
    return resolve_public_endpoint(
        endpoint.canonical_url,
        resolver=resolver,
    )


class _PinnedSyncBackend(httpcore.NetworkBackend):
    def __init__(
        self,
        endpoint: ResolvedEndpoint,
        *,
        backend: httpcore.NetworkBackend | None = None,
        resolver: Resolver | None = None,
        revalidate: bool = False,
    ) -> None:
        self.endpoint = endpoint
        self.backend = backend or httpcore.SyncBackend()
        self.resolver = resolver
        self.revalidate = revalidate

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        endpoint = (
            _resolve_for_connection(self.endpoint, self.resolver)
            if self.revalidate
            else self.endpoint
        )
        if host.lower().rstrip(".") != endpoint.hostname or port != 443:
            raise ProviderEndpointError("provider_endpoint_host_not_allowed")
        last_error: BaseException | None = None
        for address in endpoint.addresses:
            try:
                return self.backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise ProviderEndpointError("provider_endpoint_dns_failed")

    def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.NetworkStream:
        raise ProviderEndpointError("provider_endpoint_host_not_allowed")

    def sleep(self, seconds: float) -> None:
        self.backend.sleep(seconds)


class _PinnedAsyncBackend(httpcore.AsyncNetworkBackend):
    def __init__(
        self,
        endpoint: ResolvedEndpoint,
        *,
        backend: httpcore.AsyncNetworkBackend | None = None,
        resolver: Resolver | None = None,
        revalidate: bool = False,
    ) -> None:
        self.endpoint = endpoint
        self.backend = backend or httpcore.AnyIOBackend()
        self.resolver = resolver
        self.revalidate = revalidate

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        endpoint = (
            await asyncio.to_thread(
                _resolve_for_connection,
                self.endpoint,
                self.resolver,
            )
            if self.revalidate
            else self.endpoint
        )
        if host.lower().rstrip(".") != endpoint.hostname or port != 443:
            raise ProviderEndpointError("provider_endpoint_host_not_allowed")
        last_error: BaseException | None = None
        for address in endpoint.addresses:
            try:
                return await self.backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise ProviderEndpointError("provider_endpoint_dns_failed")

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        raise ProviderEndpointError("provider_endpoint_host_not_allowed")

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


class _PinnedHTTPTransport(httpx.HTTPTransport):
    def __init__(
        self,
        endpoint: ResolvedEndpoint,
        *,
        resolver: Resolver | None = None,
        revalidate: bool = False,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        allowed_target_url: str | None = None,
    ) -> None:
        super().__init__(verify=True, trust_env=False, retries=0)
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=10,
            max_keepalive_connections=0,
            retries=0,
            network_backend=_PinnedSyncBackend(
                endpoint,
                resolver=resolver,
                revalidate=revalidate,
            ),
        )
        self._max_response_bytes = max(1024, int(max_response_bytes))
        self._allowed_target_url = _validated_allowed_target(
            endpoint,
            allowed_target_url,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        _validate_outbound_request(request, self._allowed_target_url)
        request.headers["accept-encoding"] = "identity"
        response = super().handle_request(request)
        if 300 <= response.status_code < 400:
            response.close()
            raise ProviderEndpointError("provider_endpoint_redirect_blocked")
        if response.headers.get("content-encoding", "identity").lower() not in {
            "",
            "identity",
        }:
            response.close()
            raise ProviderEndpointError("provider_endpoint_protocol_mismatch")
        if _declared_response_too_large(response.headers, self._max_response_bytes):
            response.close()
            raise ProviderEndpointError("provider_endpoint_response_too_large")
        response.stream = _LimitedSyncStream(
            response.stream,
            max_bytes=self._max_response_bytes,
        )
        return response


class _PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(
        self,
        endpoint: ResolvedEndpoint,
        *,
        resolver: Resolver | None = None,
        revalidate: bool = False,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        allowed_target_url: str | None = None,
    ) -> None:
        super().__init__(verify=True, trust_env=False, retries=0)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=10,
            max_keepalive_connections=0,
            retries=0,
            network_backend=_PinnedAsyncBackend(
                endpoint,
                resolver=resolver,
                revalidate=revalidate,
            ),
        )
        self._max_response_bytes = max(1024, int(max_response_bytes))
        self._allowed_target_url = _validated_allowed_target(
            endpoint,
            allowed_target_url,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        _validate_outbound_request(request, self._allowed_target_url)
        request.headers["accept-encoding"] = "identity"
        response = await super().handle_async_request(request)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise ProviderEndpointError("provider_endpoint_redirect_blocked")
        if response.headers.get("content-encoding", "identity").lower() not in {
            "",
            "identity",
        }:
            await response.aclose()
            raise ProviderEndpointError("provider_endpoint_protocol_mismatch")
        if _declared_response_too_large(response.headers, self._max_response_bytes):
            await response.aclose()
            raise ProviderEndpointError("provider_endpoint_response_too_large")
        response.stream = _LimitedAsyncStream(
            response.stream,
            max_bytes=self._max_response_bytes,
        )
        return response


def _declared_response_too_large(headers: httpx.Headers, max_bytes: int) -> bool:
    value = headers.get("content-length")
    if value is None:
        return False
    try:
        return int(value) > max_bytes
    except ValueError:
        return False


def _validated_allowed_target(
    endpoint: ResolvedEndpoint,
    allowed_target_url: str | None,
) -> httpx.URL | None:
    if allowed_target_url is None:
        return None
    try:
        target = httpx.URL(allowed_target_url)
    except (TypeError, ValueError) as exc:
        raise ProviderEndpointError("provider_endpoint_protocol_mismatch") from exc
    if (
        target.scheme != "https"
        or target.host != endpoint.hostname
        or target.port not in {None, 443}
        or bool(target.username)
        or bool(target.password)
        or target.query
        or target.fragment
    ):
        raise ProviderEndpointError("provider_endpoint_protocol_mismatch")
    return target


def _validate_outbound_request(
    request: httpx.Request,
    allowed_target_url: httpx.URL | None,
) -> None:
    if request.method != "POST":
        raise ProviderEndpointError("provider_endpoint_protocol_mismatch")
    if request.url.query or request.url.fragment:
        raise ProviderEndpointError("provider_endpoint_protocol_mismatch")
    if allowed_target_url is not None and request.url != allowed_target_url:
        raise ProviderEndpointError("provider_endpoint_protocol_mismatch")


class _LimitedSyncStream(httpx.SyncByteStream):
    def __init__(self, stream: httpx.SyncByteStream, *, max_bytes: int) -> None:
        self._stream = stream
        self._max_bytes = max_bytes

    def __iter__(self):
        received = 0
        for chunk in self._stream:
            received += len(chunk)
            if received > self._max_bytes:
                self._stream.close()
                raise ProviderEndpointError("provider_endpoint_response_too_large")
            yield chunk

    def close(self) -> None:
        self._stream.close()


class _LimitedAsyncStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, *, max_bytes: int) -> None:
        self._stream = stream
        self._max_bytes = max_bytes

    async def __aiter__(self):
        received = 0
        async for chunk in self._stream:
            received += len(chunk)
            if received > self._max_bytes:
                await self._stream.aclose()
                raise ProviderEndpointError("provider_endpoint_response_too_large")
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


def build_safe_provider_clients(
    base_url: str,
    *,
    timeout_seconds: float,
    resolver: Resolver | None = None,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    allowed_target_url: str | None = None,
) -> tuple[httpx.Client, httpx.AsyncClient]:
    """Build redirect-free clients whose sockets only use reviewed addresses."""
    endpoint = resolve_public_endpoint(base_url, resolver=resolver)
    return build_safe_provider_clients_for_endpoint(
        endpoint,
        timeout_seconds=timeout_seconds,
        resolver=resolver,
        revalidate=True,
        max_response_bytes=max_response_bytes,
        allowed_target_url=allowed_target_url,
    )


def build_safe_provider_clients_for_endpoint(
    endpoint: ResolvedEndpoint,
    *,
    timeout_seconds: float,
    resolver: Resolver | None = None,
    revalidate: bool = False,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    allowed_target_url: str | None = None,
) -> tuple[httpx.Client, httpx.AsyncClient]:
    """Build clients for one just-resolved and approved endpoint."""
    timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 15.0))
    return (
        httpx.Client(
            transport=_PinnedHTTPTransport(
                endpoint,
                resolver=resolver,
                revalidate=revalidate,
                max_response_bytes=max_response_bytes,
                allowed_target_url=allowed_target_url,
            ),
            follow_redirects=False,
            timeout=timeout,
            trust_env=False,
        ),
        httpx.AsyncClient(
            transport=_PinnedAsyncHTTPTransport(
                endpoint,
                resolver=resolver,
                revalidate=revalidate,
                max_response_bytes=max_response_bytes,
                allowed_target_url=allowed_target_url,
            ),
            follow_redirects=False,
            timeout=timeout,
            trust_env=False,
        ),
    )
