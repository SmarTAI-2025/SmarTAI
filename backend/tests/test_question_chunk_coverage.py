import json
from types import SimpleNamespace

import pytest

from backend.agents import ingest_agent
from backend.config import settings
from backend.domain.errors import ValidationError
from backend.services.background_errors import classify_background_error


def row(number):
    return {"q_id":"q1", "number":number, "type":"证明题", "stem":"Prove the stated property.", "criterion":""}


@pytest.mark.asyncio
async def test_irrelevant_intro_chunk_does_not_abort_requested_question(monkeypatch):
    monkeypatch.setattr(settings, "source_chunk_chars", 120)
    monkeypatch.setattr(settings, "source_chunk_overlap_chars", 10)
    text = "Background material.\n" * 12 + "\n1.1.5. Prove the stated property."
    calls = []

    async def invoke(provider, messages):
        source = messages[-1].content.split("**[Problem Source Document]**", 1)[1]
        calls.append(source)
        return SimpleNamespace(content=json.dumps({"problems":[row("1.1.5")] if "1.1.5." in source else []}))

    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", invoke)
    store = {}
    await ingest_agent.extract_problems(text, SimpleNamespace(provider_id="fake"), store,
        structure_mode="extract_from_source", extraction_hint="题号: 1.1.5")
    assert len(calls) > 1
    assert [p["number"] for p in store.values()] == ["1.1.5"]


@pytest.mark.asyncio
async def test_all_empty_chunks_preserve_old_questions_and_fail(monkeypatch):
    monkeypatch.setattr(settings, "source_chunk_chars", 120)
    monkeypatch.setattr(settings, "source_chunk_overlap_chars", 10)

    async def invoke(*args):
        return SimpleNamespace(content='{"problems":[]}')

    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", invoke)
    store = {"old":{"number":"old"}}
    with pytest.raises(ValueError, match="did not extract"):
        await ingest_agent.extract_problems("Background\n" * 60, SimpleNamespace(provider_id="fake"), store)
    assert store == {"old":{"number":"old"}}


@pytest.mark.asyncio
async def test_requested_identifiers_must_be_complete_and_extras_are_excluded(monkeypatch):
    monkeypatch.setattr(settings, "source_chunk_chars", 0)
    output = [row("1.1.5")]

    async def invoke(*args):
        return SimpleNamespace(content=json.dumps({"problems":output}))

    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", invoke)
    store = {"old":{"number":"old"}}
    with pytest.raises(ValidationError) as error:
        await ingest_agent.extract_problems("Source", SimpleNamespace(provider_id="fake"), store,
            structure_mode="extract_from_source", extraction_hint="题号: 1.1.5, 1.1.7")
    assert classify_background_error(error.value, "problem_extraction_failed") == "question_targets_incomplete"
    assert store == {"old":{"number":"old"}}
    output.extend([{**row("1.1.8"), "q_id":"q2", "stem":"An unrelated extra problem."},
                   {**row("1.1.7"), "q_id":"q3", "stem":"Prove another property."}])
    await ingest_agent.extract_problems("Source", SimpleNamespace(provider_id="fake"), store,
        structure_mode="extract_from_source", extraction_hint="题号: 1.1.5, 1.1.7")
    assert [p["number"] for p in store.values()] == ["1.1.5", "1.1.7"]
    from backend.services.question_preparation_artifacts import BasePreparationPayloadV1
    assert list(BasePreparationPayloadV1(problem_data=store).problem_data) == ["q1", "q2"]
