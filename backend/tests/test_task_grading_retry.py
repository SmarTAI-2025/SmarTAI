"""Explicit retry intents, frozen whole-batch runs and safe publication; no LLM."""
import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select

from backend.db import assignment_repository as assignments
from backend.db import grading_repository as grading
from backend.db import submission_repository as submissions
from backend.db import workflow_repository as workflows
from backend.db.models import AssignmentRecord, GradingRunRecord, UserRecord
from backend.db.session import session_scope
from backend.domain.errors import DomainError, LeaseLost, ResultNotReleasable, VersionConflict
from backend.models import Correction
from backend.services import grading_adapter, grading_runs, task_facade
REAL_REGISTRY_FOR = grading_runs._registry_for
from backend.tests.test_grading_source_readiness import _seed_legacy_structured_case
from backend.tests.test_postgres_integration import pg_database  # noqa: F401


class Registry:
    def select(self, *_args, **_kwargs):
        return self

    def count(self):
        return 1


@pytest.fixture
def case(monkeypatch, request):
    if os.environ.get("SMARTAI_TEST_POSTGRES_URL") == os.environ.get("SMARTAI_DATABASE_URL"):
        request.getfixturevalue("pg_database")
    seeded = _seed_legacy_structured_case(question_count=2)
    task_id, owner = seeded["assignment_id"], seeded["owner_id"]
    monkeypatch.setattr(grading_runs, "_registry_for", lambda _owner: Registry())
    monkeypatch.setattr(grading_runs.settings, "e2e_fake_provider", False)
    return task_id, owner


def start(case, key, revision=None):
    task_id, owner = case
    if revision is None:
        revision = workflows.get_live_workflow(task_id, owner_id=owner).workflow_revision
    return task_facade.start_task_grading(task_id=task_id, owner_id=owner,
                                        expected_workflow_revision=revision, request_id=key)


def finish(case, run_id, monkeypatch, failed=("q2",)):
    calls = []

    async def fake_batch(**kwargs):
        calls.append(kwargs)
        return [{"student_id": sid, "corrections": [Correction(
            q_id=qid, type="short", score=0 if qid in failed else 8,
            max_score=10, confidence=0 if qid in failed else 1,
            synthesis_method="all_failed" if qid in failed else "single",
            comment="Transient timeout" if qid in failed else "Recovered", steps=[],
        ) for qid in kwargs["problem_store"] if kwargs.get("question_ids_by_student") is None or qid in kwargs["question_ids_by_student"].get(sid, set())]} for sid in kwargs["student_store"]]

    monkeypatch.setattr(grading_adapter, "grade_batch", fake_batch)
    asyncio.run(grading_runs.process_run(run_id=run_id, worker_id="retry-test"))
    assert len(calls) == 1
    return calls[0]


@pytest.mark.parametrize("failures", [("q2",), ("q1", "q2")])
def test_same_inputs_retry_preserves_history_and_teacher_edits_and_can_publish(case, monkeypatch, failures):
    first = start(case, "first")
    original_input = finish(case, first["job_id"], monkeypatch, failures)
    old_rows = grading.list_results_for_run(first["job_id"])
    assert grading.get_run(first["job_id"]).status == "partial_failed"
    assert task_facade.task_results(task_id=case[0], owner_id=case[1])["retry_scope"] == "failed_only"
    with pytest.raises(ResultNotReleasable):
        task_facade.confirm_finalization(task_id=case[0], owner_id=case[1], expected_revision=workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision)
    success = next((row for row in old_rows if row.ai_score is not None), None)
    if success:
        grading.add_teacher_review(success.id, teacher_id=case[1], new_score=9,
                                   new_comment="Keep this teacher edit", confirm=True)
    retry_revision = workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision
    retry = start(case, "retry", retry_revision)
    assert retry["status"] == "started" and retry["job_id"] != first["job_id"]
    assert start(case, "retry", retry_revision)["job_id"] == retry["job_id"]
    retried_input = finish(case, retry["job_id"], monkeypatch, ())
    assert retried_input["problem_store"] == original_input["problem_store"]
    assert retried_input["student_store"] == original_input["student_store"]
    assert retried_input["grading_setup"] == original_input["grading_setup"]
    replay = start(case, "retry", retry_revision)
    assert replay["status"] == "already_finished" and replay["job_id"] == retry["job_id"]
    assert len(grading.list_runs_for_assignment(case[0], actor_id=case[1])) == 2
    new_rows = grading.list_results_for_run(retry["job_id"])
    assert all(row.result_status == "graded" and row.teacher_review is None for row in new_rows)
    if success:
        kept = next(row for row in grading.list_results_for_run(first["job_id"]) if row.id == success.id)
        assert kept.ai_score == 8 and kept.effective_score == 9
        assert kept.teacher_review["new_comment"] == "Keep this teacher edit"
    assert task_facade.finalization(task_id=case[0], owner_id=case[1])["remaining_review_count"] == 0
    assert start(case, "completed-cache")["status"] == "already_done"
    final = task_facade.confirm_finalization(task_id=case[0], owner_id=case[1], expected_revision=workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision)
    assert final["status"] == "ok"


