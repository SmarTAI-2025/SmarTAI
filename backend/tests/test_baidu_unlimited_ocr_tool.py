from __future__ import annotations

import asyncio
import base64
import inspect
import logging
from urllib.parse import parse_qs

import httpx
import pytest

from backend.skills.ocr_ingest import (
    BaiduUnlimitedOCRSkill,
    OCRImage,
)
from backend.tools import baidu_unlimited_ocr as baidu_tool
from backend.tools.baidu_unlimited_ocr import (
    MAX_IMAGE_BYTES,
    BaiduUnlimitedOCRClient,
    BaiduUnlimitedOCRDocument,
    BaiduUnlimitedOCRError,
)


API_KEY = "test-api-key-secret-marker"
SECRET_KEY = "test-secret-key-secret-marker"
ACCESS_TOKEN = "test-access-token-secret-marker"


def _form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode("ascii"), keep_blank_values=True)


def _client_for_handler(handler, **kwargs):
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    )
    client = BaiduUnlimitedOCRClient(
        api_key=API_KEY,
        secret_key=SECRET_KEY,
        http_client=http_client,
        **kwargs,
    )
    return client, http_client


@pytest.mark.asyncio
async def test_official_contract_uses_oauth_post_file_data_and_markdown_only():
    requests: list[httpx.Request] = []
    query_count = 0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_count
        requests.append(request)
        if str(request.url).startswith(baidu_tool.TOKEN_ENDPOINT):
            assert request.method == "POST"
            assert request.url.params["grant_type"] == "client_credentials"
            assert request.url.params["client_id"] == API_KEY
            assert request.url.params["client_secret"] == SECRET_KEY
            return httpx.Response(
                200,
                json={"access_token": ACCESS_TOKEN, "expires_in": 2_592_000},
            )
        if request.url.path.endswith("/unlimited-ocr-parser/task"):
            assert request.method == "POST"
            assert request.url.params["access_token"] == ACCESS_TOKEN
            assert request.headers["content-type"].startswith(
                "application/x-www-form-urlencoded"
            )
            form = _form(request)
            assert form["file_data"] == [
                base64.b64encode(b"%PDF-private-content").decode("ascii")
            ]
            # User filename is not forwarded into vendor metadata.
            assert form["file_name"] == ["document.pdf"]
            assert "file_url" not in form
            return httpx.Response(200, json={"result": {"task_id": "task-1"}})
        if request.url.path.endswith("/unlimited-ocr-parser/task/query"):
            query_count += 1
            assert request.method == "POST"
            assert request.url.params["access_token"] == ACCESS_TOKEN
            assert _form(request) == {"task_id": ["task-1"]}
            if query_count == 1:
                return httpx.Response(
                    200,
                    json={"result": {"status": "running"}},
                )
            return httpx.Response(
                200,
                json={
                    "result": {
                        "status": "success",
                        "markdown_url": (
                            "https://result.bcebos.com/private/task-1.md?"
                            "authorization=signed-secret"
                        ),
                        # The adapter must not select this alternative artifact.
                        "parse_result_url": "https://evil.example/result.json",
                    }
                },
            )
        if request.url.host == "result.bcebos.com":
            assert request.method == "GET"
            return httpx.Response(200, content=b"# parsed\n")
        raise AssertionError("unexpected request")

    client, http_client = _client_for_handler(handler, sleep=fake_sleep)
    try:
        result = await client.recognize_document(
            b"%PDF-private-content",
            "student-alice-private.pdf",
        )
    finally:
        await http_client.aclose()

    assert result.markdown == "# parsed\n"
    assert result.duration_ms >= 0
    assert sleeps == [5.0]
    assert query_count == 2
    assert sum(
        request.url.path.endswith("/unlimited-ocr-parser/task")
        for request in requests
    ) == 1


@pytest.mark.asyncio
async def test_oauth_token_is_cached_in_memory_for_subsequent_documents():
    token_calls = 0
    task_calls = 0

    async def fake_sleep(_seconds: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, task_calls
        if str(request.url).startswith(baidu_tool.TOKEN_ENDPOINT):
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": ACCESS_TOKEN, "expires_in": 3600},
            )
        if request.url.path.endswith("/unlimited-ocr-parser/task"):
            task_calls += 1
            return httpx.Response(
                200,
                json={"result": {"task_id": f"task-{task_calls}"}},
            )
        if request.url.path.endswith("/unlimited-ocr-parser/task/query"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "status": "success",
                        "markdown_url": "https://result.bcebos.com/result.md",
                    }
                },
            )
        return httpx.Response(200, content=b"ok")

    client, http_client = _client_for_handler(handler, sleep=fake_sleep)
    try:
        await client.recognize_document(b"one", "one.txt")
        await client.recognize_document(b"two", "two.txt")
    finally:
        await http_client.aclose()

    assert token_calls == 1
    assert task_calls == 2


