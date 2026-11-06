"""Whole-attempt clocks preserve completed units and fence cancelled owners."""
import asyncio
import time

import pytest
from sqlalchemy import update

from backend.db import knowledge_ingestion_repository as repo
from backend.db.models import KnowledgeIngestionRecord
from backend.db.session import session_scope
from backend.knowledge.ingestion import KnowledgeIngestionWorker
from backend.services import workflow_deadlines as limits
from backend.tests.test_knowledge_ingestion import queued


@pytest.mark.asyncio
async def test_knowledge_timeout_retains_saved_page_and_resume_gets_fresh_clock(monkeypatch):
    owner, document, job_id = queued()
    saved, stopped = asyncio.Event(), asyncio.Event()
    deadlines = []
    original_timeout = asyncio.timeout
    def capture(seconds):
        deadline = original_timeout(seconds)
        if seconds is not None and 7100 < seconds <= 7200:
            deadlines.append(deadline)
        return deadline
    monkeypatch.setattr(asyncio, "timeout", capture)
    worker = KnowledgeIngestionWorker()
    async def process(job, reporter):
        repo.initialize(job, 2)
        page = repo.next_pages(job)[0]
        repo.prepare_page(job, page.id, batch_extra_remaining=0)
        repo.finish_page(job, page.id, text="Retained evidence", state="searchable",
                         evidence={"artifact_ids": [], "usage": {}})
        saved.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    monkeypatch.setattr(worker, "_process_job", process)
    task = asyncio.create_task(worker.run_once(job_id))
    try:
        await asyncio.wait_for(saved.wait(), 2)
        deadlines[0].reschedule(asyncio.get_running_loop().time())
        assert await asyncio.wait_for(task, 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    manifest = repo.manifest(document, owner)
    summary = manifest["summary"]
    assert stopped.is_set()
    assert summary["status"] == "paused" and summary["error_code"] == "knowledge_wall_time_limit_reached"
    assert summary["processed_pages"] == 1 and summary["searchable_pages"] == 1
    assert summary["execution_deadline"]["completed_at"] >= summary["execution_deadline"]["started_at"]
    assert [page["state"] for page in manifest["pages"]] == ["searchable", "unprocessed"]
    repo.resume(document, owner)
    retried = repo.claim_next(job_id=job_id)
    assert retried.configuration[limits.CHECKPOINT_KEY]["started_at"] > summary["execution_deadline"]["started_at"]
    assert repo.manifest(document, owner)["summary"]["searchable_pages"] == 1
    repo.release(retried)


def test_knowledge_queue_is_free_but_batch_release_and_reclaim_keep_deadline():
    owner, document, job_id = queued()
    with session_scope() as session:
        session.execute(update(KnowledgeIngestionRecord).where(KnowledgeIngestionRecord.id == job_id)
                        .values(created_at=time.time() - 86400))
    first = repo.claim_next(job_id=job_id)
    clock = first.configuration[limits.CHECKPOINT_KEY]
    assert clock["deadline_at"] > time.time() + 7190
    repo.release(first)
    second = repo.claim_next(job_id=job_id)
    assert second.configuration[limits.CHECKPOINT_KEY] == clock
    with session_scope() as session:
        session.execute(update(KnowledgeIngestionRecord).where(KnowledgeIngestionRecord.id == job_id)
                        .values(lease_expires_at=time.time() - 1))
    recovered = repo.claim_next(job_id=job_id)
    assert recovered.configuration[limits.CHECKPOINT_KEY] == clock
    repo.release(recovered)


@pytest.mark.asyncio
async def test_no_active_work_cutoff_or_periodic_timing_save(monkeypatch):
    # Advance active work well past the removed 2.5h cap while the 3.5h owner
    # still permits work. No timer/task is created by active-work bookkeeping.
    from backend.services.submission_work_budget import active_work
    original = asyncio.get_running_loop().time
    offset = [0]
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: original() + offset[0])
    before = asyncio.all_tasks()
    async with limits.processing_budget("synthetic", kind="grading") as guard:
        async with active_work():
            offset[0] = 9001
            await asyncio.sleep(0)
            guard.check()
        assert not guard.exhausted.is_set()
        assert asyncio.all_tasks() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", [False, True])
async def test_knowledge_heartbeat_completion_race_and_lease_loss_do_not_stop_worker(monkeypatch, lost):
    from types import SimpleNamespace
    from backend.knowledge import ingestion
    from backend.domain.errors import LeaseLost
    owner, document, job_id = queued()
    started, tick, finish, stopped = (asyncio.Event() for _ in range(4))
    proxy = SimpleNamespace(**vars(asyncio))
    async def sleep(seconds):
        assert seconds == 60
        await tick.wait()
    proxy.sleep = sleep
    monkeypatch.setattr(ingestion, "asyncio", proxy)
    def heartbeat(job):
        if lost:
            raise LeaseLost()
        finish.set()
        return False
    monkeypatch.setattr(repo, "heartbeat", heartbeat)
    worker = KnowledgeIngestionWorker()
    async def process(job, reporter):
        started.set()
        try:
            await finish.wait()
        finally:
            stopped.set()
    monkeypatch.setattr(worker, "_process_job", process)
    task = asyncio.create_task(worker.run_once(job_id))
    try:
        await asyncio.wait_for(started.wait(), 2)
        tick.set()
        assert await asyncio.wait_for(task, 2)
        assert stopped.is_set() and not task.cancelled() and task.cancelling() == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_synchronous_source_timeout_preserves_checkpoint_and_allows_retry(monkeypatch):
    import json
    from backend.api.task_preparation import _timed_question_request
    from backend.db import workflow_repository as flows
    from backend.domain.errors import InvalidTransition
    from backend.services.submission_work_budget import current_budget
    from backend.tests.test_task_background_workflows import _seed_task
    owner, task_id = _seed_task()
    records = []
    deadlines = []
    original_timeout = asyncio.timeout
    def capture(seconds):
        deadline = original_timeout(seconds)
        if seconds is not None and 8900 < seconds <= 9000:
            deadlines.append(deadline)
        return deadline
    monkeypatch.setattr(asyncio, "timeout", capture)
    @_timed_question_request
    async def preflight():
        row, _ = flows.create_operation(assignment_id=task_id, owner_id=owner,
            operation_type="problem_source", input_hash="synthetic-source", initial_status="preparing")
        current_budget().source_operation = row
        row = flows.save_operation_checkpoint(row.id, owner_id=owner, expected_attempt=row.attempt,
            expected_checkpoint_revision=0, stage="reading", checkpoint={"completed_page": "retained"})
        records.append(row)
        deadlines[0].reschedule(asyncio.get_running_loop().time())
        await asyncio.Event().wait()
    response = await preflight()
    assert response.status_code == 504
    assert json.loads(response.body)["error"]["code"] == "question_preparation_wall_time_limit_reached"
    row = flows.get_operation(records[0].id, owner_id=owner)
    assert row.status == "error" and row.error_code == "question_preparation_wall_time_limit_reached"
    assert row.checkpoint["completed_page"] == "retained"
    with pytest.raises(InvalidTransition):
        flows.update_operation(row.id, owner_id=owner, expected_attempt=row.attempt, status="ready")
    retried, _ = flows.create_operation(assignment_id=task_id, owner_id=owner,
        operation_type="problem_source", input_hash="synthetic-source", initial_status="preparing")
    assert retried.attempt == row.attempt + 1 and retried.terminal_summary is None
