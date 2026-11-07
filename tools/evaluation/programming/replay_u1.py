"""Replay the fixed U1 v2 execution oracle through the product runner.

In a clean checkout, fetch the original fixture first: git fetch origin pull/277/head
Run: python tools/evaluation/programming/replay_u1.py [--output report.json]

Only synthetic Python sources at the pinned Git commit can run. This checks
execution evidence, not teacher scores or model quality.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
U1 = "ab36293b5b00ea2dd20803cd89c727398671c835"
PACKET = "manual_test_samples/programming_u1_20261028"


def fixture(name):
    return subprocess.check_output(
        ["git", "-C", str(ROOT), "cat-file", "blob", f"{U1}:{PACKET}/{name}"]
    ).decode("utf-8")


def teacher_cases(question):
    """Use the same visible input/expectation pairs as the original U1 replay."""
    cases = []
    for row in question["cases"]:
        if question["q_id"] in {"PY-F04", "PY-F05", "PY-S02"}:
            pair = row["input"] if isinstance(row["input"], list) else row["input"].split()
            case = {"input": "、".join(map(str, pair)), "expected_output": str(row["expected"]).strip()}
        elif question["mode"] == "function_call":
            case = {"io_mode": "function", "function_name": question["function_name"],
                    "function_args": row["input"], "expected_return": repr(row["expected"])}
        else:
            case = {"input": row["input"], "expected_output": row["expected"]}
        cases.append({**case, "source": "teacher"})
    return cases


async def replay():
    from backend.models import ProblemInfo, StudentAnswerInfo
    from backend.services.programming_runs import evidence_for_answer, programming_run
    from backend.tools.code_interpreter import run_sandbox

    async def no_generation(*args, **kwargs):
        raise AssertionError("Every fixture has a frozen teacher suite")

    questions = json.loads(fixture("questions.json"))
    submissions = json.loads(fixture("submissions.json"))
    observations = []
    with programming_run(None, None):
        for q in questions:
            if q["language"] != "python":
                continue
            problem = ProblemInfo(q_id=q["q_id"], number=q["q_id"], type="编程题", stem=q["stem"],
                                  max_score=q["max_score"], criterion="\n".join(q["teacher_rules_draft"]),
                                  test_cases=teacher_cases(q))
            for row in submissions:
                if row["q_id"] != q["q_id"] or row["execution_policy"] != "execute_after_contract_confirmation":
                    continue
                answer = StudentAnswerInfo(q_id=problem.q_id, number=problem.number, type=problem.type,
                                           content=fixture(row["source_file"]))
                evidence = await evidence_for_answer(
                    problem=problem, answer=answer, student_id=row["submission_id"], task_id="synthetic-u1",
                    reporter=None, provider=None, generate=no_generation, execute=run_sandbox,
                )
                report = evidence.report
                expected = row["expected_observation"]
                failures = None if report is None else [
                    case["case_id"] for case, outcome in zip(q["cases"], report.results, strict=True)
                    if not outcome.passed
                ]
                passed = report.passed_count if report else None
                total = report.total_count if report else 0
                matched = (evidence.status == "executed" and passed == expected["passed"]
                           and total == expected["total"] and failures == expected["failed_cases"])
                observations.append({"submission": row["submission_id"], "question": problem.q_id,
                                     "passed": passed, "total": total, "failures": failures,
                                     "status": evidence.status, "reason": evidence.reason, "matched": matched})
    return observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Create a new JSON report at this path")
    args = parser.parse_args()
    if args.output is not None:
        args.output = args.output.resolve()
    if (ROOT / ".env").exists() or (ROOT / "backend/.env").exists():
        parser.error("Use a clean checkout without an environment file")
    if args.output is not None and args.output.exists():
        parser.error("The output path must be new")
    if subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{U1}^{{commit}}"],
                      stderr=subprocess.DEVNULL).returncode:
        parser.error("Fetch the original U1 fixture with git fetch origin pull/277/head")
    head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    with tempfile.TemporaryDirectory(prefix="smartai-u1-") as tmp:
        path = os.defpath
        os.environ.clear()
        os.environ.update(PATH=path, HOME=tmp, TMPDIR=tmp, SMARTAI_RUNTIME_ENVIRONMENT="test",
                          SMARTAI_GRADING_ENGINE="v2", SMARTAI_PROGRAM_RUNNER_MODE="disabled",
                          SMARTAI_DATABASE_URL=f"sqlite:///{tmp}/run.db", SMARTAI_STORAGE_ROOT=f"{tmp}/files")
        os.chdir(ROOT)
        sys.path.insert(0, str(ROOT))
        observations = asyncio.run(replay())
    total = sum(row["total"] for row in observations)
    mismatches = [r["submission"] for r in observations if not r["matched"]]
    if args.output:
        with args.output.open("x", encoding="utf-8") as out:
            json.dump({"code_commit": head, "fixture_commit": U1, "model_calls": 0, "observations": observations},
                      out, ensure_ascii=False, indent=2)
            out.write("\n")
    print(f"{len(observations)} submissions; {total} cases; mismatches: {mismatches}")
    return int(len(observations) != 20 or total != 97 or bool(mismatches))


if __name__ == "__main__":
    sys.exit(main())
