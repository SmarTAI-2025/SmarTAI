"""
ExpertRegistry: BYOK (Bring Your Own Key) expert management.

Users configure their own API keys for different providers via /experts/* endpoints.
The grading pipeline queries ExpertRegistry.list_available() to decide whether
to run single-expert or multi-expert grading.

In-memory for now (matches current state pattern in dependencies.py).
Swap to persistent storage (SQLite, Redis) later without changing callers.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from threading import Lock

from fastapi import Depends, HTTPException, status

from backend.config import settings
from backend.models import ProviderConfig
from backend.llm.providers import BaseProvider, build_provider

logger = logging.getLogger(__name__)


def provider_encryption_not_configured_error(
    *,
    api_key_was_submitted: bool = False,
) -> HTTPException:
    """Return the stable, environment-safe BYOK master-key error."""
    if settings.runtime_environment == "production":
        message = (
            "Service configuration is temporarily unavailable. Contact an "
            "administrator."
        )
    else:
        message = (
            "Server BYOK encryption is not configured. Set "
            "SMARTAI_PROVIDER_ENCRYPTION_KEY to a private random value and "
            "restart the backend."
        )
    if api_key_was_submitted:
        message = f"{message} This API key was not saved."
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "provider_encryption_not_configured",
            "message": message,
        },
    )


def _iso_utc_timestamp(value: object) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()


class SharedPoolLimitError(RuntimeError):
    """Stable signal raised before an over-budget shared-pool invocation."""

    retryable = False


class _SharedPoolUsageLimiter:
    def __init__(self) -> None:
        self._usage: Dict[tuple[str, str], tuple[int, int]] = {}
        self._lock = Lock()

    def consume(self, owner_id: str, messages: List[Any]) -> None:
        day = datetime.now(timezone.utc).date().isoformat()
        estimated_tokens = max(
            1,
            sum(len(str(getattr(message, "content", ""))) for message in messages) // 4,
        )
        request_limit = max(0, int(settings.shared_pool_daily_request_limit))
        token_limit = max(0, int(settings.shared_pool_daily_estimated_token_limit))
        with self._lock:
            for key in list(self._usage):
                if key[1] != day:
                    self._usage.pop(key, None)
            key = (owner_id, day)
            requests, tokens = self._usage.get(key, (0, 0))
            if (
                request_limit <= 0
                or token_limit <= 0
                or requests + 1 > request_limit
                or tokens + estimated_tokens > token_limit
            ):
                raise SharedPoolLimitError("shared_pool_daily_limit_reached")
            self._usage[key] = (requests + 1, tokens + estimated_tokens)


_shared_pool_usage = _SharedPoolUsageLimiter()


class _GuardedSharedProvider:
    """Owner-bound provider proxy that charges every shared invocation."""

    def __init__(self, provider: BaseProvider, owner_id: str) -> None:
        self._provider = provider
        self._owner_id = owner_id
        self.provider_id = provider.provider_id
        self.provider_type = provider.provider_type
        self.model = provider.model
        self.config = provider.config

    async def ainvoke(self, messages: List[Any]):
        if not settings.shared_pool_enabled:
            raise SharedPoolLimitError("shared_pool_disabled")
        _shared_pool_usage.consume(self._owner_id, messages)
        return await self._provider.ainvoke(messages)

    def __getattr__(self, name: str):
        return getattr(self._provider, name)


class ExpertRegistry:
    """
    Registry of configured LLM providers ("experts").
    Thread-safe because FastAPI may dispatch across multiple threads.
    """

    def __init__(
        self,
        *,
        seed_from_settings: bool = True,
        shared_owner_id: Optional[str] = None,
    ) -> None:
        self._providers: Dict[str, BaseProvider] = {}  # keyed by provider_id
        self._configs: Dict[str, ProviderConfig] = {}  # keyed by provider_id
        self._verification: Dict[str, Dict[str, object]] = {}
        self._lock = Lock()
        self._shared_owner_id = shared_owner_id
        self._uses_shared_pool = False
        if seed_from_settings and settings.shared_pool_enabled:
            self._seed_from_settings()
            self._uses_shared_pool = bool(self._providers)

    def _seed_from_settings(self) -> None:
        """Populate from env vars at startup. User can override via API."""
        if settings.gemini_api_key:
            self.register(ProviderConfig(
                provider_type="gemini",
                api_key=settings.gemini_api_key,
                model=settings.gemini_model,
            ))
        if settings.openai_api_key and settings.openai_api_key != "YOUR_API_KEY_HERE":
            self.register(ProviderConfig(
                provider_type="openai",
                api_key=settings.openai_api_key,
                model=settings.openai_model,
                base_url=settings.openai_api_base,
            ))
        if settings.zhipu_api_key:
            self.register(ProviderConfig(
                provider_type="zhipu",
                api_key=settings.zhipu_api_key,
                model=settings.zhipu_model,
                base_url=settings.zhipu_api_base,
            ))
        if settings.anthropic_api_key:
            self.register(ProviderConfig(
                provider_type="anthropic",
                api_key=settings.anthropic_api_key,
                model=settings.anthropic_model,
            ))
        if settings.deepseek_api_key:
            self.register(ProviderConfig(
                provider_type="deepseek",
                api_key=settings.deepseek_api_key,
                model=settings.deepseek_model,
                base_url=settings.deepseek_api_base,
            ))
        if settings.moonshot_api_key:
            self.register(ProviderConfig(
                provider_type="moonshot",
                api_key=settings.moonshot_api_key,
                model=settings.moonshot_model,
                base_url=settings.moonshot_api_base,
            ))
        if settings.qwen_api_key:
            self.register(ProviderConfig(
                provider_type="qwen",
                api_key=settings.qwen_api_key,
                model=settings.qwen_model,
                base_url=settings.qwen_api_base,
            ))

    def register(
        self,
        config: ProviderConfig,
        provider_id: str | None = None,
        *,
        verification_status: str = "unverified",
        last_checked_at: float | None = None,
        verification_error_code: str | None = None,
    ) -> str:
        """Register or update a provider. Returns its provider_id."""
        provider = build_provider(config)
        registry_id = provider_id or provider.provider_id
        with self._lock:
            self._providers[registry_id] = provider
            self._configs[registry_id] = config
            self._verification[registry_id] = {
                "verification_status": verification_status,
                "last_checked_at": last_checked_at,
                "verified_at": (
                    last_checked_at if verification_status == "verified" else None
                ),
                "verification_error_code": verification_error_code,
            }
        logger.info("Registered expert configuration; provider_type=%s", config.provider_type)
        return registry_id

    def unregister(self, provider_id: str) -> bool:
        """Remove an expert. Returns True if it existed."""
        with self._lock:
            existed = provider_id in self._providers
            self._providers.pop(provider_id, None)
            self._configs.pop(provider_id, None)
            self._verification.pop(provider_id, None)
        if existed:
            logger.info(f"Unregistered expert: {provider_id}")
        return existed

    def get(self, provider_id: str) -> Optional[BaseProvider]:
        """Look up a provider by its provider_id."""
        with self._lock:
            provider = self._providers.get(provider_id)
        return self._guard_shared(provider)

    def list_available(self) -> List[BaseProvider]:
        """Return all enabled providers. Order is deterministic (sorted by provider_id)."""
        with self._lock:
            available = sorted(
                [p for pid, p in self._providers.items() if self._configs[pid].enabled],
                key=lambda p: p.provider_id,
            )
        return [self._guard_shared(provider) for provider in available]

    def _guard_shared(self, provider: Optional[BaseProvider]):
        if provider is None or not self._uses_shared_pool:
            return provider
        return _GuardedSharedProvider(provider, self._shared_owner_id or "anonymous")

    def list_enabled_configs(self) -> List[ProviderConfig]:
        """Trusted backend-only view used by the owner-scoped RAG embedder."""
        with self._lock:
            return [
                config.model_copy(deep=True)
                for config in self._configs.values()
                if config.enabled
            ]

    def uses_shared_pool(self) -> bool:
        return self._uses_shared_pool

    def list_configs(self) -> List[Dict[str, object]]:
        """Return redacted config dicts (api_key stripped) for UI listing.

        Each dict includes the registry-known `provider_id` and resolved
        `display_name` so the frontend dropdown can label items without having
        to re-derive the id.
        """
        with self._lock:
            out: List[Dict[str, object]] = []
            for pid, c in self._configs.items():
                verification = self._verification.get(pid, {})
                out.append({
                    "provider_id": pid,
                    "provider_type": c.provider_type,
                    "model": c.model,
                    "base_url": c.base_url,
                    "enabled": c.enabled,
                    "display_name": c.display_name or pid,
                    "max_concurrent": c.max_concurrent,
                    "rpm": c.rpm,
                    "scope": "shared" if self._uses_shared_pool else "owner",
                    "is_shared": self._uses_shared_pool,
                    "editable": not self._uses_shared_pool,
                    "supports_vision": bool(
                        getattr(self._providers.get(pid), "supports_vision", False)
                    ),
                    "verification_status": (
                        "platform_managed" if self._uses_shared_pool
                        else verification.get("verification_status", "unverified")
                    ),
                    "last_checked_at": _iso_utc_timestamp(
                        verification.get("last_checked_at")
                    ),
                    "verified_at": _iso_utc_timestamp(
                        verification.get("verified_at")
                    ),
                    "verification_error_code": verification.get(
                        "verification_error_code"
                    ),
                })
            return out

    def select(
        self,
        provider_ids: List[str],
        *,
        primary_provider_id: str,
    ) -> "ExpertRegistryView":
        """Return a credential-free view restricted to an approved run setup.

        Providers stay owned by this registry; the view only controls which
        ids the grading algorithm can enumerate and which one is primary.
        """
        unique_ids = list(dict.fromkeys(provider_ids))
        if primary_provider_id not in unique_ids:
            raise ValueError("primary_provider_not_selected")
        configs = {str(item["provider_id"]): item for item in self.list_configs()}
        if any(
            provider_id not in configs or not configs[provider_id].get("enabled")
            for provider_id in unique_ids
        ):
            raise ValueError("provider_not_enabled")
        return ExpertRegistryView(
            self,
            unique_ids,
            primary_provider_id=primary_provider_id,
        )

    def count(self) -> int:
        """Number of available experts."""
        return len(self.list_available())

    def pick_default(self) -> Optional[BaseProvider]:
        """Return one provider for single-expert mode.
        Prefers the setting's default_provider type, falls back to first available."""
        available = self.list_available()
        if not available:
            return None
        for p in available:
            if p.provider_type == settings.default_provider:
                return p
        return available[0]

    def pick_default_id(self) -> Optional[str]:
        """Return the registry key for the deterministic default provider.

        Persisted BYOK entries use stable ``pc_*`` registry keys that differ
        from a provider object's descriptive ``provider_id``.  API contracts
        and frozen grading setup must store the registry key.
        """
        with self._lock:
            enabled = sorted(
                (
                    (provider_id, config)
                    for provider_id, config in self._configs.items()
                    if config.enabled
                ),
                key=lambda item: item[0],
            )
        if not enabled:
            return None
        preferred = next(
            (
                provider_id
                for provider_id, config in enabled
                if config.provider_type == settings.default_provider
            ),
            None,
        )
        return preferred or enabled[0][0]

    def pick_vision(self, preferred: Optional[BaseProvider] = None) -> Optional[BaseProvider]:
        """Return a provider that supports image input.

        When a caller supplies a stage-selected provider, never switch to a
        different configuration. An omitted preference retains the legacy
        capability-discovery behavior used by read-only preflight endpoints.
        """
        available = self.list_available()
        if preferred is not None:
            if getattr(preferred, "supports_vision", False) and any(
                p.provider_id == preferred.provider_id for p in available
            ):
                return preferred
            return None
        for p in available:
            if getattr(p, "supports_vision", False):
                return p
        return None


