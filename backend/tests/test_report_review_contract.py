"""The report, Ask and frontend queue share these CI contract cases."""
import json
from pathlib import Path

import pytest

from backend.domain.grading_review import review_state
from backend.services.result_artifacts import build_artifact_files

CONTRACT = json.loads((Path(__file__).resolve().parents[2] / "frontend/app/src/test/fixtures/report_review_contract.json").read_text())


@pytest.mark.parametrize("case", CONTRACT["cases"], ids=lambda case: case["name"])
def test_report_pending_count_matches_review_queue_contract(case):
    correction = {**CONTRACT["base"], **case["patch"]}
    snapshot = {"version": 1, "created_at": 1, "fingerprint": "contract", "payload": {
        "problem_data": {"q1": {"q_id": "q1", "number": "1", "type": "reasoning"}},
        "results": [{"student_id": "example", "student_name": "Example", "corrections": [correction]}],
    }}
    files = build_artifact_files(task_id="contract", task_name="Contract", snapshot=snapshot, generated_at=2)
    report = next(item.content.decode() for item in files if item.artifact_id == "learning_report_md")
    row = next(line for line in report.splitlines() if line.startswith("| Q1 |"))
    assert int(row.split("|")[-2].strip()) == int(case["pending"])
    assert review_state(correction)[0] == case["pending"]
    assert "待复核题次" in report
    assert "快照时间 / Snapshot at" in report
    assert "确认时间" not in report

