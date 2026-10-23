"""Frontier shares current task APIs while retaining a separate capability."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
import time

import jwt
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from sqlalchemy import select

from backend.auth import create_token, decode_token, hash_password
from backend.config import settings
from backend.db.frontier_demo_models import FrontierDemoDailyUsageRecord
from backend.db.models import UserRecord
from backend.db.session import session_scope
from backend.llm import registry as registries
from backend.llm.providers import LLMResponse
from backend.main import app
from backend.models import User
from backend.services import frontier_demo
from backend.state import get_user_store, register_user


class FakeProvider:
    supports_vision = True
    def __init__(self, config):
        self.config = config
        self.provider_id = config.provider_type + ":" + config.model
        self.provider_type = config.provider_type
        self.model = config.model
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        return LLMResponse(content="live test response", provider=self.provider_id, model=self.model, duration_ms=0)

    async def ainvoke_vision(self, prompt, images, **kwargs):
        return await self.ainvoke([HumanMessage(content=prompt)], **kwargs)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "frontier_demo_enabled", True)
    for provider_type in ("gemini", "openai", "zhipu", "anthropic", "deepseek", "moonshot", "qwen"):
        monkeypatch.setattr(settings, provider_type + "_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "fake-demo-only-secret")
    monkeypatch.setattr(settings, "gemini_model", "demo-model")
    monkeypatch.setattr(settings, "frontier_demo_daily_session_limit", 20)
    monkeypatch.setattr(settings, "frontier_demo_session_cooldown_seconds", 0)
    monkeypatch.setattr(settings, "frontier_demo_daily_request_limit", 500)
    monkeypatch.setattr(settings, "frontier_demo_daily_estimated_token_limit", 500000)
    monkeypatch.setattr(settings, "shared_pool_enabled", False)
    monkeypatch.setattr(settings, "require_auth", True)
    monkeypatch.setattr(registries, "build_provider", FakeProvider)
    return TestClient(app)


def headers(token):
    return {"Authorization": "Bearer " + token}


def issue(client):
    response = client.post("/auth/frontier-demo-session")
    assert response.status_code == 200, response.text
    return response.json()


def task(client, session, key="demo"):
    response = client.post("/tasks/", headers={**headers(session["token"]), "Idempotency-Key": key}, json={"name": "Synthetic Frontier test"})
    assert response.status_code == 200, response.text
    return response.json()


def test_demo_captures_language_once_and_suggests_matching_feedback(enabled):
    session = issue(enabled)
    created = task(enabled, session)
    task_id = created["task_id"]
    path = f"/tasks/{task_id}/content-language/default"
    auth = headers(session["token"])
    assert enabled.put(path, headers=auth, json={"language": "en"}).json() == {"language": "en"}
    assert enabled.put(path, headers=auth, json={"language": "zh"}).json() == {"language": "en"}
    setup = enabled.get(f"/tasks/{task_id}/grading-setup", headers=auth).json()
    assert setup["suggested_setup"]["feedback_language"] == "en"
    assert setup["grading_setup"] is None
    assert setup["workflow_revision"] == created["workflow_revision"]


def test_disabled_and_incomplete_configuration_fail_closed(enabled, monkeypatch):
    monkeypatch.setattr(settings, "frontier_demo_enabled", False)
    assert enabled.post("/auth/frontier-demo-session").status_code == 503
    monkeypatch.setattr(settings, "frontier_demo_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "")
    unavailable = enabled.post("/auth/frontier-demo-session")
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "frontier_demo_provider_unavailable"
    with session_scope() as db:
        assert db.scalar(select(FrontierDemoDailyUsageRecord)) is None


def test_demo_uses_current_tasks_with_owner_isolation_and_no_teacher_privileges(enabled):
    first, second = issue(enabled), issue(enabled)
    assert first["user"]["id"] != second["user"]["id"]
    assert "auth_scope" not in first["user"]
    assert "fake-demo-only-secret" not in str(first)
    assert enabled.cookies.get(settings.refresh_cookie_name) is None
    created = task(enabled, first)
    assert created["owner_id"] == first["user"]["id"]
    assert enabled.get(f"/tasks/{created['task_id']}", headers=headers(second["token"])).status_code == 404
    assert enabled.get("/tasks/", headers=headers(second["token"])).json() == {}
    for route in ("/courses",):
        assert enabled.get(route, headers=headers(first["token"])).status_code == 403
    experts = enabled.get("/experts/available", headers=headers(first["token"]))
    assert experts.status_code == 200
    assert [item["model"] for item in experts.json()] == ["demo-model"]
    assert "fake-demo-only-secret" not in experts.text
    assert enabled.delete("/experts/not-allowed", headers=headers(first["token"])).status_code == 403
    for route in ("/auth/activity", "/auth/logout", "/auth/password-change"):
        assert enabled.post(route, headers=headers(first["token"]), json={}).status_code == 403
    assert enabled.get("/auth/me", headers=headers(first["token"])).status_code == 200
    assert enabled.post("/auth/login", json={"username": first["user"]["username"], "password": "any-password"}).status_code == 401
    # Current main task routes provide the configured provider; no /experts bypass.
    setup = enabled.get(f"/tasks/{created['task_id']}/grading-setup", headers=headers(first["token"]))
    assert setup.status_code == 200, setup.text
    assert setup.json()["available_experts"][0]["is_shared"] is True
    assert "fake-demo-only-secret" not in setup.text
    capabilities = enabled.get(f"/tasks/{created['task_id']}/question-preparation/capabilities", headers=headers(first["token"]))
    assert capabilities.status_code == 200, capabilities.text
    prepared = enabled.post(
        f"/tasks/{created['task_id']}/question-preparation/sources/preflight",
        headers=headers(first["token"]),
        data={"inline_text": "Q1. What is 1 + 1?", "structure_mode": "organized",
              "role": "problem", "save_to_library": "false", "defer_recognition": "true"},
    )
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["source_token"]
    assert enabled.get(f"/tasks/{created['task_id']}/submission-recognition/input", headers=headers(first["token"])).status_code != 403
    # Task-scoped analytics reaches main's owner-aware 404, not session denial.
    query = enabled.post("/analytics/missing/query", headers=headers(first["token"]), json={"question": "sort ascending", "mode": "filter"})
    assert query.status_code == 404


def test_teacher_login_cookie_and_provider_isolation_survive_demo_renewal(enabled):
    teacher = User(id="real-teacher", username="realteacher", password_hash=hash_password("teacher-pass"))
    register_user(teacher)
    signed_in = enabled.post("/auth/login", json={"username": teacher.username, "password": "teacher-pass"})
    assert signed_in.status_code == 200
    cookie = enabled.cookies.get(settings.refresh_cookie_name)
    demo = issue(enabled)
    assert enabled.cookies.get(settings.refresh_cookie_name) == cookie
    assert registries._build_scoped_registry(teacher).list_available() == []
    # Renew expired demo access with a teacher cookie present: same demo owner,
    # same absolute deadline, no normal teacher token and no cookie rotation.
    payload = decode_token(demo["token"])
    payload.update(iat=time.time() - 1300, exp=time.time() - 100)
    expired = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    refreshed = enabled.post("/auth/frontier-demo-session", headers=headers(expired))
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["user"]["id"] == demo["user"]["id"]
    assert decode_token(refreshed.json()["token"])["session_exp"] == payload["session_exp"]
    assert enabled.cookies.get(settings.refresh_cookie_name) == cookie
    fallback = enabled.post("/auth/refresh", headers=headers(expired))
    assert fallback.json()["user"]["id"] == demo["user"]["id"]
    assert enabled.cookies.get(settings.refresh_cookie_name) == cookie
    refreshed_teacher = enabled.post("/auth/refresh").json()
    assert refreshed_teacher["user"]["id"] == teacher.id
    assert enabled.get(f"/tasks/{task(enabled, demo)['task_id']}", headers=headers(refreshed_teacher["token"])).status_code == 404


def test_expiry_disabled_readonly_and_private_admin_remain_enforced(enabled, monkeypatch, tmp_path):
    from backend.tests.test_private_admin_app import private_app
    demo = issue(enabled)
    private = private_app(monkeypatch, tmp_path)
    assert private.post("/api/auth/frontier-demo-session").status_code == 404
    assert private.get("/api/auth/me", headers=headers(demo["token"])).status_code == 401
    assert private.post("/api/auth/refresh", headers=headers(demo["token"])).status_code == 404
    assert private.post("/api/auth/frontier-demo-session/end", headers=headers(demo["token"])).status_code == 404
    owner = get_user_store()[demo["user"]["id"]]
    register_user(owner.model_copy(update={"is_read_only": True}))
    assert enabled.post("/tasks/", headers=headers(demo["token"]), json={"name": "denied"}).status_code == 403
    assert enabled.post("/auth/frontier-demo-session/end", headers=headers(demo["token"])).status_code == 403
    register_user(owner.model_copy(update={"is_active": False}))
    assert enabled.get("/auth/me", headers=headers(demo["token"])).status_code == 401
    assert enabled.post("/auth/frontier-demo-session", headers=headers(demo["token"])).status_code == 401
    assert not get_user_store()[owner.id].is_active
    # A normal user-format JWT cannot turn a stored guest into a teacher login.
    assert enabled.get("/auth/me", headers=headers(create_token(owner.id, "teacher"))).status_code == 401


def test_absolute_deadline_and_revocation_cannot_be_renewed(enabled):
    demo = issue(enabled)
    payload = decode_token(demo["token"])
    payload.update(iat=time.time() - 8000, exp=time.time() - 100, session_exp=time.time() - 1)
    expired = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    assert enabled.post("/auth/frontier-demo-session", headers=headers(expired)).status_code == 401
    owner = get_user_store()[demo["user"]["id"]]
    register_user(owner.model_copy(update={"auth_version": owner.auth_version + 1}))
    assert enabled.post("/auth/frontier-demo-session", headers=headers(demo["token"])).status_code == 401


def test_leaving_demo_deletes_only_guest_tasks_and_preserves_teacher_cookie(enabled):
    from backend.db.models import AssignmentRecord
    teacher = User(id="leave-teacher", username="leaveteacher", password_hash=hash_password("teacher-pass"))
    register_user(teacher)
    signed_in = enabled.post("/auth/login", json={"username": teacher.username, "password": "teacher-pass"}).json()
    cookie = enabled.cookies.get(settings.refresh_cookie_name)
    teacher_task = task(enabled, signed_in)
    first, second = issue(enabled), issue(enabled)
    guest_task = task(enabled, first)
    other_guest_task = task(enabled, second)
    assert enabled.post("/auth/frontier-demo-session/end", headers=headers(signed_in["token"])).status_code == 401
    assert enabled.post("/auth/frontier-demo-session/end").status_code == 401
    ended = enabled.post("/auth/frontier-demo-session/end", headers=headers(first["token"]))
    assert ended.status_code == 200, ended.text
    assert ended.json() == {"status": "ended", "cleanup": "queued", "tasks_removed": 1}
    with session_scope() as db:
        assert db.get(AssignmentRecord, guest_task["task_id"]).deletion_requested_at is not None
        assert db.get(AssignmentRecord, teacher_task["task_id"]).deletion_requested_at is None
        assert db.get(AssignmentRecord, other_guest_task["task_id"]).deletion_requested_at is None
        assert db.get(UserRecord, first["user"]["id"]).is_active is False
    assert enabled.cookies.get(settings.refresh_cookie_name) == cookie
    assert enabled.get(f"/tasks/{teacher_task['task_id']}", headers=headers(signed_in["token"])).status_code == 200
    assert enabled.get(f"/tasks/{other_guest_task['task_id']}", headers=headers(second["token"])).status_code == 200
    assert enabled.get("/auth/me", headers=headers(first["token"])).status_code == 401
    assert enabled.post("/auth/frontier-demo-session", headers=headers(first["token"])).status_code == 401


@pytest.mark.parametrize("expiry", ["access", "absolute", "disabled"])
def test_expired_demo_can_only_end_its_original_tasks(enabled, monkeypatch, expiry):
    from backend.db.models import AssignmentRecord
    demo = issue(enabled)
    created = task(enabled, demo)
    provider = registries._build_scoped_registry(get_user_store()[demo["user"]["id"]]).pick_default()
    payload = decode_token(demo["token"])
    payload.update(iat=time.time() - 8000, exp=time.time() - 100)
    if expiry == "absolute":
        payload["session_exp"] = time.time() - 1
    if expiry == "disabled":
        monkeypatch.setattr(settings, "frontier_demo_enabled", False)
    expired = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    assert enabled.get(f"/tasks/{created['task_id']}", headers=headers(expired)).status_code == 401

    def no_credentials(*args, **kwargs):
        pytest.fail("Demo cleanup must not create or renew credentials")

    monkeypatch.setattr(frontier_demo, "create_token", no_credentials)
    ended = enabled.post("/auth/frontier-demo-session/end", headers=headers(expired))
    assert ended.status_code == 200, ended.text
    assert ended.json() == {"status": "ended", "cleanup": "queued", "tasks_removed": 1}
    assert "set-cookie" not in ended.headers
    with session_scope() as db:
        assert db.get(AssignmentRecord, created["task_id"]).deletion_requested_at is not None
        assert db.get(UserRecord, demo["user"]["id"]).is_active is False
        usage = db.scalar(select(FrontierDemoDailyUsageRecord))
        assert (usage.sessions, usage.requests, usage.estimated_input_tokens) == (1, 0, 0)
    # Even after re-enabling execution, the ended owner's in-flight registry
    # cannot charge another model call or reach the model boundary.
    monkeypatch.setattr(settings, "frontier_demo_enabled", True)
    with pytest.raises(registries.SharedPoolLimitError, match="shared_pool_disabled"):
        asyncio.run(provider.ainvoke([HumanMessage(content="synthetic")]))
    assert provider._provider.calls == 0
    assert enabled.post("/auth/frontier-demo-session", headers=headers(expired)).status_code == 401


@pytest.mark.parametrize("invalid", ["signature", "scope", "session_scope", "role", "auth_version", "auth_invalid_before", "inactive"])
def test_expired_demo_cleanup_keeps_identity_revocation_checks(enabled, invalid):
    from backend.db.models import AssignmentRecord
    demo = issue(enabled)
    created = task(enabled, demo)
    owner = get_user_store()[demo["user"]["id"]]
    payload = decode_token(demo["token"])
    payload.update(iat=time.time() - 8000, exp=time.time() - 100, session_exp=time.time() - 1)
    if invalid in {"scope", "session_scope", "role"}:
        payload[invalid] = "invalid"
    elif invalid == "auth_version":
        register_user(owner.model_copy(update={"auth_version": owner.auth_version + 1}))
    elif invalid == "auth_invalid_before":
        register_user(owner.model_copy(update={"auth_invalid_before": time.time()}))
    elif invalid == "inactive":
        register_user(owner.model_copy(update={"is_active": False}))
    token = jwt.encode(payload, "invalid-signing-key-used-only-for-demo-test" if invalid == "signature" else settings.jwt_secret,
                       algorithm=settings.jwt_algorithm)
    assert enabled.post("/auth/frontier-demo-session/end", headers=headers(token)).status_code == 401
    with session_scope() as db:
        assert db.get(AssignmentRecord, created["task_id"]).deletion_requested_at is None


def test_session_admission_is_durable_and_concurrent(enabled, monkeypatch):
    monkeypatch.setattr(settings, "frontier_demo_daily_session_limit", 2)
    with ThreadPoolExecutor(max_workers=5) as pool:
        statuses = list(pool.map(lambda _: enabled.post("/auth/frontier-demo-session").status_code, range(5)))
    assert sorted(statuses) == [200, 200, 429, 429, 429]
    assert TestClient(app).post("/auth/frontier-demo-session").status_code == 429
    with session_scope() as db:
        row = db.scalar(select(FrontierDemoDailyUsageRecord))
        assert row.sessions == 2
        assert len(db.scalars(select(UserRecord)).all()) == 2


def test_global_model_budget_is_shared_by_all_demo_owners_and_guards_vision(enabled, monkeypatch):
    monkeypatch.setattr(settings, "frontier_demo_daily_request_limit", 1)
    first, second = issue(enabled), issue(enabled)
    # Reloading a user for the current durable worker still resolves demo creds.
    from backend.services.task_facade import _registry_for_owner
    first_provider = _registry_for_owner(first["user"]["id"]).pick_default()
    second_provider = _registry_for_owner(second["user"]["id"]).pick_default()
    asyncio.run(first_provider.ainvoke([HumanMessage(content="synthetic")]))
    with pytest.raises(registries.SharedPoolLimitError, match="shared_pool_daily_limit_reached"):
        asyncio.run(second_provider.ainvoke_vision("synthetic", []))
    assert first_provider._provider.calls == 1
    assert second_provider._provider.calls == 0
    assert settings.shared_pool_enabled is False
    with session_scope() as db:
        row = db.scalar(select(FrontierDemoDailyUsageRecord))
        assert row.requests == 1
        assert row.estimated_input_tokens > 0


def test_model_budget_kill_switch_and_input_estimate_are_checked_before_dispatch(enabled, monkeypatch):
    demo = issue(enabled)
    provider = registries._build_scoped_registry(get_user_store()[demo["user"]["id"]]).pick_default()
    monkeypatch.setattr(settings, "frontier_demo_daily_estimated_token_limit", 1)
    with pytest.raises(registries.SharedPoolLimitError):
        asyncio.run(provider.ainvoke_vision("synthetic", [object()]))
    assert provider._provider.calls == 0
    monkeypatch.setattr(settings, "frontier_demo_enabled", False)
    with pytest.raises(registries.SharedPoolLimitError, match="shared_pool_disabled"):
        asyncio.run(provider.ainvoke([HumanMessage(content="a")]))
    assert provider._provider.calls == 0


def test_demo_gemini_and_persisted_byok_use_the_same_current_adapter(enabled, monkeypatch):
    from backend.db.provider_repository import upsert_provider_config
    from backend.llm.providers import GeminiProvider, build_provider
    from backend.models import ProviderConfig

    monkeypatch.setattr(registries, "build_provider", build_provider)
    monkeypatch.setattr(settings, "gemini_model", "gemini-test-model")
    demo = issue(enabled)
    teacher = User(id="gemini-teacher", username="geminiteacher", password_hash=hash_password("teacher-pass"))
    register_user(teacher)
    upsert_provider_config(teacher.id, ProviderConfig(
        provider_type="gemini", api_key="fake-teacher-owned-key", model="gemini-test-model",
    ), master_key=settings.provider_encryption_key)
    demo_provider = registries._build_scoped_registry(get_user_store()[demo["user"]["id"]]).pick_default()
    teacher_provider = registries._build_scoped_registry(teacher).pick_default()
    assert type(demo_provider._provider) is type(teacher_provider) is GeminiProvider
    assert demo_provider.config.api_key != teacher_provider.config.api_key
    called = []

    async def observed_current_gemini(self, messages, **kwargs):
        called.append(self.config.scheduling_owner)
        return LLMResponse(content="same adapter", provider=self.provider_id, model=self.model, duration_ms=0)

    monkeypatch.setattr(GeminiProvider, "ainvoke", observed_current_gemini)
    message = [HumanMessage(content="synthetic question")]
    assert asyncio.run(demo_provider.ainvoke(message)).content == "same adapter"
    assert asyncio.run(teacher_provider.ainvoke(message)).content == "same adapter"
    assert called == [demo["user"]["id"], teacher.id]
    assert registries._build_scoped_registry(teacher).uses_shared_pool() is False
    with session_scope() as db:
        assert db.scalar(select(FrontierDemoDailyUsageRecord)).requests == 1


def test_demo_task_knowledge_reads_preserve_document_and_citation_ownership(enabled):
    from backend.tests.test_personal_knowledge import _persist_ready_document
    first, second = issue(enabled), issue(enabled)
    doc = _persist_ready_document(owner_id=first["user"]["id"], filename="synthetic.txt",
                                  content=b"synthetic calculus derivative", chunks=["synthetic calculus derivative"])
    query = {"query": "calculus", "document_ids": [doc.id]}
    response = enabled.post("/knowledge/search", headers=headers(first["token"]), json=query)
    assert response.status_code == 200, response.text
    assert response.json()["provider_calls"] == 0
    citation = response.json()["matches"][0]["citation"]
    url = f"/knowledge/documents/{doc.id}/citations/{citation['chunk_id']}"
    assert enabled.get(url, headers=headers(first["token"])).status_code == 200
    assert enabled.get(url + "/download", headers=headers(first["token"])).content == b"synthetic calculus derivative"
    assert enabled.get(url, headers=headers(second["token"])).status_code == 404
    assert enabled.get(url + "/download", headers=headers(second["token"])).status_code == 404
    assert enabled.post("/knowledge/search", headers=headers(second["token"]), json=query).status_code == 404
    assert enabled.get("/knowledge/documents", headers=headers(first["token"])).status_code == 403


@pytest.mark.parametrize("change_provider_before_worker", [False, True])
def test_demo_task_runs_current_grading_worker_and_rejects_changed_credentials(enabled, monkeypatch, change_provider_before_worker):
    from backend.db import assignment_repository, grading_repository, workflow_repository
    from backend.domain.errors import ValidationError
    from backend.services import grading_runs, task_facade

    monkeypatch.setattr(settings, "e2e_fake_provider", False)
    dispatched = []

    async def model_response(self, messages, **kwargs):
        dispatched.append(self.config.scheduling_owner)
        # The production mathematical path prepares one shared question plan
        # before requesting the student's evidence assessment.
        if "Prepare ONE shared scoring plan" in messages[0].content:
            payload = {
                "effective_criterion": "Award up to 5 points for a correct explanation.",
                "routes": [{"id": "whole", "description": "Concept coverage",
                            "applies_when": "All answers", "mode": "holistic"}],
                "follow_through": "No calculation follow-through is relevant.",
                "numerical_policy": "No numeric comparison is relevant.",
                "advisories": [],
            }
        else:
            payload = {
                "route_id": "whole", "route_reason": "Apply the shared conceptual rubric.",
                "score": 4, "confidence": 0.95,
                "claims": [{"id": "s1", "quote": "Synthetic explanation",
                            "verdict": "supported", "reason": "Covers the principal concept."}],
                "rationale": "4/5 for the principal concept; one supporting detail is missing.",
                "comment": "Synthetic answer correctly explains the concept.",
            }
        return LLMResponse(content=json.dumps(payload), provider=self.provider_id,
                           model=self.model, duration_ms=0)

    monkeypatch.setattr(FakeProvider, "ainvoke", model_response)
    demo = issue(enabled)
    owner_id = demo["user"]["id"]
    created = task(enabled, demo)
    task_id = created["task_id"]
    assignment = assignment_repository.get_assignment(task_id, actor_id=owner_id)
    assignment_repository.add_question(
        assignment_id=task_id, teacher_id=owner_id, q_id="q1", order_index=0,
        type="short", stem="Explain a synthetic concept.", max_score=5,
        criterion="Award up to 5 points for a correct explanation of the concept.",
    )
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    # Seed only the recognition output boundary. Everything from setup through
    # durable run creation, worker recovery, skill/model dispatch and result
    # persistence is the current production task path.
    task_facade._commit_imported_submissions(
        task_id=task_id, owner_id=owner_id, course_id=assignment.course_id,
        expected_workflow_revision=workflow.workflow_revision,
        students=[{"stu_id": "DEMO-001", "stu_name": "Synthetic Student",
                   "identity_status": "matched", "identity_match_method": "filename",
                   "stu_ans": [{"q_id": "q1", "number": "1", "type": "short", "content": "Synthetic explanation", "flag": []}]}],
    )
    setup = enabled.get(f"/tasks/{task_id}/grading-setup", headers=headers(demo["token"])).json()
    saved = enabled.put(f"/tasks/{task_id}/grading-setup", headers=headers(demo["token"]), json={
        "expected_workflow_revision": setup["workflow_revision"],
        "grading_setup": {**setup["suggested_setup"], "knowledge_scope": "none"},
    })
    assert saved.status_code == 200, saved.text
    started = enabled.post(f"/tasks/{task_id}/grade", headers=headers(demo["token"]), json={
        "expected_workflow_revision": saved.json()["workflow_revision"],
    })
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "started"
    run_id = started.json()["job_id"]
    frozen = workflow_repository.get_run_setup(run_id)
    assert frozen.input_manifest["provider_configuration_fingerprint"]
    assert "fake-demo-only-secret" not in str(frozen.input_manifest)
    if change_provider_before_worker:
        monkeypatch.setattr(settings, "gemini_api_key", "a-different-fake-secret")
        with pytest.raises(ValidationError, match="Provider configuration changed"):
            asyncio.run(grading_runs.process_run(run_id=run_id, worker_id="demo-current-main-worker"))
        assert dispatched == []
        assert grading_repository.get_run(run_id=run_id).status == "failed"
    else:
        asyncio.run(grading_runs.process_run(run_id=run_id, worker_id="demo-current-main-worker"))
        assert dispatched == [owner_id, owner_id]
        result = enabled.get(f"/tasks/{task_id}/result", headers=headers(demo["token"]))
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "completed"
        correction = result.json()["results"][0]["corrections"][0]
        assert correction["score"] == 4
        assert correction["max_score"] == 5
        with session_scope() as db:
            assert db.scalar(select(FrontierDemoDailyUsageRecord)).requests == 2
