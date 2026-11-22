"""Bound prepared grading contexts without changing provider admission or scoring."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager

from backend.llm.concurrency import HARD_LIMIT


class _PreparationWindow:
    """Share the same bound, alternating queued reviews and initial grades."""

    def __init__(self):
        self.active = 0
        self.waiters = (deque(), deque())
        self.prefer_review = True

    def _wake(self):
        while self.active < HARD_LIMIT:
            for queue in self.waiters:
                while queue and queue[0].cancelled():
                    queue.popleft()
            initial, review = self.waiters
            if not initial and not review:
                return
            use_review = bool(review) and (not initial or self.prefer_review)
            waiter = self.waiters[int(use_review)].popleft()
            self.prefer_review = not use_review
            self.active += 1
            waiter.set_result(None)

    async def acquire(self, review):
        waiter = asyncio.get_running_loop().create_future()
        queue = self.waiters[int(review)]
        queue.append(waiter)
        self._wake()
        try:
            await waiter
        except asyncio.CancelledError:
            if not waiter.cancelled():
                # A permit can be granted just before the waiting task is cancelled.
                self.release()
            else:
                if waiter in queue:
                    queue.remove(waiter)
                self._wake()
            raise

    def release(self):
        self.active -= 1
        self._wake()


@asynccontextmanager
async def prepared_grading(provider, *, review=False):
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
    entry = windows.setdefault(key, [_PreparationWindow(), 0])
    entry[1] += 1
    try:
        await entry[0].acquire(review)
        try:
            yield
        finally:
            entry[0].release()
    finally:
        entry[1] -= 1
        if not entry[1]:
            del windows[key]
