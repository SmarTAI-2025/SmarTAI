"""Smoke tests for the v2 task-centric workflow.

Mocks the LLM provider so tests don't require API keys / network.
Run with:
    /opt/anaconda3/envs/smartai/bin/python -m pytest backend/tests/test_tasks.py -v
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Import order matters: backend.main triggers HTTP_PROXY env var setup.
# We set up proxy=empty for tests via an env override.
import os
os.environ["SMARTAI_HTTP_PROXY"] = ""
os.environ["SMARTAI_HTTPS_PROXY"] = ""

from backend.main import app
from backend.state import (
    get_task_store, get_problem_store, get_student_store, get_job_store,
)
from backend.api.analytics import _last_query_at, _cm_cache


TEACHER_TOKEN = "demo-teacher-pytestuser"
TEACHER_HEADERS = {"Authorization": f"Bearer {TEACHER_TOKEN}"}
TEACHER_ID = "demo_pytestuser"


@pytest.fixture(autouse=True)
def reset_state():
    """Reset all in-memory stores between tests."""
    get_task_store()._tasks.clear()
    get_problem_store().clear()
    get_student_store().clear()
    get_job_store()._active.clear()
    get_job_store()._history.clear()
    _last_query_at.clear()
    _cm_cache.clear()
    yield
    # Re-clear after
    get_task_store()._tasks.clear()


@pytest.fixture
def client():
    return TestClient(app)


# ─── CRUD ────────────────────────────────────────────────────────────────────

def test_create_task(client):
    r = client.post("/tasks/", headers=TEACHER_HEADERS, json={"name": "Pytest 1"})
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"].startswith("T_")
    assert body["status"] == "draft"
    assert body["name"] == "Pytest 1"
    assert body["owner_id"] == TEACHER_ID


def test_list_tasks_owner_scoped(client):
    client.post("/tasks/", headers=TEACHER_HEADERS, json={"name": "A"})
    client.post("/tasks/", headers=TEACHER_HEADERS, json={"name": "B"})
    # Different demo user → empty list for them
    other_token = "demo-teacher-someone_else"
    r_other = client.get("/tasks/", headers={"Authorization": f"Bearer {other_token}"})
    assert r_other.status_code == 200
    assert r_other.json() == {}
    # Original user sees both
    r = client.get("/tasks/", headers=TEACHER_HEADERS)
    assert len(r.json()) == 2


def test_delete_task(client):
    r = client.post("/tasks/", headers=TEACHER_HEADERS, json={"name": "X"})
    tid = r.json()["task_id"]
    r2 = client.delete(f"/tasks/{tid}", headers=TEACHER_HEADERS)
    assert r2.status_code == 200
    r3 = client.get(f"/tasks/{tid}", headers=TEACHER_HEADERS)
    assert r3.status_code == 404


def test_update_task_name(client):
    r = client.post("/tasks/", headers=TEACHER_HEADERS, json={"name": "Old"})
    tid = r.json()["task_id"]
    r2 = client.put(f"/tasks/{tid}", headers=TEACHER_HEADERS, json={"name": "New"})
    assert r2.status_code == 200
    assert r2.json()["name"] == "New"


# ─── Idempotency: extract problems ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_extract_idempotent_same_hash(monkeypatch):
    """Same file uploaded twice → second call returns already_running or already_done."""
    # Mock the LLM extraction to immediately succeed
    async def fake_extract(text, provider, store, reporter=None):
        store.clear()
        store["q1"] = {"q_id": "q1", "number": "1", "type": "概念题", "stem": "x", "criterion": "y"}
        if reporter:
            await reporter.set_phase("done")
        return store

    monkeypatch.setattr("backend.api.tasks.extract_problems", fake_extract)
    monkeypatch.setattr(
        "backend.llm.registry.ExpertRegistry.pick_default",
        lambda self: MagicMock(provider_id="mock"),
    )

    client = TestClient(app)

    # 1st: create
    r = client.post("/tasks/", headers=TEACHER_HEADERS, json={"name": "Idem"})
    tid = r.json()["task_id"]

    # 1st upload
    r1 = client.post(
        f"/tasks/{tid}/extract_problems",
        headers=TEACHER_HEADERS,
        files={"file": ("hw.txt", b"problem 1\nproblem 2", "text/plain")},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["status"] == "started"

    # Wait for the background task to finish
    await asyncio.sleep(0.2)

    # 2nd upload (same file) → already_done because status moved to problems_ready
    r2 = client.post(
        f"/tasks/{tid}/extract_problems",
        headers=TEACHER_HEADERS,
        files={"file": ("hw.txt", b"problem 1\nproblem 2", "text/plain")},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "already_done"
    assert r2.json()["unchanged"] is True


def test_extract_different_file_starts_fresh(monkeypatch):
    """Different file content → status='started' even if status is problems_ready."""
    async def fake_extract(text, provider, store, reporter=None):
        store.clear()
        store["q1"] = {"q_id": "q1", "number": "1", "type": "概念题", "stem": "x", "criterion": "y"}
        if reporter:
            await reporter.set_phase("done")
        return store

    monkeypatch.setattr("backend.api.tasks.extract_problems", fake_extract)
    monkeypatch.setattr(
        "backend.llm.registry.ExpertRegistry.pick_default",
        lambda self: MagicMock(provider_id="mock"),
    )

    client = TestClient(app)
    tid = client.post("/tasks/", headers=TEACHER_HEADERS, json={"name": "Diff"}).json()["task_id"]

    client.post(
        f"/tasks/{tid}/extract_problems",
        headers=TEACHER_HEADERS,
        files={"file": ("a.txt", b"AAA", "text/plain")},
    )
    import time as _time
    _time.sleep(0.3)  # let bg task finish

    r2 = client.post(
        f"/tasks/{tid}/extract_problems",
        headers=TEACHER_HEADERS,
        files={"file": ("b.txt", b"BBB", "text/plain")},  # different content
    )
    assert r2.json()["status"] == "started"


# ─── Task isolation ──────────────────────────────────────────────────────────

def test_task_isolation(monkeypatch):
    """Two tasks should not share problem_data. Verifies de-globalization."""
    fake_problems_a = {"q1": {"q_id": "q1", "number": "1", "type": "概念题", "stem": "AAA stem", "criterion": ""}}
    fake_problems_b = {"q1": {"q_id": "q1", "number": "1", "type": "计算题", "stem": "BBB stem", "criterion": ""}}

    counter = {"n": 0}

    async def fake_extract(text, provider, store, reporter=None):
        counter["n"] += 1
        store.clear()
        if counter["n"] == 1:
            store.update(fake_problems_a)
        else:
            store.update(fake_problems_b)
        if reporter:
            await reporter.set_phase("done")
        return store

    monkeypatch.setattr("backend.api.tasks.extract_problems", fake_extract)
    monkeypatch.setattr(
        "backend.llm.registry.ExpertRegistry.pick_default",
        lambda self: MagicMock(provider_id="mock"),
    )

    client = TestClient(app)

    tid_a = client.post("/tasks/", headers=TEACHER_HEADERS, json={"name": "A"}).json()["task_id"]
    client.post(
        f"/tasks/{tid_a}/extract_problems", headers=TEACHER_HEADERS,
        files={"file": ("a.txt", b"A_FILE", "text/plain")},
    )
    import time as _time
    _time.sleep(0.3)

    tid_b = client.post("/tasks/", headers=TEACHER_HEADERS, json={"name": "B"}).json()["task_id"]
    client.post(
        f"/tasks/{tid_b}/extract_problems", headers=TEACHER_HEADERS,
        files={"file": ("b.txt", b"B_FILE", "text/plain")},
    )
    _time.sleep(0.3)

    full_a = client.get(f"/tasks/{tid_a}", headers=TEACHER_HEADERS).json()
    full_b = client.get(f"/tasks/{tid_b}", headers=TEACHER_HEADERS).json()
    assert full_a["problem_data"]["q1"]["stem"] == "AAA stem"
    assert full_b["problem_data"]["q1"]["stem"] == "BBB stem"


def test_grade_blocked_until_submissions(client):
    """Trying to grade a draft task → 409."""
    r = client.post("/tasks/", headers=TEACHER_HEADERS, json={"name": "X"})
    tid = r.json()["task_id"]
    r2 = client.post(f"/tasks/{tid}/grade", headers=TEACHER_HEADERS, json={"language": "en"})
    assert r2.status_code == 409


# ─── Analytics rate limit + schema validation ────────────────────────────────

def test_analytics_rate_limit(monkeypatch):
    """30s cooldown enforced — second NL query within window → 429."""
    # Stub: graded task with results
    from backend.state import get_task_store, get_job_store
    from backend.models import Task, GradingJob
    import time

    task = Task(task_id="T_test", name="t", owner_id=TEACHER_ID, status="graded",
                problem_data={"q1": {"q_id": "q1", "number": "1", "type": "概念题", "stem": "?", "criterion": ""}},
                grading_job_id="job_test")
    get_task_store().create(task)
    job = GradingJob(job_id="job_test", job_name="t", job_type="batch", status="completed",
                     results={"results": [], "task_id": "T_test"})
    get_job_store()._history["job_test"] = job

    # Stub the LLM call
    async def fake_filter(*, question, results_payload, problem_data, provider, **kw):
        from backend.agents.analytics_agent import FilterOutput
        return FilterOutput(student_ids=[], explanation="ok")

    monkeypatch.setattr("backend.agents.analytics_agent.filter_students", fake_filter)
    monkeypatch.setattr(
        "backend.llm.registry.ExpertRegistry.pick_default",
        lambda self: MagicMock(provider_id="mock"),
    )

    client = TestClient(app)
    r1 = client.post(
        "/analytics/T_test/query",
        headers=TEACHER_HEADERS,
        json={"question": "q?", "mode": "filter"},
    )
    assert r1.status_code == 200

    r2 = client.post(
        "/analytics/T_test/query",
        headers=TEACHER_HEADERS,
        json={"question": "q?", "mode": "filter"},
    )
    assert r2.status_code == 429


def test_chart_schema_rejects_unknown_trace(monkeypatch):
    """LLM that returns unknown trace type → ChartOutput validation fails."""
    from backend.agents.analytics_agent import ChartOutput
    bad_payload = {
        "title": "Bad",
        "rationale": "?",
        "traces": [{"type": "scattergeo", "x": [1], "y": [2]}],  # unknown type
        "layout": {},
    }
    with pytest.raises(Exception):
        ChartOutput.model_validate(bad_payload)


def test_chart_schema_accepts_valid_bar():
    from backend.agents.analytics_agent import ChartOutput
    ok = ChartOutput.model_validate({
        "title": "Score by question",
        "rationale": "Comparison",
        "traces": [{"type": "bar", "x": ["q1", "q2"], "y": [5, 7]}],
        "layout": {"title": "Score", "barmode": "group"},
    })
    assert ok.traces[0].type == "bar"
    assert ok.layout.barmode == "group"


def test_per_question_breakdown():
    """Deterministic per-question aggregation."""
    from backend.agents.analytics_agent import per_question_breakdown
    payload = {"results": [
        {"student_id": "S1", "student_name": "A",
         "corrections": [{"q_id": "1", "score": 8, "max_score": 10, "comment": "nice"}],
         "student_answers": [{"q_id": "1", "content": "answer A"}]},
        {"student_id": "S2", "student_name": "B",
         "corrections": [{"q_id": "1", "score": 4, "max_score": 10, "comment": "weak"}],
         "student_answers": [{"q_id": "1", "content": "answer B"}]},
    ]}
    out = per_question_breakdown("1", payload, {"1": {"q_id": "1", "number": "1", "type": "概念题", "stem": "stem"}})
    assert out["stats"]["n"] == 2
    assert out["stats"]["avg"] == 6.0
    assert out["stats"]["pct_avg"] == 60.0
    assert len(out["rows"]) == 2
    assert out["rows"][0]["answer"] == "answer A"


# ─── Reference / test-case upload (auxiliary endpoints) ──────────────────────

def _seed_problems(client, tid: str):
    """Helper: synthesize problems_ready state without going through extract LLM."""
    store = get_task_store()
    t = store.get(tid)
    t.problem_data["q1"] = {
        "q_id": "q1", "number": "1", "type": "计算题",
        "stem": "Compute 6 * 7", "criterion": "Final value 42",
    }
    t.problem_data["q2"] = {
        "q_id": "q2", "number": "2", "type": "编程题",
        "stem": "Read two ints, print sum", "criterion": "Correctness",
    }
    store.update(tid, status="problems_ready")


@pytest.mark.asyncio
async def test_upload_reference_idempotent_same_hash(monkeypatch):
    """Same reference file uploaded twice → 2nd call returns already_done."""
    async def fake_parse_ref(text, problems_data, provider, reporter=None):
        # Simulate a successful parse — match q1.
        return {"q1": "42"}

    monkeypatch.setattr("backend.api.tasks.parse_reference_to_per_question", fake_parse_ref)
    monkeypatch.setattr(
        "backend.llm.registry.ExpertRegistry.pick_default",
        lambda self: MagicMock(provider_id="mock"),
    )

    client = TestClient(app)
    r = client.post("/tasks/", headers=TEACHER_HEADERS, json={"name": "Ref idem"})
    tid = r.json()["task_id"]
    _seed_problems(client, tid)

    body = b"answer to q1: 42"
    r1 = client.post(
        f"/tasks/{tid}/upload_reference",
        headers=TEACHER_HEADERS,
        files={"file": ("ans.md", body, "text/markdown")},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["status"] == "started"

    # Wait for background worker
    await asyncio.sleep(0.2)

    # Confirm the merge happened
    full = client.get(f"/tasks/{tid}", headers=TEACHER_HEADERS).json()
    assert full["problem_data"]["q1"]["reference_answer"] == "42"
    assert full["reference_file_name"] == "ans.md"

    # Same file again → already_done
    r2 = client.post(
        f"/tasks/{tid}/upload_reference",
        headers=TEACHER_HEADERS,
        files={"file": ("ans.md", body, "text/markdown")},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "already_done"


@pytest.mark.asyncio
async def test_upload_reference_different_file_starts_fresh(monkeypatch):
    """Uploading a NEW file with different content → status='started'."""
    counter = {"calls": 0}

    async def fake_parse_ref(text, problems_data, provider, reporter=None):
        counter["calls"] += 1
        return {"q1": "v" + str(counter["calls"])}

    monkeypatch.setattr("backend.api.tasks.parse_reference_to_per_question", fake_parse_ref)
    monkeypatch.setattr(
        "backend.llm.registry.ExpertRegistry.pick_default",
        lambda self: MagicMock(provider_id="mock"),
    )

    client = TestClient(app)
    r = client.post("/tasks/", headers=TEACHER_HEADERS, json={"name": "Ref fresh"})
    tid = r.json()["task_id"]
    _seed_problems(client, tid)

    r1 = client.post(
        f"/tasks/{tid}/upload_reference",
        headers=TEACHER_HEADERS,
        files={"file": ("a.md", b"first", "text/markdown")},
    )
    assert r1.json()["status"] == "started"
    await asyncio.sleep(0.2)

    r2 = client.post(
        f"/tasks/{tid}/upload_reference",
        headers=TEACHER_HEADERS,
        files={"file": ("b.md", b"second", "text/markdown")},
    )
    assert r2.json()["status"] == "started"
    await asyncio.sleep(0.2)
    assert counter["calls"] == 2  # both files actually parsed


def test_upload_reference_in_draft_blocked(monkeypatch):
    """Reference upload before problems exist → 409."""
    monkeypatch.setattr(
        "backend.llm.registry.ExpertRegistry.pick_default",
        lambda self: MagicMock(provider_id="mock"),
    )
    client = TestClient(app)
    r = client.post("/tasks/", headers=TEACHER_HEADERS, json={"name": "Draft"})
    tid = r.json()["task_id"]
    r2 = client.post(
        f"/tasks/{tid}/upload_reference",
        headers=TEACHER_HEADERS,
        files={"file": ("x.md", b"x", "text/markdown")},
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_upload_test_cases_idempotent(monkeypatch):
    """Same test-case file uploaded twice → already_done; merges into q2."""
    from backend.models import TestCase as TC

    async def fake_parse_tc(text, problems_data, provider, reporter=None):
        return {
            "q2": [TC(input="1\n2", expected_output="3", description="1+2", source="teacher")]
        }

    monkeypatch.setattr("backend.api.tasks.parse_test_cases_to_per_question", fake_parse_tc)
    monkeypatch.setattr(
        "backend.llm.registry.ExpertRegistry.pick_default",
        lambda self: MagicMock(provider_id="mock"),
    )

    client = TestClient(app)
    r = client.post("/tasks/", headers=TEACHER_HEADERS, json={"name": "TC idem"})
    tid = r.json()["task_id"]
    _seed_problems(client, tid)

    body = b'{"q2": [{"input":"1\\n2","expected_output":"3"}]}'
    r1 = client.post(
        f"/tasks/{tid}/upload_test_cases",
        headers=TEACHER_HEADERS,
        files={"file": ("cases.json", body, "application/json")},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["status"] == "started"
    await asyncio.sleep(0.2)

    # The cases should be stored as list[dict] for JSON safety
    full = client.get(f"/tasks/{tid}", headers=TEACHER_HEADERS).json()
    cases = full["problem_data"]["q2"]["test_cases"]
    assert isinstance(cases, list)
    assert cases[0]["input"] == "1\n2"
    assert cases[0]["source"] == "teacher"
    assert full["test_cases_file_name"] == "cases.json"

    r2 = client.post(
        f"/tasks/{tid}/upload_test_cases",
        headers=TEACHER_HEADERS,
        files={"file": ("cases.json", body, "application/json")},
    )
    assert r2.json()["status"] == "already_done"


# ─── Sandbox semaphore concurrency cap ───────────────────────────────────────

@pytest.mark.asyncio
async def test_sandbox_semaphore_caps_concurrency():
    """20 concurrent sandbox calls should never exceed the configured limit."""
    from backend.tools.sandbox_runtime import init_sandbox_semaphore
    from backend.tools.code_interpreter import run_python_subprocess

    # Reset to a known small limit for the test
    init_sandbox_semaphore(limit=4)

    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def fake_subprocess_communicate(*a, **kw):
        # Simulate a small amount of work — held while inside the semaphore
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        try:
            await asyncio.sleep(0.05)
        finally:
            async with lock:
                active -= 1
        # Return (stdout, stderr) as bytes
        return b"ok\n", b""

    # Patch only communicate so that the semaphore + subprocess machinery still runs
    with patch(
        "asyncio.subprocess.Process.communicate", new=fake_subprocess_communicate
    ), patch(
        "asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=MagicMock(
            communicate=fake_subprocess_communicate,
            returncode=0,
            kill=MagicMock(),
            wait=AsyncMock(),
        )),
    ):
        await asyncio.gather(*[
            run_python_subprocess("print(1)", "", timeout=2.0)
            for _ in range(20)
        ])

    assert peak <= 4, f"sandbox peak concurrency {peak} exceeded limit 4"
    # And we should have actually fanned out — sanity check
    assert peak >= 2, f"sandbox peak {peak} too low; fan-out broken"

    # Restore the default for any subsequent tests in this session
    init_sandbox_semaphore(limit=8)

