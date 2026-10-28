"""Observe the U2 programming baseline using fixed model/runner responses only.

No student code is executed. Scores and runner outcomes are injected fixtures,
not model quality or OCI evidence. U1 proposed teacher rules remain unconfirmed.
Run with the project's Python environment and --repo/--u1/--out paths.
"""
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

parser = argparse.ArgumentParser()
parser.add_argument("--repo", type=Path, required=True)
parser.add_argument("--u1", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
repo, u1 = args.repo.resolve(), args.u1.resolve()
if (repo / ".env").exists() or (repo / "backend/.env").exists():
    raise SystemExit("Use an isolated checkout without .env")
source_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
    capture_output=True, check=True, text=True).stdout.strip()
runtime = Path(tempfile.mkdtemp(prefix="u2-probe-"))
safe_path = os.environ.get("PATH", "/usr/bin:/bin")
os.environ.clear()
os.environ.update({"PATH": safe_path, "LANG": "en_US.UTF-8", "TMPDIR": str(runtime),
    "SMARTAI_RUNTIME_ENVIRONMENT": "test", "SMARTAI_GRADING_ENGINE": "v2",
    "SMARTAI_DATABASE_URL": f"sqlite:///{runtime / 'test.db'}",
    "SMARTAI_STORAGE_ROOT": str(runtime / "uploads"), "SMARTAI_SEED_TEST_USERS": "false"})
sys.dont_write_bytecode = True
blocked = []

def guard(event, audit_args):
    if event in {"subprocess.Popen", "os.system", "os.posix_spawn"} or (
        event == "socket.connect" and audit_args[0].family in (socket.AF_INET, socket.AF_INET6)
    ) or (
        event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"} and audit_args[0] not in (None, "localhost", "127.0.0.1", "::1")
    ):
        blocked.append(event)
        raise RuntimeError("U2 baseline forbids network and process execution")

sys.addaudithook(guard)
sys.path.insert(0, str(repo))
os.chdir(repo)
from backend.models import ProblemInfo, StudentAnswerInfo, TestCase
from backend.skills import programming
from backend.tools import code_interpreter
from backend.tools.oci_grading_runner import IsolatedRunnerUnavailable

consumed_bytes = {name: (u1 / name).read_bytes() for name in ("questions.json", "submissions.json", "manifest.json")}
questions = {q["q_id"]: q for q in json.loads(consumed_bytes["questions.json"])}
submissions = {s["submission_id"]: s for s in json.loads(consumed_bytes["submissions.json"])}

def text_source(path):
    source = (u1 / path).resolve()
    if not source.is_relative_to(u1):
        raise ValueError("U1 source references must stay within the sample directory")
    if path not in consumed_bytes:
        consumed_bytes[path] = source.read_bytes()
    return consumed_bytes[path].decode("utf-8")

def cases_for(q, source="teacher"):
    # The source override is explicit test setup, not an assertion U1 is approved.
    cases = []
    for c in q["cases"]:
        values = {"description": c["case_id"], "source": source}
        if q["mode"] == "function_call":
            values.update(function_name=q["function_name"], function_args=c["input"], expected_return=repr(c["expected"]))
        else:
            values.update(input=c["input"], expected_output=str(c["expected"]))
        cases.append(TestCase(**values))
    return cases

def problem_for(q, cases):
    return ProblemInfo(q_id=q["q_id"], number=q["q_id"], type="programming",
        stem=q["stem"], criterion="\n".join(q["teacher_rules_draft"]),
        reference_answer=text_source(q["reference"]), max_score=q["max_score"], test_cases=cases)

observations = []

