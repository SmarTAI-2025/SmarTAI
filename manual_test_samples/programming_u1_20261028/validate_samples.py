"""Validate U1 constructed fixtures locally; never grade or invoke SmarTAI.

Only run this on the reviewed, versioned sample packet. This is not a sandbox
for arbitrary student code and must never receive real submissions.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
if not __debug__:
    raise RuntimeError("Do not run sample validation with Python optimization enabled")


def local_file(name: str) -> Path:
    path = (ROOT / name).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError(f"Missing or outside-packet file: {name}")
    return path


def load(name):
    return json.loads(local_file(name).read_text(encoding="utf-8"))


def validate_structure(questions, submissions):
    qmap = {q["q_id"]: q for q in questions}
    assert len(qmap) == len(questions), "Duplicate question ID"
    assert len({s["submission_id"] for s in submissions}) == len(submissions)
    case_ids = set()
    for q in questions:
        assert q["teacher_confirmed"] is False
        assert q["teacher_rules_status"] == "AI_PROPOSED_PENDING_TEACHER_CONFIRMATION"
        assert q["max_score"] == 10 and q["cases"] and q["teacher_rules_draft"]
        assert q["mode"] in {"function_call", "stdin_stdout"}
        assert q["language"] in {"python", "c", "cpp"}
        local_file(q["reference"])
        if q["mode"] == "function_call":
            assert q["function_name"].isidentifier()
        for case in q["cases"]:
            assert case["case_id"] not in case_ids
            case_ids.add(case["case_id"])
            assert case["provenance"] == "AI_CONSTRUCTED_PENDING_TEACHER_CONFIRMATION"
            raw = local_file(case["input_file"]).read_text(encoding="utf-8")
            exp = local_file(case["expected_file"]).read_text(encoding="utf-8")
            if q["mode"] == "function_call":
                assert json.loads(raw) == case["input"]
                assert isinstance(case["input"], list)
                assert exp == str(case["expected"]) + "\n"
            else:
                assert raw == case["input"] and exp == case["expected"]
    for s in submissions:
        q = qmap[s["q_id"]]
        assert s["provenance"] == "AI_CONSTRUCTED_NOT_REAL_STUDENT"
        assert s["teacher_confirmed"] is False
        assert s["teacher_confirmed_score"] is None and s["teacher_name"] is None
        assert s["teacher_confirmed_at"] is None
        score = s["suggested_score"]
        assert score is None or (type(score) in {int, float} and 0 <= score <= q["max_score"])
        assert s["deduction_reason"] and s["review_condition"] and s["execution_policy"]
        if s["source_file"]:
            local_file(s["source_file"])
        else:
            assert s["execution_policy"] == "do_not_execute_missing"
        if s["scenario"] in {"platform_unavailable", "incorrect_expected", "entry_mismatch", "conflicting_assignment"}:
            assert score is None and s["execution_policy"].startswith("do_not_execute")
    used = {s["source_file"] for s in submissions if s["source_file"]}
    actual = {str(p.relative_to(ROOT)) for p in (ROOT / "submissions").iterdir() if p.is_file()}
    assert actual == used, "Orphaned/missing source files"
    mapping = load("mapping_inputs/expected_assignments.json")
    assert mapping["order_in_document"] == ["PY-F03", "PY-F02"]
    assert mapping["product_ingestion_executed"] is False
    source = local_file(mapping["same_submission_as"]).read_text(encoding="utf-8")
    assert source in local_file(mapping["file"]).read_text(encoding="utf-8")
    return qmap, len(case_ids)


def run_process(command, cwd, input_text="", timeout=5):
    # No inherited credentials, .env loading, network client or product imports.
    return subprocess.run(command, input=input_text, capture_output=True, text=True,
                          cwd=cwd, timeout=timeout,
                          env={"PATH": "/usr/bin:/bin", "LANG": "C", "TMPDIR": str(cwd),
                               "PYTHONDONTWRITEBYTECODE": "1"})


def compile_native(q, source, temp):
    compiler = shutil.which("clang" if q["language"] == "c" else "clang++")
    if not compiler:
        return None, {"status": "compiler_unavailable", "executed": False}
    binary = Path(temp) / "sample"
    standard = "-std=c11" if q["language"] == "c" else "-std=c++17"
    flags = [standard]
    if sys.platform == "darwin":
        sdk = run_process(["/usr/bin/xcrun", "--show-sdk-path"], temp)
        if sdk.returncode or not Path(sdk.stdout.strip()).is_dir():
            return None, {"status": "compiler_environment_unavailable", "executed": False,
                          "diagnostic": "macOS SDK discovery failed"}
        flags += ["-isysroot", sdk.stdout.strip()]
        sdk_cpp_headers = Path(sdk.stdout.strip()) / "usr/include/c++/v1"
        if q["language"] == "cpp" and sdk_cpp_headers.is_dir():
            flags += ["-isystem", str(sdk_cpp_headers)]
    # Validate the compiler environment on an unrelated known-valid program.
    # Toolchain/temp-directory failures must not satisfy an expected source error.
    probe = Path(temp) / ("probe.c" if q["language"] == "c" else "probe.cpp")
    probe.write_text(("#include <stdio.h>\n" if q["language"] == "c" else "#include <iostream>\n") + "int main(void) { return 0; }\n", encoding="utf-8")
    check = run_process([compiler, *flags, str(probe), "-o", str(Path(temp) / "probe")], temp, timeout=30)
    if check.returncode:
        return None, {"status": "compiler_environment_unavailable", "executed": False, "diagnostic": check.stderr[:1500]}
    cp = run_process([compiler, *flags, "-Wall", "-Wextra", "-Werror", str(source), "-o", str(binary)], temp, timeout=30)
    if cp.returncode < 0:
        return None, {"status": "compiler_environment_unavailable", "executed": False,
                      "diagnostic": f"Compiler terminated by signal {-cp.returncode}; no source verdict"}
    if cp.returncode:
        return None, {"status": "compile_error", "executed": False, "diagnostic": cp.stderr[:1500]}
    return binary, None


def observe(q, source, temp, allow_execute=True):
    if q["language"] == "python":
        try:
            ast.parse(source.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            return {"status": "syntax_error", "executed": False, "diagnostic": f"{exc.msg}, line {exc.lineno}"}
        binary = None
        if not allow_execute:
            return {"status": "parsed_only", "executed": False}
    else:
        binary, error = compile_native(q, source, temp)
        if error:
            return error
        if not allow_execute:
            return {"status": "compiled_only", "executed": False}
    observations = []
    for case in q["cases"]:
        if q["mode"] == "function_call":
            # Exact declared entry, complete source, no inference or code repair.
            invocation = ("import json,runpy,sys; "
                          "ns=runpy.run_path(sys.argv[1]); "
                          "print(json.dumps(ns[sys.argv[2]](*json.loads(sys.argv[3]))))")
            cmd = [sys.executable, "-I", "-B", "-c", invocation, str(source), q["function_name"], json.dumps(case["input"])]
            stdin = ""
        else:
            cmd = [str(binary)] if binary else [sys.executable, "-I", "-B", str(source)]
            stdin = case["input"]
        try:
            cp = run_process(cmd, temp, stdin)
            passed = False
            if cp.returncode == 0:
                if q["mode"] == "function_call":
                    try:
                        value = json.loads(cp.stdout)
                        passed = type(value) is type(case["expected"]) and value == case["expected"]
                    except (ValueError, TypeError):
                        pass
                else:
                    passed = cp.stdout == case["expected"]
            observations.append({"case_id": case["case_id"], "passed": passed, "returncode": cp.returncode,
                                 "stdout": cp.stdout, "stderr": cp.stderr[-1500:], "timed_out": False})
        except subprocess.TimeoutExpired:
            observations.append({"case_id": case["case_id"], "passed": False, "returncode": None, "timed_out": True})
    return {"status": "observed", "executed": True, "passed": sum(x["passed"] for x in observations),
            "total": len(observations), "runtime_errors": sum(x["returncode"] not in {0, None} for x in observations),
            "exception_classes": [match.group(1) for x in observations if x["returncode"] not in {0, None}
                                  for match in re.finditer(r"^([A-Za-z_][A-Za-z_0-9]*(?:Error|Exception)):", x.get("stderr", ""), re.MULTILINE)],
            "cases": observations}


def validate_observation(expected, actual):
    if expected.get("syntax_error"):
        assert actual["status"] == "syntax_error" and actual["executed"] is False
    elif expected.get("compile_error"):
        assert actual["status"] == "compile_error" and actual["executed"] is False
        assert "expected ';'" in actual["diagnostic"], "Expected the authored missing-semicolon diagnostic"
    else:
        assert actual["status"] == "observed"
        assert (actual["passed"], actual["total"]) == (expected["passed"], expected["total"])
        assert actual["runtime_errors"] == expected.get("runtime_errors", 0)
        assert actual["exception_classes"] == expected.get("exception_classes", [])
        assert len(actual["cases"]) == actual["total"]
        assert len({x["case_id"] for x in actual["cases"]}) == actual["total"]
        assert [x["case_id"] for x in actual["cases"] if not x["passed"]] == expected.get("failed_cases", [])
        assert not any(x["timed_out"] for x in actual["cases"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-constructed", action="store_true", help="Run only reviewed, constructed toy sources on this Mac")
    parser.add_argument("--output", type=Path, help="Write local factual report; no score calculation")
    args = parser.parse_args()
    questions, submissions = load("questions.json"), load("submissions.json")
    qmap, cases = validate_structure(questions, submissions)
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
              "python": platform.python_version(), "question_count": len(questions), "case_count": cases,
              "row_count": len(submissions), "scope": "Constructed fixture facts only; not product, model or OCI acceptance",
              "score_computation": False, "structure": "passed", "references": [], "submissions": [], "failures": []}
    if args.execute_constructed:
        with tempfile.TemporaryDirectory(prefix="smartai-u1-fixtures-") as temp:
            for q in questions:
                actual = observe(q, local_file(q["reference"]), temp)
                report["references"].append({"q_id": q["q_id"],
                    "source_sha256": hashlib.sha256(local_file(q["reference"]).read_bytes()).hexdigest(), **actual})
                try:
                    validate_observation({"passed": len(q["cases"]), "total": len(q["cases"])}, actual)
                except AssertionError:
                    report["failures"].append("reference:" + q["q_id"])
            for s in submissions:
                if s["execution_policy"].startswith("do_not_execute"):
                    actual = {"status": "not_executed_by_policy", "executed": False, "reason": s["execution_policy"]}
                    if s["scenario"] in {"confirmed_missing_entry", "entry_mismatch"}:
                        tree = ast.parse(local_file(s["source_file"]).read_text(encoding="utf-8"))
                        names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
                        assert qmap[s["q_id"]]["function_name"] not in names
                else:
                    actual = observe(qmap[s["q_id"]], local_file(s["source_file"]), temp,
                                     allow_execute=s["execution_policy"] not in {"parse_only_no_execution", "compile_only_no_execution"})
                    try:
                        validate_observation(s["expected_observation"], actual)
                    except AssertionError:
                        report["failures"].append("submission:" + s["submission_id"])
                report["submissions"].append({"submission_id": s["submission_id"],
                    "source_sha256": hashlib.sha256(local_file(s["source_file"]).read_bytes()).hexdigest() if s["source_file"] else None, **actual})
    report["result"] = "passed" if not report["failures"] else "failed"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(report, ensure_ascii=False, indent=2).replace(str(ROOT), "<packet>")
        if args.execute_constructed:
            serialized = serialized.replace(temp, "<validation-temp>")
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ["result", "question_count", "case_count", "row_count", "failures"]}, ensure_ascii=False))
    return bool(report["failures"])


if __name__ == "__main__":
    raise SystemExit(main())
