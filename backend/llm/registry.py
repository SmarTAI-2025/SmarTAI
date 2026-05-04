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
from typing import Dict, List, Optional, Any
from threading import Lock

from backend.config import settings
from backend.models import ProviderConfig
from backend.llm.providers import BaseProvider, build_provider

logger = logging.getLogger(__name__)


class ExpertRegistry:
    """
    Registry of configured LLM providers ("experts").
    Thread-safe because FastAPI may dispatch across multiple threads.
    """

    def __init__(self) -> None:
        self._providers: Dict[str, BaseProvider] = {}  # keyed by provider_id
        self._configs: Dict[str, ProviderConfig] = {}  # keyed by provider_id
        self._lock = Lock()
        # Seed with env-var defaults if present
        self._seed_from_settings()

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

    def register(self, config: ProviderConfig) -> str:
        """Register or update a provider. Returns its provider_id."""
        provider = build_provider(config)
        with self._lock:
            self._providers[provider.provider_id] = provider
            self._configs[provider.provider_id] = config
        logger.info(f"Registered expert: {provider.provider_id}")
        return provider.provider_id

    def unregister(self, provider_id: str) -> bool:
        """Remove an expert. Returns True if it existed."""
        with self._lock:
            existed = provider_id in self._providers
            self._providers.pop(provider_id, None)
            self._configs.pop(provider_id, None)
        if existed:
            logger.info(f"Unregistered expert: {provider_id}")
        return existed

    def get(self, provider_id: str) -> Optional[BaseProvider]:
        """Look up a provider by its provider_id."""
        with self._lock:
            return self._providers.get(provider_id)

    def list_available(self) -> List[BaseProvider]:
        """Return all enabled providers. Order is deterministic (sorted by provider_id)."""
        with self._lock:
            return sorted(
                [p for pid, p in self._providers.items() if self._configs[pid].enabled],
                key=lambda p: p.provider_id,
            )

    def list_configs(self) -> List[Dict[str, object]]:
        """Return redacted config dicts (api_key stripped) for UI listing.

        Each dict includes the registry-known `provider_id` and resolved
        `display_name` so the frontend dropdown can label items without having
        to re-derive the id.
        """
        with self._lock:
            out: List[Dict[str, object]] = []
            for pid, c in self._configs.items():
                out.append({
                    "provider_id": pid,
                    "provider_type": c.provider_type,
                    "model": c.model,
                    "base_url": c.base_url,
                    "enabled": c.enabled,
                    "display_name": c.display_name or pid,
                    "max_concurrent": c.max_concurrent,
                    "api_key": "***",
                })
            return out

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
                _registry = ExpertRegistry()
    return _registry
