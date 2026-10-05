"""Race the two durable claim paths against the same real PostgreSQL database."""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.config import settings
from backend.db import grading_repository as grades
from backend.domain.errors import LeaseLost
from backend.tests.test_postgres_integration import pg_database  # noqa: F401
from backend.tests.test_task_execution_control import another_task, claim, seed_grade
from backend.tests.test_workflow_worker import _seed_operation

pytestmark = pytest.mark.skipif(not os.environ.get("SMARTAI_TEST_POSTGRES_URL"), reason="Requires disposable PostgreSQL")


def race(calls):
    gate = threading.Barrier(len(calls))
    def run(fn):
        gate.wait(timeout=5)
        try:
            fn()
            return "running"
        except LeaseLost as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


def test_postgres_one_owner_across_two_claimers(pg_database, monkeypatch):
    monkeypatch.setattr(settings, "workload_max_in_flight", 2)
    owner, task, op = _seed_operation()
    next_task, _ = another_task(owner, task)
    run_id = seed_grade(owner, next_task)
    results = race([lambda: claim(owner, op), lambda: grades.claim_lease(run_id, worker_id="grader", lease_seconds=60)])
    assert sorted(results) == ["owner_task_running", "running"]


def test_postgres_global_capacity_is_shared_across_users_and_workers(pg_database, monkeypatch):
    monkeypatch.setattr(settings, "workload_max_in_flight", 2)
    owner1, task1, op1 = _seed_operation()
    owner2, task2, _ = _seed_operation()
    owner3, task3, op3 = _seed_operation()
    run_id = seed_grade(owner2, task2)
    results = race([lambda: claim(owner1, op1),
                    lambda: grades.claim_lease(run_id, worker_id="grader", lease_seconds=60),
                    lambda: claim(owner3, op3)])
    assert sorted(results) == ["running", "running", "server_capacity_full"]
