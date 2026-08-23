from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from backend.db.models import ProviderConfigRecord, ProviderPreferenceRecord
from backend.db.session import session_scope
from backend.models import ProviderConfig
from backend.security.secrets import EncryptedSecret, decrypt_secret, encrypt_secret


@dataclass(frozen=True)
class StoredProviderConfig:
    id: str
    config: ProviderConfig
    verification_status: str = "unverified"
    last_checked_at: float | None = None
    verification_error_code: str | None = None


class DefaultProviderReplacementRequired(RuntimeError):
    """The current default cannot be removed while alternatives remain."""


class DefaultProviderNotEnabled(RuntimeError):
    """The requested default is not an enabled owner-scoped configuration."""


def _associated_data(owner_id: str, record_id: str) -> str:
    return f"provider-config:{owner_id}:{record_id}"


def _preference(
    session,
    owner_id: str,
    *,
    create: bool,
) -> ProviderPreferenceRecord | None:
    preference = session.get(ProviderPreferenceRecord, owner_id)
    if preference is None and create:
        now = time.time()
        preference = ProviderPreferenceRecord(
            owner_id=owner_id,
            default_provider_id=None,
            created_at=now,
            updated_at=now,
        )
        session.add(preference)
        session.flush()
    return preference


def _set_first_default_if_missing(session, owner_id: str) -> str | None:
    preference = _preference(session, owner_id, create=True)
    assert preference is not None
    if preference.default_provider_id:
        return preference.default_provider_id
    provider_id = session.scalar(
        select(ProviderConfigRecord.id)
        .where(
            ProviderConfigRecord.owner_id == owner_id,
            ProviderConfigRecord.enabled.is_(True),
        )
        .order_by(ProviderConfigRecord.created_at, ProviderConfigRecord.id)
        .limit(1)
    )
    if provider_id:
        preference.default_provider_id = provider_id
        preference.updated_at = time.time()
    return provider_id


def get_default_provider_id(owner_id: str) -> str | None:
    with session_scope() as session:
        preference = session.get(ProviderPreferenceRecord, owner_id)
        return preference.default_provider_id if preference is not None else None


def ensure_default_provider_id(owner_id: str) -> str | None:
    """Backfill a missing preference without replacing an existing choice."""
    with session_scope() as session:
        return _set_first_default_if_missing(session, owner_id)


def set_default_provider_id(owner_id: str, provider_id: str) -> str:
    now = time.time()
    with session_scope() as session:
        provider = session.scalar(
            select(ProviderConfigRecord).where(
                ProviderConfigRecord.id == provider_id,
                ProviderConfigRecord.owner_id == owner_id,
                ProviderConfigRecord.enabled.is_(True),
            )
        )
        if provider is None:
            raise DefaultProviderNotEnabled(provider_id)
        preference = _preference(session, owner_id, create=True)
        assert preference is not None
        preference.default_provider_id = provider.id
        preference.updated_at = now
        return provider.id


def _prepare_default_for_removal(
    session,
    *,
    owner_id: str,
    provider_id: str,
) -> None:
    preference = _preference(session, owner_id, create=False)
    if preference is None or preference.default_provider_id != provider_id:
        return
    alternative = session.scalar(
        select(ProviderConfigRecord.id)
        .where(
            ProviderConfigRecord.owner_id == owner_id,
            ProviderConfigRecord.id != provider_id,
            ProviderConfigRecord.enabled.is_(True),
        )
        .limit(1)
    )
    if alternative is not None:
        raise DefaultProviderReplacementRequired(provider_id)
    preference.default_provider_id = None
    preference.updated_at = time.time()


def _to_config(record: ProviderConfigRecord, master_key: str) -> ProviderConfig:
    api_key = decrypt_secret(
        EncryptedSecret(record.encrypted_api_key, record.nonce, record.key_version),
        master_key=master_key,
        associated_data=_associated_data(record.owner_id, record.id),
    )
    return ProviderConfig(
        provider_type=record.provider_type,
        api_key=api_key,
        model=record.model,
        base_url=record.base_url,
        enabled=record.enabled,
        display_name=record.display_name,
        max_concurrent=max(1, record.max_concurrent),
        rpm=max(0, record.rpm),
    )


def upsert_provider_config(owner_id: str, config: ProviderConfig, *, master_key: str) -> ProviderConfigRecord:
    now = time.time()
    with session_scope() as session:
        record = session.scalar(select(ProviderConfigRecord).where(
            ProviderConfigRecord.owner_id == owner_id,
            ProviderConfigRecord.provider_type == config.provider_type,
            ProviderConfigRecord.model == config.model,
        ))
        if record is None:
            record = ProviderConfigRecord(
                id=uuid.uuid4().hex,
                owner_id=owner_id,
                provider_type=config.provider_type,
                model=config.model,
                created_at=now,
            )
            session.add(record)
        encrypted = encrypt_secret(
            config.api_key,
            master_key=master_key,
            associated_data=_associated_data(owner_id, record.id),
        )
        record.base_url = config.base_url
        record.display_name = config.display_name
        record.encrypted_api_key = encrypted.ciphertext
        record.nonce = encrypted.nonce
        record.key_version = encrypted.key_version
        record.enabled = config.enabled
        record.max_concurrent = max(1, config.max_concurrent)
        record.rpm = max(0, config.rpm)
        record.verification_status = "unverified"
        record.last_checked_at = None
        record.verification_error_code = None
        record.updated_at = now
        session.flush()
        if record.enabled:
            _set_first_default_if_missing(session, owner_id)
        return record


