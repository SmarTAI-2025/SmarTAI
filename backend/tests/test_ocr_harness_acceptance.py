import asyncio
import hashlib

import pytest

from tools.ocr_benchmark.evaluate_harness import evaluate


def sample():
    source = hashlib.sha256(b"synthetic").hexdigest()
    def output(text):
        return dict(text=text, source_sha256=source, route_fingerprint="same-route", model="fake",
                    initial_candidate_sha256="same-candidate", calls=1, duration_ms=20)
    return dict(id="wrong-student-formula", source_sha256=source, purpose="submissions", split="holdout",
                ground_truth="1 + 1 = 3 [crossed-out: 2]", ground_truth_reviewed=True,
                critical_literals=["1 + 1 = 3", "[crossed-out: 2]"],
                outputs=dict(E1=output("1 + 1 = 3 [crossed-out: 2]"), E2=output("1 + 1 = 2")))


def test_ablation_reports_regression_unknown_usage_and_unmeasured_baseline():
    report = evaluate(dict(samples=[sample()]))
    assert report["external_calls"] == 0
    result = report["samples"][0]
    assert result["repair_delta"]["newly_missing_critical"] == 2
    assert result["repair_delta"]["repair_alignment_delta"] > 0
    assert result["arms"]["E0"]["status"] == "unverified"
    assert report["groups"][0]["usage_complete"] is False
    assert "crossed-out" not in str(report)


def test_ablation_rejects_changed_model_or_repeated_initial_call():
    item = sample()
    item["outputs"]["E2"]["model"] = "different"
    with pytest.raises(ValueError, match="same frozen route"):
        evaluate(dict(samples=[item]))
    item = sample()
    item["outputs"]["E2"]["initial_candidate_sha256"] = "another"
    with pytest.raises(ValueError, match="reuse"):
        evaluate(dict(samples=[item]))


def test_unreviewed_ground_truth_never_becomes_quality_evidence():
    item = sample()
    item["ground_truth_reviewed"] = False
    report = evaluate(dict(samples=[item]))
    assert not report["groups"]
    assert report["samples"][0]["repair_delta"] is None


@pytest.mark.parametrize("status,expired", [("pending", False), ("running", False), ("preparing", True)])
def test_unleased_artifact_fence_rejects_non_preflight_or_expired_state(tmp_path, status, expired):
    import time
    from backend.tests.test_task_background_workflows import _seed_task
    from backend.db import workflow_repository, file_repository
    from backend.storage.local import LocalStorage
    from backend.domain.errors import LeaseLost
    who, task = _seed_task()
    operation, _ = workflow_repository.create_operation(assignment_id=task, owner_id=who,
        operation_type="material_source", input_hash="synthetic-fence", payload={}, initial_status="pending" if status == "running" else status,
        expires_at=time.time() + (-1 if expired else 60))
    if status == "running":
        from backend.db.workflow_repository import WorkflowOperationRecord
        from backend.db.session import session_scope
        with session_scope() as session:
            session.get(WorkflowOperationRecord, operation.id).status = "running"
    with pytest.raises(LeaseLost):
        file_repository.save_file(storage=LocalStorage(tmp_path), owner_id=who, kind="material_import_text",
            original_name="material.txt", content=b"synthetic", assignment_id=task,
            fence_operation_id=operation.id, fence_operation_attempt=operation.attempt)


@pytest.mark.asyncio
@pytest.mark.parametrize("students", [40, 100])
async def test_grading_capacity_preserves_400_1000_answer_units_and_partial_failures(monkeypatch, students):
    from backend.agents import grading_agent
    from backend.models import Correction
    from backend.progress.tracker import ProgressReporter
    seen = set()
    async def grade(**kwargs):
        problem, student = kwargs["problem"], kwargs["student_id"]
        seen.add((student, problem.q_id))
        await asyncio.sleep(0)
        if student == "synthetic-17" and problem.q_id == "q8":
            raise ValueError("synthetic failure")
        return Correction(q_id=problem.q_id, type=problem.type, score=5, max_score=10, confidence=.9, comment="synthetic", steps=[])
    monkeypatch.setattr(grading_agent, "run_multi_expert", grade)
    monkeypatch.setattr(grading_agent._settings, "grading_item_max_retries", 0)
    problems = {f"q{n}": dict(q_id=f"q{n}", number=str(n), type="概念题", stem="synthetic question", criterion="literal") for n in range(1, 11)}
    inputs = {str(s): dict(stu_id=f"synthetic-{s}", stu_ans=[dict(q_id=q, content="1 + 1 = 3") for q in reversed(problems)]) for s in range(students)}
    reporter = ProgressReporter("capacity-synthetic")
    result = await grading_agent.grade_batch(student_store=inputs, problem_store=problems, registry=None,
                                              task_id="grading-run:synthetic", reporter=reporter)
    assert len(seen) == students * 10 and len(result) == students
    assert all([item.q_id for item in row["corrections"]] == list(reversed(problems)) for row in result)
    failed = [item for row in result for item in row["corrections"] if item.synthesis_method == "all_failed"]
    assert len(failed) == 1 and failed[0].confidence == 0
    assert all(answer["content"] == "1 + 1 = 3" for row in result for answer in row["student_answers"])
