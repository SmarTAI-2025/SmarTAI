import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.auth import create_token
from backend.db.models import KnowledgeDocumentRecord, KnowledgeIngestionRecord, StoredFileRecord
from backend.db.knowledge_storage_repository import request_document_cleanup
from backend.db.session import session_scope
from backend.knowledge.activity import list_activity
from backend.main import app
from backend.services.knowledge_storage import persist_knowledge_upload
from backend.storage import get_storage
from backend.tests.test_knowledge_ingestion import owner
from backend.tests.test_knowledge_retrieval_versions import assignment


def seed(who, name, status, *, old_ready=False, task=None):
    upload = persist_knowledge_upload(storage=get_storage(), owner_id=who, original_name=name,
        content=name.encode(), content_type="text/plain", title=name,
        retention_policy="task_only" if task else "retained", origin_assignment_id=task)
    with session_scope() as session:
        doc = session.get(KnowledgeDocumentRecord, upload.document_id)
        doc.status = "ready" if old_ready else "partial"
        doc.ingestion_summary = dict(id="run", status=status, total_pages=100, processed_pages=100,
                                     searchable_pages=99, failed_pages=1, coverage_complete=False)
    return upload.document_id


@pytest.mark.parametrize("status,group", [("queued", "active"), ("processing", "active"),
    ("complete", "completed"), ("complete_with_warning", "completed"), ("partial", "attention"),
    ("failed", "attention"), ("paused", "attention"), ("cancelled", "attention")])
def test_feed_uses_current_job_not_old_ready_document_or_processed_percentage(status, group):
    who = owner()
    document_id = seed(who, "Algebra.txt", status, old_ready=True)
    data = list_activity(who, state=group)
    assert data["total"] == 1
    assert data["items"][0]["id"] == document_id
    assert data["items"][0]["activity_status"] == status
    assert data["items"][0]["ingestion"]["coverage_complete"] is False
    assert data["active_count"] == int(group == "active")


def test_feed_paginates_filters_literal_names_and_keeps_global_active_count():
    who = owner()
    active = seed(who, "Older active.txt", "processing")
    for index in range(3):
        seed(who, f"Completed {index}.txt", "complete")
    special = seed(who, "100%_Algebra.txt", "partial")
    assert list_activity(who, page_size=1, prioritize_active=True)["items"][0]["id"] == active
    first = list_activity(who, state="completed", page_size=2)
    second = list_activity(who, state="completed", page=2, page_size=2)
    assert first["total"] == 3 and first["active_count"] == 1
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert {x["id"] for x in first["items"]}.isdisjoint(x["id"] for x in second["items"])
    literal = list_activity(who, query="%_")
    assert [x["id"] for x in literal["items"]] == [special]
    assert literal["active_count"] == 1


def test_feed_owner_task_only_visibility_deletion_and_read_only(monkeypatch):
    from backend.knowledge.ingestion import KnowledgeIngestionWorker
    from backend.services.recognition_runs import RecognitionRunService
    def forbidden(*args, **kwargs):
        pytest.fail("Progress reads must not call OCR or start ingestion")
    monkeypatch.setattr(KnowledgeIngestionWorker, "run_once", forbidden)
    monkeypatch.setattr(RecognitionRunService, "run", forbidden)
    who, other = owner(), owner()
    kept = seed(who, "Personal.txt", "complete")
    task_book = seed(who, "Task-only.txt", "processing", task=assignment(who))
    deleted = seed(who, "Deleting.txt", "paused")
    unavailable = seed(who, "Unavailable.txt", "failed")
    seed(other, "Private.txt", "queued")
    request_document_cleanup(deleted, who)
    with session_scope() as session:
        doc = session.get(KnowledgeDocumentRecord, unavailable)
        session.get(StoredFileRecord, doc.stored_file_id).availability_status = "unavailable"
        jobs_before = session.scalar(select(func.count()).select_from(KnowledgeIngestionRecord))
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + create_token(who, "teacher")}
    for _ in range(2):
        response = client.get("/knowledge/activity", headers=headers)
        assert response.status_code == 200, response.text
        assert {x["id"] for x in response.json()["items"]} == {kept, task_book}
    assert client.get("/knowledge/activity").status_code == 401
    for query in ("page_size=101", "page=0", "state=made_up", "q=" + "x" * 129):
        assert client.get("/knowledge/activity?" + query, headers=headers).status_code == 422
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(KnowledgeIngestionRecord)) == jobs_before


def test_legacy_unknown_status_is_not_reported_complete():
    who = owner()
    key = seed(who, "Legacy.txt", "complete")
    with session_scope() as session:
        doc = session.get(KnowledgeDocumentRecord, key)
        doc.ingestion_summary = None
        doc.status = "unexpected"
    assert list_activity(who, state="attention")["total"] == 1