def test_again_failed_is_truthful_and_next_explicit_intent_can_retry(case, monkeypatch):
    for key in ("first", "second"):
        run = start(case, key)
        finish(case, run["job_id"], monkeypatch, ("q1", "q2"))
        assert grading.get_run(run["job_id"]).status == "partial_failed"
        assert not task_facade.finalization(task_id=case[0], owner_id=case[1])["ready_for_confirmation"]
    third = start(case, "third")
    finish(case, third["job_id"], monkeypatch, ())
    assert grading.get_run(third["job_id"]).status == "completed"


def test_incomplete_grading_blocks_review_until_teacher_supplies_missing_scores(case, monkeypatch):
    first = start(case, "teacher-repair")
    finish(case, first["job_id"], monkeypatch, ("q2",))
    snapshot = task_facade.get_task(task_id=case[0], owner_id=case[1])
    assert snapshot["status"] == "error"
    assert snapshot["last_failed_job_id"] == first["job_id"]
    for row in grading.list_results_for_run(first["job_id"]):
        if row.effective_score is None:
            grading.add_teacher_review(row.id, teacher_id=case[1], new_score=7,
                                       new_comment="Teacher checked the original work", confirm=True)
    assert task_facade.get_task(task_id=case[0], owner_id=case[1])["status"] == "graded"


def test_active_alias_replay_after_terminal_does_not_execute_again(case, monkeypatch):
    first = start(case, "first")
    revision = workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision
    alias = start(case, "join-running", revision)
    assert alias["status"] == "already_running" and alias["job_id"] == first["job_id"]
    finish(case, first["job_id"], monkeypatch)
    replay = start(case, "join-running", revision)
    assert replay["status"] == "already_finished" and replay["job_id"] == first["job_id"]
    assert len(grading.list_runs_for_assignment(case[0], actor_id=case[1])) == 1
    with pytest.raises(VersionConflict, match="grading_request_conflict"):
        start(case, "join-running", revision + 1)


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_clicks_and_http_replays_have_one_effective_run(case, same_key, monkeypatch):
    revision = workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision
    gate = Barrier(2)

    def request(index):
        gate.wait(timeout=5)
        return start(case, "same" if same_key else f"click-{index}", revision)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(request, [0, 1]))
    assert len({response["job_id"] for response in responses}) == 1
    assert len(grading.list_runs_for_assignment(case[0], actor_id=case[1])) == 1
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(workflows.GradingRequestRecord)) == (1 if same_key else 2)
    finish(case, responses[0]["job_id"], monkeypatch)
    for index in (0, 1):
        replay = start(case, "same" if same_key else f"click-{index}", revision)
        assert replay["status"] == "already_finished"
    assert len(grading.list_runs_for_assignment(case[0], actor_id=case[1])) == 1


