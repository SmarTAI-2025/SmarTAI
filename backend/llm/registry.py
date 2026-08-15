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
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, status

from backend.config import settings
from backend.llm.endpoint_policy import is_user_defined_provider_endpoint
from backend.models import ProviderConfig
from backend.llm.providers import BaseProvider, build_provider
from backend.llm.provider_catalog import (
    PROVIDER_CATALOG_BY_TYPE,
    effective_wire_protocol,
)

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
        self._uses_shared_pool = bool(
            seed_from_settings and settings.shared_pool_enabled
        )
        if seed_from_settings and settings.shared_pool_enabled:
            self._seed_from_settings()
            self._uses_shared_pool = bool(self._providers)

    def _seed_from_settings(self) -> None:
        """Populate from env vars at startup. User can override via API."""
        if settings.gemini_api_key:
            self._register_shared_setting(ProviderConfig(
                provider_type="gemini",
                api_key=settings.gemini_api_key,
                model=settings.gemini_model,
            ))
        if settings.openai_api_key and settings.openai_api_key != "YOUR_API_KEY_HERE":
            self._register_shared_setting(ProviderConfig(
                provider_type="openai",
                api_key=settings.openai_api_key,
                model=settings.openai_model,
                base_url=settings.openai_api_base,
            ))
        if settings.zhipu_api_key:
            self._register_shared_setting(ProviderConfig(
                provider_type="zhipu",
                api_key=settings.zhipu_api_key,
                model=settings.zhipu_model,
                base_url=settings.zhipu_api_base,
            ))
        if settings.anthropic_api_key:
            self._register_shared_setting(ProviderConfig(
                provider_type="anthropic",
                api_key=settings.anthropic_api_key,
                model=settings.anthropic_model,
            ))
        if settings.deepseek_api_key:
            self._register_shared_setting(ProviderConfig(
                provider_type="deepseek",
                api_key=settings.deepseek_api_key,
                model=settings.deepseek_model,
                base_url=settings.deepseek_api_base,
            ))
        if settings.moonshot_api_key:
            self._register_shared_setting(ProviderConfig(
                provider_type="moonshot",
                api_key=settings.moonshot_api_key,
                model=settings.moonshot_model,
                base_url=settings.moonshot_api_base,
            ))
        if settings.qwen_api_key:
            self._register_shared_setting(ProviderConfig(
                provider_type="qwen",
                api_key=settings.qwen_api_key,
                model=settings.qwen_model,
                base_url=settings.qwen_api_base,
            ))

    def _register_shared_setting(self, config: ProviderConfig) -> None:
        if is_user_defined_provider_endpoint(
            config.provider_type,
            config.base_url,
            config.wire_protocol,
        ):
            logger.error(
                "Skipped shared provider with a user-defined route; provider_type=%s",
                config.provider_type,
            )
            return
        self.register(config)

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
        custom_route = is_user_defined_provider_endpoint(
            config.provider_type,
            config.base_url,
            config.wire_protocol,
        )
        if self._uses_shared_pool and custom_route:
            raise ValueError("custom_provider_shared_pool_not_allowed")
        if custom_route and not settings.custom_provider_endpoints_available:
            registry_id = provider_id or f"{config.provider_type}:{config.model}"
            with self._lock:
                self._providers.pop(registry_id, None)
                self._configs[registry_id] = config
                self._verification[registry_id] = {
                    "verification_status": verification_status,
                    "last_checked_at": last_checked_at,
                    "verified_at": (
                        last_checked_at
                        if verification_status == "verified"
                        else None
                    ),
                    "verification_error_code": verification_error_code,
                }
            return registry_id
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
        """Look up one provider while honoring the production kill switch."""
        with self._lock:
            provider = self._providers.get(provider_id)
            if (
                provider is not None
                and is_user_defined_provider_endpoint(
                    self._configs[provider_id].provider_type,
                    self._configs[provider_id].base_url,
                    self._configs[provider_id].wire_protocol,
                )
                and not settings.custom_provider_endpoints_available
            ):
                provider = None
        return self._guard_shared(provider)

    def list_available(self) -> List[BaseProvider]:
        """Return all enabled providers. Order is deterministic (sorted by provider_id)."""
        with self._lock:
            available = sorted(
                [
                    provider
                    for provider_id, provider in self._providers.items()
                    if self._is_text_available_unlocked(provider_id)
                ],
                key=lambda p: p.provider_id,
            )
        return [self._guard_shared(provider) for provider in available]

    def _is_text_available_unlocked(self, provider_id: str) -> bool:
        config = self._configs[provider_id]
        return bool(
            config.enabled
            and (
                not is_user_defined_provider_endpoint(
                    config.provider_type,
                    config.base_url,
                    config.wire_protocol,
                )
                or settings.custom_provider_endpoints_available
            )
        )

    def _guard_shared(self, provider: Optional[BaseProvider]):
        if provider is None or not self._uses_shared_pool:
            return provider
        return _GuardedSharedProvider(provider, self._shared_owner_id or "anonymous")

    def _registry_id_for_provider(self, provider: BaseProvider) -> str | None:
        target = (
            provider._provider
            if isinstance(provider, _GuardedSharedProvider)
            else provider
        )
        with self._lock:
            return next(
                (
                    provider_id
                    for provider_id, registered in self._providers.items()
                    if registered is target
                ),
                None,
            )

    def list_enabled_configs(self) -> List[ProviderConfig]:
        """Trusted backend-only view used by the owner-scoped RAG embedder."""
        return list(self._embedding_eligible_configs_by_id().values())

    def _embedding_eligible_configs_by_id(self) -> Dict[str, ProviderConfig]:
        """Return exact provider-record mappings that may receive embeddings.

        Keeping the stable record id in this internal view prevents a selected
        custom route from being confused with an official configuration that
        happens to use the same provider type and model name.
        """
        with self._lock:
            return {
                provider_id: config.model_copy(deep=True)
                for provider_id, config in self._configs.items()
                if self._is_text_available_unlocked(provider_id)
                and not is_user_defined_provider_endpoint(
                    config.provider_type,
                    config.base_url,
                    config.wire_protocol,
                )
            }

    def uses_shared_pool(self) -> bool:
        return self._uses_shared_pool

    def list_configs(self) -> List[Dict[str, object]]:
        """Return redacted config dicts (api_key stripped) for UI listing.

        Each dict includes the registry-known `provider_id` and resolved
        `display_name` so the frontend dropdown can label items without having
        to re-derive the id.
        """
        with self._lock:
            base_labels: dict[str, str] = {}
            label_counts: dict[str, int] = {}
            endpoint_descriptors: dict[str, str] = {}
            protocols: dict[str, str] = {}
            protocol_labels = {
                "openai_chat_completions": "OpenAI Chat Completions",
                "anthropic_messages": "Anthropic Messages",
                "gemini_generate_content": "Gemini generateContent",
            }
            for provider_id, config in self._configs.items():
                entry = PROVIDER_CATALOG_BY_TYPE.get(config.provider_type)
                provider_label = entry.display_name if entry else config.provider_type
                label = (
                    (config.display_name or "").strip()
                    or f"{provider_label} · {config.model}"
                )
                base_labels[provider_id] = label
                label_key = label.casefold()
                label_counts[label_key] = label_counts.get(label_key, 0) + 1
                protocol = effective_wire_protocol(
                    config.provider_type,
                    config.wire_protocol,
                )
                protocols[provider_id] = protocol
                identity = (
                    config.endpoint_identity
                    or (entry.default_base_url if entry else config.base_url)
                    or ""
                )
                parsed = urlsplit(identity)
                endpoint_label = f"{parsed.hostname or identity}{parsed.path.rstrip('/')}"
                endpoint_descriptors[provider_id] = (
                    f"{endpoint_label} · {protocol_labels.get(protocol, protocol)}"
                )

            candidate_labels = {
                provider_id: (
                    base_labels[provider_id]
                    if label_counts[base_labels[provider_id].casefold()] == 1
                    else f"{base_labels[provider_id]} · {endpoint_descriptors[provider_id]}"
                )
                for provider_id in self._configs
            }
            candidate_counts: dict[str, int] = {}
            for label in candidate_labels.values():
                key = label.casefold()
                candidate_counts[key] = candidate_counts.get(key, 0) + 1

            out: List[Dict[str, object]] = []
            for pid, c in self._configs.items():
                verification = self._verification.get(pid, {})
                resolved_display_name = candidate_labels[pid]
                if candidate_counts[resolved_display_name.casefold()] > 1:
                    resolved_display_name = (
                        f"{resolved_display_name} · {pid[-8:]}"
                    )
                out.append({
                    "provider_id": pid,
                    "provider_type": c.provider_type,
                    "model": c.model,
                    "base_url": c.base_url,
                    "endpoint_identity": c.endpoint_identity,
                    "endpoint_descriptor": endpoint_descriptors[pid],
                    "wire_protocol": protocols[pid],
                    "enabled": self._is_text_available_unlocked(pid),
                    "display_name": resolved_display_name,
                    "configured_display_name": c.display_name,
                    "resolved_display_name": resolved_display_name,
                    "max_concurrent": c.max_concurrent,
                    "rpm": c.rpm,
                    "scope": "shared" if self._uses_shared_pool else "owner",
                    "is_shared": self._uses_shared_pool,
                    "editable": not self._uses_shared_pool,
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
                    if self._is_text_available_unlocked(provider_id)
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

        If the caller already picked a default provider and it supports vision,
        keep using it. Otherwise fall back to the first enabled vision provider.
        """
        available = self.list_available()
        if preferred is not None and getattr(preferred, "supports_vision", False):
            preferred_id = self._registry_id_for_provider(preferred)
            if preferred_id is not None and any(
                self._registry_id_for_provider(provider) == preferred_id
                for provider in available
            ):
                return preferred
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
        eligible = self._registry._embedding_eligible_configs_by_id()
        return [
            eligible[provider_id]
            for provider_id in self._provider_ids
            if provider_id in eligible
        ]

    def uses_shared_pool(self) -> bool:
        return self._registry.uses_shared_pool()

    def count(self) -> int:
        return len(self.list_available())

    def pick_default(self) -> Optional[BaseProvider]:
        return self.get(self._primary_provider_id)

    def pick_default_id(self) -> Optional[str]:
        return self._primary_provider_id if self.get(self._primary_provider_id) else None

    def pick_vision(self, preferred: Optional[BaseProvider] = None) -> Optional[BaseProvider]:
        available: List[tuple[str, BaseProvider]] = []
        for provider_id in self._provider_ids:
            provider = self._registry.get(provider_id)
            if provider is None:
                continue
            available.append((provider_id, provider))
        if preferred is not None and getattr(preferred, "supports_vision", False):
            preferred_id = self._registry._registry_id_for_provider(preferred)
            if any(provider_id == preferred_id for provider_id, _ in available):
                return preferred
        return next(
            (
                item
                for _, item in available
                if getattr(item, "supports_vision", False)
            ),
            None,
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
