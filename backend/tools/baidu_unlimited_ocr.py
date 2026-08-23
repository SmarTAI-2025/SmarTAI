"""Low-level BYOK client for Baidu Document Parsing (Unlimited-OCR).

This module deliberately has no dependency on the generic LLM provider
registry.  Callers must inject one owner's Baidu API Key and Secret Key for
each client instance.  The credentials and OAuth access token stay in memory.

Only the documented ``file_data`` upload path is exposed.  User supplied URLs
are not accepted: allowing Baidu to fetch arbitrary URLs would add an SSRF and
data-ownership boundary that this adapter does not need.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

TOKEN_ENDPOINT = "https://aip.baidubce.com/oauth/2.0/token"
SUBMIT_ENDPOINT = (
    "https://aip.baidubce.com/rest/2.0/brain/online/v2/"
    "unlimited-ocr-parser/task"
)
QUERY_ENDPOINT = f"{SUBMIT_ENDPOINT}/query"

PROVIDER_ID = "baidu_unlimited_ocr"

# The API accepts larger layout documents when supplied by URL, but file_data
# itself is documented for files no larger than "50M".  The docs do not define
# decimal versus binary units, so use the smaller decimal interpretation.  The
# same conservative rule applies to the documented image "10M" limit.
MAX_FILE_DATA_BYTES = 50_000_000
MAX_IMAGE_BYTES = 10_000_000
MAX_MARKDOWN_BYTES = 10 * 1024 * 1024

# The official 8192-pixel image edge and 500-page PDF ceilings are not parsed
# locally here.  Doing so safely would require another killable media-parser
# boundary.  The existing upload pipeline already validates content type; this
# provider boundary enforces conservative byte ceilings and projects Baidu's
# own format/page errors without pretending to have inspected those fields.

SUPPORTED_SUFFIXES = frozenset(
    {
        ".pdf",
        ".jpg",
        ".jpeg",
        ".png",
        ".bmp",
        ".tif",
        ".tiff",
        ".ofd",
        ".doc",
        ".docx",
        ".txt",
        ".wps",
        ".ppt",
        ".pptx",
    }
)
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"})

# Baidu documents the result as a URL but does not promise a hostname.  Keep a
# deliberately small official-domain allowlist and fail closed if the live API
# returns anything else.  A live smoke test is required before widening it.
DEFAULT_DOWNLOAD_HOST_SUFFIXES = (".bcebos.com", ".baidubce.com")


class BaiduUnlimitedOCRError(Exception):
    """Typed, projection-safe failure from the Baidu OCR boundary.

    ``str(error)`` intentionally contains only the stable code.  Vendor error
    text, request URLs, task IDs, credentials, tokens, filenames and document
    contents are never attached to this exception.
    """

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True)
class BaiduUnlimitedOCRDocument:
    markdown: str
    duration_ms: float


@dataclass
class _RecognitionCallState:
    """Per-call phase state used to project ambiguous overall timeouts safely."""

    submit_started: bool = False
    submit_completed: bool = False


Sleep = Callable[[float], Awaitable[None]]
DownloadURLValidator = Callable[[str], bool]


class _HTTPXSecretFilter(logging.Filter):
    """Remove credential-bearing and signed URLs from HTTPX access logs."""

    @staticmethod
    def _redact(value: Any) -> Any:
        if isinstance(value, httpx.URL):
            raw = str(value)
        elif isinstance(value, str) and value.startswith(("https://", "http://")):
            raw = value
        else:
            return value
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return "<redacted-url>"
        query = parsed.query.lower()
        host = (parsed.hostname or "").lower()
        if (
            "access_token=" in query
            or "client_secret=" in query
            or "client_id=" in query
            or host == "bcebos.com"
            or host.endswith(".bcebos.com")
            or host == "baidubce.com"
            or host.endswith(".baidubce.com")
        ):
            return "<redacted-url>"
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact(value) for value in record.args)
        elif isinstance(record.args, Mapping):
            record.args = {
                key: self._redact(value) for key, value in record.args.items()
            }
        return True

    @classmethod
    def install_once(cls) -> None:
        httpx_logger = logging.getLogger("httpx")
        if not any(isinstance(item, cls) for item in httpx_logger.filters):
            httpx_logger.addFilter(cls())
        # httpcore DEBUG records can include the raw request target.  The OCR
        # boundary already emits safe phase/result logs, so suppressing these
        # transport internals is preferable to retaining credential-bearing
        # diagnostics.
        httpcore_logger = logging.getLogger("httpcore")
        if not any(
            isinstance(item, _DropBelowWarningFilter)
            for item in httpcore_logger.filters
        ):
            httpcore_logger.addFilter(_DropBelowWarningFilter())


class _DropBelowWarningFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING


def _default_download_url_validator(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
        return False
    if port not in (None, 443):
        return False
    return any(
        hostname == suffix.removeprefix(".") or hostname.endswith(suffix)
        for suffix in DEFAULT_DOWNLOAD_HOST_SUFFIXES
    )


def _vendor_error(error_code: Any) -> BaiduUnlimitedOCRError:
    try:
        normalized = int(error_code)
    except (TypeError, ValueError):
        return BaiduUnlimitedOCRError("provider_response_invalid")

    if normalized in {100, 110, 111, 282006}:
        return BaiduUnlimitedOCRError("provider_auth_failed")
    if normalized == 6:
        return BaiduUnlimitedOCRError("provider_permission_denied")
    if normalized in {17, 19, 282005}:
        return BaiduUnlimitedOCRError("provider_quota_exceeded")
    if normalized in {4, 18}:
        return BaiduUnlimitedOCRError("provider_rate_limited", retryable=True)
    if normalized in {216200, 216201, 216202, 282003, 282111}:
        return BaiduUnlimitedOCRError("ocr_input_invalid")
    if normalized in {1, 2, 282000}:
        return BaiduUnlimitedOCRError("provider_unavailable", retryable=True)
    if normalized == 282007:
        return BaiduUnlimitedOCRError("provider_task_failed")
    return BaiduUnlimitedOCRError("provider_request_failed")


class BaiduUnlimitedOCRClient:
    """Async, BYOK-only client for Baidu's Unlimited-OCR document parser."""

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        http_client: httpx.AsyncClient | None = None,
        download_client: httpx.AsyncClient | None = None,
        download_url_validator: DownloadURLValidator | None = None,
        request_timeout_seconds: float = 30.0,
        overall_timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 5.0,
        query_max_attempts: int = 3,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise BaiduUnlimitedOCRError("provider_credentials_required")
        if not isinstance(secret_key, str) or not secret_key.strip():
            raise BaiduUnlimitedOCRError("provider_credentials_required")
        if request_timeout_seconds <= 0 or overall_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        # The official integration guide recommends polling every 5-10
        # seconds.  Tests inject a no-op sleeper instead of weakening this
        # production boundary.
        if poll_interval_seconds < 5.0:
            raise ValueError("poll_interval_seconds must be at least 5.0")
        if query_max_attempts < 1 or query_max_attempts > 5:
            raise ValueError("query_max_attempts must be between 1 and 5")

        _HTTPXSecretFilter.install_once()
        self._api_key = api_key.strip()
        self._secret_key = secret_key.strip()
        self._request_timeout_seconds = float(request_timeout_seconds)
        self._overall_timeout_seconds = float(overall_timeout_seconds)
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._query_max_attempts = int(query_max_attempts)
        self._sleep = sleep
        self._download_url_validator = (
            download_url_validator or _default_download_url_validator
        )

        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(
            timeout=self._request_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        self._download_client = download_client or self._http_client
        # Injected transports belong to their caller.  The default download
        # path reuses the one client this instance owns.
        self._owns_download_client = False

        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return "BaiduUnlimitedOCRClient(credentials=<redacted>)"

    async def __aenter__(self) -> "BaiduUnlimitedOCRClient":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_download_client:
            await self._download_client.aclose()
        if self._owns_http_client:
            await self._http_client.aclose()
        self._api_key = ""
        self._secret_key = ""
        self._access_token = None
        self._access_token_expires_at = 0.0

    async def recognize_document(
        self,
        file_data: bytes,
        file_name: str,
    ) -> BaiduUnlimitedOCRDocument:
        suffix = self._validate_file(file_data, file_name)
        started = time.perf_counter()
        call_state = _RecognitionCallState()
        try:
            document = await asyncio.wait_for(
                self._recognize_document(file_data, suffix, call_state),
                timeout=self._overall_timeout_seconds,
            )
        except BaiduUnlimitedOCRError as error:
            if call_state.submit_started and error.retryable:
                # The public method does not expose task_id/resume.  Once a
                # submission may exist, advertising any outward failure as
                # retryable could make a caller create a second OCR task.
                raise BaiduUnlimitedOCRError(
                    error.code,
                    retryable=False,
                ) from None
            raise
        except (asyncio.TimeoutError, TimeoutError):
            if call_state.submit_started and not call_state.submit_completed:
                # The request may have reached Baidu before cancellation.  A
                # retryable timeout projection could induce a duplicate task.
                raise BaiduUnlimitedOCRError(
                    "provider_submit_uncertain"
                ) from None
            raise BaiduUnlimitedOCRError(
                "provider_timeout",
                retryable=not call_state.submit_started,
            ) from None
        duration_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "baidu_unlimited_ocr phase=complete result=ok duration_ms=%.0f",
            duration_ms,
        )
        return BaiduUnlimitedOCRDocument(
            markdown=document,
            duration_ms=duration_ms,
        )

    async def verify_credentials(self) -> None:
        """Verify BYOK credentials without submitting a potentially billed task.

        The OAuth token is retained only in the private in-memory cache and is
        deliberately not returned to the API/service layer.
        """

        await self._get_access_token()

    @staticmethod
    def _validate_file(file_data: bytes, file_name: str) -> str:
        if not isinstance(file_data, bytes) or not file_data:
            raise BaiduUnlimitedOCRError("ocr_input_invalid")
        suffix = PurePath(str(file_name or "")).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise BaiduUnlimitedOCRError("ocr_unsupported_file")
        size = len(file_data)
        if size > MAX_FILE_DATA_BYTES:
            raise BaiduUnlimitedOCRError("ocr_file_too_large")
        if suffix in IMAGE_SUFFIXES and size > MAX_IMAGE_BYTES:
            raise BaiduUnlimitedOCRError("ocr_file_too_large")
        return suffix

    async def _recognize_document(
        self,
        file_data: bytes,
        suffix: str,
        call_state: _RecognitionCallState,
    ) -> str:
        access_token = await self._get_access_token()
        # Never forward the user-controlled filename.  Baidu requires a name,
        # but only the extension is material; this avoids disclosing a student
        # or assignment identifier in vendor metadata.
        call_state.submit_started = True
        task_id = await self._submit_once(
            access_token=access_token,
            file_data=file_data,
            sanitized_file_name=f"document{suffix}",
        )
        call_state.submit_completed = True

        while True:
            result = await self._query_with_retry(access_token, task_id)
            raw_status = result.get("status")
            if not isinstance(raw_status, str):
                raise BaiduUnlimitedOCRError("provider_response_invalid")
            status = raw_status.strip()
            if status in {"pending", "running"}:
                await self._sleep(self._poll_interval_seconds)
                continue
            if status == "success":
                markdown_url = result.get("markdown_url")
                if not isinstance(markdown_url, str) or not markdown_url:
                    raise BaiduUnlimitedOCRError("provider_response_invalid")
                return await self._download_markdown(markdown_url)
            if status == "failed":
                raise BaiduUnlimitedOCRError("provider_task_failed")
            raise BaiduUnlimitedOCRError("provider_response_invalid")

    async def _get_access_token(self) -> str:
        # Refresh a minute early to avoid using a token at the expiry edge.
        if self._access_token and time.monotonic() < self._access_token_expires_at - 60:
            return self._access_token
        async with self._token_lock:
            if self._access_token and time.monotonic() < self._access_token_expires_at - 60:
                return self._access_token
            payload = await self._post_json_with_retry(
                TOKEN_ENDPOINT,
                params={
                    "grant_type": "client_credentials",
                    "client_id": self._api_key,
                    "client_secret": self._secret_key,
                },
                data=None,
                max_attempts=3,
                phase="token",
            )
            token = payload.get("access_token")
            try:
                expires_in = int(payload.get("expires_in") or 0)
            except (TypeError, ValueError):
                expires_in = 0
            if not isinstance(token, str) or not token or expires_in <= 0:
                raise BaiduUnlimitedOCRError("provider_auth_failed")
            self._access_token = token
            self._access_token_expires_at = time.monotonic() + expires_in
            return token

    async def _submit_once(
        self,
        *,
        access_token: str,
        file_data: bytes,
        sanitized_file_name: str,
    ) -> str:
        try:
            response = await self._http_client.post(
                SUBMIT_ENDPOINT,
                params={"access_token": access_token},
                data={
                    "file_data": base64.b64encode(file_data).decode("ascii"),
                    "file_name": sanitized_file_name,
                },
                timeout=self._request_timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.TransportError):
            # The provider may have accepted the task before the connection
            # failed.  Replaying here could create a duplicate billable task.
            raise BaiduUnlimitedOCRError("provider_submit_uncertain") from None

        if response.status_code >= 500:
            raise BaiduUnlimitedOCRError("provider_submit_uncertain")
        payload = self._decode_json_response(response, phase="submit")
        result = payload.get("result")
        task_id = result.get("task_id") if isinstance(result, Mapping) else None
        if not isinstance(task_id, str) or not task_id:
            raise BaiduUnlimitedOCRError("provider_response_invalid")
        return task_id

    async def _query_with_retry(
        self,
        access_token: str,
        task_id: str,
    ) -> Mapping[str, Any]:
        payload = await self._post_json_with_retry(
            QUERY_ENDPOINT,
            params={"access_token": access_token},
            data={"task_id": task_id},
            max_attempts=self._query_max_attempts,
            phase="query",
        )
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise BaiduUnlimitedOCRError("provider_response_invalid")
        return result

    async def _post_json_with_retry(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        data: Mapping[str, str] | None,
        max_attempts: int,
        phase: str,
    ) -> Mapping[str, Any]:
        last_error: BaiduUnlimitedOCRError | None = None
        for attempt in range(max_attempts):
            try:
                response = await self._http_client.post(
                    url,
                    params=params,
                    data=data,
                    timeout=self._request_timeout_seconds,
                )
                payload = self._decode_json_response(response, phase=phase)
                return payload
            except httpx.TimeoutException:
                last_error = BaiduUnlimitedOCRError(
                    "provider_timeout", retryable=True
                )
            except httpx.TransportError:
                last_error = BaiduUnlimitedOCRError(
                    "provider_unreachable", retryable=True
                )
            except BaiduUnlimitedOCRError as error:
                last_error = error
                if not error.retryable:
                    raise
            if attempt + 1 < max_attempts:
                await self._sleep(min(0.5 * (2**attempt), 2.0))
        assert last_error is not None
        raise last_error from None

    @staticmethod
    def _decode_json_response(
        response: httpx.Response,
        *,
        phase: str,
    ) -> Mapping[str, Any]:
        if response.status_code in {401, 403}:
            raise BaiduUnlimitedOCRError("provider_auth_failed")
        if response.status_code == 429:
            raise BaiduUnlimitedOCRError(
                "provider_rate_limited", retryable=True
            )
        if response.status_code >= 500:
            if phase == "submit":
                raise BaiduUnlimitedOCRError("provider_submit_uncertain")
            raise BaiduUnlimitedOCRError(
                "provider_unavailable", retryable=True
            )
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError):
            if phase == "submit" and response.status_code >= 500:
                raise BaiduUnlimitedOCRError("provider_submit_uncertain") from None
            raise BaiduUnlimitedOCRError("provider_response_invalid") from None
        if not isinstance(payload, Mapping):
            raise BaiduUnlimitedOCRError("provider_response_invalid")

        error_code = payload.get("error_code")
        if error_code not in (None, 0, "0"):
            raise _vendor_error(error_code)
        # OAuth errors use a string ``error`` rather than OCR error_code.
        if payload.get("error"):
            raise BaiduUnlimitedOCRError("provider_auth_failed")
        if response.status_code < 200 or response.status_code >= 300:
            raise BaiduUnlimitedOCRError("provider_request_failed")
        return payload

    async def _download_markdown(self, markdown_url: str) -> str:
        if not self._download_url_validator(markdown_url):
            raise BaiduUnlimitedOCRError("provider_download_url_rejected")
        try:
            async with self._download_client.stream(
                "GET",
                markdown_url,
                timeout=self._request_timeout_seconds,
                follow_redirects=False,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise BaiduUnlimitedOCRError("provider_result_unavailable")
                length_header = response.headers.get("content-length")
                if length_header:
                    try:
                        if int(length_header) > MAX_MARKDOWN_BYTES:
                            raise BaiduUnlimitedOCRError(
                                "provider_result_too_large"
                            )
                    except ValueError:
                        raise BaiduUnlimitedOCRError(
                            "provider_response_invalid"
                        ) from None
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > MAX_MARKDOWN_BYTES:
                        raise BaiduUnlimitedOCRError("provider_result_too_large")
                    chunks.append(chunk)
        except BaiduUnlimitedOCRError:
            raise
        except httpx.TimeoutException:
            raise BaiduUnlimitedOCRError(
                "provider_timeout", retryable=True
            ) from None
        except httpx.TransportError:
            raise BaiduUnlimitedOCRError(
                "provider_unreachable", retryable=True
            ) from None

        try:
            markdown = b"".join(chunks).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise BaiduUnlimitedOCRError("provider_response_invalid") from None
        if not markdown.strip():
            raise BaiduUnlimitedOCRError("ocr_empty_result")
        return markdown
