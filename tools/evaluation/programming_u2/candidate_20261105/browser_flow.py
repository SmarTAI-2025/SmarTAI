"""Drive the candidate UI against the same disposable SQLite rows that it grades."""
import asyncio
from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
import secrets
import socket
import subprocess
from threading import Thread
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from backend.auth import create_token
from backend.db import assignment_repository, grading_repository, workflow_repository
from backend.db.base import Base
from backend.db.provider_repository import upsert_provider_config
from backend.db.session import get_engine
from backend.llm.registry import get_scoped_expert_registry
from backend.main import create_app
from backend.models import ProviderConfig, TaskGradingSetup
from backend.services import grading_runs, programming_runs, task_facade
from backend.skills import programming
from backend.tests.test_programming_scoring_contract import award, response
from backend.tests.test_submission_incremental import _claim
from backend.tests.test_task_background_workflows import _Registry, _seed_task
from backend.tools.oci_grading_runner import IsolatedRunnerUnavailable
from backend.config import settings

# U1 S01 (rolling-state Fibonacci), entirely synthetic.
FIB = """def fib(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a
"""


@contextmanager
def local_application(dist):
    if not (dist / "index.html").is_file():
        raise ValueError("Build the candidate frontend with VITE_SMARTAI_BACKEND_URL=/api")
    app = FastAPI()
    api = create_app()
    api.dependency_overrides[get_scoped_expert_registry] = _Registry
    app.mount("/api", api)
    app.mount("/assets", StaticFiles(directory=dist / "assets"))

    @app.get("/{path:path}")
    def spa(path: str):
        return FileResponse(dist / "index.html")

    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="warning"))
    with socket.create_server(("127.0.0.1", 0)) as listener:
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("U2 loopback server did not start")
                time.sleep(.02)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}", api
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if thread.is_alive():
                raise RuntimeError("U2 loopback server did not stop")


def question(scenario):
    owner, task = _seed_task(with_question=True)
    reference = None
    if scenario.startswith("boundary_"):
        stem = "实现 def double(n): 输入 n 可以为任意整数，返回两倍的值。"
        case = dict(function_name="double", function_args=[3], expected_return="6", source="teacher")
        reference = "```python\ndef double(n): return 2*n\n```"
    elif scenario == "edit":
        stem = "实现 def fib(n): 返回第 n 项。"
        case = dict(function_name="fib", function_args=[0], expected_return="0", source="teacher")
    else:
        stem = "Read stdin and print the input."
        case = dict(input="4", expected_output="4", source="llm_generated" if scenario == "tentative" else "teacher")
    task_facade.update_problem(task_id=task, owner_id=owner, q_id="q1", patch={
        "type": "programming", "stem": stem,
        "criterion": "Full credit when all teacher examples pass." if reference else "Award correctness and method.",
        **({"reference_answer": reference} if reference else {}), "review_status": "edited", "test_cases": [case]})
    workflow_repository.update_workflow(task, owner_id=owner, presentation_status="problems_ready")
    return owner, task


async def grade(owner, task, scenario, api):
    def revision():
        return workflow_repository.get_live_workflow(task, owner_id=owner).workflow_revision

    source = FIB if scenario == "edit" else "print(0)"
    if scenario.startswith("boundary_"):
        source = "def double(n): return 0 if n == -1 else 2*n"
    # Submit a real .py multipart body through the mounted teacher API.
    registry = _Registry()
    registry.provider = SimpleNamespace(provider_id="test-provider", supports_vision=False,
                                        ainvoke=AsyncMock(side_effect=AssertionError("unexpected recognition call")))
    client = TestClient(api, headers={"Authorization": "Bearer " + create_token(owner, "teacher")})
    with patch.object(task_facade, "_registry_for_owner", return_value=registry):
        queued = client.post(f"/tasks/{task}/parse_submissions",
            files={"file": ("S1_U2.py", source.encode(), "text/x-python")},
            data={"recognition_provider_id": "test-provider"})
        assert queued.status_code == 200, queued.text
        await task_facade.run_durable_submission_recognition(_claim(owner, queued.json()["job_id"]))
    saved = client.get(f"/tasks/{task}").json()["student_data"]["S1"]["stu_ans"]
    assert len(saved) == 1 and saved[0]["q_id"] == "q1" and saved[0]["content"] == source
    registry.provider.ainvoke.assert_not_awaited()
    provider = upsert_provider_config(owner, ProviderConfig(provider_type="openai", model="fixture",
        api_key=secrets.token_hex(24)), master_key=settings.provider_encryption_key)
    setup = TaskGradingSetup(selected_provider_ids=[provider.id], primary_provider_id=provider.id,
                             multi_sample_n=1, knowledge_scope="none")
    workflow_repository.update_workflow(task, owner_id=owner, grading_setup=setup.model_dump(mode="json"),
                                        grading_setup_fingerprint="confirmed")
    score = {"edit": 10, "tentative": 6, "zero": 0, "platform": 0, "boundary_full": 10, "boundary_deduction": 8}[scenario]
    output = award(score)
    if scenario.startswith("boundary_"):
        output["boundary_case"] = {"args": [-1], "expected": "-2"}
    invoke = AsyncMock(return_value=response(output))
    route = SimpleNamespace(provider_id=provider.id, model="fixture", ainvoke=invoke)
    registry = SimpleNamespace(count=lambda: 1, select=lambda *a, **kw: registry,
        list_available=lambda: [route], get=lambda _: route, pick_default=lambda: route)
    fault = (patch.object(programming, "run_sandbox", AsyncMock(side_effect=IsolatedRunnerUnavailable()))
             if scenario == "platform" else nullcontext())
    with fault, patch.object(grading_runs, "_registry_for", return_value=registry):
        job = task_facade.start_task_grading(task_id=task, owner_id=owner, expected_workflow_revision=revision(),
                                             request_id="u2-" + scenario)["job_id"]
        await grading_runs.process_run(run_id=job, worker_id="u2", registry=registry)
    assert invoke.await_count == (0 if scenario == "platform" else 1)
    if scenario == "edit":
        event, = grading_repository.list_events(job, actor_id=owner, messages=(programming_runs.COMPLETED,))
        row, = event["payload"]["value"]["rows"]
        assert row["passed"] and row["actual_output"] == "5", row
    result = task_facade.task_results(task_id=task, owner_id=owner)
    actual = result["results"][0]["corrections"][0]
    assert actual["score"] == (None if scenario == "platform" else score)


def verify_browser(candidate, *, boundary=False):
    Base.metadata.create_all(get_engine())
    app_dir = candidate / "frontend/app"
    with local_application(app_dir / "dist") as (origin, api):
        for scenario in (("boundary_full", "boundary_deduction") if boundary else ("edit", "platform", "tentative", "zero")):
            owner, task = question(scenario)
            if scenario != "edit":
                asyncio.run(grade(owner, task, scenario, api))
            # The scoped test token travels over stdin only.
            subprocess.run(["node", str(Path(__file__).with_name("browser.mjs")), str(app_dir)], check=True,
                input=json.dumps(dict(origin=origin, scenario=scenario, task=task,
                                      token=create_token(owner, "teacher"))), text=True, timeout=60)
            if scenario == "edit":
                stored, = assignment_repository.list_questions(task, teacher_id=owner)[0].test_cases
                assert stored["function_args"] == [5] and stored["expected_return"] == "5"
                asyncio.run(grade(owner, task, scenario, api))
            print(f"PASS browser/{scenario}", flush=True)
