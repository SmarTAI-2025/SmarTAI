"""Official GLM/OpenAI-compatible timeouts must remain actionable in grading."""
import httpx
from langchain_openai.chat_models.base import OpenAITimeoutError

from backend.skills.base import classify_skill_error
from backend.tools.structured_llm import _classify_exception, TransientLLMError


def test_sdk_timeout_survives_retry_wrapper_and_skill_classification():
    error = OpenAITimeoutError(httpx.Request("POST", "https://example.test/chat/completions"))
    classified = _classify_exception(error)
    assert isinstance(classified, TransientLLMError)
    kind, message = classify_skill_error(classified)
    assert kind == "transient_llm"
    assert "超时" in message and "未知" not in message
