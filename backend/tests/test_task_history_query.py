"""Current-task Ask uses the existing bounded, owner-scoped interpreter."""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agents import history_query_agent
from backend.api import tasks
from backend.config import settings
from backend.llm.registry import get_scoped_expert_registry
from backend.models import User


class Provider:
    provider_id = "history-test"

    def __init__(self, output):
        self.output = output
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=json.dumps(self.output))


@pytest.fixture
def history_client(monkeypatch):
    owner = User(id="history-owner", username="teacher", role="teacher", password_hash="test")
    provider = Provider({
        "filters": {"needs_attention": True},
        "sort": "attention_first", "explanation": "先看需要处理的任务",
    })
    app = FastAPI()
    app.include_router(tasks.router)
    app.dependency_overrides[tasks.require_teacher] = lambda: owner
    app.dependency_overrides[get_scoped_expert_registry] = lambda: SimpleNamespace(
        pick_default=lambda: provider,
    )
    def own_tasks(*, owner_id):
        assert owner_id == owner.id
        return {}
    monkeypatch.setattr(tasks.task_facade, "list_tasks", own_tasks)
    monkeypatch.setattr(tasks, "_history_facets", lambda items, owner_id: {
        "semesters": ["2026-2027-autumn"],
        "courses": [{"id": "own-course", "name": "Calculus", "code": "MATH"}],
        "tags": [{"id": "own-tag", "name": "Quiz"}],
    })
    monkeypatch.setattr(settings, "history_query_llm_enabled", True)
    history_query_agent._last_llm_at.clear()
    history_query_agent._llm_daily_usage.clear()
    return TestClient(app), provider


def test_current_task_exact_filters_do_not_call_model(history_client):
    client, provider = history_client
    response = client.post("/tasks/query/interpret", json={"query": "未完成的任务，名称倒序"})
    assert response.status_code == 200, response.text
    assert response.json()["filters"]["unfinished"] is True
    assert response.json()["sort"] == "name_desc"
    assert response.json()["source"] == "deterministic"
    assert provider.calls == []


def test_current_task_disabled_model_preserves_unresolved_text_and_reports_fallback(history_client, monkeypatch):
    client, provider = history_client
    monkeypatch.setattr(settings, "history_query_llm_enabled", False)
    response = client.post("/tasks/query/interpret", json={"query": "草稿 微积分"})
    assert response.status_code == 200, response.text
    assert response.json()["filters"]["statuses"] == ["draft"]
    assert response.json()["filters"]["q"] == "微积分"
    assert "模型增强当前关闭" in response.json()["explanation"]
    assert provider.calls == []


def test_current_task_unresolved_language_reaches_existing_agent_with_owner_candidates(history_client):
    client, provider = history_client
    response = client.post("/tasks/query/interpret", json={"query": "先把那些需要我接手的摆到前面"})
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "llm"
    assert response.json()["filters"]["needs_attention"] is True
    assert response.json()["sort"] == "attention_first"
    assert len(provider.calls) == 1
    prompt = json.loads(str(provider.calls[0][-1].content))
    assert prompt["candidates"]["courses"][0]["id"] == "own-course"
    assert "students" not in prompt
    assert "results" not in prompt


def test_current_task_model_cannot_add_another_owners_course(history_client):
    client, provider = history_client
    provider.output["filters"] = {"course_id": "foreign-course"}
    response = client.post("/tasks/query/interpret", json={"query": "挑出另一个课程吧"})
    assert response.status_code == 200, response.text
    assert "course_id" not in response.json()["filters"]
    assert response.json()["ambiguities"]


def test_current_task_full_intent_can_correct_negated_status_without_losing_omitted_fields(history_client):
    client, provider = history_client
    provider.output = {"filters": {"statuses": ["finalized"]}}
    response = client.post("/tasks/query/interpret", json={"query": "Calculus 不要草稿，只看已完成"})
    assert response.status_code == 200, response.text
    assert response.json()["filters"]["statuses"] == ["finalized"]
    assert response.json()["filters"]["course_id"] == "own-course"
    assert len(provider.calls) == 1


def test_current_task_explicit_empty_controls_clear_tentative_filters(history_client):
    client, provider = history_client
    provider.output = {"filters": {"statuses": [], "course_id": None}, "sort": None}
    response = client.post("/tasks/query/interpret", json={"query": "不要只看 Calculus 草稿，取消名称倒序，所有任务都列出来"})
    assert response.status_code == 200, response.text
    assert response.json()["filters"]["statuses"] == []
    assert "course_id" not in response.json()["filters"]
    assert response.json()["sort"] is None


def test_current_task_foreign_tags_do_not_clear_a_valid_deterministic_tag(history_client):
    client, provider = history_client
    provider.output = {"filters": {"tag_ids": ["foreign-tag"]}}
    response = client.post("/tasks/query/interpret", json={"query": "Quiz 需要我先处理的任务"})
    assert response.status_code == 200, response.text
    assert response.json()["filters"]["tag_ids"] == ["own-tag"]
    assert response.json()["ambiguities"]
