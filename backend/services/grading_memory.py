"""Bound prepared grading contexts without changing provider admission or scoring."""
import asyncio
from contextlib import asynccontextmanager

from backend.llm.concurrency import HARD_LIMIT


@asynccontextmanager
async def prepared_grading(provider):
    # A separate window per provider lets a fast expert finish other answers
    # while another is cooling down. The window is never below the existing
    # maximum number of actual model calls. Entries live only while in use and
    # contain no client or credential; cancellation also releases the entry.
    loop = asyncio.get_running_loop()
    windows = getattr(loop, "_smartai_grading_windows", None)
    if windows is None:
        windows = {}
        loop._smartai_grading_windows = windows
    # Owner/quota guards are ephemeral; their underlying runtime model is not.
    # Do not key by a public model name, which can conflate independent clients.
    identity = getattr(provider, "preparation_identity", provider)
    key = id(identity)
    entry = windows.setdefault(key, [asyncio.Semaphore(HARD_LIMIT), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if not entry[1]:
            del windows[key]
