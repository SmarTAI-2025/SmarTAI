"""Money, week boundaries and exclusive routing using fake official responses."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy import select

from backend.config import settings
from backend.db.model_quota_models import SharedModelChargeRecord, ModelDailyUsageRecord
from backend.db.session import session_scope
from backend.domain.errors import RecognitionError
from backend.llm.providers import OpenAIProvider, ProviderRequestError, VisionImage
from backend.llm.registry import _GuardedSharedProvider, SharedPoolLimitError
from backend.llm.request_timeouts import response_timeout_scope
from backend.models import ProviderConfig
from backend.services.model_quota import ModelQuotaError, model_usage
from backend.services.shared_weekly_budget import actual_charge, reserve, finish, week_window
from backend.services.workflow_deadlines import processing_budget
from backend.tests.test_admin_business_config import client, save
from backend.tests.test_provider_response_deadlines import scheduler  # noqa: F401


def usage(inputs=1000, outputs=100, cached=200, written=300):
    return {"input_tokens": inputs, "output_tokens": outputs, "total_tokens": inputs+outputs,
            "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": written},
            "output_tokens_details": {"reasoning_tokens": outputs//2}}


def rows():
    with session_scope() as session:
        return list(session.scalars(select(SharedModelChargeRecord)))


@pytest.mark.parametrize("model,cost", [("gpt-6-luna",139500), ("gpt-6.1-terra",1385000), ("gpt-6.1-sol",2770000)])
def test_real_usage_prices_cache_and_reasoning_once(model, cost):
    actual, facts = actual_charge(model, usage())
    assert actual == cost
    assert facts["reasoning_tokens"] == 50
    assert actual_charge(model, usage(300000,100,0,0))[0] > actual_charge(model, usage(272000,100,0,0))[0]


def test_week_boundary_is_monday_midnight_singapore():
    assert week_window(datetime.fromisoformat("2026-11-15T15:59:59+00:00")) == ("2026-11-09", "2026-11-16T00:00:00+08:00")
    assert week_window(datetime.fromisoformat("2026-11-15T16:00:00+00:00")) == ("2026-11-16", "2026-11-23T00:00:00+08:00")


def test_atomic_reservation_cannot_spend_same_balance_twice(client):
    def attempt(_):
        try: return reserve("teacher", "gpt-6.1-sol", 0, max_output_tokens=100000)
        except ModelQuotaError: return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = [x for x in pool.map(attempt, range(12)) if x]
    assert len(admitted) == 5
    assert sum(r.reserved_nano_usd for r in rows()) == 5_000_000_000
    reserve("other", "gpt-6.1-sol", 0, max_output_tokens=100000)


def test_sol_has_no_half_cap_and_legacy_overrides_do_not_apply(client):
    assert save(client, {"shared_pool_daily_request_limit":0, "shared_pool_daily_estimated_token_limit":0}).status_code == 200
    for _ in range(3):
        charge, _ = reserve("teacher", "gpt-6.1-sol", 100000)
        finish("teacher", charge, usage=usage(100000,100000,0,0))
    assert sum(r.cost_nano_usd for r in rows()) == 3_600_000_000
    with session_scope() as session:
        assert session.scalar(select(ModelDailyUsageRecord)) is None
        assert model_usage(session,"teacher")["shared_weekly"]["spent_usd"] == 3.6


def test_settlement_is_idempotent_unknown_is_not_zero_and_week_is_frozen(client):
    before = datetime.fromisoformat("2026-11-15T23:59:59+08:00")
    after = datetime.fromisoformat("2026-11-16T00:00:01+08:00")
    charge, _ = reserve("teacher", "gpt-6-luna", 1000, now=before)
    finish("teacher",charge,usage=usage(),response_id="resp_synthetic",now=after)
    finish("teacher",charge,rejected=True)
    record=rows()[0]
    assert record.state == "settled" and record.cost_nano_usd == 139500 and record.week == "2026-11-09"
    pending, _ = reserve("teacher", "gpt-6.1-sol", 1000)
    finish("teacher",pending)
    assert next(r for r in rows() if r.id == pending).state == "unknown"
    with session_scope() as session:
        current=model_usage(session,"teacher")["shared_weekly"]
    assert current["unknown_requests"] == 1 and current["reserved_usd"] > 0


@pytest.mark.parametrize("value", [None, {}, {"input_tokens":1000}, usage(cached=800,written=800)])
def test_missing_or_inconsistent_usage_is_never_estimated(client,value):
    charge, _=reserve("teacher","gpt-6-luna",1000)
    if value is None: finish("teacher",charge,usage=value)
    else:
        with pytest.raises(ModelQuotaError):finish("teacher",charge,usage=value)
    assert rows()[0].state == "unknown" and rows()[0].cost_nano_usd is None


@pytest.fixture
def metered(client,monkeypatch):
    monkeypatch.setattr(settings,"shared_pool_enabled",True)
    monkeypatch.setattr(settings,"shared_pool_quota_mode","weekly_usd")
    monkeypatch.setattr(settings,"shared_pool_daily_request_limit",0)
    monkeypatch.setattr(settings,"shared_pool_daily_estimated_token_limit",0)
    provider=OpenAIProvider(ProviderConfig(provider_type="openai",model="gpt-6-luna",api_key="synthetic-unused"))
    calls=[]
    state={"usage":usage(),"failure":None}
    async def count(**kwargs):
        calls.append(("count",kwargs))
        if state.get("count_failure"):raise state["count_failure"]
        if count_gate := state.get("count_gate"):
            await count_gate.wait()
        return SimpleNamespace(input_tokens=1000)
    async def create(**kwargs):
        calls.append(("create",kwargs))
        if state["failure"]:raise state["failure"]
        return SimpleNamespace(model=state.get("model","gpt-6-luna"),service_tier=state.get("service_tier","default"),status=state.get("status","completed"),id="resp_synthetic",
            usage=SimpleNamespace(model_dump=lambda:state["usage"]) if state["usage"] is not None else None,
            output_text=state.get("text","valid response"),output=state.get("output",[]),
            incomplete_details=SimpleNamespace(reason=state.get("reason")))
    sdk=SimpleNamespace(responses=SimpleNamespace(input_tokens=SimpleNamespace(count=count),create=create))
    sdk.with_options=lambda **kwargs:sdk
    async def get():return SimpleNamespace(root_async_client=sdk)
    @asynccontextmanager
    async def capacity(*args):yield
    monkeypatch.setattr(provider,"_get_client",get)
    monkeypatch.setattr(provider,"_call_capacity",capacity)
    monkeypatch.setattr(provider,"_record_duration",lambda _:None)
    monkeypatch.setattr(_GuardedSharedProvider,"_admit",lambda *a,**kw:pytest.fail("weekly path reached daily admission"))
    return _GuardedSharedProvider(provider,"teacher"),calls,state


@pytest.mark.asyncio
@pytest.mark.parametrize("recognition", [False, True])
async def test_weekly_metering_wait_does_not_consume_generation_deadline(
    metered, scheduler, monkeypatch, recognition,
):
    from backend.llm import shared_weekly

    provider, calls, state = metered
    underlying = provider._provider
    monkeypatch.setattr(underlying, "_call_capacity", OpenAIProvider._call_capacity.__get__(underlying))
    state["count_gate"] = asyncio.Event()
    generation_markers = []
    pending = asyncio.Event()
    held = [reserve("teacher", "gpt-6.1-sol", 0, max_output_tokens=100000)[0] for _ in range(5)]
    original_reserve = shared_weekly.reserve

    def observe_reserve(*args, **kwargs):
        try:
            return original_reserve(*args, **kwargs)
        except ModelQuotaError:
            pending.set()
            raise

    monkeypatch.setattr(shared_weekly, "reserve", observe_reserve)
    loop = asyncio.get_running_loop()
    real_time, offset = loop.time, 0
    monkeypatch.setattr(loop, "time", lambda: real_time() + offset)
    monkeypatch.setattr(shared_weekly, "time", SimpleNamespace(
        monotonic=loop.time, perf_counter=shared_weekly.time.perf_counter,
    ))

    async def invoke():
        if recognition:
            with response_timeout_scope(180, response_only=True,
                                        on_send=lambda: generation_markers.append("sent")):
                return await provider.ainvoke_vision("read", [VisionImage(b"synthetic", "image/png")])
        return await provider.ainvoke([HumanMessage(content="read")])

    task = asyncio.create_task(invoke())
    try:
        async with asyncio.timeout(2):
            while not calls and not task.done():
                await asyncio.sleep(0)
        assert [name for name, _ in calls] == ["count"]
        offset = 61
        for _ in range(6):
            await asyncio.sleep(0)
        assert not task.done() and not generation_markers
        state["count_gate"].set()
        await asyncio.wait_for(pending.wait(), 2)
        offset += 181
        for _ in range(6):
            await asyncio.sleep(0)
        assert not task.done() and not generation_markers
        assert [name for name, _ in calls] == ["count"]
        finish("teacher", held[0], usage=usage(0, 100, 0, 0))
        response = await asyncio.wait_for(task, 2)
        assert response.content == "valid response"
        assert generation_markers == (["sent"] if recognition else [])
        assert [name for name, _ in calls] == ["count", "create"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_weekly_token_count_has_its_own_response_timeout(metered):
    provider, calls, state = metered
    state["count_gate"] = asyncio.Event()
    with response_timeout_scope(0.02, response_only=True) as generation:
        with pytest.raises(ProviderRequestError, match="^provider_timeout$"):
            await provider.ainvoke_vision("read", [VisionImage(b"synthetic", "image/png")])
    assert generation.attempt_count == 0 and not generation.submission_may_exist
    assert [name for name, _ in calls] == ["count"]
    assert rows() == []


@pytest.mark.asyncio
async def test_workflow_stop_interrupts_weekly_balance_wait(metered, monkeypatch):
    from backend.llm import shared_weekly

    provider, calls, _ = metered
    waiting = asyncio.Event()

    def pending(*args, **kwargs):
        waiting.set()
        raise ModelQuotaError("shared_pool_budget_pending")

    monkeypatch.setattr(shared_weekly, "reserve", pending)
    async with processing_budget("weekly-test", kind="question_preparation", raise_on_blocked=False) as budget:
        task = asyncio.create_task(provider.ainvoke([HumanMessage(content="read")]))
        try:
            await asyncio.wait_for(waiting.wait(), 2)
            budget.expire()
            with pytest.raises(RecognitionError, match="question_preparation_wall_time_limit_reached"):
                await asyncio.wait_for(task, 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert [name for name, _ in calls] == ["count"]
    assert rows() == []


@pytest.mark.asyncio
async def test_text_and_vision_share_official_count_payload_and_real_settlement(metered):
    provider,calls,_=metered
    await provider.ainvoke([HumanMessage(content="short")],max_output_tokens=256)
    await provider.ainvoke_vision("read",[VisionImage(b"synthetic","image/png")],max_output_tokens=256)
    assert len(rows()) == 2 and all(r.cost_nano_usd == 139500 for r in rows())
    for i in (0,2):
        count,generate=calls[i][1],calls[i+1][1]
        assert count["input"] == generate["input"] and count["model"] == generate["model"]
        assert generate["service_tier"] == "default" and generate["store"] is False
        assert generate["prompt_cache_options"] == {"ttl":"30m"}
    assert calls[2][1]["input"][0]["content"][1]["type"] == "input_image"


@pytest.mark.asyncio
async def test_missing_response_usage_stays_pending_without_daily_fallback(metered):
    provider,_,state=metered;state["usage"]=None
    with pytest.raises(SharedPoolLimitError,match="usage_unavailable"):
        await provider.ainvoke([HumanMessage(content="test")])
    assert rows()[0].state == "unknown"


@pytest.mark.asyncio
async def test_cancelled_generation_keeps_reservation(metered):
    provider,_,state=metered;state["failure"]=asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):await provider.ainvoke([HumanMessage(content="test")])
    assert rows()[0].state == "unknown"


def test_all_three_models_share_five_dollars_and_reset_without_touching_daily(client):
    from backend.db.session import configure_database
    before = datetime.fromisoformat("2026-11-15T23:59:00+08:00")
    after = datetime.fromisoformat("2026-11-16T00:00:00+08:00")
    for model, count, output in [("gpt-6.1-sol",3,90000), ("gpt-6.1-terra",3,120000), ("gpt-6-luna",10,100000)]:
        for _ in range(count):
            charge, cap = reserve("teacher",model,0,max_output_tokens=output,now=before)
            assert cap == output
            finish("teacher",charge,usage=usage(0,output,0,0),now=before)
    assert sum(r.cost_nano_usd for r in rows()) == 5_000_000_000
    configure_database().dispose()
    for model in ("gpt-6-luna","gpt-6.1-terra","gpt-6.1-sol"):
        with pytest.raises(ModelQuotaError,match="weekly_limit_reached"):
            reserve("teacher",model,0,now=before)
    reserve("teacher","gpt-6.1-sol",0,now=after)
    reserve("other","gpt-6.1-sol",0,now=before)
    with session_scope() as session:
        assert session.scalar(select(ModelDailyUsageRecord)) is None


def test_weekly_overrides_zero_inheritance_and_inflight_deactivation(client):
    from backend.db.models import UserRecord
    assert save(client,{"shared_pool_weekly_limit_cents":100}).status_code == 200
    assert save(client,{"shared_pool_weekly_limit_cents":0},user="teacher").status_code == 200
    with pytest.raises(ModelQuotaError,match="weekly_limit_reached"):
        reserve("teacher","gpt-6.1-sol",0)
    assert save(client,{"shared_pool_weekly_limit_cents":None},user="teacher").status_code == 200
    charge,cap=reserve("teacher","gpt-6.1-sol",0)
    assert cap == 100000
    with session_scope() as session:
        session.get(UserRecord,"teacher").is_active=False
    finish("teacher",charge,usage=usage(0,123,0,0))
    assert rows()[0].state == "settled"
    with pytest.raises(ModelQuotaError,match="owner_unavailable"):
        reserve("teacher","gpt-6.1-sol",0)


@pytest.mark.parametrize("status,expected", [(401,"released"),(429,"released"),(500,"unknown")])
@pytest.mark.asyncio
async def test_definite_rejection_releases_but_uncertain_failure_keeps_hold(metered,status,expected):
    from backend.llm.providers import ProviderRequestError
    provider,_,state=metered
    state["failure"]=ProviderRequestError("provider_request_failed",status_code=status)
    with pytest.raises(ProviderRequestError):await provider.ainvoke([HumanMessage(content="test")])
    assert rows()[0].state == expected and rows()[0].cost_nano_usd is None


@pytest.mark.asyncio
async def test_count_failure_never_generates_or_charges(metered):
    provider,calls,state=metered
    state["count_failure"]=RuntimeError("synthetic count failure")
    with pytest.raises(RuntimeError,match="count failure"):
        await provider.ainvoke([HumanMessage(content="test")])
    assert [name for name,_ in calls] == ["count"] and rows() == []


@pytest.mark.parametrize("change", [{"model":"different-model"},{"service_tier":"priority"}])
@pytest.mark.asyncio
async def test_unexpected_billing_identity_retains_response_id_without_estimate(metered,change):
    provider,_,state=metered;state.update(change)
    with pytest.raises(SharedPoolLimitError,match="usage_unavailable"):
        await provider.ainvoke([HumanMessage(content="test")])
    assert rows()[0].state == "unknown" and rows()[0].response_id == "resp_synthetic"


@pytest.mark.asyncio
async def test_invalid_mode_and_kill_switch_never_generate_or_fall_back(metered,monkeypatch):
    provider,calls,_=metered
    monkeypatch.setattr(settings,"shared_pool_quota_mode","invalid")
    with pytest.raises(SharedPoolLimitError,match="billing_unavailable"):
        await provider.ainvoke([HumanMessage(content="test")])
    monkeypatch.setattr(settings,"shared_pool_quota_mode","weekly_usd")
    monkeypatch.setattr(settings,"shared_pool_enabled",False)
    with pytest.raises(SharedPoolLimitError,match="disabled"):
        await provider.ainvoke([HumanMessage(content="test")])
    assert calls == [] and rows() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('unknown_hold', [False, True])
async def test_concurrent_calls_wait_for_settlement_instead_of_daily_fallback(metered, monkeypatch, unknown_hold):
    provider,calls,state=metered
    unknown = None
    if unknown_hold:
        unknown, _ = reserve('teacher', 'gpt-6-luna', 1000, max_output_tokens=16)
        finish('teacher', unknown)
    # Hold the entire balance, then release it after the second caller waits.
    held=[]
    for _ in range(5):held.append(reserve("teacher","gpt-6.1-sol",0,max_output_tokens=100000)[0])
    from backend.llm import shared_weekly
    original_reserve = shared_weekly.reserve
    waiting = asyncio.Event()
    def observed_reserve(*args, **kwargs):
        try:
            return original_reserve(*args, **kwargs)
        except ModelQuotaError:
            waiting.set()
            raise
    monkeypatch.setattr(shared_weekly, 'reserve', observed_reserve)
    task=asyncio.create_task(provider.ainvoke([HumanMessage(content="test")]))
    await asyncio.wait_for(waiting.wait(), 1)
    assert [name for name,_ in calls] == ["count"]
    finish("teacher",held[0],usage=usage(0,100,0,0))
    await asyncio.wait_for(task,1)
    assert [name for name,_ in calls] == ["count","create"]
    if unknown:
        record = next(r for r in rows() if r.id == unknown)
        assert record.state == 'unknown' and record.cost_nano_usd is None


def test_unknown_hold_only_blocks_when_healthy_calls_cannot_free_enough(client):
    for _ in range(5):
        charge, _ = reserve('teacher', 'gpt-6.1-sol', 0, max_output_tokens=100000)
        finish('teacher', charge)
    with pytest.raises(ModelQuotaError, match='usage_unavailable'):
        reserve('teacher', 'gpt-6-luna', 1000)


def test_cache_split_long_context_and_malformed_usage():
    value=usage(300000,100,10000,20000)
    value["input_tokens_details"]["cache_write_tokens_details"]={"ttl_30m_tokens":20000,"ttl_12h_tokens":0}
    assert actual_charge("gpt-6.1-sol",value)[0] == 1_183_500_000
    value["input_tokens_details"]["cache_write_tokens_details"]["ttl_12h_tokens"]=1
    with pytest.raises(ModelQuotaError):actual_charge("gpt-6.1-sol",value)


def test_weekly_errors_survive_background_and_recognition_boundaries():
    from backend.services.background_errors import classify_background_error,is_retryable_background_error
    from backend.domain.errors import RecognitionError
    from backend.api.experts import _verification_http_status
    for code in ("shared_pool_weekly_limit_reached","shared_pool_budget_pending","shared_pool_usage_unavailable","shared_pool_billing_unavailable","shared_pool_request_too_large"):
        error=SharedPoolLimitError(code)
        assert classify_background_error(error,"fallback") == code
        assert not is_retryable_background_error(code)
        assert RecognitionError(code).code == code
        assert _verification_http_status(code) in (422,429,503)


@pytest.mark.asyncio
async def test_locked_openai_sdk_wire_protocol_uses_count_and_responses_once(metered,monkeypatch):
    import json
    import httpx
    from openai import AsyncOpenAI
    provider,_,_=metered
    # Exercise the real scheduler/deadline envelope as well as the locked SDK.
    from backend.llm.concurrency import ModelScheduler
    from backend.tests.test_automatic_model_scheduling import FakeHost
    from backend.llm import providers
    scheduler=ModelScheduler(host_capacity=FakeHost(), memory_reader=lambda:None)
    monkeypatch.setattr(providers,"get_scheduler",lambda:scheduler)
    monkeypatch.setattr(provider._provider,"_call_capacity",provider._provider.__class__._call_capacity.__get__(provider._provider))
    wire=[]
    def handler(request):
        data=json.loads(request.content);wire.append((request.url.path,data))
        if request.url.path == "/v1/responses/input_tokens":
            return httpx.Response(200,json={"object":"response.input_tokens","input_tokens":1000})
        assert request.url.path == "/v1/responses"
        return httpx.Response(200,json={"id":"resp_wire","object":"response","created_at":1,
            "status":"completed","model":"gpt-6-luna","service_tier":"default","usage":usage(),
            "output":[{"id":"msg_wire","type":"message","role":"assistant","status":"completed",
                "content":[{"type":"output_text","text":"synthetic answer","annotations":[]}]}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sdk=AsyncOpenAI(api_key="synthetic-unused",http_client=http)
        async def get():return SimpleNamespace(root_async_client=sdk)
        monkeypatch.setattr(provider._provider,"_get_client",get)
        response=await provider.ainvoke([HumanMessage(content="test")],max_output_tokens=256)
    assert response.content == "synthetic answer" and response.input_tokens == 1000
    assert len(wire) == 2 and wire[0][1]["input"] == wire[1][1]["input"]
    assert rows()[0].response_id == "resp_wire" and rows()[0].cost_nano_usd == 139500
