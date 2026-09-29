"""Mounted HTTP -> shared adapter contracts; model quality is tested separately."""
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from backend.auth import create_token
from backend.domain.errors import RecognitionError
from backend.llm.registry import get_scoped_expert_registry
from backend.main import app
from backend.tests.test_task_background_workflows import _seed_task
from backend.tests.test_question_preparation_recovery import _RecoveryRegistry


@pytest.fixture
def client_registry():
    registry = _RecoveryRegistry()
    previous = app.dependency_overrides.get(get_scoped_expert_registry)
    app.dependency_overrides[get_scoped_expert_registry] = lambda: registry
    try:
        yield TestClient(app), registry
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_scoped_expert_registry, None)
        else:
            app.dependency_overrides[get_scoped_expert_registry] = previous


def headers(owner, role="teacher"):
    return {"Authorization": "Bearer " + create_token(owner, role)}


@pytest.mark.parametrize("alias", ["problem-sources/preflight", "question-preparation/sources/preflight"])
@pytest.mark.parametrize("role,purpose", [("problem", "problems"), ("reference_answer", "reference"),
    ("rubric", "rubric"), ("programming_tests", "test_cases")])
def test_both_preflight_aliases_route_each_purpose_to_shared_adapter(client_registry, monkeypatch, alias, role, purpose):
    from backend.api import task_preparation
    from backend.tests.test_ocr_normalized_ingest import PNG_1X1
    client, registry = client_registry
    monkeypatch.setattr(registry.provider, "supports_vision", True)
    owner, task = _seed_task()
    read = AsyncMock(side_effect=RecognitionError("recognition_request_invalid"))
    monkeypatch.setattr(task_preparation, "read_question_source", read)
    response = client.post(f"/tasks/{task}/{alias}", headers=headers(owner),
        data=dict(role=role, enable_material_ocr="true"), files={"file": ("source.png", PNG_1X1, "image/png")})
    assert response.status_code >= 400, response.text
    read.assert_awaited_once()
    assert read.call_args.kwargs["purpose"] == purpose
    assert read.call_args.kwargs["owner_id"] == owner
    assert read.call_args.kwargs["task_id"] == task


@pytest.mark.parametrize("target", ["criterion", "reference_answer", "test_cases"])
def test_material_preflight_reaches_shared_reader_without_applying(client_registry, monkeypatch, target):
    from backend.api import task_preparation
    client, _ = client_registry
    owner, task = _seed_task(with_question=True)
    read = AsyncMock(side_effect=RecognitionError("recognition_request_invalid"))
    mutate = Mock(side_effect=AssertionError("preflight cannot mutate questions"))
    monkeypatch.setattr(task_preparation, "read_question_source", read)
    monkeypatch.setattr(task_preparation.task_facade, "apply_question_patches_atomic", mutate)
    response = client.post(f"/tasks/{task}/material-imports/preflight", headers=headers(owner),
        data=dict(targets='["' + target + '"]', enable_material_ocr="true"),
        files={"file": ("source.txt", b"1. literal -2", "text/plain")})
    assert response.status_code >= 400, response.text
    read.assert_awaited_once()
    assert read.call_args.kwargs["purpose"] == {"criterion":"rubric", "reference_answer":"reference", "test_cases":"test_cases"}[target]
    mutate.assert_not_called()


@pytest.mark.parametrize("alias,target", [("upload_reference", "reference_answer"), ("upload_test_cases", "test_cases")])
def test_auxiliary_upload_aliases_only_queue_review_plan(client_registry, monkeypatch, alias, target):
    from backend.api import task_preparation
    client, _ = client_registry
    owner, task = _seed_task(with_question=True)
    preflight = AsyncMock(return_value={"source_token":"source"})
    start = AsyncMock(return_value={"job_id":"plan", "status":"started"})
    monkeypatch.setattr(task_preparation, "preflight_material_import", preflight)
    monkeypatch.setattr(task_preparation, "start_material_import", start)
    response = client.post(f"/tasks/{task}/{alias}", headers=headers(owner),
        files={"file": ("source.txt", b"1. literal -2", "text/plain")})
    assert response.status_code == 200 and response.json()["job_id"] == "plan"
    assert preflight.call_args.kwargs["targets"] == '["' + target + '"]'
    assert preflight.call_args.kwargs["enable_material_ocr"] is False
    start.assert_awaited_once()


