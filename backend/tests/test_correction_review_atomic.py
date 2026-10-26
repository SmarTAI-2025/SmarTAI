"""A rejected review must leave the whole published/draft result unchanged."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from backend.db import grading_repository, workflow_repository
from backend.db.models import (
    GradeResultRecord, GradingRunEventRecord, GradingRunRecord,
    GradingRunSubmissionRecord, TeacherReviewRecord,
)
from backend.db.session import session_scope
from backend.services import task_facade
from backend.tests.test_task_finalization_contract import _prepared_task


def _snapshot():
    records = (
        workflow_repository.AssignmentWorkflowRecord,
        workflow_repository.GradingRunSetupRecord,
        workflow_repository.ResultArtifactManifestRecord,
        GradingRunRecord, GradingRunSubmissionRecord, GradingRunEventRecord,
        GradeResultRecord, TeacherReviewRecord,
    )
    with session_scope() as session:
        return {
            model.__tablename__: sorted(
                [dict(row) for row in session.execute(select(model.__table__)).mappings()],
                key=lambda row: repr(sorted(row.items())),
            )
            for model in records
        }


def _case(state="draft"):
    from fastapi.testclient import TestClient
    from backend.auth import create_token
    from backend.main import app

    seeded = _prepared_task()
    revision = 0
    if state == "released":
        revision = task_facade.generate_artifacts(
            task_id=seeded["task_id"], owner_id=seeded["owner_id"], expected_revision=0,
        )["workflow_revision"]
    if state == "missing":
        with session_scope() as session:
            session.delete(session.get(GradeResultRecord, seeded["optional_result_id"]))
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer " + create_token(seeded["owner_id"], "teacher")
    url = f"/tasks/{seeded['task_id']}/reviews/{seeded['student_id']}/q-optional"
    payload = {"expected_workflow_revision": revision, "teacher_score": 8,
               "teacher_comment": "Checked", "confirm": True}
    return seeded, client, url, payload


@pytest.mark.parametrize("state", ["draft", "released", "missing"])
@pytest.mark.parametrize("score", [11, -1, "NaN", "Infinity", "-Infinity"])
def test_invalid_score_has_no_persisted_side_effects(state, score):
    _, client, url, payload = _case(state)
    before = _snapshot()
    response = client.put(url, json={**payload, "teacher_score": score})
    assert response.status_code == 422, response.text
    assert _snapshot() == before


@pytest.mark.parametrize("state", ["draft", "released"])
@pytest.mark.parametrize("bad_target", ["student", "question", "stale", "missing_version"])
def test_rejected_review_target_and_version_preserve_state(state, bad_target):
    seeded, client, url, payload = _case(state)
    if bad_target == "student":
        url = url.replace(seeded["student_id"], "unknown-student")
    elif bad_target == "question":
        url = url.replace("q-optional", "unknown-question")
    elif bad_target == "stale":
        payload["expected_workflow_revision"] += 10
    else:
        del payload["expected_workflow_revision"]
    before = _snapshot()
    response = client.put(url, json=payload)
    expected = {"student": 404, "question": 404, "stale": 409, "missing_version": 422}[bad_target]
    assert response.status_code == expected, response.text
    assert _snapshot() == before


@pytest.mark.parametrize("run_state", ["missing", "running", "failed", "cancelled"])
def test_unavailable_run_does_not_consume_revision(run_state):
    seeded, client, url, payload = _case()
    with session_scope() as session:
        workflow = session.get(workflow_repository.AssignmentWorkflowRecord, seeded["task_id"])
        if run_state == "missing":
            workflow.grading_job_id = "missing-run"
        else:
            session.get(GradingRunRecord, seeded["run_id"]).status = run_state
    before = _snapshot()
    response = client.put(url, json=payload)
    assert response.status_code == (404 if run_state == "missing" else 409), response.text
    assert _snapshot() == before


def test_another_teacher_cannot_edit_result():
    from backend.auth import create_token
    from backend.db.models import UserRecord

    _, client, url, payload = _case("released")
    with session_scope() as session:
        session.add(UserRecord(id="other-reviewer", username="other-reviewer", password_hash="hash",
                               role="teacher", is_active=True))
    client.headers["Authorization"] = "Bearer " + create_token("other-reviewer", "teacher")
    before = _snapshot()
    assert client.put(url, json=payload).status_code == 404
    assert _snapshot() == before


@pytest.mark.parametrize("state", ["draft", "released", "missing"])
@pytest.mark.parametrize("score", [0, 10])
@pytest.mark.parametrize("confirm", [False, True])
def test_valid_boundary_review_commits_once_and_preserves_history(state, score, confirm):
    seeded, client, url, payload = _case(state)
    payload["confirm"] = confirm
    original = grading_repository.list_results_for_run(seeded["run_id"])
    history = None
    if state == "released":
        history = task_facade.artifact_bytes(
            task_id=seeded["task_id"], owner_id=seeded["owner_id"],
            version=1, artifact_id="learning_report_md",
        )[0]
    response = client.put(url, json={**payload, "teacher_score": score})
    assert response.status_code == 200, response.text
    assert response.json()["workflow_revision"] == payload["expected_workflow_revision"] + 1
    assert response.json()["correction"]["teacher_score"] == score
    assert response.json()["correction"]["requires_human_review"] is False
    assert response.json()["correction"]["review_status"] == ("confirmed" if confirm else "edited")
    runs = grading_repository.list_runs_for_assignment(seeded["task_id"], actor_id=seeded["owner_id"])
    assert len(runs) == (2 if state == "released" else 1)
    if state == "released":
        assert grading_repository.list_results_for_run(seeded["run_id"]) == original
        assert task_facade.artifact_bytes(
            task_id=seeded["task_id"], owner_id=seeded["owner_id"],
            version=1, artifact_id="learning_report_md",
        )[0] == history
        assert runs[-1].released_at is None
    committed = _snapshot()
    assert client.put(url, json={**payload, "teacher_score": score}).status_code == 409
    assert _snapshot() == committed


def test_unchanged_confirmation_still_records_teacher_review():
    seeded, client, url, payload = _case()
    response = client.put(url, json={**payload, "teacher_score": 9, "teacher_comment": ""})
    assert response.status_code == 200, response.text
    assert response.json()["unchanged"] is True
    review = grading_repository.latest_teacher_review(seeded["optional_result_id"])
    assert review.confirmed is True
    assert review.new_score == 9


@pytest.mark.parametrize("state", ["draft", "released", "missing"])
def test_failure_after_review_flush_rolls_back_every_change(state):
    _, client, url, payload = _case(state)
    before = _snapshot()
    flushed = []

    def fail_after_review_flush(session, _context):
        # Fire after the actual INSERT, not before validation or mutation.
        if any(isinstance(row, TeacherReviewRecord) for row in session.new):
            flushed.append(True)
            raise RuntimeError("injected review persistence failure")

    event.listen(Session, "after_flush", fail_after_review_flush)
    try:
        with pytest.raises(RuntimeError, match="injected review persistence failure"):
            client.put(url, json=payload)
    finally:
        event.remove(Session, "after_flush", fail_after_review_flush)
    assert flushed
    assert _snapshot() == before
    assert client.put(url, json=payload).status_code == 200


@pytest.mark.parametrize("state", ["draft", "released"])
def test_concurrent_reviews_have_one_revision_winner(state):
    seeded, client, url, payload = _case(state)
    barrier = threading.Barrier(2)

    def save(score):
        barrier.wait(timeout=10)
        return client.put(url, json={**payload, "teacher_score": score})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(save, [7, 8]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json() for response in responses if response.status_code == 200)
    workflow = workflow_repository.get_live_workflow(seeded["task_id"], owner_id=seeded["owner_id"])
    assert workflow.workflow_revision == payload["expected_workflow_revision"] + 1
    runs = grading_repository.list_runs_for_assignment(seeded["task_id"], actor_id=seeded["owner_id"])
    assert len(runs) == (2 if state == "released" else 1)
    reviewed = next(row for row in grading_repository.list_results_for_run(runs[-1].id) if row.q_id == "q-optional")
    assert reviewed.effective_score == winner["correction"]["teacher_score"]


def test_retired_comment_writer_is_unreachable():
    from backend.main import app

    seeded, client, _, _ = _case("released")
    before = _snapshot()
    response = client.post(f"/tasks/{seeded['task_id']}/teacher_comment", json={
        "student_id": seeded["student_id"], "q_id": "q-optional", "comment": "old client",
    })
    assert response.status_code == 404
    assert "/tasks/{task_id}/teacher_comment" not in app.openapi()["paths"]
    assert _snapshot() == before


@pytest.mark.parametrize("state", ["draft", "missing"])
def test_frozen_maximum_is_authoritative_after_question_change(state):
    from backend.db.models import AssignmentQuestionRecord

    seeded, client, url, payload = _case(state)
    with session_scope() as session:
        questions = session.scalars(select(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == seeded["task_id"],
        )).all()
        session.add(workflow_repository.GradingRunSetupRecord(
            grading_run_id=seeded["run_id"], assignment_id=seeded["task_id"],
            owner_id=seeded["owner_id"], setup={}, fingerprint="frozen-review-test",
            input_manifest={"questions": [{"id": row.id, "max_score": 10} for row in questions]},
            created_at=0,
        ))
        for question in questions:
            question.max_score = 20
    before = _snapshot()
    response = client.put(url, json={**payload, "teacher_score": 11})
    assert response.status_code == 422, response.text
    assert _snapshot() == before
    assert client.put(url, json={**payload, "teacher_score": 10}).status_code == 200


def test_rewind_between_candidate_read_and_lock_cannot_edit_hidden_history(monkeypatch):
    seeded, client, url, payload = _case()
    original_list = grading_repository.list_runs_for_assignment
    rewound = []

    def rewind_after_read(*args, **kwargs):
        runs = original_list(*args, **kwargs)
        workflow_repository.update_workflow(
            seeded["task_id"], owner_id=seeded["owner_id"], bump_revision=False,
            grading_job_id=None, presentation_status="problems_ready",
        )
        rewound.append(_snapshot())
        return runs

    monkeypatch.setattr(grading_repository, "list_runs_for_assignment", rewind_after_read)
    response = client.put(url, json=payload)
    assert response.status_code == 409, response.text
    assert rewound and _snapshot() == rewound[-1]


def test_busy_task_review_does_not_consume_revision():
    seeded, client, url, payload = _case("released")
    workflow_repository.update_workflow(
        seeded["task_id"], owner_id=seeded["owner_id"], bump_revision=False,
        active_job_id="active-recognition",
    )
    before = _snapshot()
    assert client.put(url, json=payload).status_code == 409
    assert _snapshot() == before


def test_report_generation_and_review_share_one_revision_winner():
    seeded, client, url, payload = _case()
    barrier = threading.Barrier(2)

    def save(action):
        barrier.wait(timeout=10)
        if action == "review":
            return client.put(url, json=payload)
        return client.post(f"/tasks/{seeded['task_id']}/artifacts/generate", json={
            "expected_workflow_revision": payload["expected_workflow_revision"],
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(save, ["review", "report"]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    workflow = workflow_repository.get_live_workflow(seeded["task_id"], owner_id=seeded["owner_id"])
    assert workflow.workflow_revision == 1
    runs = grading_repository.list_runs_for_assignment(seeded["task_id"], actor_id=seeded["owner_id"])
    assert len(runs) == 1
    if responses[0].status_code == 200:
        assert runs[0].released_at is None
        assert workflow.final_result_version == 0
    else:
        assert runs[0].released_at is not None
        assert grading_repository.latest_teacher_review(seeded["optional_result_id"]) is None


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf"), -1, 11])
def test_internal_review_call_cannot_bypass_score_validation(score):
    from backend.domain.errors import ValidationError

    seeded, _, _, payload = _case("released")
    before = _snapshot()
    with pytest.raises(ValidationError, match="review_score_out_of_range"):
        task_facade.update_correction_review(
            task_id=seeded["task_id"], owner_id=seeded["owner_id"],
            display_student_id=seeded["student_id"], q_id="q-optional",
            teacher_score=score, teacher_comment="", confirm=True,
            expected_revision=payload["expected_workflow_revision"],
        )
    assert _snapshot() == before


@pytest.mark.parametrize("state", ["draft", "released", "missing"])
@pytest.mark.parametrize("raw_score", ["1e999", "-1e999", "NaN", "Infinity", '{"nested":[1e999]}'])
def test_raw_nonfinite_score_returns_json_422_without_writes(state, raw_score):
    import json
    from fastapi.testclient import TestClient
    from backend.main import app

    _, authenticated, url, payload = _case(state)
    client = TestClient(app, raise_server_exceptions=False)
    body = json.dumps(payload).replace('"teacher_score": 8', '"teacher_score": ' + raw_score)
    before = _snapshot()
    response = client.put(url, content=body, headers={
        "Authorization": authenticated.headers["Authorization"], "Content-Type": "application/json",
    })
    assert _snapshot() == before
    assert response.status_code == 422, response.text
    # The response itself must be strict JSON, with no NaN/Infinity extensions.
    def reject_constant(value):
        raise AssertionError(f"Invalid JSON response constant: {value}")
    detail = json.loads(response.text, parse_constant=reject_constant)["detail"][0]
    assert detail["loc"] == ["body", "teacher_score"]
    assert detail["input"] == ({"nested": [None]} if raw_score.startswith("{") else None)


def test_finite_validation_error_preserves_input_and_constraint():
    _, client, url, payload = _case()
    response = client.put(url, json={**payload, "teacher_score": -1})
    assert response.status_code == 422
    assert response.json()["detail"] == [{
        "type": "greater_than_equal", "loc": ["body", "teacher_score"],
        "msg": "Input should be greater than or equal to 0", "input": -1,
        "ctx": {"ge": 0},
    }]


def test_overflow_score_returns_422_over_real_http():
    import json
    import socket
    import time
    import httpx
    import uvicorn
    from backend.main import app

    _, authenticated, url, payload = _case("released")
    before = _snapshot()
    body = json.dumps(payload).replace('"teacher_score": 8', '"teacher_score": 1e999')
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="critical"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline and thread.is_alive():
                time.sleep(.01)
            assert server.started
            response = httpx.put(f"http://127.0.0.1:{listener.getsockname()[1]}{url}",
                content=body, headers={"Authorization": authenticated.headers["Authorization"],
                                       "Content-Type": "application/json"}, trust_env=False)
            assert _snapshot() == before
            assert response.status_code == 422, response.text
            assert response.json()["detail"][0]["type"] == "finite_number"
            assert response.json()["detail"][0]["input"] is None
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()


def _published_mixed_reviews(confirmed):
    seeded, client, url, payload = _case()
    # Give this result two reviews with opposite flags, so the copy must use
    # the latest review's state rather than any earlier confirmation.
    for target, score, flag in [
        (url, 6, not confirmed), (url, 7, confirmed),
        (url.replace("q-optional", "q-required"), 8, not confirmed),
    ]:
        response = client.put(target, json={**payload, "teacher_score": score,
                                           "confirm": flag})
        assert response.status_code == 200, response.text
        payload["expected_workflow_revision"] = response.json()["workflow_revision"]
    response = client.post(f"/tasks/{seeded['task_id']}/artifacts/generate", json={
        "expected_workflow_revision": payload["expected_workflow_revision"],
    })
    assert response.status_code == 200, response.text
    payload.update(expected_workflow_revision=response.json()["workflow_revision"],
                   teacher_score=7, confirm=False)
    return seeded, client, url, payload


@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("action", ["other_question", "same_content", "confirm"])
def test_published_review_copy_preserves_confirmation(confirmed, action):
    seeded, client, url, payload = _published_mixed_reviews(confirmed)
    before = _snapshot()
    history = task_facade.artifact_bytes(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"],
        version=1, artifact_id="learning_report_md",
    )[0]
    if action == "other_question":
        url = url.replace("q-optional", "q-required")
        payload["teacher_score"] = 9
    elif action == "confirm":
        payload["confirm"] = True
    response = client.put(url, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["unchanged"] is (action != "other_question")
    assert response.json()["workflow_revision"] == payload["expected_workflow_revision"] + 1
    expected = {"q-optional": confirmed, "q-required": not confirmed}
    target = "q-required" if action == "other_question" else "q-optional"
    if action != "same_content":
        expected[target] = payload["confirm"]
    assert response.json()["correction"]["review_status"] == (
        "confirmed" if expected[target] else "edited")
    refreshed = client.get(f"/tasks/{seeded['task_id']}/result")
    assert refreshed.status_code == 200, refreshed.text
    assert {row["q_id"]: row["review_status"]
            for student in refreshed.json()["results"] for row in student["corrections"]} == {
        q_id: "confirmed" if flag else "edited" for q_id, flag in expected.items()
    }
    runs = grading_repository.list_runs_for_assignment(seeded["task_id"], actor_id=seeded["owner_id"])
    assert len(runs) == 2
    clone = next(run for run in runs if run.id != seeded["run_id"])
    assert clone.released_at is None
    for row in grading_repository.list_results_for_run(clone.id):
        assert row.teacher_review["confirmed"] is expected[row.q_id]
        assert row.effective_score == {"q-optional": 7, "q-required": (
            9 if action == "other_question" else 8)}[row.q_id]
        assert row.effective_comment == "Checked"
        with session_scope() as session:
            reviews = session.scalars(select(TeacherReviewRecord).where(
                TeacherReviewRecord.grade_result_id == row.id,
            ).order_by(TeacherReviewRecord.review_sequence)).all()
            assert reviews[0].confirmed is (
                confirmed if row.q_id == "q-optional" else not confirmed)
            assert len(reviews) == (2 if row.q_id == target and action != "same_content" else 1)
    # Every published row and historical report stays byte-for-byte intact;
    # only the workflow pointer/revision and new draft rows may change.
    after = _snapshot()
    for table, rows in before.items():
        if table != workflow_repository.AssignmentWorkflowRecord.__tablename__:
            assert all(row in after[table] for row in rows), table
    assert task_facade.artifact_bytes(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"],
        version=1, artifact_id="learning_report_md",
    )[0] == history


@pytest.mark.parametrize("failure_sequence", [1, 2], ids=["copied_reviews", "new_review"])
def test_published_review_copy_failure_preserves_confirmation(failure_sequence):
    seeded, client, url, payload = _published_mixed_reviews(False)
    payload["teacher_score"] = 8
    before = _snapshot()
    flushed = []

    def fail_after_review_flush(session, _context):
        if any(isinstance(row, TeacherReviewRecord) and row.review_sequence == failure_sequence
               for row in session.new):
            flushed.append(True)
            raise RuntimeError("injected copy review failure")

    event.listen(Session, "after_flush", fail_after_review_flush)
    try:
        with pytest.raises(RuntimeError, match="injected copy review failure"):
            client.put(url, json=payload)
    finally:
        event.remove(Session, "after_flush", fail_after_review_flush)
    assert flushed
    assert _snapshot() == before
    # Retrying the same version after rollback must still preserve both flags.
    assert client.put(url, json=payload).status_code == 200
    runs = grading_repository.list_runs_for_assignment(seeded["task_id"], actor_id=seeded["owner_id"])
    assert len(runs) == 2
    clone = next(run for run in runs if run.id != seeded["run_id"])
    assert {row.q_id: row.teacher_review["confirmed"]
            for row in grading_repository.list_results_for_run(clone.id)} == {
        "q-optional": False, "q-required": True,
    }
