"""Single source of truth for supported model-provider network metadata."""
from __future__ import annotations

from dataclasses import asdict, dataclass


WIRE_PROTOCOLS = (
    "openai_chat_completions",
    "anthropic_messages",
    "gemini_generate_content",
)


@dataclass(frozen=True)
class ProviderCatalogEntry:
    provider_type: str
    display_name: str
    default_base_url: str
    wire_protocol: str
    custom_base_url_supported: bool
    docs_url: str
    console_url: str
    usage_url: str

    def public_dict(self) -> dict[str, str | bool]:
        return asdict(self)


PROVIDER_CATALOG: tuple[ProviderCatalogEntry, ...] = (
    ProviderCatalogEntry(
        "gemini",
        "Google Gemini",
        "https://generativelanguage.googleapis.com",
        "gemini_generate_content",
        True,
        "https://ai.google.dev/gemini-api/docs",
        "https://aistudio.google.com/app/apikey",
        "https://aistudio.google.com/usage",
    ),
    ProviderCatalogEntry(
        "openai",
        "OpenAI",
        "https://api.openai.com/v1",
        "openai_chat_completions",
        True,
        "https://platform.openai.com/docs",
        "https://platform.openai.com/api-keys",
        "https://platform.openai.com/usage",
    ),
    ProviderCatalogEntry(
        "zhipu",
        "Zhipu AI",
        "https://open.bigmodel.cn/api/paas/v4",
        "openai_chat_completions",
        True,
        "https://docs.bigmodel.cn/",
        "https://open.bigmodel.cn/usercenter/apikeys",
        "https://open.bigmodel.cn/console/overview",
    ),
    ProviderCatalogEntry(
        "deepseek",
        "DeepSeek",
        "https://api.deepseek.com/v1",
        "openai_chat_completions",
        True,
        "https://api-docs.deepseek.com/",
        "https://platform.deepseek.com/api_keys",
        "https://platform.deepseek.com/usage",
    ),
    ProviderCatalogEntry(
        "moonshot",
        "Moonshot (Kimi)",
        "https://api.moonshot.cn/v1",
        "openai_chat_completions",
        True,
        "https://platform.moonshot.cn/docs",
        "https://platform.moonshot.cn/console/api-keys",
        "https://platform.moonshot.cn/console/account",
    ),
    ProviderCatalogEntry(
        "qwen",
        "Qwen (通义千问)",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "openai_chat_completions",
        True,
        "https://help.aliyun.com/zh/model-studio/",
        "https://bailian.console.aliyun.com/",
        "https://bailian.console.aliyun.com/",
    ),
    ProviderCatalogEntry(
        "anthropic",
        "Anthropic",
        "https://api.anthropic.com",
        "anthropic_messages",
        True,
        "https://docs.anthropic.com/en/docs",
        "https://platform.claude.com/settings/keys",
        "https://platform.claude.com/usage",
    ),
)

PROVIDER_CATALOG_BY_TYPE = {
    entry.provider_type: entry for entry in PROVIDER_CATALOG
}


def catalog_entry(provider_type: str) -> ProviderCatalogEntry:
    try:
        return PROVIDER_CATALOG_BY_TYPE[provider_type]
    except KeyError as exc:
        raise ValueError("provider_type_not_supported") from exc


def effective_wire_protocol(
    provider_type: str,
    wire_protocol: str | None,
) -> str:
    entry = catalog_entry(provider_type)
    protocol = wire_protocol or entry.wire_protocol
    if protocol not in WIRE_PROTOCOLS:
        raise ValueError("provider_wire_protocol_not_supported")
    return protocol