def test_frozen_inputs_new_revision_and_provider_change_fail_safely(case, monkeypatch):
    run = start(case, "first")
    finish(case, run["job_id"], monkeypatch)
    question = assignments.get_questions_by_assignment(case[0])[0]
    with session_scope() as session:
        from backend.db.models import AssignmentQuestionRecord
        row = session.get(AssignmentQuestionRecord, question.id)
        row.stem = "Changed question"
        row.version += 1
    new_run = start(case, "changed-input")
    frozen = workflows.get_run_setup(new_run["job_id"])
    assert frozen.input_manifest["questions"][0]["stem"] == "Changed question"
    from backend.db.provider_repository import set_provider_enabled
    set_provider_enabled(case[1], frozen.setup["primary_provider_id"], False)
    with pytest.raises(DomainError):
        asyncio.run(grading_runs.process_run(run_id=new_run["job_id"], worker_id="provider-changed"))
    assert grading.get_run(new_run["job_id"]).status == "failed"
    assert not task_facade.finalization(task_id=case[0], owner_id=case[1])["ready_for_confirmation"]


def test_deleted_or_other_owner_cannot_replay_request(case):
    start(case, "first")
    with pytest.raises(DomainError):
        start((case[0], "other-owner"), "first", 0)
    with session_scope() as session:
        session.get(AssignmentRecord, case[0]).deletion_requested_at = time.time()
    with pytest.raises(DomainError):
        start(case, "first", 0)


def test_run_creation_rolls_back_entire_bundle(case, monkeypatch):
    before = workflows.get_live_workflow(case[0], owner_id=case[1])
    def fail_record(*_args, **_kwargs):
        raise RuntimeError("simulate disconnect before transaction commit")
    monkeypatch.setattr(grading, "_record_request_in_session", fail_record)
    with pytest.raises(RuntimeError):
        start(case, "first")
    after = workflows.get_live_workflow(case[0], owner_id=case[1])
    assert after.workflow_revision == before.workflow_revision and after.active_job_id is None
    assert grading.list_runs_for_assignment(case[0], actor_id=case[1]) == []
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(workflows.GradingRunSetupRecord)) == 0


@pytest.mark.parametrize("legacy_setup", [False, True])
def test_restart_fences_interrupted_work_without_automatic_model_replay(case, monkeypatch, legacy_setup):
    run = start(case, "first")
    grading.claim_lease(run["job_id"], worker_id="dead-worker", lease_seconds=60)
    with session_scope() as session:
        session.get(GradingRunRecord, run["job_id"]).lease_expiry = time.time() - 1
        if legacy_setup:
            frozen = session.get(workflows.GradingRunSetupRecord, run["job_id"])
            frozen.input_manifest = {key: value for key, value in frozen.input_manifest.items() if key != "grading_recovery_policy"}
    def forbidden(*_args, **_kwargs):
        pytest.fail("must not silently replay potentially paid work")
    monkeypatch.setattr(grading_adapter, "run_grading", forbidden)
    assert run["job_id"] in grading_runs.poll_queued_runs()
    asyncio.run(grading_runs.process_run(run_id=run["job_id"], worker_id="replacement"))
    assert grading.get_run(run["job_id"]).status == "failed"
    assert workflows.get_live_workflow(case[0], owner_id=case[1]).active_job_id is None
    with pytest.raises(LeaseLost):
        grading.mark_completed(run["job_id"], worker_id="dead-worker", completed=1, failed=0)
    assert start(case, "manual-retry")["job_id"] != run["job_id"]


def test_same_worker_duplicate_process_does_not_call_model(case, monkeypatch):
    run = start(case, "first")
    grading.claim_lease(run["job_id"], worker_id="same-worker", lease_seconds=60)
    def forbidden(*_args, **_kwargs):
        pytest.fail("duplicate live process must not invoke the model")
    monkeypatch.setattr(grading_adapter, "run_grading", forbidden)
    asyncio.run(grading_runs.process_run(run_id=run["job_id"], worker_id="same-worker"))
    assert grading.get_run(run["job_id"]).status == "running"


