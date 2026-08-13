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


Resolver = Callable[[str, int], Iterable[str]]

CUSTOM_PROVIDER_TYPE = "openai_compatible"
CUSTOM_PROVIDER_RISK_ACK_VERSION = "2026-08-12.v1"

OFFICIAL_PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "deepseek": "https://api.deepseek.com/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}
OFFICIAL_PROVIDER_ENDPOINT_IDENTITIES = {
    **OFFICIAL_PROVIDER_BASE_URLS,
    "gemini": "https://generativelanguage.googleapis.com",
    "anthropic": "https://api.anthropic.com",
}

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
            or any(ord(character) < 32 for character in segment)
        ):
            raise ProviderEndpointError("provider_endpoint_invalid")
        normalized_segments.append(quote(segment, safe="!$&'()*+,;=:@~-._"))
    if any(not segment for segment in normalized_segments[1:-1]):
        raise ProviderEndpointError("provider_endpoint_invalid")
    path = "/".join(normalized_segments).rstrip("/")
    if path and not path.startswith("/"):
        path = "/" + path
    if path.lower().endswith("/chat/completions"):
        raise ProviderEndpointError("provider_endpoint_invalid")
    return urlunsplit(("https", hostname, path, "", ""))


def normalize_provider_endpoint(
    provider_type: str,
    base_url: str | None,
) -> tuple[str | None, str]:
    """Validate official endpoints and return (stored URL, endpoint identity)."""
    value = base_url.strip() if base_url else ""
    if provider_type == CUSTOM_PROVIDER_TYPE:
        if not value:
            raise ProviderEndpointError("provider_endpoint_invalid")
        canonical = canonicalize_provider_base_url(value)
        return canonical, canonical
    identity = OFFICIAL_PROVIDER_ENDPOINT_IDENTITIES.get(provider_type)
    if identity is None:
        raise ProviderEndpointError("provider_base_url_not_allowed")
    if value:
        expected = OFFICIAL_PROVIDER_BASE_URLS.get(provider_type)
        try:
            canonical = canonicalize_provider_base_url(value)
        except ProviderEndpointError as exc:
            raise ProviderEndpointError("provider_base_url_not_allowed") from exc
        if expected is None:
            raise ProviderEndpointError("provider_base_url_not_allowed")
        expected_parts = urlsplit(expected)
        expected_origin = urlunsplit((
            expected_parts.scheme,
            expected_parts.netloc,
            "",
            "",
            "",
        ))
        if canonical not in {expected_origin, expected}:
            raise ProviderEndpointError("provider_base_url_not_allowed")
        return expected, identity
    return None, identity


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

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            raise ProviderEndpointError("provider_endpoint_protocol_mismatch")
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

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            raise ProviderEndpointError("provider_endpoint_protocol_mismatch")
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
) -> tuple[httpx.Client, httpx.AsyncClient]:
    """Build redirect-free clients whose sockets only use reviewed addresses."""
    endpoint = resolve_public_endpoint(base_url, resolver=resolver)
    return build_safe_provider_clients_for_endpoint(
        endpoint,
        timeout_seconds=timeout_seconds,
        resolver=resolver,
        revalidate=True,
        max_response_bytes=max_response_bytes,
    )


def build_safe_provider_clients_for_endpoint(
    endpoint: ResolvedEndpoint,
    *,
    timeout_seconds: float,
    resolver: Resolver | None = None,
    revalidate: bool = False,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
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
            ),
            follow_redirects=False,
            timeout=timeout,
            trust_env=False,
        ),
    )