class ExpertRegistryView:
    """Read-only provider selection frozen for one operation or grading run."""

    def __init__(
        self,
        registry: ExpertRegistry,
        provider_ids: List[str],
        *,
        primary_provider_id: str,
    ) -> None:
        self._registry = registry
        self._provider_ids = tuple(provider_ids)
        self._primary_provider_id = primary_provider_id

    def get(self, provider_id: str) -> Optional[BaseProvider]:
        if provider_id not in self._provider_ids:
            return None
        return self._registry.get(provider_id)

    def list_available(self) -> List[BaseProvider]:
        providers = [self._registry.get(provider_id) for provider_id in self._provider_ids]
        return [provider for provider in providers if provider is not None]

    def list_configs(self) -> List[Dict[str, object]]:
        allowed = set(self._provider_ids)
        return [
            item for item in self._registry.list_configs()
            if item.get("provider_id") in allowed
        ]

    def list_enabled_configs(self) -> List[ProviderConfig]:
        allowed = set(self._provider_ids)
        configs = {
            str(item.get("provider_id")): item
            for item in self._registry.list_configs()
        }
        # The embedder only needs provider configuration values, not selection
        # metadata. Preserve registry order while filtering by the frozen ids.
        enabled = self._registry.list_enabled_configs()
        by_signature = {
            (config.provider_type, config.model): config for config in enabled
        }
        output: List[ProviderConfig] = []
        for provider_id in self._provider_ids:
            meta = configs.get(provider_id)
            if meta is None or provider_id not in allowed:
                continue
            config = by_signature.get((meta.get("provider_type"), meta.get("model")))
            if config is not None:
                output.append(config)
        return output

    def uses_shared_pool(self) -> bool:
        return self._registry.uses_shared_pool()

    def count(self) -> int:
        return len(self.list_available())

    def pick_default(self) -> Optional[BaseProvider]:
        return self.get(self._primary_provider_id)

    def pick_default_id(self) -> Optional[str]:
        return self._primary_provider_id if self.get(self._primary_provider_id) else None

    def pick_vision(self, preferred: Optional[BaseProvider] = None) -> Optional[BaseProvider]:
        available = self.list_available()
        if preferred is not None:
            if getattr(preferred, "supports_vision", False) and any(
                item.provider_id == preferred.provider_id for item in available
            ):
                return preferred
            return None
        return next(
            (item for item in available if getattr(item, "supports_vision", False)),
            None,
        )


