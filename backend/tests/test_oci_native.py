"""Native contracts exercised only by the real Linux OCI acceptance job."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from backend.config import settings
from backend.models import ProblemInfo, StudentAnswerInfo, TestCase as Case
from backend.services.programming_runs import evidence_for_answer, programming_run
from backend.services.programming_submission_mapping import source_answers
from backend.tools.code_interpreter import run_sandbox
from backend.tools.oci_grading_runner import IsolatedRunnerUnavailable, run_program_oci
from backend.skills.programming_evidence import describe_run
from tools.evaluation.programming.replay_u1 import U1, fixture, teacher_cases

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
    observations = []
    with programming_run(None, None):
        for q in (q for q in questions if q["language"] in {"c", "cpp"}):
            problem = ProblemInfo(q_id=q["q_id"], number="1", type="编程题", stem=q["stem"],
                                  max_score=10, criterion="\n".join(q["teacher_rules_draft"]), test_cases=teacher_cases(q), solution_code=fixture(q["reference"]))
            for row in (r for r in submissions if r["q_id"] == q["q_id"]):
                filename = row["source_file"]
                if filename:
                    raw, = source_answers(filename, fixture(filename), {problem.q_id: problem.model_dump()}, {problem.q_id})
                else:
                    raw = dict(q_id=problem.q_id, number="1", type="编程题", content="")
                evidence = await evidence_for_answer(problem=problem, answer=StudentAnswerInfo.model_validate(raw),
                    student_id=row["submission_id"], task_id="u1", provider=None, reporter=None,
                    generate=None, execute=run_sandbox)
                zh, examples = describe_run(evidence, "zh", problem)
                en, _ = describe_run(evidence, "en", problem)
                cases = []
                observations.append({"submission_id": row["submission_id"], "source_file": filename,
                    "question": problem.q_id, "criterion": problem.criterion, "execution": evidence.status,
                    "evidence": zh.model_dump(), "safe_summary_en": en.safe_summary, "examples": examples,
                    "cases": cases})
                if not filename:
                    assert evidence.status == "no_submission" and zh.primary_reason == "no_submission"
                    continue
                assert evidence.status == "executed", evidence.reason
                expected = row["expected_observation"]
                assert evidence.report.passed_count == expected.get("passed", 0)
                for case, result in zip(q["cases"], evidence.report.results, strict=True):
                    reason = ("compile_error" if expected.get("compile_error") else
                              "wrong_answer" if case["case_id"] in expected["failed_cases"] else "success")
                    assert result.exit_reason == reason
                    assert result.isolation_mode == "oci_container" and result.execution_id not in seen_ids
                    if reason == "compile_error":
                        assert result.error.startswith("missing_semicolon line "), result.error
                        assert examples[0]["diagnostic"] == "missing_semicolon" and examples[0]["line"] > 0
                    seen_ids.add(result.execution_id)
                    cases.append({"case_id": case["case_id"], "input": case["input"], "expected": case["expected"],
                                  "actual": result.actual_output, "passed": result.passed, "outcome": result.exit_reason,
                                  "diagnostic": result.error if reason == "compile_error" else None,
                                  "execution_id": result.execution_id, "request_digest": result.request_digest,
                                  "isolation_mode": result.isolation_mode, "duration_ms": result.duration_ms})
    assert len(observations) == 8
    if target := os.environ.get("SMARTAI_U1_NATIVE_EVIDENCE"):
        with Path(target).open("x", encoding="utf-8") as output:
            json.dump({"fixture_commit": U1, "code_commit": os.environ.get("GITHUB_SHA"),
                       "image_id": settings.program_runner_image, "provider_calls": 0,
                       "observations": observations}, output, indent=2, ensure_ascii=False)
    assert len(seen_ids) == 36


@pytest.mark.parametrize("filename", ["answer.c", "answer.cc"])
def test_native_teacher_save_to_grade(monkeypatch, filename):
    from backend.tests.test_programming_saved_grading_flow import exercise_saved_grading_flow
    row = exercise_saved_grading_flow(monkeypatch, io_mode="stdin", native_filename=filename)
    assert row["isolation_mode"] == "oci_container"


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["teacher_zero", "platform", "ai_only_confirm"])
async def test_native_review_and_release(monkeypatch, scenario):
    from backend.tests.test_programming_scoring_contract import exercise_review_and_release
    await exercise_review_and_release(monkeypatch, scenario, native=True)


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
    if broken := os.environ.get("SMARTAI_TEST_BROKEN_CC_IMAGE"):
        from backend.tools.oci_grading_runner import NativeBuildUnavailable
        monkeypatch.setattr(settings, "program_runner_image", broken)
        with pytest.raises(NativeBuildUnavailable) as failure:
            await run("int main(void){return 0;}")
        assert failure.value.reason == "toolchain_unavailable"
    monkeypatch.setattr(settings, "program_runner_image", "smartai-intentionally-absent:missing")
    with pytest.raises(IsolatedRunnerUnavailable):
        await run("int main(void){return 0;}")
    monkeypatch.setattr(settings, "program_runner_mode", "disabled")
    with pytest.raises(IsolatedRunnerUnavailable):
        await run_sandbox("int main(void){return 0;}", [Case(input="", expected_output="")], language="c")


@pytest.mark.asyncio
@pytest.mark.parametrize("language,header,macro,expected", [
    ("c", "stdio.h", "__STDC_VERSION__", "201112"),
    ("cpp", "cstdio", "__cplusplus", "201703"),
])
async def test_native_compiler_standards(language, header, macro, expected):
    source = f'#include <{header}>\nint main(void){{printf("%ld", (long){macro});}}'
    report = await run_sandbox(source, [Case(input="", expected_output=expected)], language=language)
    assert report.passed_count == 1 and report.results[0].actual_output == expected


@pytest.mark.asyncio
async def test_merged_python_reference_and_interface_review(monkeypatch):
    problem = ProblemInfo(q_id="q1", number="1", type="编程题", stem="首个整数 n，其后有 n 个整数，输出它们之和。",
                          criterion="Sum.", solution_code="print(-2)", test_cases=[Case(input="1 -2", expected_output="-2")])
    answer = StudentAnswerInfo(q_id="q1", number="1", type="编程题", content="print(sum([1,2])")
    run = await evidence_for_answer(problem=problem, answer=answer, student_id="synthetic", task_id="merged",
                                   provider=None, reporter=None, generate=None, execute=run_sandbox)
    assert run.report.results[0].exit_reason == "syntax_error"
    assert run.report.results[0].isolation_mode == "oci_container"
    from backend.tests.test_programming_scoring_contract import exercise_review_and_release
    await exercise_review_and_release(monkeypatch, "entry_only")


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["count.c", "count.cpp"])
async def test_native_signed_count_preserves_literal_record(filename):
    record = " \t-2147483648,0,+2147483647   \r\n"
    expected = f"1 {len(record)}"
    problem = ProblemInfo(q_id="q1", number="1", type="programming",
                          stem="Count negative integers and report the input byte length.",
                          criterion="Exact count and length.",
                          test_cases=[Case(input=record, expected_output=expected, comparison="exact")])
    source = """#include <stdio.h>