@pytest.mark.asyncio
async def test_verify_credentials_fetches_token_without_returning_or_submitting():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if str(request.url).startswith(baidu_tool.TOKEN_ENDPOINT):
            return httpx.Response(
                200,
                json={"access_token": ACCESS_TOKEN, "expires_in": 3600},
            )
        raise AssertionError("credential verification must not submit OCR")

    client, http_client = _client_for_handler(handler)
    try:
        result = await client.verify_credentials()
    finally:
        await http_client.aclose()

    assert result is None
    assert paths == ["/oauth/2.0/token"]


@pytest.mark.asyncio
async def test_submit_transport_failure_is_never_replayed_and_is_projection_safe():
    submit_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submit_calls
        if str(request.url).startswith(baidu_tool.TOKEN_ENDPOINT):
            return httpx.Response(
                200,
                json={"access_token": ACCESS_TOKEN, "expires_in": 3600},
            )
        if request.url.path.endswith("/unlimited-ocr-parser/task"):
            submit_calls += 1
            raise httpx.ReadTimeout(
                f"raw {SECRET_KEY} {ACCESS_TOKEN} student-private.pdf",
                request=request,
            )
        raise AssertionError("query must not run after uncertain submission")

    client, http_client = _client_for_handler(handler)
    try:
        with pytest.raises(BaiduUnlimitedOCRError) as raised:
            await client.recognize_document(b"private-body", "student-private.pdf")
    finally:
        await http_client.aclose()

    assert raised.value.code == "provider_submit_uncertain"
    assert raised.value.retryable is False
    assert str(raised.value) == "provider_submit_uncertain"
    assert raised.value.__cause__ is None
    assert submit_calls == 1
    projected = repr(raised.value)
    assert SECRET_KEY not in projected
    assert ACCESS_TOKEN not in projected
    assert "student-private" not in projected
    assert "private-body" not in projected


@pytest.mark.asyncio
async def test_overall_timeout_during_submit_is_uncertain_and_never_replayed():
    submit_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submit_calls
        if str(request.url).startswith(baidu_tool.TOKEN_ENDPOINT):
            return httpx.Response(
                200,
                json={"access_token": ACCESS_TOKEN, "expires_in": 3600},
            )
        if request.url.path.endswith("/unlimited-ocr-parser/task"):
            submit_calls += 1
            await asyncio.sleep(0.1)
            return httpx.Response(200, json={"result": {"task_id": "task"}})
        raise AssertionError("query must not run after uncertain submission")

    client, http_client = _client_for_handler(
        handler,
        overall_timeout_seconds=0.02,
    )
    try:
        with pytest.raises(BaiduUnlimitedOCRError) as raised:
            await client.recognize_document(b"private-body", "document.pdf")
    finally:
        await http_client.aclose()

    assert raised.value.code == "provider_submit_uncertain"
    assert raised.value.retryable is False
    assert submit_calls == 1


@pytest.mark.asyncio
async def test_submit_rate_limit_is_not_retryable_or_replayed():
    submit_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submit_calls
        if str(request.url).startswith(baidu_tool.TOKEN_ENDPOINT):
            return httpx.Response(
                200,
                json={"access_token": ACCESS_TOKEN, "expires_in": 3600},
            )
        if request.url.path.endswith("/unlimited-ocr-parser/task"):
            submit_calls += 1
            return httpx.Response(
                429,
                json={"error_code": 18, "error_msg": "raw rate detail"},
            )
        raise AssertionError("query must not run after rejected submission")

    client, http_client = _client_for_handler(handler)
    try:
        with pytest.raises(BaiduUnlimitedOCRError) as raised:
            await client.recognize_document(b"body", "document.txt")
    finally:
        await http_client.aclose()

    assert raised.value.code == "provider_rate_limited"
    assert raised.value.retryable is False
    assert submit_calls == 1


