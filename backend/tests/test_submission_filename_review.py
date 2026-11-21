"""Source-backed identity review survives persistence and gates grading."""
import pytest

from backend.services import task_facade
from backend.tests.test_submission_incremental import _claim, _registry
from backend.tests.test_submission_partial_responses import _answer, _response, _setup


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_id,raw_name,expected_id,expected_name", [
    ("001-A", "", "001-A", "work.txt"),
    (None, "Li Si", "work.txt", "Li Si"),
    ("", "[Unknown Student]", "work.txt", "work.txt"),
    ("001-A", "work.txt", "001-A", "work.txt"),
])
async def test_source_identity_fallback_blocks_grading_until_teacher_confirmation(
    monkeypatch, raw_id, raw_name, expected_id, expected_name,
):
    owner, task = _setup()
    response = _response([_answer("q1", "First"), _answer("q2", "Second")], stu_id=raw_id, stu_name=raw_name)
    registry = _registry(monkeypatch, lambda _: response)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner, filename="work.txt",
        content=b"Unlabelled answer sheet.", content_type="text/plain", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    saved = task_facade.get_task(task_id=task, owner_id=owner, full=True)
    student, = saved["student_data"].values()
    assert (student["stu_id"], student["stu_name"]) == (expected_id, expected_name)
    assert student["identity_status"] == "needs_review"
    assert saved["submission_sources"][0]["status"] == "identity_needs_review"
    assert "submission_identities_unresolved" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
    task_facade.update_student_identity(task_id=task, owner_id=owner, current_display_id=expected_id,
        new_display_id=raw_id or "009-B", new_display_name="Li Si", expected_revision=saved["workflow_revision"])
    final = task_facade.get_task(task_id=task, owner_id=owner, full=True)
    assert final["submission_sources"][0]["status"] == "parsed"
    assert "submission_identities_unresolved" not in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]


@pytest.mark.parametrize("reverse", [False, True])
def test_identity_fields_across_question_units_remain_available_for_review(reverse):
    from types import SimpleNamespace
    from backend.agents.ingest_agent import SubmissionSourceParseResult
    from backend.services.submission_incremental import merge_source_answer_units

    source = SimpleNamespace(filename="work.txt", source_id="source", stored_file_id="stored")
    units = []
    for qid, sid, name in [("q1", "001-A", "work.txt"), ("q2", "work.txt", "Li Si")]:
        student = dict(stu_id=sid, stu_name=name, stu_ans=[_answer(qid, "Retain answer")],
                       identity_status="needs_review", identity_match_method="filename")
        units.append(SubmissionSourceParseResult("source", "stored", "work.txt", "identity_conflict", student, sid,
                                                 1, (), "identity_needs_review", "identity", False))
    merged = merge_source_answer_units(units[::-1] if reverse else units, source, {"q1": {}, "q2": {}})
    assert (merged.student_candidate, merged.student["stu_name"]) == ("001-A", "Li Si")
    assert merged.status == "identity_conflict" and merged.student["identity_status"] == "needs_review"
