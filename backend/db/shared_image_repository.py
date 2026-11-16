"""Owner/configuration-bound evidence for read-only platform models."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from sqlalchemy import case, update
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


def _next_text_probe_version():
    now = time.time()
    version = SharedProviderImageRecord.updated_at
    # Atomic and strictly increasing, including simultaneous starts/processes
    # and a wall clock that stalls or moves backwards. Not a displayed time.
    return case((version >= now, version + 1), else_=now)


def shared_image_evidence(owner: str, config: ProviderConfig, *, create: bool = False,
                          text_probe: bool = False, start_probe: bool = False) -> SharedImageEvidence:
    if start_probe and not (text_probe and create):
        raise ValueError("text_probe_start_requires_created_evidence")
    fingerprint = shared_image_fingerprint(config)
    # Fixed OpenAI presets each need independent evidence. Other configured
    # providers retain their legacy slot and edit/revert invalidation behavior.
    from backend.llm.registry import SHARED_OPENAI_MODELS
    slot = f"openai:{config.model}" if config.provider_type == "openai" and config.model in SHARED_OPENAI_MODELS else config.provider_type
    # Reuse the bounded evidence record in a separate namespace, so text
    # checks never replace image evidence and no credential is duplicated.
    if text_probe:
        slot = "text:" + slot
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
            ).values(fingerprint=fingerprint, status="unverified", checked_at=None, reason=None,
                     updated_at=_next_text_probe_version() if text_probe else time.time()))
            session.refresh(row)
        if start_probe:
            # Claim the result slot when starting, not when finishing: only
            # the latest-started request may publish its eventual result.
            version = session.execute(update(SharedProviderImageRecord).where(
                SharedProviderImageRecord.owner_id == owner, SharedProviderImageRecord.provider_type == slot,
                SharedProviderImageRecord.fingerprint == fingerprint,
            ).values(updated_at=_next_text_probe_version()).returning(SharedProviderImageRecord.updated_at)).scalar_one_or_none()
            return SharedImageEvidence(slot, fingerprint, version, row.status, row.checked_at, row.reason)
        return SharedImageEvidence(slot, row.fingerprint, row.updated_at, row.status, row.checked_at, row.reason)


def set_shared_image_evidence(owner: str, snapshot: SharedImageEvidence, *, status: str,
                              checked_at: float, reason: str | None, text_probe: bool = False) -> bool:
    allowed = {"verified", "failed"} if text_probe else {"passed", "unsupported", "inconclusive"}
    if status not in allowed or snapshot.provider_type.startswith("text:") != text_probe:
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
