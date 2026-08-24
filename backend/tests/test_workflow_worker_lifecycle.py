from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient


def test_app_starts_and_stops_empty_workflow_worker(monkeypatch):
    from backend.services import workflow_worker

    events: list[object] = []

    class FakeWorker:
        def __init__(self, *, handlers):
            events.append(("constructed", dict(handlers)))

        async def run_forever(self):
            events.append("run_started")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("run_cancelled")
                raise

        def stop(self):
            events.append("stop")

        async def shutdown(self):
            events.append("shutdown")

    monkeypatch.setattr(workflow_worker, "WorkflowWorker", FakeWorker)

    from backend.main import create_app

    app = create_app()
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert ("constructed", {}) in events
        assert "run_started" in events

    assert events.index("stop") < events.index("run_cancelled")
    assert events.index("run_cancelled") < events.index("shutdown")


def test_workflow_worker_lifecycle_never_bulk_releases_leases(monkeypatch):
    from backend.db import workflow_repository
    from backend.services import workflow_worker

    release_calls: list[tuple] = []
    monkeypatch.setattr(
        workflow_repository,
        "release_operation",
        lambda *args, **kwargs: release_calls.append((args, kwargs)),
    )

    class FakeWorker:
        def __init__(self, *, handlers):
            assert dict(handlers) == {}

        async def run_forever(self):
            await asyncio.Event().wait()

        def stop(self):
            return None

        async def shutdown(self):
            return None

    monkeypatch.setattr(workflow_worker, "WorkflowWorker", FakeWorker)

    from backend.main import create_app

    with TestClient(create_app()):
        pass

    assert release_calls == []