@pytest.mark.asyncio
async def test_query_transport_failure_has_bounded_retry():
    query_calls = 0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_calls
        if str(request.url).startswith(baidu_tool.TOKEN_ENDPOINT):
            return httpx.Response(
                200,
                json={"access_token": ACCESS_TOKEN, "expires_in": 3600},
            )
        if request.url.path.endswith("/unlimited-ocr-parser/task"):
            return httpx.Response(200, json={"result": {"task_id": "task"}})
        if request.url.path.endswith("/unlimited-ocr-parser/task/query"):
            query_calls += 1
            if query_calls < 3:
                raise httpx.ConnectError("raw vendor network detail", request=request)
            return httpx.Response(
                200,
                json={
                    "result": {
                        "status": "success",
                        "markdown_url": "https://result.bcebos.com/result.md",
                    }
                },
            )
        return httpx.Response(200, content=b"done")

    client, http_client = _client_for_handler(
        handler,
        sleep=fake_sleep,
        query_max_attempts=3,
    )
    try:
        result = await client.recognize_document(b"body", "document.txt")
    finally:
        await http_client.aclose()

    assert result.markdown == "done"
    assert query_calls == 3
    assert sleeps == [0.5, 1.0]


@pytest.mark.asyncio
async def test_exhausted_query_retry_is_nonretryable_after_submission():
    submit_calls = 0
    query_calls = 0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submit_calls, query_calls
        if str(request.url).startswith(baidu_tool.TOKEN_ENDPOINT):
            return httpx.Response(
                200,
                json={"access_token": ACCESS_TOKEN, "expires_in": 3600},
            )
        if request.url.path.endswith("/unlimited-ocr-parser/task"):
            submit_calls += 1
            return httpx.Response(200, json={"result": {"task_id": "task"}})
        if request.url.path.endswith("/unlimited-ocr-parser/task/query"):
            query_calls += 1
            raise httpx.ConnectError("raw query failure", request=request)
        raise AssertionError("download must not run")

    client, http_client = _client_for_handler(
        handler,
        sleep=fake_sleep,
        query_max_attempts=2,
    )
    try:
        with pytest.raises(BaiduUnlimitedOCRError) as raised:
            await client.recognize_document(b"body", "document.txt")
    finally:
        await http_client.aclose()

    assert raised.value.code == "provider_unreachable"
    assert raised.value.retryable is False
    assert submit_calls == 1
    assert query_calls == 2
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_total_timeout_includes_poll_wait():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(baidu_tool.TOKEN_ENDPOINT):
            return httpx.Response(
                200,
                json={"access_token": ACCESS_TOKEN, "expires_in": 3600},
            )
        if request.url.path.endswith("/unlimited-ocr-parser/task"):
            return httpx.Response(200, json={"result": {"task_id": "task"}})
        return httpx.Response(
            200,
            json={"result": {"status": "running"}},
        )

    client, http_client = _client_for_handler(
        handler,
        overall_timeout_seconds=0.02,
    )
    try:
        with pytest.raises(BaiduUnlimitedOCRError) as raised:
            await client.recognize_document(b"body", "document.txt")
    finally:
        await http_client.aclose()

    assert raised.value.code == "provider_timeout"
    assert raised.value.retryable is False


@pytest.mark.asyncio
async def test_result_download_url_is_fail_closed_without_requesting_unknown_host():
    unknown_host_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal unknown_host_called
        if str(request.url).startswith(baidu_tool.TOKEN_ENDPOINT):
            return httpx.Response(
                200,
                json={"access_token": ACCESS_TOKEN, "expires_in": 3600},
            )
        if request.url.path.endswith("/unlimited-ocr-parser/task"):
            return httpx.Response(200, json={"result": {"task_id": "task"}})
        if request.url.path.endswith("/unlimited-ocr-parser/task/query"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "status": "success",
                        "markdown_url": (
                            "https://attacker.example/private.md?token=secret"
                        ),
                    }
                },
            )
        unknown_host_called = True
        return httpx.Response(200, content=b"must-not-be-read")

    client, http_client = _client_for_handler(handler)
    try:
        with pytest.raises(BaiduUnlimitedOCRError) as raised:
            await client.recognize_document(b"body", "document.txt")
    finally:
        await http_client.aclose()

    assert raised.value.code == "provider_download_url_rejected"
    assert unknown_host_called is False


@pytest.mark.asyncio
async def test_vendor_task_error_and_raw_text_are_not_projected():
    raw_vendor_text = "vendor raw detail with student and secret"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(baidu_tool.TOKEN_ENDPOINT):
            return httpx.Response(
                200,
                json={"access_token": ACCESS_TOKEN, "expires_in": 3600},
            )
        if request.url.path.endswith("/unlimited-ocr-parser/task"):
            return httpx.Response(200, json={"result": {"task_id": "private-task"}})
        return httpx.Response(
            200,
            json={
                "result": {
                    "status": "failed",
                    "task_error": {"error_msg": raw_vendor_text},
                }
            },
        )

    client, http_client = _client_for_handler(handler)
    try:
        with pytest.raises(BaiduUnlimitedOCRError) as raised:
            await client.recognize_document(b"body", "student-name.txt")
    finally:
        await http_client.aclose()

    assert str(raised.value) == "provider_task_failed"
    assert raw_vendor_text not in repr(raised.value)
    assert "private-task" not in repr(raised.value)


