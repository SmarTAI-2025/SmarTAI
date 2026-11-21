"""Documented effort choices for SmarTAI's official API endpoints.

Match exact API model IDs. A deployment alias or another vendor's deployment
of the same model can implement a different set of parameters.
"""
from backend.llm.endpoint_policy import is_user_defined_provider_endpoint


_LOW_MEDIUM_HIGH = ("low", "medium", "high")
_LOW_HIGH_MAX = ("low", "high", "max")
_OFF_THROUGH_XHIGH = ("none", *_LOW_MEDIUM_HIGH, "xhigh")
_LOW_THROUGH_MAX = (*_LOW_MEDIUM_HIGH, "xhigh", "max")
_MINIMAL_THROUGH_HIGH = ("minimal", *_LOW_MEDIUM_HIGH)
_OFF_LOW_HIGH_MAX = ("none", *_LOW_HIGH_MAX)
_HY_MODELS = ("hy3", "hy4", "hy4-flash", "hy4-preview")
_GEMINI_BUDGET_MODELS = ("gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite")

# Google documents these equivalent token budgets for reasoning_effort.
# https://aistudio.google.com/docs/openai#thinking
_GEMINI_BUDGETS = {"none": 0, "low": 1024, "medium": 8192, "high": 24576}

OFFICIAL_MODEL_EFFORTS = {
    # https://developers.openai.com/api/docs/models
    "openai": {
        **dict.fromkeys(("o1", "o1-2024-12-17", "o3-mini", "o3-mini-2025-01-31",
                         "o3", "o3-2025-04-16", "o4-mini", "o4-mini-2025-04-16"), _LOW_MEDIUM_HIGH),
        **dict.fromkeys(("gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-5-2025-08-07",
                         "gpt-5-mini-2025-08-07", "gpt-5-nano-2025-08-07"), _MINIMAL_THROUGH_HIGH),
        **dict.fromkeys(("gpt-5.1", "gpt-5.1-2025-11-13"), ("none", *_LOW_MEDIUM_HIGH)),
        **dict.fromkeys(("gpt-5.2", "gpt-5.2-2025-12-11", "gpt-5.4", "gpt-5.4-2026-03-05",
                         "gpt-5.4-mini", "gpt-5.4-mini-2026-03-17", "gpt-5.4-nano",
                         "gpt-5.4-nano-2026-03-17", "gpt-5.5", "gpt-5.5-2026-04-23"), _OFF_THROUGH_XHIGH),
        **dict.fromkeys(("gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
                         "gpt-6-sol", "gpt-6-luna", "gpt-6.1-terra"), ("none", *_LOW_THROUGH_MAX)),
        **dict.fromkeys(("gpt-6-astra", "gpt-6.1-sol", "gpt-6.2-astra", "gpt-6.2-sol"), _LOW_THROUGH_MAX),
    },
    # https://aistudio.google.com/docs/thinking
    "gemini": {
        "gemini-2.5-pro": _LOW_MEDIUM_HIGH,
        "gemini-2.5-flash": ("none", *_LOW_MEDIUM_HIGH),
        "gemini-2.5-flash-lite": ("none", *_LOW_MEDIUM_HIGH),
        "gemini-3-pro-preview": ("low", "high"),
        "gemini-3.1-flash-lite-image": ("minimal", "high"),
        **dict.fromkeys(("gemini-3.1-flash-lite", "gemini-3-flash-preview", "gemini-3.5-flash-lite",
                         "gemini-3.6-flash"), _MINIMAL_THROUGH_HIGH),
        **dict.fromkeys(("gemini-3.1-pro-preview", "gemini-3.8-flash", "gemini-4-argon",
                         "gemini-4-carbon"), _LOW_MEDIUM_HIGH),
        **dict.fromkeys(("gemini-4-neon", "gemini-4-hydrogen"), (*_LOW_MEDIUM_HIGH, "xhigh")),
    },
    # https://api-docs.deepseek.com/api/list-models/ and /api/create-chat-completion/
    "deepseek": dict.fromkeys(("deepseek-flash", "deepseek-v4-pro"), _LOW_HIGH_MAX),
    # https://docs.z.ai/api-reference/llm/chat-completion (GLM-5.2 and GLM-5.3 series)
    "zhipu": {
        "glm-5.2": ("none", "high", "max"),
        **dict.fromkeys(("glm-5.3", "glm-5.3-flash", "glm-5.3-flashx"), _LOW_HIGH_MAX),
    },
    # https://platform.kimi.com/docs/guide/use-reasoning-effort
    "moonshot": dict.fromkeys(("kimi-k3", "kimi-k3.1-pro", "kimi-k3.1-pro-highspeed"), _LOW_HIGH_MAX),
    # https://help.aliyun.com/en/model-studio/qwen-api-via-openai-chat-completions
    "qwen": {
        "qwen4-max": ("none", "low", "high", "xhigh"),
        **dict.fromkeys(("qwen3.8-max", "qwen3.8-max-0902", "qwen3.8-flash", "qwen3.8-2.4t-a95b",
                         "qwen3.8-27b", "qwen3.8-omni-flash"), ("none", "low", "medium", "xhigh")),
        "kimi-k3.1-pro": (*_LOW_MEDIUM_HIGH, "max"),
        "MiniMax/MiniMax-M3.1-Flash": _LOW_THROUGH_MAX,
        **dict.fromkeys(("glm-5.2", "glm-5.1", "glm-5", "deepseek-v4-pro"), ("high", "max")),
        **dict.fromkeys(("kimi/kimi-k3", "kimi/kimi-k3.1-pro", "kimi/kimi-k3.1-pro-highspeed"), ("max",)),
        **dict.fromkeys(("glm-5.3", "ZHIPU/GLM-5.3", "ZHIPU/GLM-5.3-Flash", "ZHIPU/GLM-5.3-FlashX",
                         "kimi-k3", "deepseek-v4-flash-0731", "deepseek-v4-pro-0813", "deepseek-v4.1-flash"),
                        _LOW_HIGH_MAX),
    },
    # https://cloud.tencent.com/document/product/1823/131208
    "tencent": {
        **dict.fromkeys(_HY_MODELS, ("none", "high")),
        "hy3": ("none", "low", "high"),
        "deepseek-v4-pro": _OFF_LOW_HIGH_MAX,
        "glm-5.2": ("none", "minimal", "low", "medium", "high", "xhigh", "max"),
        **dict.fromkeys(("glm-5.3", "glm-5.3-flash", "glm-5.3-flashx", "kimi-k3"), _LOW_HIGH_MAX),
    },
    # https://ark.volcengine.com/region:cn-beijing/docs/deep-thinking.md
    "doubao": {
        **dict.fromkeys(("doubao-seed-evolving", "doubao-seed-2-1-pro-260915", "doubao-seed-2-1-lite-260915",
                         "doubao-seed-2-1-pro-260628", "doubao-seed-2-1-turbo-260628", "doubao-seed-2-0-lite-260428",
                         "doubao-seed-2-0-mini-260428", "doubao-seed-2-0-pro-260215", "doubao-seed-2-0-lite-260215",
                         "doubao-seed-2-0-mini-260215", "doubao-seed-2-0-code-preview-260215",
                         "doubao-seed-character-260628"), ("none", *_LOW_MEDIUM_HIGH)),
        **dict.fromkeys(("glm-5-3-flash-260828", "glm-5-2-260617", "kimi-k3-1-pro-261024"), _LOW_HIGH_MAX),
        **dict.fromkeys(("deepseek-v4-1-flash-261017", "deepseek-v4-1-flash-260910",
                         "deepseek-v4-1-flash-economy-261017", "deepseek-v4-pro-ga-260813",
                         "deepseek-v4-flash-ga-260731"), _OFF_LOW_HIGH_MAX),
    },
    # https://platform.minimax.cn/docs/api-reference/text-openai-api
    "minimax": {"MiniMax-M3.1-Flash": _LOW_THROUGH_MAX},
}


def manual_model_efforts(provider_type: str, base_url: str | None, protocol: str) -> dict[str, tuple[str, ...]]:
    if is_user_defined_provider_endpoint(provider_type, base_url, protocol):
        return {}
    return OFFICIAL_MODEL_EFFORTS.get(provider_type, {})


def gemini_thinking_config(model: str, effort: str) -> dict:
    if model in _GEMINI_BUDGET_MODELS:
        return {"thinking_budget": _GEMINI_BUDGETS[effort]}
    return {"thinking_level": effort.upper()}


def compatible_reasoning_effort(provider_type: str, model: str, effort: str | None) -> str | None:
    if provider_type == "tencent" and model in _HY_MODELS and effort == "none":
        return "no_think"
    return effort
