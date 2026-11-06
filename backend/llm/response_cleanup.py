"""Response disposal must never replace received bytes or authorize a retry.

Only response streams and explicitly owned temporary clients are disposed here.
Never close a shared SDK client to recover one failed response-stream close.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import httpx2

logger = logging.getLogger(__name__)
CLEANUP_SECONDS = 10.0
_cleanup_tasks: set[asyncio.Task] = set()


def _retain(task: asyncio.Task) -> None:
    _cleanup_tasks.add(task)

    def finished(done):
        _cleanup_tasks.discard(done)
        if not done.cancelled() and (error := done.exception()) is not None:
            logger.warning("Provider cleanup failed; exception_type=%s", type(error).__name__)

    task.add_done_callback(finished)


def start_cleanup(awaitable, *, seconds: float | None = None, after=None) -> asyncio.Task:
    """Own cleanup independently; even cancellation-resistant close cannot block callers."""
    task = asyncio.create_task(awaitable)
    _retain(task)

    def expired():
        task.cancel()
        logger.warning("Provider cleanup exceeded its time limit")

    timer = asyncio.get_running_loop().call_later(CLEANUP_SECONDS if seconds is None else seconds, expired)

    def finished(done):
        timer.cancel()
        if after is not None:
            _retain(asyncio.create_task(after()))

    task.add_done_callback(finished)
    # Cancellation-resistant cleanup remains retained, with its resource slot,
    # until it actually exits. No request/result waits for that late cleanup.
    return task


class _ResponseStream(httpx.AsyncByteStream, httpx2.AsyncByteStream):
    def __init__(self, stream):
        self._stream = stream
        self._closed = False

    async def __aiter__(self):
        async for chunk in self._stream:
            yield chunk

    async def aclose(self):
        if not self._closed:
            self._closed = True
            # HTTPX waits for this before returning the body to the SDK. Do not
            # spend the response deadline on disposal after reading has ended.
            start_cleanup(self._stream.aclose())


async def protect_response(response) -> None:
    if not isinstance(response.stream, _ResponseStream):
        response.stream = _ResponseStream(response.stream)


def protect_client(client):
    hooks = client.event_hooks["response"]
    if protect_response not in hooks:
        hooks.append(protect_response)
    return client


class GeminiHTTPTransport(httpx.BaseTransport, httpx.AsyncBaseTransport):
    """Google passes the same client_args to sync/async HTTPX constructors.

    An explicit transport selects its supported HTTPX path instead of the
    optional aiohttp path, keeping response cleanup and cancellation consistent.
    The two halves belong exclusively to this one Gemini SDK client.
    """
    def __init__(self, *, proxy=None):
        self._sync = httpx.HTTPTransport(proxy=proxy)
        self._async = httpx.AsyncHTTPTransport(proxy=proxy)

    def handle_request(self, request):
        return self._sync.handle_request(request)

    async def handle_async_request(self, request):
        response = await self._async.handle_async_request(request)
        await protect_response(response)
        return response

    def close(self):
        self._sync.close()

    async def aclose(self):
        await self._async.aclose()