@pytest.mark.asyncio
async def test_httpx_access_logs_redact_credentials_tokens_and_signed_urls(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(baidu_tool.TOKEN_ENDPOINT):
            return httpx.Response(
                200,
                json={"access_token": ACCESS_TOKEN, "expires_in": 3600},
            )
        if request.url.path.endswith("/unlimited-ocr-parser/task"):
            return httpx.Response(200, json={"result": {"task_id": "task"}})
        if request.url.path.endswith("/unlimited-ocr-parser/task/query"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "status": "success",
                        "markdown_url": (
                            "https://result.bcebos.com/private-path.md?"
                            "authorization=signed-secret"
                        ),
                    }
                },
            )
        return httpx.Response(200, content=b"safe")

    caplog.set_level(logging.INFO, logger="httpx")
    client, http_client = _client_for_handler(handler)
    try:
        await client.recognize_document(b"body", "student-private.txt")
    finally:
        await http_client.aclose()

    logged = caplog.text
    assert API_KEY not in logged
    assert SECRET_KEY not in logged
    assert ACCESS_TOKEN not in logged
    assert "signed-secret" not in logged
    assert "private-path" not in logged
    assert "student-private" not in logged


@pytest.mark.asyncio
async def test_input_contract_rejects_missing_credentials_unsupported_and_large_image():
    with pytest.raises(BaiduUnlimitedOCRError) as missing:
        BaiduUnlimitedOCRClient(api_key="", secret_key="secret")
    assert missing.value.code == "provider_credentials_required"

    client = BaiduUnlimitedOCRClient(api_key="api", secret_key="secret")
    with pytest.raises(BaiduUnlimitedOCRError) as unsupported:
        client._validate_file(b"body", "document.webp")
    assert unsupported.value.code == "ocr_unsupported_file"

    with pytest.raises(BaiduUnlimitedOCRError) as too_large:
        client._validate_file(b"x" * (MAX_IMAGE_BYTES + 1), "image.png")
    assert too_large.value.code == "ocr_file_too_large"
    assert repr(client) == "BaiduUnlimitedOCRClient(credentials=<redacted>)"
    await client.aclose()


def test_public_client_surface_has_no_file_url_or_registry_configuration():
    signature = inspect.signature(BaiduUnlimitedOCRClient.recognize_document)
    assert list(signature.parameters) == ["self", "file_data", "file_name"]
    source = inspect.getsource(baidu_tool.BaiduUnlimitedOCRClient)
    assert "ExpertRegistry" not in source
    assert "os.getenv" not in source
    assert "backend.config" not in source


class _FakeBaiduClient:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str]] = []

    async def recognize_document(
        self,
        file_data: bytes,
        file_name: str,
    ) -> BaiduUnlimitedOCRDocument:
        self.calls.append((file_data, file_name))
        return BaiduUnlimitedOCRDocument(markdown="  parsed  ", duration_ms=12.0)


@pytest.mark.asyncio
async def test_skill_preserves_layering_supports_document_and_one_image_only():
    fake = _FakeBaiduClient()
    skill = BaiduUnlimitedOCRSkill(client=fake)  # type: ignore[arg-type]

    document = await skill.recognize_document(b"pdf", "private-name.pdf", "problems")
    assert document.text == "parsed"
    assert document.provider == "baidu_unlimited_ocr"
    assert document.model is None
    assert document.duration_ms == 12.0
    assert fake.calls == [(b"pdf", "private-name.pdf")]

    image = await skill.recognize_images(
        [
            OCRImage(
                data=b"png",
                media_type="image/png; charset=binary",
                label="student-alice.png",
            )
        ],
        "submissions",
    )
    assert image.text == "parsed"
    assert fake.calls[-1] == (b"png", "image.png")

    with pytest.raises(BaiduUnlimitedOCRError) as multiple:
        await skill.recognize_images(
            [
                OCRImage(data=b"one", media_type="image/png"),
                OCRImage(data=b"two", media_type="image/png"),
            ],
            "submissions",
        )
    assert multiple.value.code == "ocr_multiple_images_unsupported"


def test_poll_defaults_follow_official_recommendation_and_qps_floor():
    signature = inspect.signature(BaiduUnlimitedOCRClient.__init__)
    assert signature.parameters["poll_interval_seconds"].default >= 5.0
    with pytest.raises(ValueError):
        BaiduUnlimitedOCRClient(
            api_key="api",
            secret_key="secret",
            poll_interval_seconds=4.99,
        )
