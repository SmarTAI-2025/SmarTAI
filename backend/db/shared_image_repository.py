"""Owner/configuration-bound evidence for read-only platform models."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from backend.config import settings
from backend.db.models import SharedProviderImageRecord
from backend.db.provider_repository import _route_identity
from backend.db.session import session_scope
from backend.models import ProviderConfig


@dataclass(frozen=True)
class SharedImageEvidence:
    provider_type: str
    fingerprint: str
    updated_at: float | None = None
    status: str = "unverified"
    checked_at: float | None = None
    reason: str | None = None


def shared_image_fingerprint(config: ProviderConfig) -> str:
    endpoint, protocol = _route_identity(config)
    content = json.dumps([config.provider_type, config.model, endpoint, protocol, config.api_key]).encode()
    # Persist only an irreversible keyed identity, never the shared credential.
    return hmac.new(settings.jwt_secret.encode(), content, hashlib.sha256).hexdigest()


def shared_image_evidence(owner: str, config: ProviderConfig, *, create: bool = False) -> SharedImageEvidence:
    fingerprint = shared_image_fingerprint(config)
    # The platform has one configured slot per provider. Keep that identity
    # stable across model edits so changing A -> B -> A never revives A's proof.
    slot = config.provider_type
    with session_scope() as session:
        row = session.get(SharedProviderImageRecord, (owner, slot))
        if row is None and create:
            row = SharedProviderImageRecord(owner_id=owner, provider_type=slot, fingerprint=fingerprint,
                                            status="unverified", updated_at=time.time())
            session.add(row)
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                row = session.get(SharedProviderImageRecord, (owner, slot))
        if row is None:
            return SharedImageEvidence(slot, fingerprint)
        if row.fingerprint != fingerprint:
            session.execute(update(SharedProviderImageRecord).where(
                SharedProviderImageRecord.owner_id == owner, SharedProviderImageRecord.provider_type == slot,
                SharedProviderImageRecord.updated_at == row.updated_at,
            ).values(fingerprint=fingerprint, status="unverified", checked_at=None, reason=None, updated_at=time.time()))
            session.refresh(row)
        return SharedImageEvidence(slot, row.fingerprint, row.updated_at, row.status, row.checked_at, row.reason)


def set_shared_image_evidence(owner: str, snapshot: SharedImageEvidence, *, status: str,
                              checked_at: float, reason: str | None) -> bool:
    if status not in {"passed", "unsupported", "inconclusive"}:
        raise ValueError("invalid_image_capability_status")
    with session_scope() as session:
        if snapshot.updated_at is None:
            # Normal recognition may discover an explicit rejection before any
            # probe exists. Insert only; never overwrite concurrent/new evidence.
            session.add(SharedProviderImageRecord(owner_id=owner, provider_type=snapshot.provider_type,
                fingerprint=snapshot.fingerprint, status=status, checked_at=checked_at, reason=reason, updated_at=time.time()))
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                return False
            return True
        result = session.execute(update(SharedProviderImageRecord).where(
            SharedProviderImageRecord.owner_id == owner, SharedProviderImageRecord.provider_type == snapshot.provider_type,
            SharedProviderImageRecord.fingerprint == snapshot.fingerprint,
            SharedProviderImageRecord.updated_at == snapshot.updated_at,
        ).values(status=status, checked_at=checked_at, reason=reason))
        return result.rowcount == 1


def record_shared_image_rejection(owner: str, snapshot: SharedImageEvidence) -> bool:
    return set_shared_image_evidence(owner, snapshot, status="unsupported", checked_at=time.time(),
                                    reason="provider_vision_not_supported")
