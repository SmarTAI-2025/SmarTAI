"""Credential-free fingerprints for immutable grading-run provider inputs."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from backend.config import settings
from backend.db.models import ProviderConfigRecord
from backend.db.session import session_scope
from backend.domain.errors import ValidationError


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
    with session_scope() as session:
        records = session.scalars(
            select(ProviderConfigRecord).where(
                ProviderConfigRecord.owner_id == owner_id,
                ProviderConfigRecord.id.in_(selected),
            )
        ).all() if selected else []
    by_id = {record.id: record for record in records}
    rows: list[dict] = []
    for provider_id in selected:
        record = by_id.get(provider_id)
        if record is not None:
            secret_digest = hashlib.sha256(
                f"{record.encrypted_api_key}:{record.nonce}".encode("utf-8")
            ).hexdigest()
            rows.append({
                "provider_id": provider_id,
                "provider_type": record.provider_type,
                "model": record.model,
                "base_url": record.base_url,
                "enabled": record.enabled,
                "max_concurrent": record.max_concurrent,
                "rpm": record.rpm,
                "key_version": record.key_version,
                "secret_digest": secret_digest,
                "scope": "owner",
            })
            continue

        shared = _shared_provider_row(provider_id)
        if shared is None:
            raise ValidationError(
                "Selected provider configuration is unavailable.",
                code="provider_not_enabled",
            )
        rows.append(shared)
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _shared_provider_row(provider_id: str) -> dict | None:
    if not settings.shared_pool_enabled:
        return None
    candidates = (
        ("gemini", settings.gemini_model, settings.gemini_api_key, None),
        ("openai", settings.openai_model, settings.openai_api_key, settings.openai_api_base),
        ("zhipu", settings.zhipu_model, settings.zhipu_api_key, settings.zhipu_api_base),
        ("anthropic", settings.anthropic_model, settings.anthropic_api_key, None),
    )
    for provider_type, model, api_key, base_url in candidates:
        if not api_key:
            continue
        # Provider implementations use this descriptive id for environment
        # configs. Keep matching deliberately narrow; unknown ids fail closed.
        expected_ids = {f"{provider_type}:{model}", f"{provider_type}/{model}"}
        if provider_id not in expected_ids:
            continue
        return {
            "provider_id": provider_id,
            "provider_type": provider_type,
            "model": model,
            "base_url": base_url,
            "secret_digest": hashlib.sha256(api_key.encode("utf-8")).hexdigest(),
            "scope": "shared",
        }
    return None
