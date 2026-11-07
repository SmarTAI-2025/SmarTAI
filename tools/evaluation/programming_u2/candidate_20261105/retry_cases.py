"""Observe which fixed add(a,b) calls the real dispatcher repeats after interruption."""
import asyncio
from collections import Counter
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from backend.models import ProblemInfo, StudentAnswerInfo, TestCase
from backend.services import programming_runs as runs
from backend.skills import programming
from backend.tools import code_interpreter


async def verify_retry(interruption):
    cases = [TestCase(function_name="add", function_args=[a, b], expected_return=str(expected))
             for a, b, expected in [(2, 3, 5), (3, 4, 7)]]
    problem = ProblemInfo(q_id="q1", number="1", type="编程题", stem="Implement def add(a,b):",
                          criterion="Return the exact sum.", max_score=10, test_cases=cases)
    answer = StudentAnswerInfo(q_id="q1", number="1", type="编程题", content="def add(a,b): return a+b")
    checkpoint = SimpleNamespace(run_id="first", worker_id="worker", manifest={}, questions={},
                                 revisions={"s": "revision-1"})

    def run_owner(run_id):
        return SimpleNamespace(id=run_id, teacher_id="teacher", assignment_id="assignment")

    events, calls = {}, []
    first_done = asyncio.Event()
    original = code_interpreter._run_case

    def record(run_id, *, message, payload, **_):
        events.setdefault(run_id, []).append({"message": message, "payload": deepcopy(payload)})

    async def per_case(code, test, *args, **kwargs):
        inputs = tuple(test.function_args)
        calls.append((checkpoint.run_id, inputs))
        if interruption == "second_case_failure" and checkpoint.run_id == "first" and inputs == (3, 4):
            await asyncio.wait_for(first_done.wait(), timeout=5)
            raise runs.IsolatedRunnerUnavailable()
        row = await original(code, test, *args, **kwargs)
        assert row.passed
        if inputs == (2, 3):
            first_done.set()
        return row

    async def cancel_after_last(*args, on_case_complete, **kwargs):
        task = asyncio.current_task()
        recorded = []

        async def complete(row):
            await on_case_complete(row)
            recorded.append(row.case_index)
            if len(recorded) == len(cases):
                task.cancel()

        return await programming.run_sandbox(*args, on_case_complete=complete, **kwargs)

    cancel_after_last.supports_case_checkpoints = getattr(programming.run_sandbox, "supports_case_checkpoints", False)

    async def grade(execute):
        return await runs.evidence_for_answer(problem=problem, answer=answer, student_id="s",
                                              task_id="assignment", provider=None, reporter=None,
                                              generate=None, execute=execute)

    # Only the private checkpoint repository is in memory. Use the actual
    # preparation, binding, per-case subprocess, comparison and retry manifest.
    with (patch.object(runs, "current_checkpoints", lambda: checkpoint),
          patch.object(runs.grading_repository, "get_run", side_effect=run_owner),
          patch.object(runs.grading_repository, "list_events", lambda rid, **_: events.get(rid, [])),
          patch.object(runs.grading_repository, "record_event", record),
          patch.object(code_interpreter, "_run_case", per_case)):
        with runs.programming_run(SimpleNamespace(), None):
            if interruption == "second_case_failure":
                first = await grade(programming.run_sandbox)
                assert first.status == "unavailable" and first.report is None
            else:
                work = asyncio.create_task(grade(cancel_after_last))
                try:
                    await asyncio.wait_for(work, timeout=10)
                except asyncio.CancelledError:
                    assert work.cancelled()
                else:
                    raise AssertionError("expected cancellation after both callbacks")
        checkpoint.manifest = {"programming_artifacts": runs.artifacts_for_retry("first", {}, "teacher")}
        checkpoint.run_id = "retry"
        with runs.programming_run(SimpleNamespace(), None):
            recovered = await grade(programming.run_sandbox)
    assert Counter(args for stage, args in calls if stage == "first") == Counter([(2, 3), (3, 4)])
    expected = [(3, 4)] if interruption == "second_case_failure" else []
    assert [args for stage, args in calls if stage == "retry"] == expected, calls
    assert recovered.status == "executed" and recovered.report.passed_count == 2
    assert [(r.case_index, r.case_id, r.passed) for r in recovered.report.results] == [
        (0, "case-1", True), (1, "case-2", True)]
