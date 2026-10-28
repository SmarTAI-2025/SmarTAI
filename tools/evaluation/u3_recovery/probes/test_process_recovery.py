"""Real process exit/restart with synthetic PDF and fixed recognition responses.

Only lease expiry is accelerated in the disposable SQLite fixture. This does not
claim real provider, OS sleep, elapsed lease timer, browser, or PostgreSQL coverage.
"""
from audit_guard import require_probe_run
require_probe_run()

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from backend.db import workflow_repository
from backend.db.session import session_scope
from backend.tests.test_recognition_v2_artifacts import setup

WORKER = Path(__file__).with_name("crash_worker.py")

@pytest.mark.parametrize("cut", ["before_second", "pending_second"])
def test_actual_process_exit_preserves_page_and_uncertain_dispatch(tmp_path, cut):
    _, request, data, results = setup(tmp_path / "storage", pdf=True, pages=3)
    pdf = tmp_path / "three-page-synthetic.pdf"
    pdf.write_bytes(data)
    state = {"request": request.model_dump(mode="json"), "storage": str(tmp_path / "storage"),
        "pdf": str(pdf), "texts": [f"Q{i}. x = -2" for i in (1, 2, 3)],
        "calls": str(tmp_path / "calls.jsonl"), "result": str(tmp_path / "result.json"), "cut": cut}
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state))

    def invoke(mode):
        result = subprocess.run([sys.executable, str(WORKER), str(state_path), mode],
            env=os.environ.copy(), capture_output=True, text=True, timeout=30)
        assert result.returncode == (86 if mode == "crash" else 0), result.stderr
        return None if mode == "crash" else json.loads(Path(state["result"]).read_text())

    def pages():
        return [json.loads(line)["page"] for line in Path(state["calls"]).read_text().splitlines()]

    invoke("crash")
    expected = [1] if cut == "before_second" else [1, 2]
    assert pages() == expected
    with session_scope() as db:
        operation = db.query(workflow_repository.WorkflowOperationRecord).filter_by(
            assignment_id=request.source.business_id).one()
        original_id = operation.id
        checkpoint = operation.checkpoint
        assert checkpoint["calls"][0]["state"] == "stored"
        if cut == "pending_second":
            assert checkpoint["calls"][1]["state"] == "pending"
    assert invoke("resume")["status"] == "already_running"
    assert pages() == expected

    # Test-clock acceleration only; no production database is reachable.
    with session_scope() as db:
        operation = db.get(workflow_repository.WorkflowOperationRecord, original_id)
        operation.lease_expires_at = time.time() - 1

    resumed = invoke("resume")
    if cut == "pending_second":
        assert resumed["status"] == "needs_review"
        assert resumed["safe_error_code"] == "provider_submit_uncertain"
        assert pages() == [1, 2]
        assert invoke("resume")["status"] == "needs_review"
        assert pages() == [1, 2]
        resumed = invoke("ack")
        assert pages() == [1, 2, 2, 3]
    else:
        assert pages() == [1, 2, 3]
    assert resumed["status"] == "completed", resumed
    text = resumed["assembly"]["document"]["final_markdown"]
    for marker in state["texts"]:
        assert text.count(marker) == 1
    assert resumed["assembly"]["raw"]["request"]["source"] == state["request"]["source"]
    before = pages()
    assert invoke("ack" if cut == "pending_second" else "resume")["status"] == "already_done"
    assert pages() == before
