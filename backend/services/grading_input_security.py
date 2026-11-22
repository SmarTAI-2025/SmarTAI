"""Credential-free fingerprints for immutable grading-run provider inputs."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from backend.config import settings
from backend.db.models import ByokConnectionRecord, ProviderConfigRecord
from backend.db.session import session_scope
from backend.domain.errors import ValidationError
from backend.llm.provider_catalog import catalog_entry


def provider_configuration_fingerprint(
    *, owner_id: str, selected_provider_ids: list[str]
) -> str:
    """Hash provider configuration without exposing or storing plaintext keys.

    Persisted BYOK rows contribute their encrypted secret+nonce digest and every
    invocation-affecting setting. Shared-pool providers contribute a one-way
    digest of the process configuration. A queued worker recomputes this value
    and refuses to run if anything changed after teacher confirmation.
    """
    selected = list(dict.fromkeys(selected_provider_ids))
    from backend.services.frontier_demo import is_frontier_user
    from backend.state import get_user_store
    demo_owner = is_frontier_user(get_user_store().get(owner_id))
    with session_scope() as session:
        records = session.execute(
            select(ProviderConfigRecord, ByokConnectionRecord).outerjoin(
                ByokConnectionRecord, ByokConnectionRecord.id == ProviderConfigRecord.byok_connection_id,
            ).where(
                ProviderConfigRecord.owner_id == owner_id,
                ProviderConfigRecord.id.in_(selected),
            )
        ).all() if selected else []
    by_id = {record.id: (record, connection) for record, connection in records}
    rows: list[dict] = []
    for provider_id in selected:
        bound = by_id.get(provider_id)
        if bound is not None:
            record, connection = bound
            credential = connection or record
            secret_digest = hashlib.sha256(
                f"{credential.encrypted_api_key}:{credential.nonce}".encode("utf-8")
            ).hexdigest()
            rows.append({
                "provider_id": provider_id,
                "provider_type": record.provider_type,
                "model": record.model,
                "base_url": record.base_url,
                "endpoint_identity": record.endpoint_identity,
                "wire_protocol": record.wire_protocol,
                "enabled": record.enabled,
                "max_concurrent": record.max_concurrent,
                "rpm": record.rpm,
                "key_version": credential.key_version,
                "secret_digest": secret_digest,
                "scope": "owner",
            })
            if connection is not None:
                rows[-1].update(connection_id=connection.id, reasoning_effort=record.reasoning_effort)
            continue

        shared = _shared_provider_row(provider_id, frontier_demo=demo_owner)
        if shared is None:
            raise ValidationError(
                "Selected provider configuration is unavailable.",
                code="provider_not_enabled",
            )
        rows.append(shared)
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _shared_provider_row(provider_id: str, *, frontier_demo: bool = False) -> dict | None:
    if not (settings.frontier_demo_enabled if frontier_demo else settings.shared_pool_enabled):
        return None
    from backend.llm.registry import shared_provider_configs
    for config in shared_provider_configs(frontier_demo=frontier_demo):
        provider_type, model = config.provider_type, config.model
        # Provider implementations use this descriptive id for environment
        # configs. Keep matching deliberately narrow; unknown ids fail closed.
        expected_ids = {f"{provider_type}:{model}", f"{provider_type}/{model}"}
        if provider_id not in expected_ids:
            continue
        return {
            "provider_id": provider_id,
            "provider_type": provider_type,
            "model": model,
            "base_url": config.base_url,
            "endpoint_identity": catalog_entry(provider_type).default_base_url,
            "wire_protocol": catalog_entry(provider_type).wire_protocol,
            "secret_digest": hashlib.sha256(config.api_key.encode("utf-8")).hexdigest(),
            "scope": "shared",
        }
    return None
