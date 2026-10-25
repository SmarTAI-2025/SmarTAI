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
    provider_id = "synthetic:repair"
    model = "synthetic"
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
@pytest.mark.parametrize("text,expected", [
    (r"$$x+y=z$", r"$x+y=z$"),
    (r"Keep $\frac{1}{$ for editing.", r"Keep $\frac{1}{$ for editing."),
    (r"Keep $\unknowncommand{x}$ for editing.", r"Keep $\unknowncommand{x}$ for editing."),
])
async def test_display_only_math_issues_do_not_retry_or_fail_generation(text, expected):
    provider = Provider([json.dumps({"candidates": [{"target_id": "q1:reference_answer", "q_id": "q1",
        "target": "reference_answer", "text_value": text}]})])
    assert (await generate(provider))[0].text_value == expected
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_missing_subpart_is_repaired_within_the_same_question():
    from backend.agents.question_preparation_agent import generate_major_question_materials, requested_major_question_materials
    from backend.progress.tracker import ProgressReporter
    from backend.tests.test_question_preparation_recovery import _questions, _candidates
    problems = _questions(1)
    correct = [item.model_dump(mode="json") for item in _candidates("q1")]
    incomplete = [dict(item) for item in correct]
    next(item for item in incomplete if item["target"] == "reference_answer")["text_value"] = "(a) Only the first subpart."
    provider = Provider([json.dumps({"candidates": incomplete}), json.dumps({"candidates": correct})])
    result = await generate_major_question_materials(problems_data=problems,
        requested_targets=requested_major_question_materials(problems), test_case_count=6,
        provider=provider, reporter=ProgressReporter("format-test"))
    assert len(provider.calls) == 2
    assert "(b)" in next(item.text_value for item in result if item.target == "reference_answer")


@pytest.mark.asyncio
async def test_invalid_repair_stops_without_a_third_generation():
    provider = Provider(["invalid", "invalid", valid()])
    with pytest.raises(StructuredOutputInvalidError):
        await generate(provider)
    assert len(provider.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_partial", [False, True])
@pytest.mark.parametrize("repair_succeeds", [False, True])
async def test_malformed_unicode_retries_before_accepting_generated_materials(allow_partial, repair_succeeds):
    broken = (r'{"candidates":[{"target_id":"q1:reference_answer","q_id":"q1",'
              r'"target":"reference_answer","text_value":"\u8bbe $\\mathbb{Z}$ \uffa"}]}')
    answer = '设 $\\bar{a} \\in \\mathbb{Z}/n\\mathbb{Z}$。'
    corrected = json.dumps({"candidates": [{"target_id": "q1:reference_answer", "q_id": "q1",
        "target": "reference_answer", "text_value": answer}]})
    provider = Provider([broken, corrected if repair_succeeds else broken])
    call = generate_missing_question_materials(
        {"q1": {"stem": "Prove residue-class multiplication is associative.", "max_score": 10}},
        [{"target_id": "q1:reference_answer", "q_id": "q1", "target": "reference_answer"}],
        6, provider, allow_partial=allow_partial)
    if repair_succeeds:
        assert (await call)[0].text_value == answer
    else:
        with pytest.raises(StructuredOutputInvalidError):
            await call
    assert len(provider.calls) == 2
    assert provider.calls[0][1] is provider.calls[1][1]
    assert broken not in provider.calls[1][-1].content


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


@pytest.mark.asyncio
async def test_function_question_repairs_stdin_cases_before_persistence():
    def response(case):
        return json.dumps({"candidates": [{"target_id": "q1:test_cases", "q_id": "q1",
            "target": "test_cases", "test_cases": [case]}]})
    provider = Provider([
        response({"input": "[0,0]", "expected_output": "[0.5,0.5]"}),
        response({"function_name": "stable_softmax", "function_args": [[0, 0]],
                  "expected_return": "[0.5, 0.5]"}),
    ])
    result = await generate_missing_question_materials(
        {"q1": {"stem": "Implement the function.\n```python\ndef stable_softmax(xs):\n    ...\n```", "max_score": 10}},
        [{"target_id": "q1:test_cases", "q_id": "q1", "target": "test_cases"}], 6, provider)
    assert len(provider.calls) == 2
    case = result[0].test_cases[0]
    assert case.function_args == [[0, 0]]
    assert case.io_mode == "function"
    assert case.source == "llm_generated"


@pytest.mark.asyncio
async def test_stdin_program_keeps_stdin_cases_without_extra_generation():
    provider = Provider([json.dumps({"candidates": [{"target_id": "q1:test_cases", "q_id": "q1",
        "target": "test_cases", "test_cases": [{"input": "2\n3", "expected_output": "5"}]}]})])
    result = await generate_missing_question_materials(
        {"q1": {"stem": "Read two integers from stdin and print their sum.", "max_score": 10}},
        [{"target_id": "q1:test_cases", "q_id": "q1", "target": "test_cases"}], 6, provider)
    assert len(provider.calls) == 1
    assert result[0].test_cases[0].input == "2\n3"
    assert result[0].test_cases[0].function_name is None
