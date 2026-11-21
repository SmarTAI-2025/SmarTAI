"""Persist BYOK connections and reuse their credential across model selections."""
from __future__ import annotations

import json
import time
import uuid

from sqlalchemy import select, update

from backend.db.models import ByokConnectionRecord, ProviderConfigRecord
from backend.db.provider_repository import _prepare_default_for_removal
from backend.db.session import session_scope
from backend.llm.concurrency import initial_concurrency
from backend.llm.model_discovery import ModelCatalog, official_efforts
from backend.security.secrets import EncryptedSecret, decrypt_secret, encrypt_secret


def _credential_context(record: ByokConnectionRecord) -> str:
    return f"byok-connection:{record.owner_id}:{record.id}"


def connection_key(record: ByokConnectionRecord, master_key: str) -> str:
    return decrypt_secret(
        EncryptedSecret(record.encrypted_api_key, record.nonce, record.key_version),
        master_key=master_key, associated_data=_credential_context(record),
    )


def _replace_key(record: ByokConnectionRecord, api_key: str, master_key: str) -> None:
    key = encrypt_secret(api_key, master_key=master_key, associated_data=_credential_context(record))
    record.encrypted_api_key = key.ciphertext
    record.nonce = key.nonce
    record.key_version = key.key_version


def _query(owner_id: str):
    return select(ByokConnectionRecord).where(ByokConnectionRecord.owner_id == owner_id)


def _locked_connection(session, owner_id: str, connection_id: str, *, revision: float | None = None):
    where = [ByokConnectionRecord.owner_id == owner_id, ByokConnectionRecord.id == connection_id]
    if revision is not None:
        where.append(ByokConnectionRecord.updated_at == revision)
    if session.get_bind().dialect.name == "sqlite":
        # SQLite has no SELECT FOR UPDATE. Acquire its write lock before
        # reading the credential/catalog used to materialize or edit a route.
        matched = session.execute(update(ByokConnectionRecord).where(*where).values(
            updated_at=ByokConnectionRecord.updated_at,
        ))
        if matched.rowcount != 1:
            return None
    return session.scalar(select(ByokConnectionRecord).where(*where).with_for_update())


def list_connections(owner_id: str) -> list[ByokConnectionRecord]:
    with session_scope() as session:
        return list(session.scalars(_query(owner_id).order_by(ByokConnectionRecord.created_at, ByokConnectionRecord.id)))


def get_connection(owner_id: str, connection_id: str) -> ByokConnectionRecord | None:
    with session_scope() as session:
        return session.scalar(_query(owner_id).where(ByokConnectionRecord.id == connection_id))


def create_connection(owner_id: str, *, provider_type: str, base_url: str | None,
                      endpoint_identity: str, protocol: str, display_name: str | None,
                      rpm: int, api_key: str, master_key: str, catalog: ModelCatalog) -> ByokConnectionRecord:
    record = ByokConnectionRecord(
        id=uuid.uuid4().hex, owner_id=owner_id, provider_type=provider_type,
        base_url=base_url, endpoint_identity=endpoint_identity, wire_protocol=protocol,
        display_name=display_name, rpm=rpm, models=catalog.models, catalog_status=catalog.status,
    )
    _replace_key(record, api_key, master_key)
    with session_scope() as session:
        session.add(record)
    return record


def update_connection(previous: ByokConnectionRecord, *, api_key: str | None, master_key: str,
                      display_name: str | None, rpm: int, catalog: ModelCatalog) -> ByokConnectionRecord | None:
    """Commit a directory refresh only if the credential still matches."""
    with session_scope() as session:
        record = _locked_connection(session, previous.owner_id, previous.id, revision=previous.updated_at)
        if record is None:
            return None
        if api_key is not None:
            _replace_key(record, api_key, master_key)
            record.adaptive_limits = None
        if catalog.models or api_key is not None:
            record.models = catalog.models
        record.catalog_status = catalog.status
        if api_key is not None or rpm != record.rpm or display_name != record.display_name:
            now = time.time()
            record.updated_at = now
            record.rpm = rpm
            record.display_name = display_name
            values = dict(rpm=rpm, max_concurrent=initial_concurrency(rpm), updated_at=now)
            if api_key is not None:
                values.update(
                    verification_status="unverified", last_checked_at=None, verification_error_code=None,
                    image_capability_status="unverified", image_checked_at=None, image_reason=None,
                )
            session.execute(update(ProviderConfigRecord).where(
                ProviderConfigRecord.byok_connection_id == record.id,
                ProviderConfigRecord.owner_id == record.owner_id,
            ).values(**values))
        return record


def delete_connection(owner_id: str, connection_id: str) -> bool:
    with session_scope() as session:
        record = _locked_connection(session, owner_id, connection_id)
        if record is None:
            return False
        # Disable all variants before checking alternatives to this connection.
        routes = list(session.scalars(select(ProviderConfigRecord).where(
            ProviderConfigRecord.owner_id == owner_id, ProviderConfigRecord.byok_connection_id == record.id,
        )))
        for route in routes:
            route.enabled = False
        session.flush()
        for route in routes:
            _prepare_default_for_removal(session, owner_id=owner_id, provider_id=route.id)
        session.delete(record)
        return True


def bind_model(owner_id: str, connection_id: str, model: str, effort: str | None) -> str:
    """Validate a choice and materialize exactly one stable provider ID."""
    provider_id = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(
        ["smartai:byok", owner_id, connection_id, model, effort], ensure_ascii=False,
    )).hex
    with session_scope() as session:
        record = _locked_connection(session, owner_id, connection_id)
        if record is None:
            raise LookupError("byok_connection_not_found")
        match = next((entry for entry in record.models if entry["id"] == model), None)
        if match is None and record.catalog_status == "ready":
            raise ValueError("byok_model_not_listed")
        levels = match["reasoning_efforts"] if match else official_efforts(
            record.provider_type, record.base_url, record.wire_protocol, model,
        )
        if effort is not None and effort not in levels:
            raise ValueError("byok_effort_unsupported")
        existing = session.get(ProviderConfigRecord, provider_id)
        if existing is not None and not existing.enabled:
            raise ValueError("byok_model_disabled")
        if existing is None:
            route = ProviderConfigRecord(
                id=provider_id, owner_id=owner_id, byok_connection_id=record.id,
                provider_type=record.provider_type, model=model, base_url=record.base_url,
                endpoint_identity=record.endpoint_identity, wire_protocol=record.wire_protocol,
                reasoning_effort=effort, route_variant=f"{record.id}:{effort or 'default'}",
                max_concurrent=initial_concurrency(record.rpm), rpm=record.rpm,
            )
            session.add(route)
        return provider_id
