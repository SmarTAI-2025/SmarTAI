"""Observe current U2 HTTP/SQLite contracts; never call a real provider or runner.

Run with the repository's Python and --source <checkout> --output <new folder>.
This is an acceptance probe, not an implementation of the pending programming work.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--readback", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    if args.readback:
        context = json.loads(args.readback.read_text())
        runtime = Path(context["runtime"])
    else:
        output.mkdir(parents=True, exist_ok=False)
        runtime = Path(tempfile.mkdtemp(prefix="smartai-u2-workflow-"))
    # Use only public runtime settings; no .env, BYOK, production database or storage.
    if (source / ".env").exists() or (source / "backend/.env").exists():
        raise SystemExit("Use a clean checkout without .env files")
    keep = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "SYSTEMROOT") if key in os.environ}
    os.environ.clear()
    os.environ.update(keep)
    os.environ.update({
        "SMARTAI_RUNTIME_ENVIRONMENT": "test", "SMARTAI_GRADING_ENGINE": "v2",
        "SMARTAI_DATABASE_URL": f"sqlite:///{runtime / 'audit.db'}",
        "SMARTAI_DATABASE_HEAVY": "OFF", "SMARTAI_DATABASE_AUTO_CREATE": "true",
        "SMARTAI_STORAGE_ROOT": str(runtime / "uploads"), "SMARTAI_SEED_TEST_USERS": "false",
        "SMARTAI_PROVIDER_ENCRYPTION_KEY": "test-suite-provider-master-key-0123456789abcdef",
        "SMARTAI_JWT_SECRET": "test-suite-jwt-secret-0123456789abcdef",
        "SMARTAI_HTTP_PROXY": "", "SMARTAI_HTTPS_PROXY": "",
    })
    os.chdir(source)
    sys.path.insert(0, str(source))
    blocked = []
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    def guard(method):
        def connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                blocked.append("network_attempt")
                raise OSError("U2 probe denies network")
            return method(sock, address)
        return connect
    socket.socket.connect = guard(real_connect)
    socket.socket.connect_ex = guard(real_connect_ex)
    real_getaddrinfo = socket.getaddrinfo
    def guarded_dns(host, *positional, **keywords):
        if host not in (None, "localhost", "127.0.0.1", "::1"):
            blocked.append("dns_attempt")
            raise OSError("U2 probe denies external DNS")
        return real_getaddrinfo(host, *positional, **keywords)
    socket.getaddrinfo = guarded_dns
    def process_guard(event, values):
        if event in {"os.system", "os.posix_spawn"}:
            blocked.append("process_attempt")
            raise RuntimeError("U2 workflow probe forbids arbitrary subprocesses")
        if event == "subprocess.Popen":
            command = values[1]
            allowed_readback = (isinstance(command, (list, tuple))
                and command[:2] == [sys.executable, str(Path(__file__).resolve())]
                and "--readback" in command)
            if command != ["git", "rev-parse", "HEAD"] and not allowed_readback:
                blocked.append("process_attempt")
                raise RuntimeError("U2 workflow probe forbids runner execution")
    sys.addaudithook(process_guard)

    from fastapi.testclient import TestClient
    from backend.auth import create_token
    from backend.main import create_app
    from backend.db.base import Base
    from backend.db import assignment_repository as assignments, workflow_repository as workflows
    from backend.db.session import get_engine
    from backend.models import Correction, ExpertResult, ProblemInfo, ProviderConfig, TaskGradingSetup
    from backend.db.provider_repository import upsert_provider_config
    from backend.services import task_facade, grading_adapter, grading_runs
    from backend.llm.registry import get_scoped_expert_registry
    from backend.tests.test_task_background_workflows import _seed_task, _Registry

    Base.metadata.create_all(get_engine())
    app = create_app()
    app.dependency_overrides[get_scoped_expert_registry] = lambda: _Registry()
    client = TestClient(app)  # No lifespan worker: queued uploads cannot invoke providers.
    def headers(owner):
        return {"Authorization": "Bearer " + create_token(owner, "teacher")}
    if args.readback:
        response = client.get(f"/tasks/{context['task']}", headers=headers(context["owner"]))
        response.raise_for_status()
        print(json.dumps(response.json()["problem_data"], ensure_ascii=False))
        return

    started = time.perf_counter()
    observations = []
    def observe(name, **data):
        observations.append({"id": name, **data})
    owner, task = _seed_task(with_question=True)
    assignments.add_question(task, teacher_id=owner, q_id="q2", order_index=1, number="2",
        type="programming", stem="Implement def count_positive(nums).", criterion="Award 10 points for correctness.", max_score=10)
    workflows.update_workflow(task, owner_id=owner, presentation_status="problems_ready")
    def snapshot():
        response = client.get(f"/tasks/{task}", headers=headers(owner))
        response.raise_for_status()
        return response.json()
    before = snapshot()
    cases = [{"input": "2\n3\n", "expected_output": "5", "source": "teacher",
              "description": "synthetic exact saved case", "io_mode": "stdin"}]
    updated = client.put(f"/tasks/{task}/problems/q1", headers=headers(owner), json={
        "expected_workflow_revision": before["workflow_revision"], "type": "programming",
        "stem": "Read two integers and print their sum.", "criterion": "Correct sum 8 points; input/output 2 points.",
        "reference_answer": "a=int(input()); b=int(input()); print(a+b)",
        "solution_code": "a=int(input()); b=int(input()); print(a+b)", "test_cases": cases})
    updated.raise_for_status()
    after = snapshot()
    assert after["problem_data"]["q1"]["test_cases"] == cases
    assert after["problem_data"]["q2"] == before["problem_data"]["q2"]
    observe("HTTP_SAVE", status=updated.status_code, cases_exact=True, other_question_unchanged=True,
            revision_before=before["workflow_revision"], revision_after=after["workflow_revision"])
    stale = client.put(f"/tasks/{task}/problems/q1", headers=headers(owner), json={
        "expected_workflow_revision": before["workflow_revision"], "test_cases": [{"input": "stale"}]})
    assert stale.status_code == 409
    assert snapshot()["problem_data"]["q1"]["test_cases"] == cases
    observe("STALE_SAVE", status=stale.status_code, body=stale.json(), saved_cases_unchanged=True)
    context_file = output / "readback-context.json"
    context_file.write_text(json.dumps({"runtime": str(runtime), "task": task, "owner": owner}))
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--source", str(source),
        "--output", str(output), "--readback", str(context_file)], capture_output=True, text=True, timeout=60)
    if child.returncode:
        raise RuntimeError("Fresh-process read failed: " + child.stderr[-2000:])
    reread = json.loads(child.stdout.strip().splitlines()[-1])
    assert reread == after["problem_data"]
    observe("FRESH_PROCESS_READ", exact=True, database="disposable SQLite", mocked_storage=False)

    # Exact payload observed from the existing function-case editor by the browser probe.
    function_owner, function_task = _seed_task(with_question=True)
    function_case = {"input": "[5]", "expected_output": "5", "function_name": "fib", "function_args": [0],
        "expected_return": "0", "io_mode": "function", "source": "teacher", "sandbox_feasible": True}
    saved_function = client.put(f"/tasks/{function_task}/problems/q1", headers=headers(function_owner), json={
        "type": "programming", "test_cases": [function_case]})
    saved_function.raise_for_status()
    function_question = assignments.list_questions(function_task, teacher_id=function_owner)[0]
    execution_input = grading_adapter.build_problem_store([function_question])["q1"]
    typed_case = ProblemInfo.model_validate(execution_input).test_cases[0]
    observe("FUNCTION_EDITOR_FIELDS", http_status=saved_function.status_code,
        stored_case=function_question.test_cases[0], grading_input_case=typed_case.model_dump(mode="json"),
        actual_execution="not performed", effective_function_args=typed_case.function_args,
        effective_expected_return=typed_case.expected_return,
        requested_text_input="[5]", requested_text_expected="5")

    # Queue through mounted public routes; do not confuse queued with recognized.
    for name, mime in (("S01.py", "text/x-python"), ("S01.txt", "text/plain")):
        upload_owner, upload_task = _seed_task(with_question=True)
        response = client.post(f"/tasks/{upload_task}/parse_submissions", headers=headers(upload_owner),
            files={"file": (name, b"def fib(n):\n    return n\n", mime)},
            data={"recognition_provider_id": "test-provider"})
        observe("UPLOAD_" + name.rsplit('.', 1)[-1].upper(), status=response.status_code,
            body=response.json(), recognition="not run: real model prohibited", actual_model_calls=0)

    # Prepared structured submissions are explicit fixtures, not model recognition.
    assignment = assignments.get_assignment(task, actor_id=owner)
    students = [{"stu_id": "S001", "stu_name": "Synthetic", "source_id": "",
        "identity_status": "matched", "identity_match_method": "filename", "stu_ans": [
            {"q_id": "q2", "number": "2", "type": "programming", "content": "def count_positive(nums): return 0", "flag": []},
            {"q_id": "q1", "number": "1", "type": "programming", "content": "a=int(input()); b=int(input()); print(a+b)", "flag": []}]}]
    task_facade._commit_imported_submissions(task_id=task, owner_id=owner, course_id=assignment.course_id,
        students=students, expected_workflow_revision=snapshot()["workflow_revision"], submission_file_name="synthetic.txt")
    provider = upsert_provider_config(owner, ProviderConfig(provider_type="openai", api_key="u2-synthetic-never-used",
        model="u2-fixed", base_url="https://api.openai.com/v1"), master_key=os.environ["SMARTAI_PROVIDER_ENCRYPTION_KEY"])
    setup = TaskGradingSetup(selected_provider_ids=[provider.id], primary_provider_id=provider.id, knowledge_scope="none")
    workflows.update_workflow(task, owner_id=owner, grading_setup=setup.model_dump(mode="json"), grading_setup_fingerprint="approved")
    class Registry:
        def select(self, *_args, **_kwargs): return self
        def count(self): return 1
    captured = []
    async def fixed_batch(**kwargs):
        captured.append(kwargs)
        return [{"student_id": sid, "corrections": [Correction(q_id=qid, type="programming",
            score=10 if qid == "q1" else 4, max_score=10, confidence=1,
            synthesis_method="single", comment="Fixed observation; not a real model grade", steps=[])
            for qid in kwargs["problem_store"]]} for sid in kwargs["student_store"]]
    with patch.object(grading_runs, "_registry_for", lambda _: Registry()), patch.object(grading_adapter, "grade_batch", fixed_batch):
        run = task_facade.start_task_grading(task_id=task, owner_id=owner,
            expected_workflow_revision=snapshot()["workflow_revision"], request_id="u2-fixed-run")
        asyncio.run(grading_runs.process_run(run_id=run["job_id"], worker_id="u2-local"))
    assert len(captured) == 1
    assert captured[0]["problem_store"]["q1"]["test_cases"] == cases
    results = task_facade.task_results(task_id=task, owner_id=owner)
    assert results["results"]
    observe("GRADING_INPUT_AND_RESULTS", saved_cases_equal_batch_input=True, batch_invocations=1,
        batch_provider="fixed response", model_calls=0, execution="not performed", result=results)

    async def fixed_platform_failure(**kwargs):
        return [{"student_id": sid, "corrections": [Correction(q_id=qid, type="programming",
            score=10 if qid == "q1" else 0, max_score=10, confidence=1 if qid == "q1" else 0,
            synthesis_method="single" if qid == "q1" else "all_failed",
            comment="Fixed platform failure; no OCI execution", steps=[],
            expert_results=[] if qid == "q1" else [ExpertResult(provider="u2-fixed", score=0,
                max_score=10, confidence=0, comment="Isolated runner unavailable",
                error_kind="programming_execution_unavailable")])
            for qid in kwargs["problem_store"]]} for sid in kwargs["student_store"]]
    with patch.object(grading_runs, "_registry_for", lambda _: Registry()), patch.object(grading_adapter, "grade_batch", fixed_platform_failure):
        failed_run = task_facade.start_task_grading(task_id=task, owner_id=owner,
            expected_workflow_revision=snapshot()["workflow_revision"], request_id="u2-fixed-platform-failure")
        asyncio.run(grading_runs.process_run(run_id=failed_run["job_id"], worker_id="u2-local"))
    failed_results = task_facade.task_results(task_id=task, owner_id=owner)
    failed_correction = next(c for s in failed_results["results"] for c in s["corrections"] if c["q_id"] == "q2")
    assert failed_results["grading_run_status"] == "partial_failed"
    assert failed_correction["score"] is None and failed_correction["requires_human_review"]
    assert not task_facade.finalization(task_id=task, owner_id=owner)["ready_for_confirmation"]
    observe("PLATFORM_FAILURE_PERSISTENCE", fixture="explicit all_failed correction with programming_execution_unavailable",
        grading_run_status=failed_results["grading_run_status"], correction=failed_correction,
        finalization=task_facade.finalization(task_id=task, owner_id=owner),
        execution="not performed; only failure-result persistence tested")

    # Probe malformed teacher data separately, without altering the successful task.
    invalid_owner, invalid_task = _seed_task(with_question=True)
    malformed = [{"function_name": "fib", "function_args": "not-a-list", "expected_return": 1}]
    response = client.put(f"/tasks/{invalid_task}/problems/q1", headers=headers(invalid_owner), json={
        "type": "programming", "test_cases": malformed})
    validation_error, saved_malformed = None, None
    if response.status_code == 200:
        saved = task_facade.get_task(task_id=invalid_task, owner_id=invalid_owner)["problem_data"]["q1"]
        saved_malformed = saved.get("test_cases")
        try:
            ProblemInfo.model_validate(saved)
        except Exception as exc:
            validation_error = type(exc).__name__
    observe("MALFORMED_CASE_SAVE", status=response.status_code,
        invalid_case_persisted=(saved_malformed == malformed and validation_error is not None),
        saved_cases=saved_malformed, later_problem_validation_error=validation_error, body=response.json())
    report = {"schema_version": 1, "source_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "evidence_type": "real mounted HTTP + SQLite; fixed grading batch; no model/OCI",
        "python": sys.version.split()[0], "runtime": str(runtime), "observations": observations,
        "local_probe_wall_seconds": round(time.perf_counter() - started, 3), "blocked_io_attempts": blocked,
        "real_model_calls": 0, "real_oci_executions": 0, "cost": {"incurred_by_this_probe": 0, "production_estimate": None}}
    (output / "workflow.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"observations": len(observations), "output": str(output), "blocked_io_attempts": blocked}))
    if blocked:
        raise SystemExit("Rejected network attempt: observations are not a clean offline run")


if __name__ == "__main__":
    main()