def list_provider_configs(owner_id: str, *, master_key: str) -> list[StoredProviderConfig]:
    with session_scope() as session:
        records = list(session.scalars(select(ProviderConfigRecord).where(
            ProviderConfigRecord.owner_id == owner_id).order_by(ProviderConfigRecord.created_at)))
    return [
        StoredProviderConfig(
            id=record.id,
            config=_to_config(record, master_key),
            verification_status=record.verification_status,
            last_checked_at=record.last_checked_at,
            verification_error_code=record.verification_error_code,
        )
        for record in records
    ]


def has_provider_configs(owner_id: str) -> bool:
    """Check owner-scoped record existence without decrypting credentials."""
    with session_scope() as session:
        record_id = session.scalar(
            select(ProviderConfigRecord.id)
            .where(ProviderConfigRecord.owner_id == owner_id)
            .limit(1)
        )
    return record_id is not None


def get_provider_config(owner_id: str, provider_id: str, *, master_key: str) -> StoredProviderConfig | None:
    with session_scope() as session:
        record = session.scalar(select(ProviderConfigRecord).where(
            ProviderConfigRecord.id == provider_id,
            ProviderConfigRecord.owner_id == owner_id,
        ))
    if record is None:
        return None
    return StoredProviderConfig(
        id=record.id,
        config=_to_config(record, master_key),
        verification_status=record.verification_status,
        last_checked_at=record.last_checked_at,
        verification_error_code=record.verification_error_code,
    )


def update_provider_config(
    owner_id: str,
    provider_id: str,
    config: ProviderConfig,
    *,
    master_key: str,
) -> StoredProviderConfig | None:
    """Replace one owner-scoped record while preserving its stable id.

    Callers that want to keep an omitted API key first load the existing
    encrypted record and pass the recovered key in ``config``. The plaintext is
    never written to logs or returned by public API serializers.
    """
    now = time.time()
    with session_scope() as session:
        record = session.scalar(select(ProviderConfigRecord).where(
            ProviderConfigRecord.id == provider_id,
            ProviderConfigRecord.owner_id == owner_id,
        ))
        if record is None:
            return None
        encrypted = encrypt_secret(
            config.api_key,
            master_key=master_key,
            associated_data=_associated_data(owner_id, record.id),
        )
        record.provider_type = config.provider_type
        record.model = config.model
        record.base_url = config.base_url
        record.display_name = config.display_name
        record.encrypted_api_key = encrypted.ciphertext
        record.nonce = encrypted.nonce
        record.key_version = encrypted.key_version
        record.enabled = config.enabled
        record.max_concurrent = max(1, config.max_concurrent)
        record.rpm = max(0, config.rpm)
        record.verification_status = "unverified"
        record.last_checked_at = None
        record.verification_error_code = None
        record.updated_at = now
        session.flush()
        return StoredProviderConfig(
            id=record.id,
            config=config.model_copy(deep=True),
            verification_status=record.verification_status,
            last_checked_at=record.last_checked_at,
            verification_error_code=record.verification_error_code,
        )


def set_provider_verification(
    owner_id: str,
    provider_id: str,
    *,
    verification_status: str,
    checked_at: float,
    error_code: str | None = None,
) -> bool:
    with session_scope() as session:
        record = session.scalar(
            select(ProviderConfigRecord).where(
                ProviderConfigRecord.id == provider_id,
                ProviderConfigRecord.owner_id == owner_id,
            )
        )
        if record is None:
            return False
        record.verification_status = verification_status
        record.last_checked_at = checked_at
        record.verification_error_code = error_code
        record.updated_at = checked_at
        return True


def set_provider_enabled(owner_id: str, provider_id: str, enabled: bool) -> bool:
    with session_scope() as session:
        record = session.scalar(select(ProviderConfigRecord).where(
            ProviderConfigRecord.id == provider_id,
            ProviderConfigRecord.owner_id == owner_id,
        ))
        if record is None:
            return False
        if not enabled:
            _prepare_default_for_removal(
                session,
                owner_id=owner_id,
                provider_id=provider_id,
            )
        record.enabled = enabled
        record.updated_at = time.time()
        session.flush()
        if enabled:
            _set_first_default_if_missing(session, owner_id)
        return True


def delete_provider_config(owner_id: str, provider_id: str) -> bool:
    with session_scope() as session:
        record = session.scalar(select(ProviderConfigRecord).where(
            ProviderConfigRecord.id == provider_id,
            ProviderConfigRecord.owner_id == owner_id,
        ))
        if record is None:
            return False
        _prepare_default_for_removal(
            session,
            owner_id=owner_id,
            provider_id=provider_id,
        )
        session.delete(record)
        return True