def test_assignment_import_reaches_shared_adapter(client_registry, monkeypatch):
    from backend.services import question_sources
    client, _ = client_registry
    owner, task = _seed_task()
    read = AsyncMock(side_effect=RecognitionError("recognition_request_invalid"))
    monkeypatch.setattr(question_sources, "read_question_source", read)
    response = client.post(f"/assignments/{task}/questions/import-file", headers=headers(owner),
        files={"file": ("source.txt", b"1. literal -2", "text/plain")})
    assert response.status_code >= 400, response.text
    read.assert_awaited_once()
    assert read.call_args.kwargs["task_id"] == task


@pytest.mark.parametrize("action", ["extract_problems", "parse_submissions"])
def test_task_upload_only_publishes_durable_job(client_registry, monkeypatch, action):
    from backend.api import tasks
    client, registry = client_registry
    owner, task = _seed_task(with_question=action == "parse_submissions")
    method = "queue_task_problem_extraction" if action == "extract_problems" else "queue_task_submission_parsing"
    queue = Mock(return_value={"job_id":"durable", "status":"started"})
    monkeypatch.setattr(tasks.task_facade, method, queue)
    response = client.post(f"/tasks/{task}/{action}", headers=headers(owner),
        files={"file": ("source.txt", b"literal -2", "text/plain")})
    assert response.status_code == 200 and response.json()["job_id"] == "durable"
    queue.assert_called_once()
    assert queue.call_args.kwargs["content"] == b"literal -2"
    assert queue.call_args.kwargs["registry"] is registry


@pytest.mark.parametrize("actor,alias", [("student", "upload"), ("teacher", "teacher-upload")])
def test_individual_submission_upload_reaches_same_faithful_reader(client_registry, monkeypatch, actor, alias):
    from backend.services import submission_uploads
    from backend.tests.test_ocr_normalized_ingest import _published_assignment
    client, _ = client_registry
    teacher, student, task = _published_assignment(alias)
    read = AsyncMock(side_effect=RecognitionError("recognition_request_invalid"))
    monkeypatch.setattr(submission_uploads, "read_question_source", read)
    response = client.post(f"/submissions/{alias}", headers=headers(teacher if actor == "teacher" else student, actor),
        data=dict(assignment_id=task, student_id=student), files={"file": ("answer.txt", b"1+1=3", "text/plain")})
    assert response.status_code >= 400, response.text
    read.assert_awaited_once()
    assert read.call_args.kwargs["purpose"] == "submissions"
    assert read.call_args.kwargs["content"] == b"1+1=3"


@pytest.mark.parametrize("api_name,url", [("knowledge", "/knowledge/documents"), ("materials", "/course-materials/"),
    ("tasks", "/tasks/{task}/kb")])
def test_all_knowledge_uploads_share_whole_book_ingestion(client_registry, monkeypatch, api_name, url):
    from backend.api import knowledge, materials, tasks
    client, _ = client_registry
    owner, task = _seed_task()
    ingest = AsyncMock(side_effect=RecognitionError("recognition_request_invalid"))
    monkeypatch.setattr({"knowledge":knowledge, "materials":materials, "tasks":tasks}[api_name], "ingest_document", ingest)
    response = client.post(url.format(task=task), headers=headers(owner),
        files={"file": ("book.txt", b"full book", "text/plain")})
    assert response.status_code >= 400, response.text
    ingest.assert_awaited_once()
    assert ingest.call_args.kwargs["content"] == b"full book"
    assert ingest.call_args.kwargs["owner_id"] == owner
