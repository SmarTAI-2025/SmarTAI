"""No network/provider calls: validate security floors and SDK construction."""
from pathlib import Path
import socket

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]


def _requirements(path):
    return {canonicalize_name(r.name): r for raw in map(str.strip, path.read_text().splitlines())
            if raw and not raw.startswith(("#", "-"))
            for r in [Requirement(raw)]}


@pytest.mark.parametrize("name,vulnerable", [
    ("python-multipart", "0.0.26"), ("cryptography", "46.0.7"),
    ("langchain-openai", "0.3.35"), ("langchain-anthropic", "1.4.3"),
    ("pyjwt", "2.12.1"), ("py7zr", "1.1.0"),
    ("starlette", "0.52.1"), ("pillow", "11.3.0"),
    ("anyio", "4.13.0"), ("urllib3", "2.5.0"),
])
def test_deployment_excludes_audited_vulnerable_versions(name, vulnerable):
    requirements = _requirements(ROOT / "render-requirements.txt")
    requirements.update(_requirements(ROOT / "security-constraints.txt"))
    assert vulnerable not in requirements[name].specifier
    assert "-c security-constraints.txt" in (ROOT / "render-requirements.txt").read_text()


@pytest.mark.parametrize("kind,model,cls_name", [
    ("openai", "gpt-4o-mini", "OpenAIProvider"),
    ("gemini", "gemini-2.0-flash", "GeminiProvider"),
    ("anthropic", "claude-test-model", "AnthropicProvider"),
])
def test_patched_provider_sdks_construct_without_network_or_credentials(monkeypatch, kind, model, cls_name):
    from backend.llm import providers
    from backend.models import ProviderConfig
    def refuse_network(*args, **kwargs):
        raise AssertionError("SDK construction must not call a provider")
    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    monkeypatch.setattr(socket, "create_connection", refuse_network)
    monkeypatch.setattr(providers.settings, "https_proxy", "")
    monkeypatch.setattr(providers.settings, "http_proxy", "")
    provider = getattr(providers, cls_name)(ProviderConfig(
        provider_type=kind, model=model, api_key="synthetic-sdk-constructor-test"))
    client = provider._build_client_sync()
    assert client is not None
    assert client.max_retries == 0
