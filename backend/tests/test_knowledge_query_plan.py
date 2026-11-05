import asyncio
import json

import pytest

from backend.knowledge.query_plan import QueryPlanner, fuse_results
from backend.llm.providers import LLMResponse
from backend.models import ProviderConfig
from backend.tools.knowledge import KnowledgeChunk


class Provider:
    config = ProviderConfig(provider_type="zhipu", api_key="unit-test-only", model="test")

    def __init__(self, queries=None, raw=None):
        self.queries = queries or ["An English search query"]
        self.raw, self.calls, self.messages = raw, 0, []

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        self.messages = messages
        assert kwargs == {"max_output_tokens":640}
        await asyncio.sleep(0)
        return LLMResponse(self.raw if self.raw is not None else json.dumps({"queries":self.queries}),
                           "fake", "test", 1, input_tokens=20, output_tokens=10)


@pytest.mark.asyncio
async def test_scoped_singleflight_reuses_plan_not_usage_or_corpus():
    planner, provider = QueryPlanner(), Provider()
    results = await asyncio.gather(*(planner.plan("一个查询", provider=provider, scope="owner:1") for _ in range(8)))
    assert provider.calls == sum(r.provider_calls for r in results) == 1
    assert sum((r.input_tokens or 0) for r in results) == 20
    assert json.loads(provider.messages[1].content) == {"query":"一个查询"}
    assert (await planner.plan("一个查询", provider=provider, scope="owner:1")).cached
    await planner.plan("一个查询", provider=provider, scope="owner:2")
    assert provider.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ['{"queries":["Exercise 9.9.9"]}', '{"queries":["x^{2}"]}',
                                   '{"queries":["https://untrusted.example"]}', '{"queries":[1]}',
                                   'not JSON', 'secret-do-not-return' * 1000])
async def test_untrusted_output_cannot_rewrite_constraints_or_leak_raw_errors(raw):
    provider, planner = Provider(raw=raw), QueryPlanner()
    plan = await planner.plan("x^{-1}", provider=provider, scope="owner")
    assert not plan.queries and plan.provider_calls == 1
    assert "secret" not in repr(plan)
    assert (await planner.plan("x^{-1}", provider=provider, scope="owner")).provider_calls == 0
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_exact_id_never_calls_model_and_unknown_scope_never_calls_model():
    provider, planner = Provider(), QueryPlanner()
    assert (await planner.plan("Exercise 9.9.9", provider=provider, scope="owner")).status == "exact_identifier"
    assert not (await planner.plan("query", provider=provider, scope=None)).queries
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_rewrite_preserves_negation_and_numeric_conditions():
    provider = Provider(queries=["injective linear map", "not injective map in dimension 4"])
    plan = await QueryPlanner().plan("not injective map in dimension 5", provider=provider, scope="owner")
    assert not plan.queries


@pytest.mark.asyncio
async def test_shared_pool_never_receives_extra_query_call():
    provider = Provider()
    provider.is_shared_pool = True
    assert (await QueryPlanner().plan("a query", provider=provider, scope="owner")).provider_calls == 0
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_timeout_is_not_automatically_replayed():
    class Slow(Provider):
        async def ainvoke(self, *args, **kwargs):
            self.calls += 1
            await asyncio.sleep(1)
    provider, planner = Slow(), QueryPlanner(timeout=.01)
    assert (await planner.plan("query", provider=provider, scope="owner")).status == "rewrite_timeout"
    assert (await planner.plan("query", provider=provider, scope="owner")).provider_calls == 0
    assert provider.calls == 1


def test_rrf_deduplicates_per_query_and_retains_original_citations():
    first = KnowledgeChunk("original", "book", .1, {"chunk_id":"one"})
    second = KnowledgeChunk("other", "book", 1, {"chunk_id":"two"})
    result = fuse_results([[second, first], [first, first]], 2)
    assert result[0].content == "original" and result[0].citation == first.citation
    assert len(result) == 2


@pytest.mark.parametrize("status,payload,expected", [
    (400, {"error":{"message":"User location is not supported for the API use.", "status":"FAILED_PRECONDITION"}}, "provider_region_unsupported"),
    (400, {"error":{"message":"secret-not-for-ui", "details":[{"reason":"API_KEY_INVALID"}]}}, "provider_auth_failed"),
    (404, {"error":{"message":"secret-not-for-ui"}}, "provider_model_not_found"),
])
def test_actual_google_errors_reach_safe_background_codes(status, payload, expected):
    errors = pytest.importorskip("google.genai.errors")
    from backend.services.background_errors import classify_background_error
    original = errors.ClientError(status, payload)
    wrapper = RuntimeError("provider call failed")
    wrapper.__cause__ = original
    assert classify_background_error(wrapper, "provider_request_failed") == expected


@pytest.mark.asyncio
async def test_remaining_budget_bounds_rewrite_and_caches_timeout():
    class Slow(Provider):
        async def ainvoke(self, *args, **kwargs):
            self.calls += 1
            await asyncio.sleep(10)
    provider, planner = Slow(), QueryPlanner(timeout=8)
    result = await asyncio.wait_for(planner.plan("query", provider=provider, scope="owner", timeout=.01), .2)
    assert result.status == "rewrite_timeout" and result.provider_calls == 1
    assert (await planner.plan("query", provider=provider, scope="owner")).cached
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_short_waiter_does_not_cancel_or_duplicate_shared_provider_call():
    started, release = asyncio.Event(), asyncio.Event()
    class Controlled(Provider):
        async def ainvoke(self, *args, **kwargs):
            self.calls += 1
            started.set()
            await release.wait()
            return LLMResponse('{"queries":["equivalent query"]}', "fake", "test", 1)
    provider, planner = Controlled(), QueryPlanner()
    leader = asyncio.create_task(planner.plan("query", provider=provider, scope="owner"))
    await started.wait()
    waiter = await planner.plan("query", provider=provider, scope="owner", timeout=.01)
    assert waiter.status == "rewrite_timeout" and waiter.provider_calls == 0
    release.set()
    assert (await leader).status == "rewritten"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_timeout_does_not_wait_for_slow_provider_cancellation_cleanup():
    release, stopped = asyncio.Event(), asyncio.Event()
    class SlowCleanup(Provider):
        async def ainvoke(self, *args, **kwargs):
            self.calls += 1
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await release.wait()
            finally:
                stopped.set()
            return LLMResponse('{"queries":["late result"]}', "fake", "test", 1)
    provider, planner = SlowCleanup(), QueryPlanner(timeout=.01)
    try:
        result = await asyncio.wait_for(planner.plan("query", provider=provider, scope="owner"), .2)
        assert result.status == "rewrite_timeout"
        assert (await planner.plan("query", provider=provider, scope="owner")).provider_calls == 0
    finally:
        release.set()
        await asyncio.wait_for(stopped.wait(), .2)
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_rewrite_uses_cancellable_provider_context_without_changing_success():
    from backend.llm.request_timeouts import cancellable_provider_calls
    class Cancellable(Provider):
        async def ainvoke(self, *args, **kwargs):
            assert cancellable_provider_calls()
            return await super().ainvoke(*args, **kwargs)
    provider = Cancellable(["equivalent search"])
    result = await QueryPlanner().plan("search", provider=provider, scope="owner")
    assert result.status == "rewritten" and result.queries == ("equivalent search",)
    assert provider.calls == 1 and not cancellable_provider_calls()
