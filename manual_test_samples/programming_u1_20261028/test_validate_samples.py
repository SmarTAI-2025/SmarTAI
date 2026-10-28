"""Independent packet-verifier checks; no real models, OCI or subprocesses.

These test verification boundaries, not student grades or product behavior.
The packet's deliberately invalid examples must never defeat a no-execution
policy merely because somebody later corrects their source text.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


PACKET = Path(__file__).resolve().parent


@pytest.fixture
def validator():
    spec = importlib.util.spec_from_file_location("u1_sample_validator", PACKET / "validate_samples.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _single_policy_run(validator, monkeypatch, tmp_path, *, language, policy, expected):
    extension = "py" if language == "python" else "c"
    reference = tmp_path / f"reference.{extension}"
    submitted = tmp_path / f"corrected_submission.{extension}"
    code = "def answer(x):\n    return x\n" if language == "python" else "int main(void) { return 0; }\n"
    reference.write_text(code)
    submitted.write_text(code)
    q = {"q_id": "Q", "reference": reference.name, "language": language,
         "mode": "function_call" if language == "python" else "stdin_stdout",
         "function_name": "answer", "cases": [{"case_id": "T", "input": [5] if language == "python" else "",
                                                   "expected": 5 if language == "python" else "5\n"}]}
    submission = {"submission_id": "S", "q_id": "Q", "source_file": submitted.name,
                  "scenario": "syntax_error" if language == "python" else "compile_error",
                  "execution_policy": policy, "expected_observation": expected}
    # Structure validation is a separate contract; this test replaces a reviewed
    # erroneous source with valid text to prove the phase boundary still holds.
    monkeypatch.setattr(validator, "load", lambda name: [q] if name == "questions.json" else [submission])
    monkeypatch.setattr(validator, "validate_structure", lambda *_: ({"Q": q}, 1))
    monkeypatch.setattr(validator, "local_file", lambda name: tmp_path / name)
    commands = []

    def process(command, *args, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "5\n", "")

    monkeypatch.setattr(validator, "run_process", process)
    if language != "python":
        monkeypatch.setattr(validator, "compile_native", lambda q, source, temp: (source.with_suffix(".bin"), None))
    monkeypatch.setattr(sys, "argv", ["validate_samples.py", "--execute-constructed"])
    exit_code = validator.main()
    assert exit_code, "Correcting an intentionally broken fixture must invalidate its expected-error claim"
    assert commands, "Reference should still have its independent execution check"
    return submitted, commands


def test_parse_only_policy_never_executes_accidentally_corrected_source(validator, monkeypatch, tmp_path):
    submitted, commands = _single_policy_run(
        validator, monkeypatch, tmp_path, language="python", policy="parse_only_no_execution",
        expected={"syntax_error": True},
    )
    assert all(str(submitted) not in command for command in commands)


def test_compile_only_policy_never_executes_accidentally_corrected_source(validator, monkeypatch, tmp_path):
    submitted, commands = _single_policy_run(
        validator, monkeypatch, tmp_path, language="c", policy="compile_only_no_execution",
        expected={"compile_error": True},
    )
    assert all(str(submitted.with_suffix(".bin")) not in command for command in commands)


def test_compiler_environment_failure_cannot_validate_authored_compile_error(validator, monkeypatch, tmp_path):
    monkeypatch.setattr(validator.shutil, "which", lambda _: "/mock/clang")
    def process(command, *args, **kwargs):
        if command[0] == "/usr/bin/xcrun":
            return subprocess.CompletedProcess(command, 0, str(tmp_path) + "\n", "")
        return subprocess.CompletedProcess(command, 1, "", "unable to make temporary file")
    monkeypatch.setattr(validator, "run_process", process)
    binary, actual = validator.compile_native({"language": "c"}, tmp_path / "source.c", tmp_path)
    assert binary is None and actual["executed"] is False
    assert actual["status"] != "compile_error"
    with pytest.raises(AssertionError):
        validator.validate_observation({"compile_error": True}, actual)


def test_killed_compiler_is_not_a_student_compile_error(validator, monkeypatch, tmp_path):
    monkeypatch.setattr(validator.shutil, "which", lambda _: "/mock/clang")
    results = iter([(0, ""), (-9, "compiler terminated by signal")])

    def process(command, *args, **kwargs):
        if command[0] == "/usr/bin/xcrun":
            return subprocess.CompletedProcess(command, 0, str(tmp_path) + "\n", "")
        returncode, stderr = next(results)
        return subprocess.CompletedProcess(command, returncode, "", stderr)

    monkeypatch.setattr(validator, "run_process", process)
    binary, actual = validator.compile_native({"language": "c"}, tmp_path / "source.c", tmp_path)
    assert binary is None and actual["executed"] is False
    assert actual["status"] != "compile_error"


def test_platform_failure_cannot_satisfy_expected_zero_division_error(validator):
    submission = next(s for s in validator.load("submissions.json") if s["submission_id"] == "S06-F01")
    question = next(q for q in validator.load("questions.json") if q["q_id"] == submission["q_id"])
    rows = [{"case_id": c["case_id"], "passed": True, "returncode": 0,
             "stdout": str(c["expected"]) + "\n", "stderr": "", "timed_out": False}
            for c in question["cases"]]
    rows[0].update(passed=False, returncode=-9, stdout="", stderr="dyld: platform runtime unavailable")
    actual = {"status": "observed", "executed": True, "passed": len(rows) - 1,
              "total": len(rows), "runtime_errors": 1, "exception_classes": [], "cases": rows}
    with pytest.raises(AssertionError):
        validator.validate_observation(submission["expected_observation"], actual)


def test_packet_no_execution_rows_are_skipped_and_no_scores_are_computed(validator, monkeypatch, tmp_path):
    questions = validator.load("questions.json")
    submissions = validator.load("submissions.json")
    original = (PACKET / "submissions.json").read_bytes()
    seen = []

    def observe(question, source, temp, **kwargs):
        seen.append(source)
        return {"status": "observed", "executed": True, "passed": len(question["cases"]),
                "total": len(question["cases"]), "runtime_errors": 0, "exception_classes": [], "cases": []}

    # Valid/mismatching constructed results may fail other expectations here;
    # this check concerns the actual packet's no-execution routing and output.
    monkeypatch.setattr(validator, "observe", observe)
    report_path = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["validate_samples.py", "--execute-constructed", "--output", str(report_path)])
    validator.main()
    report = json.loads(report_path.read_text())
    by_id = {s["submission_id"]: s for s in report["submissions"]}
    for row in submissions:
        if row["execution_policy"].startswith("do_not_execute"):
            assert by_id[row["submission_id"]]["executed"] is False
            if row["source_file"] and not any(
                other["source_file"] == row["source_file"] and not other["execution_policy"].startswith("do_not_execute")
                for other in submissions
            ):
                assert validator.local_file(row["source_file"]) not in seen
    assert report["score_computation"] is False
    assert all("score" not in row and "suggested_score" not in row for row in report["submissions"])
    assert (PACKET / "submissions.json").read_bytes() == original


@pytest.mark.parametrize("scenario", ["platform_unavailable", "incorrect_expected", "entry_mismatch", "conflicting_assignment"])
def test_unresolved_scenario_cannot_claim_a_score(validator, scenario):
    questions = validator.load("questions.json")
    submissions = copy.deepcopy(validator.load("submissions.json"))
    next(s for s in submissions if s["scenario"] == scenario)["suggested_score"] = 0
    with pytest.raises(AssertionError):
        validator.validate_structure(questions, submissions)
