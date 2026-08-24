"""Explicit owner-credential construction for Baidu Unlimited-OCR.

There is deliberately no zero-argument/default factory: every runtime caller
must supply both the authenticated owner id and a credential id.  This keeps
the OCR adapter out of the generic LLM registry and prevents shared-key or
cross-owner fallback.
"""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from backend.config import settings
from backend.db.ocr_provider_repository import (
    StoredOCRProviderCredential,
    get_baidu_unlimited_ocr_credential,
    get_baidu_unlimited_ocr_credential_metadata,
)


class OCRCredentialNotFoundError(LookupError):
    """The explicitly selected credential does not belong to this owner."""


class OCRCredentialEncryptionUnavailableError(RuntimeError):
    """The server cannot decrypt persisted owner credentials safely."""


class BaiduCredentialVerifier(Protocol):
    def __call__(
        self,
        *,
        api_key: str,
        secret_key: str,
    ) -> Awaitable[None]: ...


def get_owner_baidu_unlimited_ocr_credentials(
    owner_id: str,
    credential_id: str,
    *,
    master_key: str | None = None,
) -> StoredOCRProviderCredential:
    """Load one explicitly selected owner credential and fail closed.

    The scoped metadata lookup intentionally happens before checking the
    master key so a record owned by somebody else is indistinguishable from a
    nonexistent id.
    """
    metadata = get_baidu_unlimited_ocr_credential_metadata(owner_id, credential_id)
    if metadata is None:
        raise OCRCredentialNotFoundError("OCR credential not found")
    encryption_key = settings.provider_encryption_key if master_key is None else master_key
    if not encryption_key or not encryption_key.strip():
        raise OCRCredentialEncryptionUnavailableError(
            "OCR credential encryption is unavailable"
        )
    encryption_key = encryption_key.strip()
    credential = get_baidu_unlimited_ocr_credential(
        owner_id,
        credential_id,
        master_key=encryption_key,
    )
    if credential is None:  # Defensive against deletion between the two reads.
        raise OCRCredentialNotFoundError("OCR credential not found")
    return credential


def build_owner_baidu_unlimited_ocr_skill(
    owner_id: str,
    credential_id: str,
    *,
    master_key: str | None = None,
    client_kwargs: dict[str, Any] | None = None,
):
    """Build the OCR Skill from an explicit owner record, with no fallback."""
    credential = get_owner_baidu_unlimited_ocr_credentials(
        owner_id,
        credential_id,
        master_key=master_key,
    )
    # Delayed imports keep credential persistence independent of the optional
    # network adapter while retaining Agent -> Skill -> Tool construction.
    from backend.skills.ocr_ingest import BaiduUnlimitedOCRSkill
    from backend.tools.baidu_unlimited_ocr import BaiduUnlimitedOCRClient

    client = BaiduUnlimitedOCRClient(
        api_key=credential.api_key,
        secret_key=credential.secret_key,
        **(client_kwargs or {}),
    )
    return BaiduUnlimitedOCRSkill(client=client)


async def verify_baidu_unlimited_ocr_credentials(
    *,
    api_key: str,
    secret_key: str,
) -> None:
    """Exchange credentials for a token only; do not submit an OCR page."""
    from backend.tools.baidu_unlimited_ocr import BaiduUnlimitedOCRClient

    client = BaiduUnlimitedOCRClient(api_key=api_key, secret_key=secret_key)
    try:
        await client.verify_credentials()
    finally:
        close = getattr(client, "aclose", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result


def get_baidu_unlimited_ocr_credential_verifier() -> Callable[..., Awaitable[None]]:
    """FastAPI dependency seam used by focused contract tests."""
    return verify_baidu_unlimited_ocr_credentials
