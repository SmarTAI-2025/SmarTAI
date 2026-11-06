"""The OCR token, submit, poll and download paths share the cleanup guarantee."""
import asyncio
import json

import httpx
import pytest

from backend.tools import baidu_unlimited_ocr as baidu
from backend.tests.test_provider_response_deadlines import turns


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["error", "self_cancel", "hang"])
async def test_complete_ocr_response_survives_stream_and_owned_client_close(fault, monkeypatch):
    requests, closes = [], []
    release = asyncio.Event()

    async def close():
        closes.append(True)
        if fault == "error":
            raise RuntimeError("synthetic disposal error")
        if fault == "self_cancel":
            raise asyncio.CancelledError()
        await release.wait()

    async def send(self, request):
        requests.append(request.url.path)
        if str(request.url).startswith(baidu.TOKEN_ENDPOINT):
            payload = {"access_token": "fixture", "expires_in": 3600}
        elif request.url.path.endswith("/task"):
            payload = {"result": {"task_id": "fixture"}}
        else:
            payload = {"result": {"status": "success", "markdown_url": "https://result.bcebos.com/result.md"}}
        data = b"# complete" if request.method == "GET" else json.dumps(payload).encode()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield data

            async def aclose(self):
                await close()

        return httpx.Response(200, stream=Stream())

    async def sleep(seconds):
        pass

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    client = baidu.BaiduUnlimitedOCRClient(api_key="fixture", secret_key="fixture", sleep=sleep)
    original_close = client._http_client.aclose
    monkeypatch.setattr(client._http_client, "aclose", close)
    try:
        async with client:
            result = await asyncio.wait_for(client.recognize_document(b"%PDF-fixture", "fixture.pdf"), 2)
        assert result.markdown == "# complete"
        assert len(requests) == 4  # Token, one paid submission, one poll, one download.
        await turns()
        assert len(closes) == 5
    finally:
        release.set()
        await turns()
        await original_close()
