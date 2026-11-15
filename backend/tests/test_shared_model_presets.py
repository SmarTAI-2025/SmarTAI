"""Preset coexistence and atomic sublimits; fake models, disposable databases."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from langchain_core.messages import HumanMessage
from sqlalchemy import select

from backend.api import experts
from backend.config import settings
from backend.db.models import ProviderConfigRecord
from backend.db.provider_repository import upsert_provider_config
from backend.db.session import session_scope, configure_database
from backend.llm import registry as registries
from backend.llm.providers import LLMResponse, VisionImage
from backend.models import ProviderConfig, User
from backend.services.model_quota import admit_model_call, ModelQuotaError, model_usage
from backend.services.grading_input_security import provider_configuration_fingerprint
from backend.tests.test_admin_business_config import client, headers, save
from backend.tests.test_shared_vision_quota import FakeProvider

LUNA = "openai:gpt-6-luna"
TERRA = "openai:gpt-6.1-terra"
SOL = "openai:gpt-6.1-sol"
OWNER = User(id="teacher", username="teacher")


@pytest.fixture
def presets(client, monkeypatch):
    monkeypatch.setattr(settings, "shared_pool_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", "synthetic-platform-key")
    monkeypatch.setattr(settings, "openai_api_base", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "gemini_api_key", "synthetic-demo-key")
    monkeypatch.setattr(settings, "custom_provider_verification_cooldown_seconds", 0)
    monkeypatch.setattr(registries, "build_provider", FakeProvider)
    client.app.include_router(experts.router)
    return registries._build_scoped_registry(OWNER)


def usage(owner="teacher", **kwargs):
    with session_scope() as session:
        return model_usage(session, owner, **kwargs)


def own(model="gpt-6.1-sol", **kwargs):
    return upsert_provider_config("teacher", ProviderConfig(provider_type="openai",
        model=model, api_key="synthetic-personal-key", **kwargs),
        master_key=settings.provider_encryption_key)


def test_three_presets_coexist_with_byok_default_and_frozen_selection(presets, client, monkeypatch):
    personal = own()
    registry = registries._build_scoped_registry(OWNER)
    configs = {c["provider_id"]: c for c in registry.list_configs()}
    assert set(configs) == {LUNA, TERRA, SOL, personal.id}
    for pid in (LUNA, TERRA, SOL):
        assert configs[pid]["is_shared"] and not configs[pid]["editable"]
        assert "api_key" not in configs[pid]
    assert not configs[personal.id]["is_shared"] and configs[personal.id]["editable"]
    assert registry.pick_default_id() == personal.id
    assert registries.resolve_owner_default_provider_id("teacher", registry) == personal.id
    assert presets.pick_default_id() == LUNA
    # A selected task, including one restored after another BYOK is added,
    # must retain its source and registry ID rather than fall back to defaults.
    fingerprint = provider_configuration_fingerprint(owner_id="teacher", selected_provider_ids=[SOL])
    own(model="another-model")
    restored = registries._build_scoped_registry(OWNER).select([SOL], primary_provider_id=SOL)
    assert restored.pick_default_id() == SOL and restored.uses_shared_pool()
    assert fingerprint == provider_configuration_fingerprint(owner_id="teacher", selected_provider_ids=[SOL])
    assert not registry.select([personal.id], primary_provider_id=personal.id).uses_shared_pool()
    # Shared API credentials must never escape into the unguarded embedder.
    assert [c.api_key for c in registry.list_enabled_configs()] == ["synthetic-personal-key"]
    monkeypatch.setattr(settings, "openai_api_key", "rotated-platform-key")
    assert fingerprint != provider_configuration_fingerprint(owner_id="teacher", selected_provider_ids=[SOL])


@pytest.mark.asyncio
async def test_text_image_and_same_named_byok_charge_only_their_owner_pool(presets, client):
    assert save(client, {"shared_pool_daily_request_limit": 4, "shared_pool_daily_estimated_token_limit": 10000}).status_code == 200
    personal = own()
    registry = registries._build_scoped_registry(OWNER)
    await registry.get(SOL).ainvoke([HumanMessage(content="test")])
    await registry.get(SOL).ainvoke_vision("read", [VisionImage(b"pixels", "image/png")])
    assert usage()["shared_sol"]["requests"] == usage()["shared"]["requests"] == 2
    assert usage()["shared_sol"]["estimated_input_tokens"] == 1026
    with pytest.raises(registries.SharedPoolLimitError, match="sol_daily_limit"):
        await registry.get(SOL).ainvoke([])
    await registry.get(TERRA).ainvoke([])
    await registry.get(LUNA).ainvoke([])
    for pid in (LUNA, TERRA, SOL):
        with pytest.raises(registries.SharedPoolLimitError, match="shared_pool_daily_limit"):
            await registry.get(pid).ainvoke([])
    await registry.get(personal.id).ainvoke([])
    await registry.get(personal.id).ainvoke_vision("read", [VisionImage(b"pixels", "image/png")])
    assert usage()["shared"]["requests"] == 4
    other = registries._build_scoped_registry(User(id="other", username="other"))
    await other.get(SOL).ainvoke([])
    assert usage("other")["shared_sol"]["requests"] == 1


@pytest.mark.asyncio
async def test_shared_text_and_image_probes_are_bounded_quota_guarded_and_independent(presets, client, monkeypatch):
    assert save(client, {"shared_pool_daily_request_limit": 4}).status_code == 200
    monkeypatch.setattr(experts, "make_image_challenge", lambda: (b"pixels", "1234"))
    calls = []
    async def response(self, messages, **kwargs):
        calls.append((self.model, kwargs))
        return LLMResponse(content="1234", model=self.model, provider=self.provider_id, duration_ms=1)
    monkeypatch.setattr(FakeProvider, "ainvoke", response)
    assert (await experts.verify_provider(SOL, OWNER, presets))["status"] == "success"
    assert (await experts.verify_provider_image(SOL, OWNER, presets))["image_capability_status"] == "passed"
    assert calls == [("gpt-6.1-sol", {"max_output_tokens": 256})] * 2
    with pytest.raises(HTTPException) as error:
        await experts.verify_provider(SOL, OWNER, presets)
    assert error.value.status_code == 429 and error.value.detail["code"] == "shared_pool_sol_daily_limit_reached"
    assert len(calls) == 2
    assert (await experts.verify_provider_image(LUNA, OWNER, presets))["image_capability_status"] == "passed"
    fresh = {c["provider_id"]: c for c in registries._build_scoped_registry(OWNER).list_configs()}
    assert fresh[LUNA]["image_capability_status"] == fresh[SOL]["image_capability_status"] == "passed"
    assert fresh[TERRA]["image_capability_status"] == "unverified"
    assert usage()["shared"]["requests"] == 3 and usage()["shared_sol"]["requests"] == 2
    with session_scope() as session:
        assert list(session.scalars(select(ProviderConfigRecord))) == []


def test_shared_rows_cannot_be_edited_disabled_deleted_and_personal_add_stays_available(presets, client):
    h = headers("teacher")
    assert client.put(f"/experts/{SOL}", json={"model": "tampered"}, headers=h).status_code == 404
    assert client.delete(f"/experts/{SOL}", headers=h).json()["status"] == "not_found"
    assert client.post("/experts/select", json={"provider_id": SOL, "enabled": False}, headers=h).json()["status"] == "not_found"
    added = client.post("/experts/keys", json={"provider_type": "openai", "model": "gpt-6.1-sol", "api_key": "personal-test"}, headers=h)
    assert added.status_code == 200, added.text
    pid = added.json()["provider_id"]
    assert client.put("/experts/default", json={"provider_id": pid}, headers=h).status_code == 200
    result = client.get("/experts/available", headers=h)
    assert result.status_code == 200 and len(result.json()) == 4
    assert "synthetic-platform-key" not in result.text and "personal-test" not in result.text
    assert sum(c["is_default"] for c in result.json()) == 1
    assert next(c for c in result.json() if c["provider_id"] == pid)["is_default"]
    assert next(c for c in result.json() if c["provider_id"] == SOL)["enabled"]


@pytest.mark.parametrize("request_limit,token_limit,estimate,accepted", [(7, 1000, 1, 3), (100, 15, 3, 2), (1, 100, 1, 0), (0, 0, 0, 0)])
def test_sol_atomic_subcap_floor_and_denied_calls_do_not_charge_total(client, request_limit, token_limit, estimate, accepted):
    assert save(client, {"shared_pool_daily_request_limit": request_limit, "shared_pool_daily_estimated_token_limit": token_limit}).status_code == 200
    def call(_):
        try:
            admit_model_call("teacher", "shared", shared_sol=True, estimated_input_tokens=estimate)
            return True
        except ModelQuotaError:
            return False
    with ThreadPoolExecutor(max_workers=10) as pool:
        assert sum(pool.map(call, range(30))) == accepted
    configure_database().dispose()
    q = usage()
    assert q["shared"]["requests"] == q["shared_sol"]["requests"] == accepted
    assert q["shared"]["estimated_input_tokens"] == accepted * estimate


def test_mixed_concurrency_subcap_cannot_increase_total(client):
    assert save(client, {"shared_pool_daily_request_limit": 8}).status_code == 200
    def call(sol):
        try:
            admit_model_call("teacher", "shared", shared_sol=sol, estimated_input_tokens=2)
            return True
        except ModelQuotaError:
            return False
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(call, [True, False] * 20)) == 8
    q = usage()
    assert q["shared"]["requests"] == 8 and q["shared"]["estimated_input_tokens"] == 16
    assert q["shared_sol"]["requests"] <= 4


def test_user_override_unlimited_and_utc_reset(client):
    before = datetime(2026, 11, 16, 23, 59, 59, tzinfo=timezone.utc)
    assert save(client, {"shared_pool_daily_request_limit": 2}).status_code == 200
    admit_model_call("teacher", "shared", shared_sol=True, now=before)
    with pytest.raises(ModelQuotaError, match="sol_daily_limit"):
        admit_model_call("teacher", "shared", shared_sol=True, now=before)
    admit_model_call("teacher", "shared", shared_sol=True, now=before + timedelta(seconds=1))
    assert save(client, {"shared_pool_daily_request_limit": -1, "shared_pool_daily_estimated_token_limit": -1}, user="teacher").status_code == 200
    for _ in range(5):
        admit_model_call("teacher", "shared", shared_sol=True, estimated_input_tokens=1000000, now=before)
    q = usage(now=before)
    assert q["shared_sol"]["request_limit"] == q["shared_sol"]["estimated_token_limit"] == -1
    assert q["shared_sol"]["requests"] == 6
    assert usage("other", now=before)["shared_sol"]["request_limit"] == 1


def test_shared_grading_stays_single_expert_and_sample_while_byok_keeps_multiple(presets):
    from backend.api.tasks import _validate_grading_setup
    from backend.models import TaskGradingSetup
    first, second = own(), own(model="second")
    registry = registries._build_scoped_registry(OWNER)
    def setup(ids, samples=1):
        return TaskGradingSetup(selected_provider_ids=ids, primary_provider_id=ids[0],
            aggregation_method="single" if len(ids) == 1 else "weighted_average", multi_sample_n=samples)
    _validate_grading_setup(setup([LUNA]), registry, "teacher")
    _validate_grading_setup(setup([first.id, second.id], 2), registry, "teacher")
    for invalid in (setup([SOL], 2), setup([LUNA, TERRA]), setup([first.id, SOL])):
        with pytest.raises(HTTPException) as error:
            _validate_grading_setup(invalid, registry, "teacher")
        assert error.value.detail["code"] == "shared_pool_single_expert_required"


def test_migration_preserves_existing_aggregate_and_starts_subcounts_at_zero(tmp_path, monkeypatch):
    from alembic import command
    from sqlalchemy import create_engine, text
    from backend.tests.test_migration_roundtrip import _alembic_config
    url = f"sqlite:///{tmp_path / 'shared-upgrade.db'}"
    cfg = _alembic_config(url, monkeypatch)
    command.upgrade(cfg, "0033_shared_admission")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO model_daily_usage(owner_id,scope,day,requests,estimated_input_tokens,last_admitted_at) VALUES('existing','shared','2026-11-16',7,999,1)"))
    command.upgrade(cfg, "head")
    with engine.connect() as connection:
        assert tuple(connection.execute(text("SELECT requests,estimated_input_tokens,sol_requests,sol_estimated_input_tokens FROM model_daily_usage")).one()) == (7, 999, 0, 0)
    engine.dispose()