def resolve_owner_default_provider_id(
    owner_id: str,
    registry: ExpertRegistry,
) -> str | None:
    """Resolve the owner's persisted default to a stable registry record ID.

    Shared-pool and in-memory test registries retain their deterministic
    registry default. Owner-scoped BYOK registries must use the persisted
    preference and never silently switch after a saved choice becomes invalid.
    """
    def registry_default_id() -> str | None:
        picker = getattr(registry, "pick_default_id", None)
        if callable(picker):
            return picker()
        provider = registry.pick_default()
        return str(provider.provider_id) if provider is not None else None

    uses_shared_pool = getattr(registry, "uses_shared_pool", None)
    if callable(uses_shared_pool) and uses_shared_pool():
        return registry_default_id()

    from backend.db.provider_repository import (
        ensure_default_provider_id,
        get_default_provider_id,
        has_provider_configs,
    )

    if not has_provider_configs(owner_id):
        return registry_default_id()
    provider_id = get_default_provider_id(owner_id)
    if provider_id is None:
        provider_id = ensure_default_provider_id(owner_id)
    enabled = {
        str(item.get("provider_id"))
        for item in registry.list_configs()
        if item.get("enabled")
    }
    return provider_id if provider_id in enabled else None


def resolve_owner_default_provider(
    owner_id: str,
    registry: ExpertRegistry,
) -> BaseProvider | None:
    provider_id = resolve_owner_default_provider_id(owner_id, registry)
    if provider_id is None:
        return None
    getter = getattr(registry, "get", None)
    if callable(getter):
        return getter(provider_id)
    provider = registry.pick_default()
    return (
        provider
        if provider is not None and str(provider.provider_id) == provider_id
        else None
    )


