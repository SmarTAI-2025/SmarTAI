import asyncio
from types import SimpleNamespace

import pytest

from backend.db.knowledge_repository import set_task_documents
from backend.knowledge.query_plan import QueryPlanner
from backend.knowledge.retriever import PersistentKnowledgeRetriever
from backend.llm.providers import LLMResponse
from backend.models import ProviderConfig
from backend.tests.test_knowledge_ingestion import owner
from backend.tests.test_knowledge_retrieval_versions import assignment, document
from backend.tools import knowledge


class Provider:
    config = ProviderConfig(provider_type="zhipu", model="test", api_key="synthetic")
    is_shared_pool = False

    def __init__(self):
        self.calls = []

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(messages)
        await asyncio.sleep(.01)
        return LLMResponse('{"queries":["finite group even order"]}', "grade-route", "test", 1,
                           input_tokens=12, output_tokens=7)


@pytest.mark.asyncio
async def test_grading_rewrite_shared_by_student_batch_without_sending_corpus(monkeypatch):
    from backend.knowledge import query_plan
    who = owner()
    task = assignment(who)
    doc = document(who, ["finite group even order PRIVATE_SOURCE_MARKER"])
    set_task_documents(assignment_id=task, owner_id=who, document_ids=[doc.id])
    monkeypatch.setattr(knowledge, "_retriever", PersistentKnowledgeRetriever())
    monkeypatch.setattr(query_plan, "QUERY_PLANNER", QueryPlanner())
    provider = Provider()
    metrics = []

    async def increment(**kwargs):
        metrics.append(kwargs)

    results = await asyncio.gather(*(knowledge.retrieve_for_grading(
        "偶数阶有限群", 3, scope=task, provider=provider,
        reporter=SimpleNamespace(increment_stage_metrics=increment)) for _ in range(40)))
    assert len(provider.calls) == 1
    assert sum(m["knowledge_query_provider_calls"] for m in metrics) == 1
    assert sum(m["knowledge_query_input_tokens"] for m in metrics) == 12
    assert all(result[0].citation["document_id"] == doc.id for result in results)
    assert "PRIVATE_SOURCE_MARKER" not in str(provider.calls)
    assert "偶数阶有限群" in provider.calls[0][-1].content


@pytest.mark.asyncio
async def test_grading_without_selected_knowledge_never_rewrites(monkeypatch):
    who = owner()
    task = assignment(who)
    monkeypatch.setattr(knowledge, "_retriever", PersistentKnowledgeRetriever())
    provider = Provider()
    assert await knowledge.retrieve_for_grading("finite group", scope=task, provider=provider) == []
    assert await knowledge.retrieve_for_grading("finite group", provider=provider) == []
    assert not provider.calls


@pytest.mark.asyncio
async def test_deselecting_knowledge_during_rewrite_discards_old_results(monkeypatch):
    from backend.knowledge import query_plan
    who = owner()
    task = assignment(who)
    doc = document(who, ["finite group even order"])
    set_task_documents(assignment_id=task, owner_id=who, document_ids=[doc.id])
    monkeypatch.setattr(knowledge, "_retriever", PersistentKnowledgeRetriever())
    monkeypatch.setattr(query_plan, "QUERY_PLANNER", QueryPlanner())

    class RevokingProvider(Provider):
        async def ainvoke(self, messages, **kwargs):
            set_task_documents(assignment_id=task, owner_id=who, document_ids=[])
            return await super().ainvoke(messages, **kwargs)

    assert await knowledge.retrieve_for_grading("finite group", scope=task, provider=RevokingProvider()) == []


@pytest.mark.asyncio
async def test_switching_selection_during_local_retrieval_discards_old_results():
    who = owner()
    task = assignment(who)
    old = document(who, ["finite group original evidence"])
    new = document(who, ["finite group replacement evidence"])
    set_task_documents(assignment_id=task, owner_id=who, document_ids=[old.id])

    class ChangingRetriever(PersistentKnowledgeRetriever):
        async def retrieve_documents(self, *args, **kwargs):
            result = await super().retrieve_documents(*args, **kwargs)
            assert result
            set_task_documents(assignment_id=task, owner_id=who, document_ids=[new.id])
            return result

    assert await ChangingRetriever().retrieve("finite group", scope=task) == []


@pytest.mark.asyncio
async def test_switching_selection_before_rewrite_does_not_charge_or_mix_results(monkeypatch):
    who = owner()
    task = assignment(who)
    old = document(who, ["finite group original evidence"])
    new = document(who, ["finite group replacement evidence"])
    set_task_documents(assignment_id=task, owner_id=who, document_ids=[old.id])
    retriever = PersistentKnowledgeRetriever()

    async def change_after_read(*args, **kwargs):
        result = await retriever.retrieve(*args, **kwargs)
        assert result
        set_task_documents(assignment_id=task, owner_id=who, document_ids=[new.id])
        return result

    monkeypatch.setattr(knowledge, "retrieve", change_after_read)
    provider = Provider()
    assert await knowledge.retrieve_for_grading("finite group", scope=task, provider=provider) == []
    assert not provider.calls