async def observe(label, submission_id, *, stored=True, source="teacher", fixed_score=None,
                  fixed_confidence=.95, failure=None, generation_failure=False, repeats=1,
                  force_passed=None, problem_override=None):
    sub = submissions[submission_id]
    q = questions[sub["q_id"]]
    code = text_source(sub["source_file"]) if sub["source_file"] else ""
    cases = cases_for(q, source)
    problem = problem_override or problem_for(q, cases if stored else None)
    before = problem.model_dump(mode="json")
    fixed_score = sub.get("suggested_score") if fixed_score is None else fixed_score
    fixed_score = 0 if fixed_score is None else fixed_score
    event_counts = {"generation_boundary": 0, "grading_boundary": 0, "runner_boundary": 0}
    generated_names, runner_cases, prompts, results = [], [], [], []
    provider = SimpleNamespace(provider_id="u2:fixed-response", ainvoke=AsyncMock(side_effect=AssertionError("No live provider")))

    async def fixed_model(*unused, output_model, **kwargs):
        if output_model is programming.TestCaseList:
            event_counts["generation_boundary"] += 1
            name = re.search(r"Student function name: ([^\n]+)", kwargs["user_prompt"])
            name = name.group(1) if name else None
            generated_names.append(name)
            if generation_failure:
                raise RuntimeError("synthetic test-generation failure")
            generated = [c.model_copy(update={"source": "llm_generated", **({"function_name": name} if name else {})}) for c in cases]
            return programming.TestCaseList(cases=generated), SimpleNamespace(content="{}", duration_ms=0)
        event_counts["grading_boundary"] += 1
        prompts.append(kwargs["user_prompt"])
        return programming.ProgrammingGradingOutput(score=fixed_score, max_score=q["max_score"],
            confidence=fixed_confidence, comment="U2 fixed-response fixture; not a real grade.", steps=[], logs=""), SimpleNamespace(content="{}", duration_ms=0)

    async def fixed_runner(student_code, selected_cases, **kwargs):
        event_counts["runner_boundary"] += 1
        runner_cases.append([c.model_dump(mode="json") for c in selected_cases])
        if failure == "platform":
            raise IsolatedRunnerUnavailable()
        failed_ids = {"S03-F01": {"PY-F01-T01"}, "S06-F01": {"PY-F01-T01"},
                      "S09-F02": {"PY-F02-T04"}, "S14-S01": {"PY-S01-T02"}}.get(submission_id, set())
        result_cases = []
        for i, c in enumerate(selected_cases):
            passed = (i < force_passed) if force_passed is not None else failure != "syntax" and c.description not in failed_ids
            result_cases.append(code_interpreter.TestResult(test=c, passed=passed,
                actual_output=(c.expected_return or c.expected_output) if passed else "synthetic mismatch",
                error=("SyntaxError: synthetic" if failure == "syntax" else "ZeroDivisionError: synthetic" if failure == "runtime" else "") if not passed else "",
                duration_ms=0, exit_reason="success" if passed else "runtime_error" if failure else "wrong_answer",
                isolation_mode="u2_fixed_response_no_execution"))
        count = sum(r.passed for r in result_cases)
        return code_interpreter.ExecutionReport(passed_count=count, total_count=len(selected_cases),
            pass_rate=count / len(selected_cases), results=result_cases,
            summary=f"FIXTURE ONLY: {count}/{len(selected_cases)}; code not executed")

    started = time.perf_counter()
    with patch.object(programming, "structured_llm_call", fixed_model), patch.object(programming, "run_sandbox", fixed_runner), patch.object(programming.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[])):
        skill = programming.ProgrammingSkill(provider, language="zh")
        for idx in range(repeats):
            answer = StudentAnswerInfo(q_id=q["q_id"], number=q["q_id"], type="programming", content=code)
            result = await skill.grade(problem, answer, student_id=f"synthetic-{idx}")
            results.append(result.model_dump(mode="json"))
    provider.ainvoke.assert_not_called()
    row = {"id": label, "u1_submission_id": submission_id, "u1_rules_teacher_confirmed": sub["teacher_confirmed"],
        "evidence_kind": "real ProgrammingSkill; fixed model and runner responses; no code execution",
        "injected_response": {"score": fixed_score, "confidence": fixed_confidence,
            "failure": failure, "generation_failure": generation_failure, "force_passed": force_passed},
        "repeats": repeats, "event_counts": event_counts, "generated_function_names": generated_names,
        "selected_runner_cases": runner_cases, "results": results,
        "grading_prompt_branch": [next((line for line in p.splitlines() if line.startswith("SANDBOX_")), "") for p in prompts],
        "grading_prompt_contains_reference": all(problem.reference_answer in p for p in prompts) if prompts else None,
        "problem_test_cases_mutated": problem.model_dump(mode="json") != before,
        "local_fixture_wall_seconds": round(time.perf_counter() - started, 6)}
    observations.append(row)
    return row

