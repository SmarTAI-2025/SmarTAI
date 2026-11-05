"""Native contracts exercised only by the real Linux OCI acceptance job."""
import asyncio
import json
import os

import pytest

from backend.config import settings
from backend.models import ProblemInfo, StudentAnswerInfo, TestCase as Case
from backend.services.programming_runs import evidence_for_answer, programming_run
from backend.services.programming_submission_mapping import source_answers
from backend.tools.code_interpreter import run_sandbox
from backend.tools.oci_grading_runner import IsolatedRunnerUnavailable, run_program_oci
from tools.evaluation.programming.replay_u1 import fixture, teacher_cases

pytestmark = pytest.mark.skipif(os.getenv("SMARTAI_OCI_REAL_TEST") != "1", reason="Requires real Linux OCI")


@pytest.fixture(autouse=True)
def oci(monkeypatch):
    monkeypatch.setattr(settings, "program_runner_mode", "oci")
    monkeypatch.setattr(settings, "program_runner_image", os.environ["SMARTAI_PROGRAM_RUNNER_IMAGE"])


@pytest.mark.asyncio
async def test_u1_native_submissions():
    questions = json.loads(fixture("questions.json"))
    submissions = json.loads(fixture("submissions.json"))
    seen_ids = set()
    with programming_run(None, None):
        for q in (q for q in questions if q["language"] in {"c", "cpp"}):
            problem = ProblemInfo(q_id=q["q_id"], number="1", type="编程题", stem=q["stem"],
                                  max_score=10, test_cases=teacher_cases(q), solution_code=fixture(q["reference"]))
            for row in (r for r in submissions if r["q_id"] == q["q_id"]):
                filename = row["source_file"]
                raw, = source_answers(filename, fixture(filename), {problem.q_id:problem.model_dump()}, {problem.q_id})
                evidence = await evidence_for_answer(problem=problem, answer=StudentAnswerInfo.model_validate(raw),
                    student_id=row["submission_id"], task_id="u1", provider=None, reporter=None,
                    generate=None, execute=run_sandbox)
                assert evidence.status == "executed", evidence.reason
                expected = row["expected_observation"]
                assert evidence.report.passed_count == expected.get("passed", 0)
                for case, result in zip(q["cases"], evidence.report.results, strict=True):
                    reason = ("compile_error" if expected.get("compile_error") else
                              "wrong_answer" if case["case_id"] in expected["failed_cases"] else "success")
                    assert result.exit_reason == reason
                    assert result.isolation_mode == "oci_container" and result.execution_id not in seen_ids
                    seen_ids.add(result.execution_id)
    assert len(seen_ids) == 36


@pytest.mark.parametrize("filename", ["answer.c", "answer.cc"])
def test_native_teacher_save_to_grade(monkeypatch, filename):
    from backend.tests.test_programming_saved_grading_flow import exercise_saved_grading_flow
    row = exercise_saved_grading_flow(monkeypatch, io_mode="stdin", native_filename=filename)
    assert row["isolation_mode"] == "oci_container"


@pytest.mark.asyncio
async def test_native_failures_cancellation_and_isolation(monkeypatch):
    async def run(code, timeout=3):
        return await run_program_oci(code, language="c", timeout=timeout)

    assert (await run("int absent(void); int main(void){return absent();}")).exit_reason == "link_error"
    assert (await run("#include <signal.h>\nint main(void){raise(SIGSEGV);}")).exit_reason == "runtime_error"
    assert (await run("int main(void){for(;;){}}", .2)).exit_reason == "timeout"
    assert (await run('#include <stdio.h>\nint main(void){return !fopen("/work/secret","w");}')).passed
    assert (await run('#include <unistd.h>\nint main(void){return access("/work/secret",F_OK)==0;}')).passed
    assert (await run('#include <net/if.h>\nint main(void){return if_nametoindex("eth0")!=0;}')).passed
    task = asyncio.create_task(run("int main(void){for(;;){}}", 30))
    await asyncio.sleep(1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await run("int main(void){return 0;}")).passed
    monkeypatch.setattr(settings, "program_runner_image", "smartai-intentionally-absent:missing")
    with pytest.raises(IsolatedRunnerUnavailable):
        await run("int main(void){return 0;}")
    monkeypatch.setattr(settings, "program_runner_mode", "disabled")
    with pytest.raises(IsolatedRunnerUnavailable):
        await run_sandbox("int main(void){return 0;}", [Case(input="", expected_output="")], language="c")
