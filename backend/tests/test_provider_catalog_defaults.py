from backend.config import Settings
from backend.llm.provider_catalog import PROVIDER_CATALOG


def test_catalog_order_and_new_configuration_defaults():
    expected = {
        "gemini": "gemini-3.5-flash-lite", "openai": "gpt-6-luna",
        "anthropic": "claude-sonnet-5-5", "deepseek": "deepseek-flash",
        "zhipu": "glm-5.3-flash", "moonshot": "kimi-k3", "qwen": "qwen3.8-flash",
    }
    assert [entry.provider_type for entry in PROVIDER_CATALOG] == list(expected)
    for entry in PROVIDER_CATALOG:
        assert entry.public_dict()["default_model"] == expected[entry.provider_type]
        assert Settings.model_fields[f"{entry.provider_type}_model"].default == entry.default_model
        assert entry.default_base_url.startswith("https://")
        assert "{" not in entry.default_base_url


def test_explicit_model_override_is_preserved():
    config = Settings(_env_file=None, openai_model="my-pinned-model")
    assert config.openai_model == "my-pinned-model"
