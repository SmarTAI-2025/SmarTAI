"""Run-scoped invariants: each student sees the same tests, never another student's result."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from backend.models import ProblemInfo, StudentAnswerInfo, TestCase
from backend.services import programming_runs as runs
from backend.services.grading_checkpoints import CheckpointWriteError
from backend.tools.code_interpreter import ExecutionReport, TestResult as ObservedCase


def question(cases=None, **changes):
    return ProblemInfo(q_id="q7", number="7", type="编程题", stem="Implement the Python function:\ndef add(a, b):",
                       criterion="Award correctness according to the teacher's rule.", test_cases=cases, **changes)


def submission(code="def add(a, b):\n    return a + b\n"):
    return StudentAnswerInfo(q_id="q7", number="7", type="编程题", content=code)


def case(expected="5"):
    return TestCase(function_name="add", io_mode="function", function_args=[2, 3], expected_return=expected)


def observed(cases, passed=True):
    rows = [ObservedCase(test=c, passed=passed, actual_output="5", error="", duration_ms=0.4) for c in cases]
    return ExecutionReport(passed_count=len(cases) if passed else 0, total_count=len(cases),
                           pass_rate=float(passed), results=rows, summary="fixture execution")


def grade(problem, student, generate, execute, answer=None, provider=None):
    return runs.evidence_for_answer(problem=problem, answer=answer or submission(), student_id=student,
        task_id="task", provider=provider or NS(provider_id="expert"), reporter=None,
        generate=generate, execute=execute)


@pytest.mark.asyncio
async def test_concurrent_students_and_experts_share_only_the_right_work():
    primary = NS(provider_id="selected-primary")
    async def make_cases(provider, problem, function_name):
        assert provider is primary and function_name == "add"
        await asyncio.sleep(0)
        return [case()], None
    async def run(code, cases, **kwargs):
        await asyncio.sleep(0)
        return observed(cases)
    generate, execute = AsyncMock(side_effect=make_cases), AsyncMock(side_effect=run)
    problem = question()
    with runs.programming_run(NS(pick_default=lambda:primary), None):
        results = await asyncio.gather(*(grade(problem, str(s), generate, execute)
                                         for s in range(4) for _ in range(3)))
        assert {e.status for e in results} == {"executed"}
        assert len({e.evidence_id for e in results}) == 4
        assert len({e.case_ids for e in results}) == 1
    assert (generate.await_count, execute.await_count) == (1, 4)
    with runs.programming_run(NS(pick_default=lambda:primary), None):
        await grade(problem, "0", generate, execute)
    assert (generate.await_count, execute.await_count) == (2, 5)


@pytest.mark.asyncio
async def test_changed_inputs_never_reuse_evidence_and_teacher_cases_skip_generation():
    generate = AsyncMock(side_effect=AssertionError("teacher cases already exist"))
    execute = AsyncMock(side_effect=lambda code, cases, **_: observed(cases))
    with runs.programming_run(NS(), None):
        first = await grade(question([case()]), "s", generate, execute)
        edited = await grade(question([case("6")]), "s", generate, execute)
        new_source = await grade(question([case()]), "s", generate, execute, submission("def add(a,b): return a-b"))
    assert all(r.status == "executed" for r in (first, edited, new_source))
    assert len({r.evidence_id for r in (first, edited, new_source)}) == 3
    assert execute.await_count == 3


@pytest.mark.asyncio
async def test_reference_conflict_stops_every_student_without_changing_expected():
    p = question([case()], solution_code="def add(a,b): return a-b")
    execute = AsyncMock(side_effect=lambda code, cases, **_: observed(cases, passed=False))
    with runs.programming_run(NS(), None):
        results = await asyncio.gather(*(grade(p, str(i), AsyncMock(), execute) for i in range(5)))
    assert {r.reason for r in results} == {"programming_reference_conflict"}
    assert execute.await_count == 1 and p.test_cases[0].expected_return == "5"


@pytest.mark.asyncio
async def test_ambiguous_or_wrong_runner_result_is_unavailable_for_all_experts():
    execute = AsyncMock(return_value=observed([case("unrelated expected")]))
    with runs.programming_run(NS(), None):
        result = await asyncio.gather(*(grade(question([case()]), "s", AsyncMock(), execute) for _ in range(3)))
    assert {r.status for r in result} == {"unavailable"}
    assert execute.await_count == 1


@pytest.mark.asyncio
async def test_paid_generation_interruption_is_not_blindly_repeated():
    entered = asyncio.Event()
    async def wait(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    generate, execute = AsyncMock(side_effect=wait), AsyncMock()
    with runs.programming_run(NS(), None):
        task = asyncio.create_task(grade(question(), "s1", generate, execute))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        other = await grade(question(), "s2", generate, execute)
    assert other.reason == "programming_preparation_interrupted"
    assert generate.await_count == 1 and execute.await_count == 0


@pytest.mark.asyncio
async def test_leased_checkpoint_reclaim_retry_fresh_and_corruption(monkeypatch):
    events = {}
    cp = NS(run_id="first", worker_id="worker1", manifest={}, questions={"q7":NS(id="q-db",version=4)},
            revisions={"s":"revision-2"})
    monkeypatch.setattr(runs, "current_checkpoints", lambda: cp)
    monkeypatch.setattr(runs.grading_repository, "get_run", lambda rid: NS(id=rid,teacher_id="teacher",assignment_id="task"))
    def write(run_id, *, worker_id, message, payload, **_):
        assert worker_id == cp.worker_id
        events.setdefault(run_id, []).append({"message":message,"payload":deepcopy(payload)})
    monkeypatch.setattr(runs.grading_repository, "record_event", write)
    monkeypatch.setattr(runs.grading_repository, "list_events", lambda rid, actor_id, messages: events.get(rid, []))
    generate = AsyncMock(return_value=([case()], None))
    execute = AsyncMock(side_effect=lambda code, cases, **_:observed(cases))
    with runs.programming_run(NS(), None):
        initial = await grade(question(), "s", generate, execute)
    cp.worker_id = "replacement"
    with runs.programming_run(NS(), None):
        reclaimed = await grade(question(), "s", generate, execute)
    reused = runs.artifacts_for_retry("first", {}, "teacher")
    cp.run_id, cp.manifest = "retry", {"programming_artifacts":reused}
    with runs.programming_run(NS(), None):
        retried = await grade(question(), "s", generate, execute)
    assert initial.evidence_id == reclaimed.evidence_id == retried.evidence_id
    assert generate.await_count == execute.await_count == 1
    broken = deepcopy(reused)
    record_key = next(k for k, ref in broken.items() if ref["kind"] == runs.COMPLETED)
    assert "value" not in broken[record_key]
    broken[record_key]["digest"] = "wrong evidence"
    cp.manifest = {"programming_artifacts":broken}
    with runs.programming_run(NS(), None), pytest.raises(CheckpointWriteError):
        await grade(question(), "s", generate, execute)
    cp.run_id, cp.manifest = "fresh", {}
    with runs.programming_run(NS(), None):
        await grade(question(), "s", generate, execute)
    assert generate.await_count == execute.await_count == 2