def test_changed_answer_revision_is_frozen_for_new_intent(case, monkeypatch):
    first = start(case, "first")
    finish(case, first["job_id"], monkeypatch)
    old_revision = grading.list_frozen_submissions(first["job_id"])[0]
    revision = submissions.get_revision(revision_id=old_revision.id, actor_id=case[1])
    changed = submissions.add_revision(
        submission_id=revision.submission_id, student_id=old_revision.student_id,
        source="online", answers=[{**answer.model_dump(), "content": "Corrected answer"} for answer in revision.answers],
    )
    retry = start(case, "changed-answer")
    assert grading.list_frozen_submissions(retry["job_id"])[0].id == changed.id
    assert grading.list_frozen_submissions(first["job_id"])[0].id == old_revision.id


@pytest.mark.parametrize("change", ["disabled", "role"])
def test_permission_change_after_freezing_blocks_execution(case, monkeypatch, change):
    # Restore the production identity gate, never reach a real provider.
    monkeypatch.setattr(grading_runs, "_registry_for", REAL_REGISTRY_FOR)
    run = start(case, "first")
    with session_scope() as session:
        owner = session.get(UserRecord, case[1])
        if change == "disabled":
            owner.is_active = False
        else:
            owner.role = "student"
    with pytest.raises(DomainError):
        asyncio.run(grading_runs.process_run(run_id=run["job_id"], worker_id="revoked"))
    assert grading.get_run(run["job_id"]).status == "failed"


def test_quota_rejection_is_not_success_and_new_intent_can_recover(case, monkeypatch):
    from backend.services.model_quota import admit_model_call, ModelQuotaError
    from backend.config import settings
    monkeypatch.setattr(settings, "shared_pool_daily_request_limit", 0)
    async def exhausted(**_kwargs):
        admit_model_call(case[1], "shared", estimated_input_tokens=1)
        pytest.fail("quota must reject before provider invocation")
    monkeypatch.setattr(grading_adapter, "grade_batch", exhausted)
    first = start(case, "quota-first")
    with pytest.raises(ModelQuotaError):
        asyncio.run(grading_runs.process_run(run_id=first["job_id"], worker_id="quota"))
    assert grading.get_run(first["job_id"]).status == "failed"
    assert not task_facade.finalization(task_id=case[0], owner_id=case[1])["ready_for_confirmation"]
    monkeypatch.setattr(settings, "shared_pool_daily_request_limit", 100)
    retry = start(case, "quota-restored")
    finish(case, retry["job_id"], monkeypatch, ())
    assert grading.get_run(retry["job_id"]).status == "completed"


def test_legacy_client_revision_deduplication_still_allows_later_retry(case, monkeypatch):
    revision = workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision
    first = task_facade.start_task_grading(task_id=case[0], owner_id=case[1], expected_workflow_revision=revision)
    finish(case, first["job_id"], monkeypatch)
    duplicate = task_facade.start_task_grading(task_id=case[0], owner_id=case[1], expected_workflow_revision=revision)
    assert duplicate["job_id"] == first["job_id"] and duplicate["status"] == "already_finished"
    new_revision = workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision
    retry = task_facade.start_task_grading(task_id=case[0], owner_id=case[1], expected_workflow_revision=new_revision)
    assert retry["status"] == "started" and retry["job_id"] != first["job_id"]


def test_request_alias_and_publication_share_safe_lock_order(case, monkeypatch):
    first = start(case, "first")
    finish(case, first["job_id"], monkeypatch, ())
    revision = workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision
    gate = Barrier(2)

    def alias():
        gate.wait(timeout=5)
        return grading.bind_grading_request(
            assignment_id=case[0], teacher_id=case[1], request_id="publication-alias",
            request_revision=revision, run_id=first["job_id"],
        )

    def publish():
        gate.wait(timeout=5)
        return task_facade.confirm_finalization(task_id=case[0], owner_id=case[1], expected_revision=revision)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, p = pool.submit(alias), pool.submit(publish)
        assert a.result(timeout=10).id == first["job_id"]
        assert p.result(timeout=10)["status"] == "ok"
    assert start(case, "publication-alias", revision)["job_id"] == first["job_id"]


