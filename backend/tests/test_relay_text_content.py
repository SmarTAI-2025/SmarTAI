"""Relay text must retain its JSON type through parsing and text verification."""

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.db.provider_repository import (
    get_provider_config, set_provider_verification, upsert_provider_config,
)
from backend.llm.providers import ProviderRequestError, SafeRelayProvider
from backend.main import app
from backend.models import ProviderConfig
from backend.tests.test_custom_provider_api import _teacher


PROTOCOLS = ["openai_chat_completions", "anthropic_messages", "gemini_generate_content"]


def relay_config(protocol):
    return ProviderConfig(
        provider_type="deepseek", model="fixture", api_key="synthetic-relay-key",
        base_url="https://relay.example/v1", wire_protocol=protocol,
    )


def relay_payload(protocol, blocks):
    if protocol == "openai_chat_completions":
        return {
            "choices": [{"message": {"content": blocks}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 2},
        }
    if protocol == "anthropic_messages":
        return {
            "content": blocks, "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 2},
        }
    return {
        "candidates": [{"content": {"parts": blocks}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 2},
    }


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("raw_text", [None, {}, 42, 4.2, True, False, [], ["OK"]])
def test_relay_does_not_stringify_non_text_values(protocol, raw_text):
    provider = SafeRelayProvider(relay_config(protocol))
    blocks = [{"type": "text", "text": raw_text}]
    response = provider._parse_response(relay_payload(protocol, blocks), 1.5)

    assert response.content == ""
    assert (response.input_tokens, response.output_tokens) == (12, 2)
    assert response.duration_ms == 1.5
    assert response.finish_reason == "stop"


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("text", ["OK", "42", "None", "{}", "你好", " \n ", ""])
def test_relay_preserves_actual_strings_without_a_blacklist(protocol, text):
    provider = SafeRelayProvider(relay_config(protocol))
    response = provider._parse_response(
        relay_payload(protocol, [{"type": "text", "text": text}]), 1,
    )
    assert response.content == text


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_relay_joins_only_string_blocks_in_original_order(protocol):
    blocks = [
        {"type": "text", "text": "O"},
        {"type": "text", "text": None},
        {"type": "text", "text": {}},
        {"type": "text", "text": 42},
        {"type": "text"}, None, "ignored", 42,
        {"type": "text", "text": "K"},
    ]
    response = SafeRelayProvider(relay_config(protocol))._parse_response(
        relay_payload(protocol, blocks), 1,
    )
    assert response.content == "OK"
    assert (response.input_tokens, response.output_tokens) == (12, 2)


@pytest.mark.parametrize("protocol,hidden", [
    ("anthropic_messages", {"type": "thinking", "text": "PRIVATE"}),
    ("gemini_generate_content", {"thought": True, "text": "PRIVATE"}),
])
def test_relay_still_excludes_thought_blocks(protocol, hidden):
    response = SafeRelayProvider(relay_config(protocol))._parse_response(
        relay_payload(protocol, [hidden, {"type": "text", "text": "OK"}]), 1,
    )
    assert response.content == "OK"


@pytest.mark.parametrize("content", [None, {}, 42, True])
def test_openai_relay_still_rejects_invalid_top_level_content(content):
    provider = SafeRelayProvider(relay_config("openai_chat_completions"))
    with pytest.raises(ProviderRequestError, match="provider_response_invalid"):
        provider._parse_response(relay_payload("openai_chat_completions", content), 1)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("raw_text,verified", [
    (None, False), ({}, False), (42, False), (True, False), ([], False),
    ("", False), (" \n ", False), ("OK", True), ("42", True),
    ("None", True), ("{}", True),
])
def test_text_verification_uses_real_relay_parser(monkeypatch, protocol, raw_text, verified):
    owner_id = "relay-text-owner"
    headers = _teacher(owner_id)
    stored = upsert_provider_config(
        owner_id, relay_config(protocol), master_key=settings.provider_encryption_key,
    )
    # A failed recheck must replace an earlier green result; a successful
    # recheck must clear the earlier failure reason.
    assert set_provider_verification(
        owner_id, stored.id, verification_status="failed" if verified else "verified",
        checked_at=1, error_code="expert_verification_empty_response" if verified else None,
    )
    monkeypatch.setattr(settings, "custom_provider_verification_cooldown_seconds", 0)
    monkeypatch.setattr("backend.llm.providers._ENDPOINT_BREAKERS", {})
    calls = []

    class Client:
        async def post(self, url, **kwargs):
            calls.append((url, kwargs["json"]))
            return httpx.Response(200, json=relay_payload(
                protocol, [{"type": "text", "text": raw_text}],
            ))

    async def relay_client(self):
        return Client()

    # Replace only the upstream transport; keep provider dispatch, parser,
    # mounted verification API and persistence on their real paths.
    monkeypatch.setattr(SafeRelayProvider, "_relay_client", relay_client)
    with TestClient(app) as client:
        response = client.post(f"/experts/{stored.id}/verify", headers=headers)
        listed = client.get("/experts/available", headers=headers)

    after = get_provider_config(owner_id, stored.id, master_key=settings.provider_encryption_key)
    expected = "verified" if verified else "failed"
    assert response.status_code == (200 if verified else 502), response.text
    assert after.verification_status == expected
    assert after.config.enabled
    assert len(calls) == 1
    assert calls[0][0] == SafeRelayProvider(relay_config(protocol))._target_url
    assert listed.status_code == 200
    current = next(item for item in listed.json() if item["provider_id"] == stored.id)
    assert current["verification_status"] == expected
    if verified:
        assert response.json()["verification_status"] == "verified"
        assert after.verification_error_code is None
    else:
        code = "expert_verification_empty_response"
        assert response.json()["detail"]["code"] == code
        assert after.verification_error_code == current["verification_error_code"] == code
