from fastapi.testclient import TestClient
from types import SimpleNamespace

from backend.auth import create_token
from backend.db.knowledge_storage_repository import request_document_cleanup
from backend.knowledge.query_plan import QueryPlan
from backend.main import app
from backend.tests.test_knowledge_ingestion import owner
from backend.tests.test_knowledge_retrieval_versions import document


class Registry:
    def get(self, key):
        return SimpleNamespace(config=SimpleNamespace(enabled=True)) if key == "allowed" else None

    def uses_shared_pool(self):
        return False


def test_query_expansion_is_explicit_owner_scoped_and_revocable(monkeypatch):
    from backend.knowledge.query_plan import QUERY_PLANNER
    calls = []
    who, other = owner(), owner()
    doc = document(who, ["finite group even order element two"])
    headers = {"Authorization":"Bearer " + create_token(who, "teacher")}
    outsider = {"Authorization":"Bearer " + create_token(other, "teacher")}
    client = TestClient(app)
    async def plan(query, **kwargs):
        # Keep the existing rewrite allowance until the shared HTTP deadline
        # is near; do not impose an unrelated eight-second model cutoff.
        assert 8 < kwargs["timeout"] <= 30
        calls.append((query, kwargs["scope"]))
        return QueryPlan(queries=("finite group even order",), status="rewritten", provider_calls=1)
    monkeypatch.setattr(QUERY_PLANNER, "plan", plan)
    monkeypatch.setattr("backend.api.knowledge.get_scoped_expert_registry", lambda _: Registry())
    try:
        payload = dict(query="偶数阶有限群", document_ids=[doc.id])
        assert client.post("/knowledge/search", headers=headers, json=payload).json()["provider_calls"] == 0
        assert not calls
        payload["query_provider_id"] = "missing"
        assert client.post("/knowledge/search", headers=headers, json=payload).status_code == 404
        payload["query_provider_id"] = "allowed"
        assert client.post("/knowledge/search", headers=outsider, json=payload).status_code == 404
        assert not calls
        response = client.post("/knowledge/search", headers=headers, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["matches"][0]["content"] == "finite group even order element two"
        assert calls == [(payload["query"], "owner:" + who)]
        async def revoke(query, **kwargs):
            request_document_cleanup(doc.id, who)
            return QueryPlan(queries=("finite group even order",), status="rewritten", provider_calls=1)
        monkeypatch.setattr(QUERY_PLANNER, "plan", revoke)
        response = client.post("/knowledge/search", headers=headers, json={**payload, "query":"finite group"})
        assert response.status_code == 200 and response.json()["matches"] == []
    finally:
        client.close()


def test_local_search_does_not_load_saved_provider_credentials(monkeypatch):
    who = owner()
    doc = document(who, ["finite group evidence"])

    def unavailable(_):
        raise AssertionError("Local search must not load credentials")

    monkeypatch.setattr("backend.api.knowledge.get_scoped_expert_registry", unavailable)
    with TestClient(app) as client:
        response = client.post("/knowledge/search", headers={"Authorization": "Bearer " + create_token(who, "teacher")},
                               json={"query": "finite group", "document_ids": [doc.id]})
    assert response.status_code == 200
    assert response.json()["matches"] and response.json()["provider_calls"] == 0


def test_expansion_timeout_preserves_local_results_and_rechecks_deletion(monkeypatch):
    import asyncio
    import time
    from backend.knowledge.query_plan import QUERY_PLANNER
    from backend.knowledge.retriever import PersistentKnowledgeRetriever
    who = owner()
    headers = {"Authorization": "Bearer " + create_token(who, "teacher")}
    original = PersistentKnowledgeRetriever.retrieve_documents
    revoke = False
    async def retrieve(self, query, *args, **kwargs):
        if query == "expanded query":
            if revoke:
                request_document_cleanup(kwargs["documents"][0].id, who)
            await asyncio.sleep(10)
        return await original(self, query, *args, **kwargs)
    async def plan(query, **kwargs):
        assert 0 < kwargs["timeout"] <= .4
        return QueryPlan(queries=("expanded query",), status="rewritten", provider_calls=1)
    monkeypatch.setattr(PersistentKnowledgeRetriever, "retrieve_documents", retrieve)
    monkeypatch.setattr(QUERY_PLANNER, "plan", plan)
    monkeypatch.setattr("backend.api.knowledge.get_scoped_expert_registry", lambda _: Registry())
    monkeypatch.setattr("backend.api.knowledge.SEARCH_TIMEOUT_SECONDS", .5)
    monkeypatch.setattr("backend.api.knowledge.SEARCH_VISIBILITY_RESERVE_SECONDS", .1)
    with TestClient(app) as client:
        for revoke in (False, True):
            doc = document(who, ["finite group evidence"])
            started = time.monotonic()
            response = client.post("/knowledge/search", headers=headers,
                json={"query": "finite group", "document_ids": [doc.id], "query_provider_id": "allowed"})
            assert time.monotonic() - started < 1.5
            assert response.status_code == 200
            data = response.json()
            assert data["partial"] and "expansion_timeout" in data["warnings"]
            assert bool(data["matches"]) is not revoke
            if revoke:
                assert data["documents"] == []
            assert data["provider_calls"] == 1


def test_local_search_timeout_is_partial_not_a_complete_empty_search(monkeypatch):
    import asyncio
    from backend.knowledge.retriever import PersistentKnowledgeRetriever
    who = owner()
    doc = document(who, ["finite group evidence"])
    async def slow(*args, **kwargs):
        await asyncio.sleep(10)
    monkeypatch.setattr(PersistentKnowledgeRetriever, "retrieve_documents", slow)
    monkeypatch.setattr("backend.api.knowledge.SEARCH_TIMEOUT_SECONDS", .3)
    monkeypatch.setattr("backend.api.knowledge.SEARCH_VISIBILITY_RESERVE_SECONDS", .1)
    with TestClient(app) as client:
        response = client.post("/knowledge/search", headers={"Authorization": "Bearer " + create_token(who, "teacher")},
            json={"query": "finite group", "document_ids": [doc.id]})
    assert response.status_code == 200
    data = response.json()
    assert data["matches"] == [] and data["partial"]
    assert data["warnings"] == ["local_search_timeout"]


def test_visibility_timeout_drops_retained_results_and_metadata(monkeypatch):
    import time
    who = owner()
    doc = document(who, ["finite group evidence"])
    from backend.knowledge.retriever import PersistentKnowledgeRetriever
    from backend.tools.knowledge import KnowledgeChunk
    async def found(*args, **kwargs):
        return [KnowledgeChunk("private evidence", "book", 1, {"document_id": doc.id})]
    def slow(*args, **kwargs):
        time.sleep(.5)
        return {doc.id}
    monkeypatch.setattr(PersistentKnowledgeRetriever, "retrieve_documents", found)
    monkeypatch.setattr("backend.knowledge.retriever.live_references", slow)
    monkeypatch.setattr("backend.api.knowledge.SEARCH_TIMEOUT_SECONDS", .2)
    monkeypatch.setattr("backend.api.knowledge.SEARCH_VISIBILITY_RESERVE_SECONDS", .1)
    with TestClient(app) as client:
        response = client.post("/knowledge/search", headers={"Authorization": "Bearer " + create_token(who, "teacher")},
            json={"query": "finite group", "document_ids": [doc.id]})
    data = response.json()
    assert data["matches"] == data["documents"] == []
    assert data["partial"] and "visibility_timeout" in data["warnings"]


def test_rewrite_timeout_returns_local_results_without_a_billed_retry(monkeypatch):
    import asyncio
    from backend.knowledge.query_plan import QueryPlanner
    from backend.tests.test_knowledge_query_plan import Provider
    class Slow(Provider):
        async def ainvoke(self, *args, **kwargs):
            self.calls += 1
            await asyncio.sleep(10)
    provider = Slow()
    class SlowRegistry(Registry):
        def get(self, key):
            return provider
    who = owner()
    doc = document(who, ["finite group evidence"])
    # Isolate the provider deadline from real database/thread scheduling.
    # A rewrite timeout must not require local visibility reads to finish in 100ms.
    monkeypatch.setattr("backend.knowledge.query_plan.QUERY_PLANNER", QueryPlanner(timeout=.01))
    monkeypatch.setattr("backend.api.knowledge.get_scoped_expert_registry", lambda _: SlowRegistry())
    monkeypatch.setattr("backend.api.knowledge.SEARCH_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr("backend.api.knowledge.SEARCH_VISIBILITY_RESERVE_SECONDS", 2)
    with TestClient(app) as client:
        for expected_calls in (1, 0):
            response = client.post("/knowledge/search", headers={"Authorization": "Bearer " + create_token(who, "teacher")},
                json={"query": "finite group", "document_ids": [doc.id], "query_provider_id": "allowed"})
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["matches"] and data["partial"]
            assert data["query_plan"]["status"] == "rewrite_timeout"
            assert data["provider_calls"] == expected_calls
    assert provider.calls == 1