def test_fast_terminal_legacy_run_during_pointer_repair_keeps_intent(case, monkeypatch):
    legacy = grading_runs.create_run(assignment_id=case[0], teacher_id=case[1])
    repair = workflows.bind_existing_active_grading_run
    def finish_before_repair(*args, **kwargs):
        finish(case, legacy.id, monkeypatch)
        return repair(*args, **kwargs)
    monkeypatch.setattr(workflows, "bind_existing_active_grading_run", finish_before_repair)
    response = start(case, "join-fast-legacy")
    assert response["status"] == "already_finished" and response["job_id"] == legacy.id
    assert len(grading.list_runs_for_assignment(case[0], actor_id=case[1])) == 1


@pytest.mark.parametrize("switch_model", [False, True])
def test_failed_only_retry_preserves_success_and_teacher_review(case, monkeypatch, switch_model):
    first = start(case, "original")
    finish(case, first["job_id"], monkeypatch)
    original = grading.list_results_for_run(first["job_id"])
    success = next(row for row in original if row.q_id == "q1")
    grading.add_teacher_review(success.id, teacher_id=case[1], new_score=9,
                               new_comment="Preserve teacher feedback", confirm=True)
    if switch_model:
        monkeypatch.setattr("backend.services.grading_input_security.provider_configuration_fingerprint",
                            lambda **kwargs: "changed-model")
    retry = task_facade.start_task_grading(task_id=case[0], owner_id=case[1],
        expected_workflow_revision=workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision,
        request_id="failed-only", retry_scope="failed_only")
    called = finish(case, retry["job_id"], monkeypatch, ())
    assert all(qids == {"q2"} for qids in called["question_ids_by_student"].values())
    rows = grading.list_results_for_run(retry["job_id"])
    kept = next(row for row in rows if row.q_id == "q1")
    assert kept.ai_score == success.ai_score == 8
    assert kept.effective_score == 9 and kept.teacher_review["confirmed"]
    assert kept.teacher_review["new_comment"] == "Preserve teacher feedback"
    assert grading.get_run(retry["job_id"]).status == "completed"
    assert len(grading.list_results_for_run(first["job_id"])) == 2


def test_failed_only_retry_does_not_repeat_manually_scored_failure(case, monkeypatch):
    first = start(case, "original")
    finish(case, first["job_id"], monkeypatch, ("q1", "q2"))
    row = next(row for row in grading.list_results_for_run(first["job_id"]) if row.q_id == "q1")
    grading.add_teacher_review(row.id, teacher_id=case[1], new_score=0, new_comment="Blank answer", confirm=True)
    retry = task_facade.start_task_grading(task_id=case[0], owner_id=case[1],
        expected_workflow_revision=workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision,
        request_id="retain-zero", retry_scope="failed_only")
    called = finish(case, retry["job_id"], monkeypatch, ())
    assert all(qids == {"q2"} for qids in called["question_ids_by_student"].values())
    kept = next(row for row in grading.list_results_for_run(retry["job_id"]) if row.q_id == "q1")
    assert kept.ai_score is None and kept.effective_score == 0
    assert kept.teacher_review["new_comment"] == "Blank answer"


def test_failed_only_rejects_changed_grading_content(case, monkeypatch):
    first = start(case, "original")
    finish(case, first["job_id"], monkeypatch)
    with session_scope() as session:
        from backend.db.models import AssignmentQuestionRecord
        question = session.scalar(select(AssignmentQuestionRecord).where(AssignmentQuestionRecord.assignment_id == case[0]))
        question.stem = "Changed grading input"
        question.version += 1
    with pytest.raises(VersionConflict, match="Grading inputs changed"):
        task_facade.start_task_grading(task_id=case[0], owner_id=case[1],
            expected_workflow_revision=workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision,
            request_id="changed", retry_scope="failed_only")
    assert len(grading.list_runs_for_assignment(case[0], actor_id=case[1])) == 1