# ─── Module-level singleton ──────────────────────────────────────────────────
# Global, but construction is lazy to avoid loading providers before env is ready.

_registry: Optional[ExpertRegistry] = None
_registry_lock = Lock()


def get_expert_registry() -> ExpertRegistry:
    """FastAPI dependency: returns the global ExpertRegistry singleton."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = ExpertRegistry(shared_owner_id="anonymous")
    return _registry


def _build_scoped_registry(current) -> ExpertRegistry:
    owner_id = getattr(current, "id", None) or "anonymous"
    if current is None:
        return ExpertRegistry(shared_owner_id=owner_id)
    if not settings.provider_encryption_key:
        from backend.db.provider_repository import has_provider_configs

        # Do not make existing BYOK records disappear and then silently route
        # the same user's request through the shared provider pool. Checking
        # record existence does not require decrypting or exposing a secret.
        if has_provider_configs(current.id):
            raise provider_encryption_not_configured_error()
        return ExpertRegistry(shared_owner_id=owner_id)
    try:
        from backend.db.provider_repository import list_provider_configs
        stored_configs = list_provider_configs(
            current.id,
            master_key=settings.provider_encryption_key,
        )
        # BYOK and the shared environment pool must never be combined. An owner
        # with at least one persisted config receives only those configs;
        # otherwise an explicitly enabled shared pool is used as fallback.
        registry = ExpertRegistry(
            seed_from_settings=not stored_configs,
            shared_owner_id=owner_id,
        )
        for stored in stored_configs:
            registry.register(
                stored.config,
                provider_id=stored.id,
                verification_status=stored.verification_status,
                last_checked_at=stored.last_checked_at,
                verification_error_code=stored.verification_error_code,
            )
    except ValueError as exc:
        logger.error("Unable to load encrypted provider configurations for user %s", current.id)
        raise HTTPException(503, detail="Saved provider credentials cannot be loaded.") from exc
    return registry


# Import auth after the registry class is defined so direct registry imports
# remain lightweight and the dependency graph stays explicit.
from backend.auth import get_optional_user  # noqa: E402


def get_scoped_expert_registry(current=Depends(get_optional_user)) -> ExpertRegistry:
    return _build_scoped_registry(current)
