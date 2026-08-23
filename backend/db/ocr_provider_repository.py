"""Owner-scoped persistence for specialized OCR BYOK credentials.

The Baidu API key and secret key are encrypted independently.  Neither this
module nor its callers may add these credentials to ``ProviderConfig`` or the
generic ``ExpertRegistry``.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, update

from backend.db.models import OCRProviderCredentialRecord
from backend.db.session import session_scope
from backend.security.secrets import EncryptedSecret, decrypt_secret, encrypt_secret


BAIDU_UNLIMITED_OCR_PROVIDER_TYPE = "baidu_unlimited_ocr"


@dataclass(frozen=True)
class OCRCredentialMetadata:
    id: str
    owner_id: str
    provider_type: str
    verification_status: str
    last_checked_at: float | None
    verification_error_code: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class StoredOCRProviderCredential:
    metadata: OCRCredentialMetadata
    api_key: str = field(repr=False)
    secret_key: str = field(repr=False)


def _associated_data(owner_id: str, record_id: str, field: str) -> str:
    return f"ocr-provider-credential:{owner_id}:{record_id}:{field}"


def _metadata(record: OCRProviderCredentialRecord) -> OCRCredentialMetadata:
    return OCRCredentialMetadata(
        id=record.id,
        owner_id=record.owner_id,
        provider_type=record.provider_type,
        verification_status=record.verification_status,
        last_checked_at=record.last_checked_at,
        verification_error_code=record.verification_error_code,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _record_query(owner_id: str, credential_id: str):
    return select(OCRProviderCredentialRecord).where(
        OCRProviderCredentialRecord.id == credential_id,
        OCRProviderCredentialRecord.owner_id == owner_id,
        OCRProviderCredentialRecord.provider_type
        == BAIDU_UNLIMITED_OCR_PROVIDER_TYPE,
    )


def upsert_baidu_unlimited_ocr_credential(
    owner_id: str,
    *,
    api_key: str,
    secret_key: str,
    master_key: str,
) -> OCRCredentialMetadata:
    """Create or replace the owner's sole Baidu Unlimited-OCR credential."""
    now = time.time()
    with session_scope() as session:
        record = session.scalar(
            select(OCRProviderCredentialRecord).where(
                OCRProviderCredentialRecord.owner_id == owner_id,
                OCRProviderCredentialRecord.provider_type
                == BAIDU_UNLIMITED_OCR_PROVIDER_TYPE,
            )
        )
        if record is None:
            record = OCRProviderCredentialRecord(
                id=uuid.uuid4().hex,
                owner_id=owner_id,
                provider_type=BAIDU_UNLIMITED_OCR_PROVIDER_TYPE,
                created_at=now,
            )
            session.add(record)

        encrypted_api_key = encrypt_secret(
            api_key,
            master_key=master_key,
            associated_data=_associated_data(owner_id, record.id, "api-key"),
        )
        encrypted_secret_key = encrypt_secret(
            secret_key,
            master_key=master_key,
            associated_data=_associated_data(owner_id, record.id, "secret-key"),
        )
        record.encrypted_api_key = encrypted_api_key.ciphertext
        record.api_key_nonce = encrypted_api_key.nonce
        record.api_key_version = encrypted_api_key.key_version
        record.encrypted_secret_key = encrypted_secret_key.ciphertext
        record.secret_key_nonce = encrypted_secret_key.nonce
        record.secret_key_version = encrypted_secret_key.key_version
        record.verification_status = "unverified"
        record.last_checked_at = None
        record.verification_error_code = None
        record.updated_at = now
        session.flush()
        return _metadata(record)


def get_baidu_unlimited_ocr_credential_metadata(
    owner_id: str,
    credential_id: str,
) -> OCRCredentialMetadata | None:
    """Read non-secret metadata with owner + id + fixed-provider isolation."""
    with session_scope() as session:
        record = session.scalar(_record_query(owner_id, credential_id))
    return _metadata(record) if record is not None else None


def get_current_baidu_unlimited_ocr_credential_metadata(
    owner_id: str,
) -> OCRCredentialMetadata | None:
    """Find the sole fixed-provider record without decrypting it."""
    with session_scope() as session:
        record = session.scalar(
            select(OCRProviderCredentialRecord).where(
                OCRProviderCredentialRecord.owner_id == owner_id,
                OCRProviderCredentialRecord.provider_type
                == BAIDU_UNLIMITED_OCR_PROVIDER_TYPE,
            )
        )
    return _metadata(record) if record is not None else None


def get_baidu_unlimited_ocr_credential(
    owner_id: str,
    credential_id: str,
    *,
    master_key: str,
) -> StoredOCRProviderCredential | None:
    """Decrypt exactly one explicitly selected owner credential; no fallback."""
    with session_scope() as session:
        record = session.scalar(_record_query(owner_id, credential_id))
    if record is None:
        return None

    api_key = decrypt_secret(
        EncryptedSecret(
            record.encrypted_api_key,
            record.api_key_nonce,
            record.api_key_version,
        ),
        master_key=master_key,
        associated_data=_associated_data(owner_id, record.id, "api-key"),
    )
    secret_key = decrypt_secret(
        EncryptedSecret(
            record.encrypted_secret_key,
            record.secret_key_nonce,
            record.secret_key_version,
        ),
        master_key=master_key,
        associated_data=_associated_data(owner_id, record.id, "secret-key"),
    )
    return StoredOCRProviderCredential(
        metadata=_metadata(record),
        api_key=api_key,
        secret_key=secret_key,
    )


def set_baidu_unlimited_ocr_verification(
    owner_id: str,
    credential_id: str,
    *,
    expected_updated_at: float,
    verification_status: str,
    checked_at: float,
    error_code: str | None = None,
) -> bool:
    if verification_status not in {"credentials_verified", "failed"}:
        raise ValueError("invalid OCR credential verification status")
    with session_scope() as session:
        result = session.execute(
            update(OCRProviderCredentialRecord)
            .where(
                OCRProviderCredentialRecord.id == credential_id,
                OCRProviderCredentialRecord.owner_id == owner_id,
                OCRProviderCredentialRecord.provider_type
                == BAIDU_UNLIMITED_OCR_PROVIDER_TYPE,
                OCRProviderCredentialRecord.updated_at == expected_updated_at,
            )
            .values(
                verification_status=verification_status,
                last_checked_at=checked_at,
                verification_error_code=error_code,
                updated_at=checked_at,
            )
        )
        return result.rowcount == 1


def delete_baidu_unlimited_ocr_credential(
    owner_id: str,
    credential_id: str,
) -> bool:
    with session_scope() as session:
        record = session.scalar(_record_query(owner_id, credential_id))
        if record is None:
            return False
        session.delete(record)
        return True
