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
