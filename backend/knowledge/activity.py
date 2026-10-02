"""Owner-scoped, metadata-only progress feed. Reads never resume ingestion."""
from sqlalchemy import case, func, select

from backend.db.knowledge_repository import _document
from backend.db.models import KnowledgeDocumentRecord, KnowledgeStorageRecord, StoredFileRecord
from backend.db.session import session_scope


def list_activity(owner_id, *, query="", state="all", page=1, page_size=20, prioritize_active=False):
    doc = KnowledgeDocumentRecord
    status = func.coalesce(doc.ingestion_summary["status"].as_string(),
        case((doc.status == "ready", "complete"), else_=doc.status))
    group = case((status.in_(["queued", "processing"]), "active"),
                 (status.in_(["complete", "complete_with_warning"]), "completed"), else_="attention")
    base = (select(doc).join(KnowledgeStorageRecord, KnowledgeStorageRecord.document_id == doc.id)
        .join(StoredFileRecord, StoredFileRecord.id == doc.stored_file_id).where(
            doc.owner_id == owner_id, KnowledgeStorageRecord.owner_id == owner_id,
            KnowledgeStorageRecord.state == "available", StoredFileRecord.owner_id == owner_id,
            StoredFileRecord.availability_status == "available", StoredFileRecord.knowledge_document_id == doc.id,
            StoredFileRecord.sha256 == doc.sha256))
    with session_scope() as session:
        # Poll while any visible book is active, including one outside this filter/page.
        active_count = session.scalar(base.with_only_columns(func.count()).where(group == "active"))
        if query.strip():
            base = base.where(doc.title.icontains(query.strip(), autoescape=True)
                              | doc.original_name.icontains(query.strip(), autoescape=True))
        counts = dict(session.execute(base.with_only_columns(group, func.count()).group_by(group)).all())
        total = sum(counts.values()) if state == "all" else counts.get(state, 0)
        if state != "all":
            base = base.where(group == state)
        order = [doc.updated_at.desc(), doc.id]
        if prioritize_active:
            order.insert(0, case((group == "active", 0), (group == "attention", 1), else_=2))
        rows = session.execute(base.add_columns(status.label("activity_status"))
            .order_by(*order).offset((page - 1) * page_size).limit(page_size)).all()
        return dict(items=[dict(_document(row[0]).public(), activity_status=row[1]) for row in rows],
                    total=total, active_count=active_count, counts=counts, page=page, page_size=page_size)
