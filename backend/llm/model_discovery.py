"""Read bounded model catalogs and project only confirmed reasoning options."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from backend.llm.endpoint_policy import (
    ProviderEndpointError,
    build_safe_provider_catalog_client,
    effective_provider_base_url,
    is_user_defined_provider_endpoint,
)
from backend.llm.official_reasoning import OFFICIAL_MODEL_EFFORTS
from backend.models import ProviderConfig

EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_MAX_MODELS = 1000
_MAX_BYTES = 2 * 1024 * 1024
_TIMEOUT = 8.0


@dataclass(frozen=True)
class ModelCatalog:
    models: list[dict]
    status: str


def is_openrouter(base_url: str | None, protocol: str | None) -> bool:
    url = urlsplit(base_url or "")
    return (
        protocol == "openai_chat_completions"
        and url.scheme == "https"
        and url.hostname == "openrouter.ai"
        and url.path.rstrip("/") == "/api/v1"
    )


def relay_reasoning_fields(config: ProviderConfig) -> dict:
    effort = config.reasoning_effort
    if effort is None:
        return {}
    if is_openrouter(config.base_url, config.wire_protocol):
        return {"reasoning": {"effort": effort}}
    if config.wire_protocol == "anthropic_messages":
        return {"output_config": {"effort": effort}}
    if config.wire_protocol == "openai_chat_completions":
        return {"reasoning_effort": effort}
    return {}


def valid_model_id(value: str) -> bool:
    return 0 < len(value) <= 200 and all(character.isprintable() and not character.isspace() for character in value)


def _entries(payload: dict, provider_type: str, protocol: str, *, official: bool, openrouter: bool) -> list[dict]:
    items = payload.get("models" if protocol == "gemini_generate_content" else "data")
    if not isinstance(items, list):
        raise ValueError("invalid_model_catalog")
    models = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        model = item.get("id")
        if protocol == "gemini_generate_content":
            if isinstance(item.get("name"), str):
                model = item["name"].removeprefix("models/")
            methods = item.get("supportedGenerationMethods")
            if isinstance(methods, list) and "generateContent" not in methods:
                continue
        if not isinstance(model, str) or not valid_model_id(model):
            continue
        if official and provider_type == "openai" and model.startswith((
            "text-embedding-", "whisper-", "tts-", "dall-e-", "gpt-image-", "omni-moderation-",
        )):
            continue
        efforts = OFFICIAL_MODEL_EFFORTS.get(provider_type, {}).get(model, ()) if official else ()
        if official and provider_type == "deepseek" and "effort" in item:
            # DeepSeek reports thinking-on efforts for this particular model.
            metadata = item["effort"]
            supported = metadata.get("supported_levels") if isinstance(metadata, dict) else None
            efforts = [level for level in EFFORT_LEVELS if level in supported] if isinstance(supported, list) else []
        elif protocol == "anthropic_messages":
            capabilities = item.get("capabilities")
            capability = capabilities.get("effort") if isinstance(capabilities, dict) else None
            if isinstance(capability, dict) and capability.get("supported") is not False:
                efforts = [
                    level for level in EFFORT_LEVELS if isinstance(capability.get(level), dict)
                    and capability[level].get("supported") is True
                ]
        elif openrouter and isinstance(item.get("reasoning"), dict):
            reasoning = item["reasoning"]
            supported = reasoning.get("supported_efforts", [])
            if supported is None:
                supported = EFFORT_LEVELS
            if isinstance(supported, (list, tuple)):
                efforts = [level for level in EFFORT_LEVELS if level in supported
                           and not (level == "none" and reasoning.get("mandatory") is True)]
        models[model] = {"id": model, "reasoning_efforts": list(efforts)}
        if len(models) >= _MAX_MODELS:
            break
    return list(models.values())


def _headers(protocol: str, api_key: str) -> dict[str, str]:
    if protocol == "anthropic_messages":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    if protocol == "gemini_generate_content":
        return {"x-goog-api-key": api_key}
    return {"Authorization": f"Bearer {api_key}"}


def _catalog_url(base_url: str, protocol: str) -> str:
    path = urlsplit(base_url).path.lower().rstrip("/")
    if protocol == "anthropic_messages" and not path.endswith("/v1"):
        return base_url + "/v1/models"
    if protocol == "gemini_generate_content" and not path.endswith("/v1beta"):
        return base_url + "/v1beta/models"
    return base_url + "/models"


def discover_models(provider_type: str, base_url: str | None, protocol: str, api_key: str) -> ModelCatalog:
    base = effective_provider_base_url(provider_type, base_url).rstrip("/")
    official = not is_user_defined_provider_endpoint(provider_type, base_url, protocol)
    target = _catalog_url(base, protocol)
    known_router = is_openrouter(base, protocol)
    params = None
    if official:
        if protocol == "anthropic_messages":
            params = {"limit": 1000}
        elif protocol == "gemini_generate_content":
            params = {"pageSize": 1000}
    try:
        if official:
            from backend.llm.providers import _configured_proxy_url
            proxy = _configured_proxy_url() if provider_type in {"openai", "gemini", "anthropic"} else None
            client = httpx.Client(proxy=proxy, trust_env=False, follow_redirects=False, timeout=_TIMEOUT)
        else:
            client = build_safe_provider_catalog_client(base, target, timeout_seconds=_TIMEOUT)
        deadline = time.monotonic() + _TIMEOUT
        with client, client.stream("GET", target, headers=_headers(protocol, api_key), params=params) as response:
            if response.status_code in {401, 403}:
                return ModelCatalog([], "denied")
            if response.status_code == 429:
                return ModelCatalog([], "rate_limited")
            if response.status_code != 200:
                return ModelCatalog([], "unavailable")
            data = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() >= deadline:
                    return ModelCatalog([], "timeout")
                if len(data) + len(chunk) > _MAX_BYTES:
                    return ModelCatalog([], "unavailable")
                data.extend(chunk)
            payload = json.loads(data)
        if not isinstance(payload, dict):
            return ModelCatalog([], "unavailable")
        models = _entries(payload, provider_type, protocol, official=official, openrouter=known_router)
        if not models:
            return ModelCatalog([], "empty")
        incomplete = payload.get("has_more") is True or bool(payload.get("nextPageToken")) or len(models) >= _MAX_MODELS
        return ModelCatalog(models, "partial" if incomplete else "ready")
    except httpx.TimeoutException:
        return ModelCatalog([], "timeout")
    except (httpx.HTTPError, ProviderEndpointError, ValueError):
        return ModelCatalog([], "unavailable")