def test_question_fields_confirm_independently_and_block_failed_content(case):
    from backend.db.models import AssignmentQuestionRecord
    from backend.domain.errors import ValidationError
    with session_scope() as session:
        row = session.scalar(select(AssignmentQuestionRecord).where(AssignmentQuestionRecord.assignment_id == case[0], AssignmentQuestionRecord.q_id == "q1"))
        row.source = {"presentation": {"max_score_review_status": "needs_review", "review_status": "needs_review", "preparation_issues": [
            {"issue_id": "score", "field": "max_score", "code": "default_max_score_requires_review", "status": "open", "severity": "warning"},
            {"issue_id": "ocr", "field": "stem", "code": "recognition_partial", "status": "open", "severity": "warning"},
        ]}}
    def confirm(fields=None):
        return task_facade.update_problem(task_id=case[0], owner_id=case[1], q_id="q1",
            expected_revision=workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision,
            patch={"review_status": "confirmed", **({"review_fields": fields} if fields else {})})["problem"]
    score = confirm(["max_score"])
    assert score["max_score_review_status"] == "confirmed"
    assert score["review_status"] == "needs_review"
    assert [i["field"] for i in score["preparation_issues"] if i["status"] == "open"] == ["stem"]
    assert confirm(["stem"])["review_status"] == "confirmed"
    with session_scope() as session:
        row = session.scalar(select(AssignmentQuestionRecord).where(AssignmentQuestionRecord.assignment_id == case[0], AssignmentQuestionRecord.q_id == "q1"))
        row.stem = ""
    with pytest.raises(ValidationError, match="Fix the failed recognition"):
        confirm()


def test_failed_answer_requires_repair_but_genuine_blank_can_be_confirmed(case):
    from backend.db.models import SubmissionAnswerRecord
    from backend.domain.errors import ValidationError
    task = task_facade.get_task(task_id=case[0], owner_id=case[1])
    student_id = next(iter(task["student_data"].values()))["stu_id"]
    with session_scope() as session:
        rows = session.scalars(select(SubmissionAnswerRecord)).all()
        for row in rows:
            row.content = ""
            row.flag = ["recognition_failed", "external_annotation_present"] if row.q_id == "q1" else []
    def update(qid, patch):
        return task_facade.update_student_answer(task_id=case[0], owner_id=case[1], display_student_id=student_id, q_id=qid,
            expected_revision=workflows.get_live_workflow(case[0], owner_id=case[1]).workflow_revision, patch=patch)
    with pytest.raises(ValidationError, match="Retry or correct"):
        update("q1", {"review_status": "confirmed"})
    assert update("q2", {"review_status": "confirmed"})["answer"]["review_status"] == "confirmed"
    repaired = update("q1", {"content": "Teacher corrected transcription", "review_status": "confirmed"})["answer"]
    assert repaired["review_status"] == "confirmed"
    assert repaired["flag"] == ["external_annotation_present"]


def test_real_batch_dispatches_only_requested_failed_pairs(monkeypatch):
    from backend.agents import grading_agent
    calls = []
    async def grade_student(**kwargs):
        calls.append(kwargs)
        return {"student_id": kwargs["student_data"]["stu_id"], "corrections": []}
    monkeypatch.setattr(grading_agent, "grade_student", grade_student)
    students = {sid: {"stu_id": sid, "stu_ans": [{"q_id": "q1"}, {"q_id": "q2"}]} for sid in ("a", "b")}
    asyncio.run(grading_agent.grade_batch(student_store=students, problem_store={"q1": {}, "q2": {}}, registry=Registry(), question_ids_by_student={"a": {"q2"}, "b": set()}))
    assert len(calls) == 1
    assert calls[0]["student_data"]["stu_ans"] == [{"q_id": "q2"}]
    assert calls[0]["problem_store"] == {"q2": {}}