int main(void) {
    char buf[100];
    size_t n = fread(buf, 1, sizeof(buf)-1, stdin);
    buf[n] = 0;
    long long a,b,c;
    if (sscanf(buf, "%lld,%lld,%lld", &a,&b,&c) != 3) return 1;
    printf("%d %zu", (a<0)+(b<0)+(c<0), n);
}"""
    for program, outcome in [(source, "success"), (source.replace("b<0", "b<=0"), "wrong_answer")]:
        raw, = source_answers(filename, program, {"q1": problem.model_dump()}, {"q1"})
        evidence = await evidence_for_answer(problem=problem, answer=StudentAnswerInfo.model_validate(raw),
            student_id="fixture", task_id="signed-count", provider=None, reporter=None, generate=None, execute=run_sandbox)
        result, = evidence.report.results
        assert result.test.input == record and result.exit_reason == outcome
        assert result.actual_output == (expected if outcome == "success" else f"2 {len(record)}")


@pytest.mark.asyncio
async def test_ambiguous_missing_header_cannot_count_as_student_zero():
    from backend.tools.oci_grading_runner import NativeBuildUnavailable
    with pytest.raises(NativeBuildUnavailable) as failure:
        await run_program_oci('#include "course_fixture_missing.h"\nint main(void){return 0;}', language="c")
    assert failure.value.reason == "native_header_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("filename,source,marker", [
    ("solve.py", "print(1 // int(input()))", "ZeroDivisionError"),
    ("solve.c", "#include <signal.h>\nint main(void){raise(SIGSEGV);}", "SIGSEGV"),
    ("solve.c", '#include <stdio.h>\nint main(void){fputs("Traceback (most recent call last):\\nZeroDivisionError\\n",stderr);return 1;}', None),
])
async def test_runtime_feedback_follows_observed_failure(filename, source, marker):
    problem = ProblemInfo(q_id="q1", number="1", type="programming", stem="Read one integer from stdin.",
                          criterion="Exact output.", test_cases=[Case(input="0", expected_output="1")])
    raw, = source_answers(filename, source, {"q1": problem.model_dump()}, {"q1"})
    run = await evidence_for_answer(problem=problem, answer=StudentAnswerInfo.model_validate(raw),
        student_id="fixture", task_id="runtime-feedback", provider=None, reporter=None, generate=None, execute=run_sandbox)
    for language, memory_hint in [("zh", "内存访问"), ("en", "memory accesses")]:
        evidence, _ = describe_run(run, language, problem)
        assert evidence.failure_counts == {"runtime_error": 1} and evidence.teacher_tests.passed == 0
        assert evidence.score_status == "final"
        assert (memory_hint in evidence.safe_summary) == (marker == "SIGSEGV")
        if marker:
            assert marker in evidence.safe_summary
        else:
            assert "ZeroDivisionError" not in evidence.safe_summary


@pytest.mark.asyncio
@pytest.mark.parametrize("language,filename", [("c","candidate.c"), ("cpp","candidate.cpp")])
@pytest.mark.parametrize("generated_solution", [False, True], ids=["teacher-solution", "teacher-reference"])
async def test_reference_outputs_and_optional_boundary_use_real_native_processes(language, filename, generated_solution):
    from backend.skills.programming import ProgrammingSkill, _generate_test_cases
    from backend.tests.test_programming_scoring_contract import award, fixed_provider, response

    source = '#include <stdio.h>\nint main(void){int n; if(scanf("%d",&n)!=1)return 1; printf("%d\\n",2*n); return 0;}'
    student = source.replace("2*n", "(n==-1 ? 0 : 2*n)")
    problem = ProblemInfo(q_id="double", number="1", type="编程题", stem="Read an integer from stdin and print double its value.",
                          max_score=10, criterion="All teacher examples correct earns full credit.",
                          solution_code=f"```{language}\n{source}\n```")
    if generated_solution:
        problem.reference_answer = problem.solution_code
        problem.solution_code = "```python\nprint(2 * int(input()))\n```"
        problem._math_rubric_source = {"presentation": {"ai_completion_provenance": {"solution_code": {}}}}
    raw, = source_answers(filename, student, {problem.q_id:problem.model_dump()}, {problem.q_id})
    answer = StudentAnswerInfo.model_validate(raw)
    route = fixed_provider(response({"inputs":[{"input":"-1\n"}]}))
    with programming_run(None, None):
        generated = await evidence_for_answer(problem=problem, answer=answer, student_id="native-student", task_id="t",
                provider=route, reporter=None, generate=_generate_test_cases, execute=run_sandbox)
    assert generated.status == "executed" and generated.report.passed_count == 0
    assert generated.cases[0].expected_output == "-2\n" and generated.cases[0].source == "llm_generated"
    assert generated.report.results[0].isolation_mode == "oci_container"

    problem.test_cases = [Case(input="3\n", expected_output="6\n", source="teacher")]
    route = fixed_provider(response({**award(10), "boundary_case":{"stdin":"-1\n","expected":"-2\n"}}))
    with programming_run(None, None):
        vote = await ProgrammingSkill(route, language="zh").grade(problem, answer, student_id="native-student")
    assert vote.score == 10 and vote.programming_evidence.score_status == "final"
    assert vote.programming_evidence.teacher_tests.passed == 1 and "-2" in vote.programming_evidence.boundary_note
    assert vote.programming_evidence.isolation_mode == "oci_container"


@pytest.mark.asyncio
@pytest.mark.parametrize("language,source", [
    ("c", '#include <stdio.h>\nint main(void) { puts("2"); return 0; }'),
    ("cpp", '#include <iostream>\nint main() { std::cout << 2; return 0; }'),
    ("python", 'print(2)'),
])
@pytest.mark.parametrize("generated_solution", [True, False])
async def test_selected_reference_executes_in_its_language(monkeypatch, language, source, generated_solution):
    from backend.services.programming_submission_mapping import PYTHON_FLAG
    other_language, other_source = ("python", "print(2)") if language != "python" else (
        "c", '#include <stdio.h>\nint main(void) { puts("2"); return 0; }')
    problem = ProblemInfo(q_id="q1", number="1", type="编程题", stem="Read an integer from stdin and print it.",
        criterion="Correct output.", max_score=10, solution_code=f"```{other_language}\n{other_source}\n```",
        reference_answer=f"```{language}\n{source}\n```", test_cases=[Case(input="2", expected_output="2")])
    if generated_solution:
        problem._math_rubric_source = {"presentation": {"ai_completion_provenance": {"solution_code": {}}}}
    answer = StudentAnswerInfo(q_id="q1", number="1", type="编程题", content="print(2)", flag=[PYTHON_FLAG])
    with programming_run(None, None):
        result = await evidence_for_answer(problem=problem, answer=answer, student_id="synthetic",
            task_id="reference-language", provider=None, reporter=None, generate=None, execute=run_sandbox)
    assert result.status == "executed", result.reason
    assert result.report.passed_count == 1
    assert result.report.results[0].isolation_mode == "oci_container"


@pytest.mark.asyncio
async def test_pre_fix_wrong_language_validation_cannot_survive_recovery(monkeypatch):
    from backend.services import programming_runs as runs
    from backend.tests.test_programming_reference_language import POLYGLOT_REFERENCE

    problem = ProblemInfo(q_id="polyglot", number="1", type="编程题", stem="Read stdin.", criterion="Correct output.",
        solution_code="```python\nprint(2)\n```", reference_answer=f"```c\n{POLYGLOT_REFERENCE}\n```",
        test_cases=[Case(input="2", expected_output="2")])
    problem._math_rubric_source = {"presentation": {"ai_completion_provenance": {"solution_code": {}}}}
    state = runs.ProgrammingRun()
    identity = runs._reference_execution_identity
    with monkeypatch.context() as old:
        old.setattr(runs, "_reference_language", lambda *_: "python")
        old.setattr(runs, "_reference_execution_identity", lambda p: {
            k: v for k, v in identity(p).items() if k != "reference_language_selection_version"})
        old_key, cases, reason = await runs._prepare(state, problem, None, None, run_sandbox, None)
    assert cases and not reason and state.read(old_key, runs.PREPARED)
    key, cases, reason = await runs._prepare(state, problem, None, None, run_sandbox, None)
    assert key != old_key and not cases and reason == "programming_reference_conflict"