async def main():
    await observe("B01-correct-function", "S01-F01")
    await observe("B02-partial-function", "S03-F01")
    await observe("B03-weighted-rubric-not-pass-rate", "S09-F02")
    missing = await observe("B04-confirmed-empty-code", "S04-F01")
    assert missing["event_counts"] == {"generation_boundary": 0, "grading_boundary": 0, "runner_boundary": 0}
    await observe("B05-syntax-error-fixture", "S05-F01", failure="syntax")
    await observe("B06-runtime-error-fixture", "S06-F01", failure="runtime")
    unavailable = await observe("B07-platform-unavailable-fixture", "S01-F01", failure="platform")
    assert unavailable["results"][0]["error_kind"] == "programming_execution_unavailable"
    await observe("B08-generation-failure-fallback", "S01-F01", stored=False, generation_failure=True, fixed_score=9, fixed_confidence=.99)
    await observe("B09-helper-first-without-cases", "S08-F02", stored=False)
    await observe("B10-reversed-functions-without-cases", "S07-F02", stored=False)
    await observe("B11-empty-cases-three-students", "S01-F01", stored=False, repeats=3)
    await observe("B12-saved-cases-three-students", "S01-F01", repeats=3)
    await observe("B13-ai-function-zero-passes", "S01-F01", source="llm_generated", force_passed=0, fixed_score=9, fixed_confidence=.99)
    await observe("B14-stdin-normal", "S13-S01")
    await observe("B15-stdin-partial", "S14-S01")
    await observe("B16-ai-stdin-zero-passes", "S13-S01", source="llm_generated", force_passed=0, fixed_score=9, fixed_confidence=.99)
    # A real comparison-path probe with the process boundary replaced: nothing runs.
    async def fake_process(*unused, **kwargs):
        return code_interpreter.TestResult(test=TestCase(), passed=True, actual_output="UNEXPECTED\n", error="", duration_ms=0)
    with patch.object(code_interpreter, "run_python_subprocess", fake_process):
        report = await code_interpreter.run_sandbox("print('UNEXPECTED')", [TestCase(input="", expected_output="")])
    observations.append({"id": "B17-empty-stdout-expectation", "evidence_kind": "real comparison; fixed process output; no code execution", "expected_output": "", "actual_output": "UNEXPECTED\n", "passed_count": report.passed_count, "total_count": report.total_count})
    fenced = "```python\ndef helper(): return 1\n```\n```python\ndef solve(): return helper()\n```"
    extracted = programming._extract_code(fenced)
    observations.append({"id": "B18-multiple-code-fences", "evidence_kind": "pure function; no execution", "input": fenced, "extracted_code": extracted, "solve_preserved": "def solve" in extracted})
    # Real local inability to start OCI, before a container or student code runs.
    available = {name: shutil.which(name) is not None for name in ("docker", "podman")}
    if not any(available.values()):
        q = questions["PY-F01"]
        provider = SimpleNamespace(provider_id="u2:no-live-provider", ainvoke=AsyncMock(side_effect=AssertionError("No live provider")))
        problem = problem_for(q, cases_for(q))
        answer = StudentAnswerInfo(q_id=q["q_id"], number=q["q_id"], type="programming", content=text_source("submissions/S01_fib_correct.py"))
        with patch.object(code_interpreter.settings, "program_runner_mode", "oci"), patch.object(code_interpreter.settings, "runtime_environment", "production"), patch.object(code_interpreter.settings, "program_runner_image", "sha256:" + "a" * 64):
            result = await programming.ProgrammingSkill(provider, language="zh").grade(problem, answer, student_id="synthetic")
        assert result.error_kind == "programming_execution_unavailable"
        provider.ainvoke.assert_not_called()
        observations.append({"id": "B19-local-oci-unavailable", "evidence_kind": "real local capability rejection, no process or container launched", "cli_available": available, "results": [result.model_dump(mode="json")]})
    else:
        observations.append({"id": "B19-local-oci-unavailable", "evidence_kind": "not_run", "reason": "An OCI CLI exists; explicit runtime validation is outside this offline probe", "cli_available": available})
    await observe("B20-cpp-code-review-only", "X01")
    output = {"schema_version": 1, "observed_repository_sha": source_sha,
        "python_version": sys.version.split()[0], "python_executable": sys.executable,
        "evidence_kind": "offline fixed-response baseline, NOT real grading quality or OCI validation",
        "rules_status": "U1 AI-proposed; not teacher gold standard", "real_model_calls": 0,
        "real_oci_executions": 0, "host_student_code_executions": 0, "billed_cost": "not_applicable_no_calls",
        "network_or_process_attempts_blocked": len(blocked),
        "u1_input_sha256": {name: hashlib.sha256(value).hexdigest() for name, value in sorted(consumed_bytes.items())},
        "observations": observations}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"observations": len(observations), "result": str(args.out), "blocked_events": blocked}, ensure_ascii=False))
    if blocked:
        raise SystemExit("Rejected network/process attempt: observations are not a clean offline run")

asyncio.run(main())
