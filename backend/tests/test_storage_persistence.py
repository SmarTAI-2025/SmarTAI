"""Durable file storage linked to assignments / submission revisions (Task 5).

Files are linked via explicit FK columns (assignment_id / submission_revision_id)
rather than the legacy task_id. Owner-scoped reads return nothing for other
owners, and files survive backend recreation because metadata is in the DB and
bytes are in the storage backend.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from backend.db.base import Base
from backend.db.file_repository import (
    delete_files_for_revision,
    get_file,
    list_files,
    save_file,
)
from backend.db.session import configure_database, get_engine, session_scope
from backend.storage import get_storage
from backend.storage.local import LocalStorage


def _seed_owner(owner_id: str = "owner-1") -> None:
    from backend.db.models import UserRecord
    with session_scope() as session:
        session.merge(UserRecord(
            id=owner_id, username=owner_id, password_hash="hash", role="teacher",
            is_active=True,
        ))


def _seed_assignment(assignment_id: str = "asg-1", owner_id: str = "owner-1") -> None:
    from backend.db.models import AssignmentRecord, CourseRecord
    with session_scope() as session:
        session.merge(CourseRecord(id="c-1", name="C", teacher_id=owner_id))
        session.merge(AssignmentRecord(
            id=assignment_id, course_id="c-1", teacher_id=owner_id, name="A",
            status="draft", version=1,
        ))


def _seed_revision(revision_id: str, student_id: str = "owner-1") -> None:
    """Insert a real submission + revision row so the file FK resolves.

    Order matters under SQLite immediate FK: insert the submission row first
    (without the revision back-reference), then the revision row (which points
    at the now-existing submission), then set the submission's current revision.
    """
    from backend.db.models import SubmissionRecord, SubmissionRevisionRecord
    import time as _time
    now = _time.time()
    with session_scope() as session:
        session.add(SubmissionRecord(
            id="sub-1", assignment_id="asg-1", student_id=student_id,
            current_revision_id=None, created_at=now, updated_at=now,
        ))
        session.flush()
        session.add(SubmissionRevisionRecord(
            id=revision_id, submission_id="sub-1", revision_number=1,
            source="online", file_name="", created_at=now,
        ))
        session.flush()
        sub = session.get(SubmissionRecord, "sub-1")
        assert sub is not None
        sub.current_revision_id = revision_id


def test_local_storage_rejects_path_traversal(tmp_path):
    storage = LocalStorage(tmp_path / "uploads")
    with pytest.raises(ValueError):
        storage.save("../secret.txt", b"no")


def test_file_linked_to_revision_survives_recreation(tmp_path):
    configure_database(f"sqlite:///{(tmp_path / 'files.db').as_posix()}")
    Base.metadata.create_all(get_engine())
    _seed_owner()
    _seed_assignment()
    _seed_revision("rev-1")

    storage = LocalStorage(tmp_path / "uploads")
    saved = save_file(
        storage=storage, owner_id="owner-1", kind="submission",
        original_name="hw.pdf", content=b"permanent", content_type="application/pdf",
        submission_revision_id="rev-1",
    )

    recreated_storage = LocalStorage(tmp_path / "uploads")
    restored = get_file(file_id=saved.id, owner_id="owner-1")
    assert restored is not None
    assert restored.submission_revision_id == "rev-1"
    assert recreated_storage.open(restored.storage_key).read() == b"permanent"
    assert [f.id for f in list_files(owner_id="owner-1", submission_revision_id="rev-1")] == [saved.id]
    # Owner predicate: another owner reads nothing.
    assert get_file(file_id=saved.id, owner_id="someone-else") is None


def test_file_linked_to_assignment_lists_and_deletes(tmp_path):
    configure_database(f"sqlite:///{(tmp_path / 'files2.db').as_posix()}")
    Base.metadata.create_all(get_engine())
    _seed_owner()
    _seed_assignment("asg-2")

    storage = LocalStorage(tmp_path / "uploads2")
    saved = save_file(
        storage=storage, owner_id="owner-1", kind="problem",
        original_name="题目.txt", content=b"visible", assignment_id="asg-2",
    )
    listed = list_files(owner_id="owner-1", assignment_id="asg-2")
    assert [f.id for f in listed] == [saved.id]

    deleted = delete_files_for_revision(storage=storage, submission_revision_id="rev-none", owner_id="owner-1")
    assert deleted == 0  # nothing linked to that revision
    # Delete via assignment link.
    from backend.db.file_repository import delete_files_for_assignment
    n = delete_files_for_assignment(storage=storage, assignment_id="asg-2", owner_id="owner-1")
    assert n == 1
    assert storage.exists(saved.storage_key) is False


def test_save_file_recovers_when_commit_succeeds_but_acknowledgement_is_lost(
    tmp_path,
    monkeypatch,
):
    """An ambiguous commit must never delete the object behind a committed row."""
    import backend.db.file_repository as file_repository

    configure_database(f"sqlite:///{(tmp_path / 'ambiguous.db').as_posix()}")
    Base.metadata.create_all(get_engine())
    _seed_owner()
    _seed_assignment()
    storage = LocalStorage(tmp_path / "ambiguous-uploads")
    real_session_scope = file_repository.session_scope
    calls = 0

    @contextmanager
    def commit_then_lose_ack():
        nonlocal calls
        calls += 1
        if calls == 1:
            with real_session_scope() as session:
                yield session
            raise RuntimeError("injected_commit_ack_loss")
        with real_session_scope() as session:
            yield session

    monkeypatch.setattr(file_repository, "session_scope", commit_then_lose_ack)

    saved = file_repository.save_file(
        storage=storage,
        owner_id="owner-1",
        kind="submission_source",
        original_name="answer.txt",
        content=b"durable",
        content_type="text/plain",
        assignment_id="asg-1",
    )

    assert calls == 2
    assert storage.exists(saved.storage_key)
    assert get_file(file_id=saved.id, owner_id="owner-1") == saved


def test_save_file_preserves_object_when_commit_state_cannot_be_verified(
    tmp_path,
    monkeypatch,
):
    """Unknown database state favors recoverable cleanup over object loss."""
    import backend.db.file_repository as file_repository

    configure_database(f"sqlite:///{(tmp_path / 'unverified.db').as_posix()}")
    Base.metadata.create_all(get_engine())
    _seed_owner()
    _seed_assignment()
    storage = LocalStorage(tmp_path / "unverified-uploads")
    saved_keys: list[str] = []
    real_save = storage.save

    def capture_save(key: str, content: bytes) -> None:
        saved_keys.append(key)
        real_save(key, content)

    @contextmanager
    def unavailable_database():
        raise RuntimeError("injected_database_unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(storage, "save", capture_save)
    monkeypatch.setattr(file_repository, "session_scope", unavailable_database)

    with pytest.raises(RuntimeError, match="injected_database_unavailable"):
        file_repository.save_file(
            storage=storage,
            owner_id="owner-1",
            kind="submission_source",
            original_name="answer.txt",
            content=b"recoverable",
            content_type="text/plain",
            assignment_id="asg-1",
        )

    assert len(saved_keys) == 1
    assert storage.exists(saved_keys[0])


def test_storage_factory_selects_object_backend_from_settings(monkeypatch):
    from backend.storage import S3Storage, build_storage

    monkeypatch.setattr("backend.storage.settings.storage_backend", "object")
    monkeypatch.setattr("backend.storage.settings.storage_s3_bucket", "test-bucket")
    monkeypatch.setattr("backend.storage.settings.storage_s3_access_key", "access")
    monkeypatch.setattr("backend.storage.settings.storage_s3_secret_key", "secret")
    storage = build_storage()
    assert isinstance(storage, S3Storage)
    assert storage.bucket == "test-bucket"


@pytest.mark.parametrize(
    ("error_code", "http_status", "expected_exception"),
    [
        ("NoSuchKey", 404, "StorageObjectNotFound"),
        ("NoSuchBucket", 404, "StorageUnavailable"),
        ("AccessDenied", 403, "StorageUnavailable"),
    ],
)
def test_object_storage_open_distinguishes_missing_from_unavailable(
    error_code, http_status, expected_exception
):
    from botocore.exceptions import ClientError

    from backend.storage import S3Storage
    from backend.storage.base import StorageObjectNotFound, StorageUnavailable

    class FailingClient:
        def get_object(self, **_kwargs):
            raise ClientError(
                {
                    "Error": {"Code": error_code, "Message": "internal detail"},
                    "ResponseMetadata": {"HTTPStatusCode": http_status},
                },
                "GetObject",
            )

    storage = S3Storage.__new__(S3Storage)
    storage.bucket = "private-bucket"
    storage.client = FailingClient()
    expected = {
        "StorageObjectNotFound": StorageObjectNotFound,
        "StorageUnavailable": StorageUnavailable,
    }[expected_exception]

    with pytest.raises(expected) as failure:
        storage.open("private/key")

    assert "private-bucket" not in str(failure.value)
    assert "private/key" not in str(failure.value)
