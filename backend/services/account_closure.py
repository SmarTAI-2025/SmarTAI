"""Close accounts only after restart-safe task and object cleanup has finished.

A request blocks login immediately. Username/email are released atomically at
completion, never while old producers might still publish user data.
"""
from __future__ import annotations
import asyncio
import time
import uuid
from sqlalchemy import delete, select, update
from backend.db.session import session_scope
from backend.db.models import (AccountClosureRecord, BlockedRegistrationEmailRecord, UserRecord,
    AssignmentRecord, KnowledgeDocumentRecord, KnowledgeStorageRecord, StoredFileRecord,
    SourceStorageReservationRecord, EmailVerificationRequestRecord, InviteCodeRecord,
    KnowledgeIngestionRecord, AdminUsageEventRecord)
from backend.services.admin_transactions import lock_administration
from backend.services.email_registration import normalize_email
from backend.storage import get_storage


def _claim():
    now = time.time()
    with session_scope() as session:
        lock_administration(session)
        row = session.scalar(select(AccountClosureRecord).where(
            AccountClosureRecord.status != "completed",
            (AccountClosureRecord.lease_until.is_(None)) | (AccountClosureRecord.lease_until < now),
        ).order_by(AccountClosureRecord.created_at).limit(1).with_for_update())
        if row is None:
            return None
        row.lease_token = uuid.uuid4().hex
        row.lease_until = now + 120
        return row.id, row.user_id, row.lease_token


def _release(closure_id, token, code=None):
    with session_scope() as session:
        row = session.get(AccountClosureRecord, closure_id)
        if row and row.lease_token == token:
            row.lease_token = None
            row.lease_until = time.time() + 5
            row.error_code = code


def process_one_closure(*, raise_errors: bool = False) -> bool:
    claimed = _claim()
    if claimed is None:
        return False
    closure_id, user_id, token = claimed
    try:
        with session_scope() as session:
            task_ids = list(session.scalars(select(AssignmentRecord.id).where(AssignmentRecord.teacher_id == user_id)))
        if task_ids:
            from backend.services.task_facade import delete_task
            for task_id in task_ids:
                delete_task(task_id=task_id, owner_id=user_id)
            _release(closure_id, token, "waiting_for_task_cleanup")
            return True
        with session_scope() as session:
            # Stop durable knowledge producers. Publication checks their lease.
            for row in session.scalars(select(KnowledgeIngestionRecord).where(KnowledgeIngestionRecord.owner_id == user_id).with_for_update()):
                row.status = "cancelled"
                row.lease_token = None
                row.lease_expires_at = None
            documents = list(session.scalars(select(KnowledgeDocumentRecord.id).where(KnowledgeDocumentRecord.owner_id == user_id)))
        if documents:
            from backend.db.knowledge_storage_repository import request_document_cleanup
            for document_id in documents:
                request_document_cleanup(document_id, user_id)
            _release(closure_id, token, "waiting_for_knowledge_cleanup")
            return True
        with session_scope() as session:
            if (session.scalar(select(KnowledgeStorageRecord.id).where(KnowledgeStorageRecord.owner_id == user_id).limit(1))
                or session.scalar(select(SourceStorageReservationRecord.id).where(((SourceStorageReservationRecord.file_owner_id == user_id) | (SourceStorageReservationRecord.quota_owner_id == user_id))).limit(1))):
                _pending = True
            else:
                _pending = False
            files = [(row.storage_backend, row.storage_key) for row in session.scalars(select(StoredFileRecord).where(StoredFileRecord.owner_id == user_id))]
        if _pending:
            _release(closure_id, token, "waiting_for_upload_cleanup")
            return True
        storage = get_storage()
        from backend.config import settings
        if any(backend != getattr(storage, "name", settings.storage_backend) for backend, _ in files):
            raise RuntimeError("storage_backend_unavailable")
        keys = set(storage.list_keys(f"users/{user_id}/")) | {key for _, key in files}
        for key in sorted(keys):
            storage.delete(key)
        if storage.list_keys(f"users/{user_id}/"):
            raise RuntimeError("storage_cleanup_pending")
        # Both idempotency and final-admin mutation use the same management lock.
        with session_scope() as session:
            lock_administration(session)
            closure = session.scalar(select(AccountClosureRecord).where(AccountClosureRecord.id == closure_id).with_for_update())
            if closure is None or closure.lease_token != token or (closure.lease_until or 0) <= time.time():
                return True
            user = session.scalar(select(UserRecord).where(UserRecord.id == user_id).with_for_update())
            if user is not None:
                if (session.scalar(select(AssignmentRecord.id).where(AssignmentRecord.teacher_id == user_id).limit(1))
                    or session.scalar(select(KnowledgeDocumentRecord.id).where(KnowledgeDocumentRecord.owner_id == user_id).limit(1))
                    or session.scalar(select(KnowledgeStorageRecord.id).where(KnowledgeStorageRecord.owner_id == user_id).limit(1))
                    or session.scalar(select(SourceStorageReservationRecord.id).where(((SourceStorageReservationRecord.file_owner_id == user_id) | (SourceStorageReservationRecord.quota_owner_id == user_id))).limit(1))):
                    closure.lease_token = None
                    closure.lease_until = time.time() + 5
                    closure.error_code = "waiting_for_upload_cleanup"
                    return True
                email = normalize_email(user.email) if user.email else None
                if closure.mode == "blacklist":
                    if not email:
                        raise ValueError("blacklist_requires_email")
                    if session.get(BlockedRegistrationEmailRecord, email) is None:
                        session.add(BlockedRegistrationEmailRecord(normalized_email=email, created_at=time.time()))
                session.execute(delete(EmailVerificationRequestRecord).where(
                    (EmailVerificationRequestRecord.normalized_username == user.username)
                    | (EmailVerificationRequestRecord.normalized_email == email)))
                if email:
                    session.execute(delete(InviteCodeRecord).where(InviteCodeRecord.email == email))
                session.execute(update(AdminUsageEventRecord).where(AdminUsageEventRecord.user_id == user_id).values(user_id="closed_" + uuid.uuid4().hex, dimensions=None))
                session.delete(user)
                session.flush()
            closure.status = "completed"
            closure.completed_at = time.time()
            closure.error_code = None
            closure.lease_token = None
            closure.lease_until = None
        return True
    except Exception:
        # Exception messages may contain storage paths or credentials.
        _release(closure_id, token, "cleanup_retry_required")
        if raise_errors:
            raise
        return True


async def account_closure_loop():
    while True:
        try:
            active_pass = asyncio.create_task(asyncio.to_thread(process_one_closure))
            try:
                await asyncio.shield(active_pass)
            except asyncio.CancelledError:
                # Cancelling an await does not stop its synchronous file/DB work.
                await active_pass
                raise
        except asyncio.CancelledError:
            raise
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Account cleanup temporarily unavailable; will retry")
        await asyncio.sleep(5)
