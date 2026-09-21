"""Scoped regression tests preserved from PR 79."""
import asyncio


def test_failed_duplicate_lease_claim_does_not_remove_active_reporter(monkeypatch):
    from backend.domain.errors import LeaseLost
    from backend.progress import tracker
    from backend.services import grading_runs
    run_id = "synthetic-live-run"
    reporter = tracker.get_or_create_reporter(run_id, 1, 1)
    def reject_claim(**_kwargs):
        raise LeaseLost("run_not_claimable")
    monkeypatch.setattr(grading_runs.grading_repository, "claim_lease", reject_claim)
    try:
        asyncio.run(grading_runs.process_run(run_id=run_id, worker_id="duplicate-worker"))
        assert tracker.get_reporter(run_id) is reporter
    finally:
        tracker.remove_reporter(run_id)


def test_stale_worker_cannot_remove_replacement_reporter():
    from backend.progress import tracker
    run_id = "synthetic-reclaimed-run"
    old = tracker.get_or_create_reporter(run_id, 1, 1)
    tracker.remove_reporter(run_id)
    current = tracker.get_or_create_reporter(run_id, 1, 1)
    try:
        tracker.remove_reporter(run_id, expected=old)
        assert tracker.get_reporter(run_id) is current
        tracker.remove_reporter(run_id, expected=current)
        assert tracker.get_reporter(run_id) is None
    finally:
        tracker.remove_reporter(run_id)
