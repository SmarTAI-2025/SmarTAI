"""Explicit asyncio time advances with an independent real-time hang guard."""

import asyncio
from contextlib import contextmanager
from threading import Timer
from types import SimpleNamespace

import pytest


@contextmanager
def manual_clock(monkeypatch, *, watchdog_seconds=30):
    loop = asyncio.get_running_loop()
    owner = asyncio.current_task()
    clock = SimpleNamespace(now=loop.time())
    active, expired = True, False

    def expire():
        nonlocal expired
        if active and not owner.done():
            expired = True
            owner.cancel()

    # asyncio.wait_for would use the frozen clock too, so cannot guard a hang.
    watchdog = Timer(watchdog_seconds, lambda: loop.call_soon_threadsafe(expire))
    watchdog.daemon = True
    with monkeypatch.context() as patch:
        patch.setattr(loop, "time", lambda: clock.now)
        watchdog.start()
        try:
            yield clock
        except asyncio.CancelledError:
            if expired:
                pytest.fail("Controlled-clock probe exceeded its real-time watchdog")
            raise
        finally:
            active = False
            watchdog.cancel()
            watchdog.join()
