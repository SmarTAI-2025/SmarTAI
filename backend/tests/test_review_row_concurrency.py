"""Real transactions on separate connections, with no mocked service internals."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, local

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from backend.db import assignment_repository, workflow_repository
from backend.db.models import AssignmentQuestionRecord, SubmissionAnswerRecord
from backend.db.session import get_engine
from backend.domain.errors import NotFound, VersionConflict
from backend.services import question_review_rows, student_review_rows, task_facade
from backend.tests.test_task_background_workflows import _seed_task


def _case(kind):
    owner, task = _seed_task(with_question=True)
    service = question_review_rows if kind == "question" else student_review_rows
    add = service.add_question if kind == "question" else service.add_student
    delete = service.delete_question if kind == "question" else service.delete_student
    key = "q_id" if kind == "question" else "student_id"
    data_key = "problem_data" if kind == "question" else "student_data"
    workflow = workflow_repository.update_workflow(task, owner_id=owner,
        presentation_status="problems_ready" if kind == "question" else "submissions_ready")
    # Both callers can delete a different existing row, so disappearance of
    # one target cannot hide a broken workflow revision check.
    for _ in range(1 if kind == "question" else 2):
        add(task_id=task, owner_id=owner, expected_revision=workflow.workflow_revision)
        workflow = workflow_repository.get_workflow(task, owner_id=owner)
    before = task_facade.get_task(task_id=task, owner_id=owner)
    return owner, task, add, delete, key, data_key, before


def assert_concurrent_row_mutations(kind, actions, *, attempts=10):
    for _ in range(attempts):
        owner, task, add, delete, key, data_key, before = _case(kind)
        revision = before["workflow_revision"]
        targets = list(before[data_key])
        barrier = Barrier(2)
        connections = set()
        engine = get_engine()

        def synchronize_start(connection):
            # This is before the first SQL statement, not between the
            # service's version check and write. Each caller owns a real,
            # independent connection to the same disposable database.
            connections.add(id(connection.connection.driver_connection))
            barrier.wait(timeout=10)

        def mutate(index):
            kwargs = dict(task_id=task, owner_id=owner, expected_revision=revision)
            try:
                result = (add(**kwargs) if actions[index] == "add"
                          else delete(**kwargs, **{key: targets[index]}))
                return index, result
            except VersionConflict as exc:
                assert exc.code == "stale_revision" and exc.status_code == 409
                return index, None

        event.listen(engine, "begin", synchronize_start)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(mutate, (0, 1)))
        finally:
            event.remove(engine, "begin", synchronize_start)
        assert len(connections) == 2
        winners = [(index, result) for index, result in outcomes if result is not None]
        assert len(winners) == 1, outcomes
        index, result = winners[0]
        after = task_facade.get_task(task_id=task, owner_id=owner)
        assert after["workflow_revision"] == result["workflow_revision"] == revision + 1
        expected_ids = set(before[data_key])
        if actions[index] == "add":
            expected_ids.add(result[key])
        else:
            expected_ids.remove(targets[index])
        assert set(after[data_key]) == expected_ids
        for row_id in expected_ids.intersection(before[data_key]):
            assert after[data_key][row_id] == before[data_key][row_id]


@pytest.mark.parametrize("kind", ["question", "student"])
@pytest.mark.parametrize("actions", [("add", "add"), ("add", "delete"), ("delete", "delete")])
def test_concurrent_row_mutations_have_one_winner(kind, actions):
    assert_concurrent_row_mutations(kind, actions)


@pytest.mark.parametrize("kind", ["question", "student"])
def test_invalid_row_mutation_rolls_back_revision_claim(kind):
    owner, task, add, delete, key, data_key, before = _case(kind)
    revision = before["workflow_revision"]
    with pytest.raises(NotFound):
        delete(task_id=task, owner_id=owner, expected_revision=revision, **{key: "missing"})
    after = task_facade.get_task(task_id=task, owner_id=owner)
    assert after["workflow_revision"] == revision and after[data_key] == before[data_key]
    assert add(task_id=task, owner_id=owner, expected_revision=revision)["workflow_revision"] == revision + 1


@pytest.mark.parametrize("kind,path", [("question", "problems"), ("student", "students")])
def test_language_and_row_request_contracts_coexist(kind, path):
    from backend.tests.test_task_stage_contract import _client

    owner, task, _, _, key, _, before = _case(kind)
    client = _client(owner)
    language_path = f"/tasks/{task}/content-language/default"
    assert client.put(language_path, json={"language": "en"}).json() == {"language": "en"}
    assert client.put(language_path, json={"language": "invalid"}).status_code == 422
    payload = {"expected_workflow_revision": before["workflow_revision"]}
    row_path = f"/tasks/{task}/{path}"
    assert client.post(row_path, json={**payload, "unexpected": True}).status_code == 422
    created = client.post(row_path, json=payload)
    assert created.status_code == 200
    stale = client.post(row_path, json=payload)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_revision"
    removed = client.request("DELETE", f"{row_path}/{created.json()[key]}",
        json={"expected_workflow_revision": created.json()["workflow_revision"]})
    assert removed.status_code == 200
    assert client.put(language_path, json={"language": "zh"}).json() == {"language": "en"}


DETAIL_KINDS = ("question", "question_confirm", "answer", "answer_confirm", "identity")


def assert_detail_and_row_mutation_share_revision(kind, action, *, with_revision=True):
    is_question = kind.startswith("question")
    owner, task, add, delete, key, data_key, before = _case("question" if is_question else "student")
    target = "q1" if is_question else next(iter(before[data_key]))
    if not is_question:
        assignment = assignment_repository.get_assignment(task, actor_id=owner)
        assignment_repository.publish(task, teacher_id=owner, expected_version=assignment.version)
        task_facade.update_student_answer(task_id=task, owner_id=owner, display_student_id=target,
            q_id="q1", patch={"content": "Original answer"}, expected_revision=before["workflow_revision"])
        before = task_facade.get_task(task_id=task, owner_id=owner)
    revision = before["workflow_revision"]
    snapshot_read, release_editor, row_started = Event(), Event(), Event()
    role = local()
    connections = set()
    engine = get_engine()
    model = (AssignmentQuestionRecord if is_question else SubmissionAnswerRecord
             if kind.startswith("answer") else workflow_repository.AssignmentStudentPresentationRecord)

    def pause_after_detail_read(session, instance):
        if getattr(role, "value", None) == "detail" and isinstance(instance, model) and not snapshot_read.is_set():
            connections.add(id(session.connection().connection.driver_connection))
            snapshot_read.set()
            assert release_editor.wait(timeout=10)

    def observe_row_write(connection, cursor, statement, parameters, context, executemany):
        sql = context.compiled.statement if context.compiled else None
        if (getattr(role, "value", None) == "row" and getattr(sql, "is_update", False)
                and sql.table.name == workflow_repository.AssignmentWorkflowRecord.__tablename__):
            connections.add(id(connection.connection.driver_connection))
            row_started.set()

    def edit():
        kwargs = dict(task_id=task, owner_id=owner, expected_revision=revision if with_revision else None)
        if is_question:
            return task_facade.update_problem(**kwargs, q_id=target,
                patch={"review_status": "confirmed"} if kind.endswith("confirm") else {"stem": "Edited question"})
        if kind.startswith("answer"):
            return task_facade.update_student_answer(**kwargs, display_student_id=target, q_id="q1",
                patch={"review_status": "confirmed"} if kind.endswith("confirm") else {"content": "Edited answer"})
        return task_facade.update_student_identity(**kwargs, current_display_id=target,
            new_display_id="S-edited", new_display_name="Edited student")

    def mutate_row():
        kwargs = dict(task_id=task, owner_id=owner, expected_revision=revision)
        return add(**kwargs) if action == "add" else delete(**kwargs, **{key: target})

    def run(name, operation):
        role.value = name
        try:
            return operation()
        except VersionConflict as exc:
            assert exc.code == "stale_revision" and exc.status_code == 409
            return None

    event.listen(Session, "loaded_as_persistent", pause_after_detail_read)
    event.listen(engine, "before_cursor_execute", observe_row_write)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            detail_future = pool.submit(run, "detail", edit)
            try:
                assert snapshot_read.wait(timeout=10)
                row_future = pool.submit(run, "row", mutate_row)
                assert row_started.wait(timeout=10)
                # Old SQLite code lets the row mutation commit while the
                # detail holds an old snapshot. Fixed code holds the workflow
                # write lock, so the other connection waits until we release.
                try:
                    row_future.result(timeout=1)
                except TimeoutError:
                    pass
            finally:
                release_editor.set()
            detail_result, row_result = detail_future.result(), row_future.result()
    finally:
        event.remove(Session, "loaded_as_persistent", pause_after_detail_read)
        event.remove(engine, "before_cursor_execute", observe_row_write)
    assert len(connections) == 2
    after = task_facade.get_task(task_id=task, owner_id=owner)
    if action == "delete" and row_result is not None:
        assert target not in after[data_key], "A completed delete was undone by a stale detail save"
    assert sum(result is not None for result in (detail_result, row_result)) == 1
    winner = detail_result or row_result
    assert after["workflow_revision"] == winner["workflow_revision"] == revision + 1
    if detail_result:
        assert row_result is None
        if kind == "identity":
            assert set(after[data_key]) == (set(before[data_key]) - {target}) | {"S-edited"}
        else:
            assert set(after[data_key]) == set(before[data_key])
    else:
        expected_ids = (set(before[data_key]) | {row_result[key]}) if action == "add" else (set(before[data_key]) - {target})
        assert set(after[data_key]) == expected_ids
        for row_id in expected_ids.intersection(before[data_key]):
            assert after[data_key][row_id] == before[data_key][row_id]


@pytest.mark.parametrize("kind", DETAIL_KINDS)
@pytest.mark.parametrize("action", ["add", "delete"])
def test_detail_and_row_mutation_share_revision(kind, action):
    assert_detail_and_row_mutation_share_revision(kind, action)


@pytest.mark.parametrize("kind", ["question", "answer"])
def test_legacy_detail_without_revision_still_serializes_with_delete(kind):
    assert_detail_and_row_mutation_share_revision(kind, "delete", with_revision=False)
