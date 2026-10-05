import json

import pytest

from backend.agents.ingest_agent import generate_missing_question_materials
from backend.llm.providers import LLMResponse
from backend.tools import structured_llm
from backend.tools.structured_llm import StructuredOutputInvalidError


def valid():
    return json.dumps({"candidates": [{"target_id": "q1:reference_answer", "q_id": "q1",
        "target": "reference_answer", "text_value": "42"}]})


class Provider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return LLMResponse(content=value, model="synthetic", provider="synthetic", duration_ms=1)


async def generate(provider):
    return await generate_missing_question_materials({"q1": {"stem": "6 * 7", "max_score": 1}},
        [{"target_id": "q1:reference_answer", "q_id": "q1", "target": "reference_answer"}], 6, provider)


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["not JSON", '{"candidates": []}', '{"candidates": [{"q_id": "q1"}]}'])
async def test_one_format_repair_keeps_the_original_question_scope(broken):
    provider = Provider([broken, valid()])
    result = await generate(provider)
    assert result[0].text_value == "42"
    assert len(provider.calls) == 2
    assert provider.calls[0][1] is provider.calls[1][1]
    assert "schema" in provider.calls[1][-1].content
    assert broken not in provider.calls[1][-1].content


@pytest.mark.asyncio
async def test_deterministically_repairable_json_uses_no_extra_model_call():
    provider = Provider(["```json\n" + valid() + "}\n```"])
    assert (await generate(provider))[0].q_id == "q1"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_invalid_repair_stops_without_a_third_generation():
    provider = Provider(["invalid", "invalid", valid()])
    with pytest.raises(StructuredOutputInvalidError):
        await generate(provider)
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_transient_and_format_attempts_share_three_call_limit(monkeypatch):
    async def no_sleep(_seconds):
        pass
    monkeypatch.setattr(structured_llm._ainvoke_with_retry.retry, "sleep", no_sleep)
    provider = Provider([RuntimeError("503 unavailable"), "invalid", RuntimeError("503 unavailable"), valid()])
    with pytest.raises(structured_llm.TransientLLMError):
        await generate(provider)
    assert len(provider.calls) == 3


@pytest.mark.asyncio
async def test_third_attempt_invalid_output_does_not_get_a_fresh_repair_budget(monkeypatch):
    async def no_sleep(_seconds):
        pass
    monkeypatch.setattr(structured_llm._ainvoke_with_retry.retry, "sleep", no_sleep)
    provider = Provider([RuntimeError("503 unavailable"), RuntimeError("503 unavailable"), "invalid", valid()])
    with pytest.raises(StructuredOutputInvalidError):
        await generate(provider)
    assert len(provider.calls) == 3
