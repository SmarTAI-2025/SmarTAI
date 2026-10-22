"""Public factual telemetry, with no live provider calls."""
import asyncio
import json
from types import SimpleNamespace as Obj

import pytest

from backend.agents import multi_expert
from backend.agents.grading_agent import grade_batch
from backend.models import ExpertResult, ProviderConfig
from backend.progress.grading_activity import (
    GradingActivity, observe_expert, report_provider_state, model_eta_seconds,
)
from backend.progress.tracker import ProgressReporter
from backend.tests.test_multi_expert_robustness import _FakeProvider, _FakeRegistry


def provider(name):
    result = _FakeProvider(name)
    result.config = ProviderConfig(provider_type="openai", model=name, api_key="secret-key", display_name=name)
    return result


def recorder(total=2):
    reporter = ProgressReporter("activity", 1, total)
    events = []
    reporter.set_event_sink(lambda event, payload: events.append(payload))
    reporter.grading_activity = GradingActivity(reporter, total)
    return reporter, events


@pytest.mark.asyncio
async def test_plan_requests_are_public_before_expert_activity_without_counting_answers(monkeypatch):
    from contextlib import asynccontextmanager
    from backend.llm import providers
    from backend.models import ProblemInfo
    from backend.progress.grading_activity import public_event
    from backend.services import math_grading_context as context, math_rubric_plan as rubric
    from backend.tools.structured_llm import _before_retry_sleep, TransientLLMError

    p = provider("Gemini")
    p._endpoint_breaker = lambda: Obj(is_open=False)
    reporter = ProgressReporter("plan-activity", 1, 2)
    rows = []
    reporter.set_event_sink(lambda event, payload: rows.append({
        "sequence": len(rows) + 1, "created_at": event.ts,
        "message": event.message, "payload": payload,
    }))
    question = ProblemInfo(q_id="q1", number="1", type="计算题", stem="2 + 2",
                           criterion="Method 8, result 2.", max_score=10)

    @asynccontextmanager
    async def lease(**kwargs):
        yield

    monkeypatch.setattr(providers, "get_scheduler", lambda: Obj(lease=lease))

    async def prepare(value, selected, **kwargs):
        # Exercise the real provider admission and structured retry hooks,
        # without dispatching a model request.
        async with providers.BaseProvider._call_capacity(selected, []):
            assert public_event(rows[-1])["code"] == "responding"
            assert (await reporter.snapshot()).completed_units == 0
        await _before_retry_sleep(Obj(
            args=[selected], outcome=Obj(exception=lambda: TransientLLMError("PRIVATE_ERROR")),
            next_action=Obj(sleep=1), attempt_number=1,
        ))
        assert public_event(rows[-1])["code"] == "retrying"
        async with providers.BaseProvider._call_capacity(selected, []):
            pass
        return rubric.fallback_math_rubric_plan(value)

    monkeypatch.setattr(rubric, "prepare_math_rubric_plan", prepare)
    plans = await context.prepare_math_plans({"q1": question}, _FakeRegistry([p]), reporter=reporter)
    assert plans["q1"].input_fingerprint == rubric.math_rubric_fingerprint(question)
    assert reporter.grading_activity is None
    public = [event for row in rows if (event := public_event(row))]
    assert [event["code"] for event in public] == ["queued", "responding", "retrying", "queued", "responding"]
    assert public[2]["attempt"] == 2 and public[2]["reason"] == "unavailable"
    assert all("model_progress" not in row["payload"] for row in rows)
    assert all(row["payload"]["completed_units"] == 0 for row in rows)
    count = len(rows)
    with context.math_plans_context(plans):
        await context.prepare_math_plans({"q1": question}, _FakeRegistry([p]), reporter=reporter)
    await report_provider_state(p, "responding")  # auxiliary scope has ended
    assert len(rows) == count

    reporter.grading_activity = GradingActivity(reporter, 2)
    async with observe_expert(reporter, p, [(p, 0)]) as unit:
        await report_provider_state(p, "responding")
        unit["successful"] = True
    model = rows[-1]["payload"]["model_progress"][0]
    assert model["total"] == 2 and model["succeeded"] == 1
    assert model["failed"] == model["responding"] == model["retrying"] == 0
    assert "PRIVATE_ERROR" not in json.dumps(rows) and "secret-key" not in json.dumps(rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_plan_activity_restores_existing_observer_on_failure_and_cancellation(monkeypatch, failure):
    from copy import deepcopy
    from backend.models import ProblemInfo
    from backend.progress.recognition_activity import report_provider_state as provider_state
    from backend.services import math_grading_context as context, math_rubric_plan as rubric

    p = provider("Gemini")
    reporter, events = recorder()
    observer = reporter.grading_activity
    question = ProblemInfo(q_id="q1", number="1", type="计算题", stem="2 + 2",
                           criterion="Method 8, result 2.", max_score=10)

    async def prepare(*args, **kwargs):
        await provider_state(p, "responding")
        await provider_state(p, "retrying", attempt=2, max_attempts=3, retry_at=123,
                             reason="timeout", raw_error="PRIVATE_ERROR", prompt="PRIVATE_PROMPT")
        raise failure("PRIVATE_FAILURE")

    monkeypatch.setattr(rubric, "prepare_math_rubric_plan", prepare)
    async with observe_expert(reporter, p, [(p, 0)]) as unit:
        await report_provider_state(p, "responding")
        before = deepcopy(observer.models)
        count = len(events)
        if failure is asyncio.CancelledError:
            with pytest.raises(asyncio.CancelledError):
                await context.prepare_math_plans({"q1": question}, _FakeRegistry([p]), reporter=reporter)
        else:
            plans = await context.prepare_math_plans({"q1": question}, _FakeRegistry([p]), reporter=reporter)
            assert plans["q1"].input_fingerprint == rubric.math_rubric_fingerprint(question)
        assert reporter.grading_activity is observer
        assert observer.models == before
        auxiliary = [row for row in events[count:] if "grading_activity" in row]
        assert [row["grading_activity"]["code"] for row in auxiliary] == ["responding", "retrying"]
        assert all("model_progress" not in row for row in auxiliary)
        assert auxiliary[-1]["grading_activity"] == {
            "code": "retrying", "attempt": 2, "max_attempts": 3,
            "retry_at": 123, "reason": "timeout",
        }
        # The outer expert's unit is restored, not left in auxiliary mode.
        await report_provider_state(p, "queued")
        assert events[-1]["model_progress"][0]["queued"] == 1
        unit["successful"] = True
    model = events[-1]["model_progress"][0]
    assert model["total"] == 2 and model["succeeded"] == 1
    assert model["failed"] == model["cancelled"] == model["queued"] == 0
    assert (await reporter.snapshot()).completed_units == 0
    count = len(events)
    await report_provider_state(p, "responding")
    assert len(events) == count
    assert "PRIVATE" not in json.dumps(events) and "secret-key" not in json.dumps(events)


@pytest.mark.asyncio
async def test_fast_model_finishes_other_questions_while_slow_waits(monkeypatch):
    fast, slow = provider("Gemini"), provider("Tsinghua")
    fast_done, release_slow = asyncio.Event(), asyncio.Event()
    fast_count = 0

    class Skill:
        name = "PRIVATE_SKILL"

        def __init__(self, provider, **kwargs):
            self.provider = provider

        async def grade(self, problem, answer, **kwargs):
            nonlocal fast_count
            await report_provider_state(self.provider, "queued")
            await report_provider_state(self.provider, "responding")
            if self.provider is slow:
                await release_slow.wait()
            else:
                fast_count += 1
                if fast_count == 3:
                    fast_done.set()
            return ExpertResult(provider=self.provider.provider_id, score=8, max_score=10, confidence=1, comment="ok")

    monkeypatch.setattr(multi_expert, "get_skill_for_type", lambda _: Skill)
    reporter, events = recorder(3)
    problems = {f"q{i}": {"q_id": f"q{i}", "type": "简答题", "number": str(i), "stem": "question", "criterion": "Correctness", "max_score": 10} for i in range(3)}
    students = {"s": {"stu_id": "s", "stu_ans": [{"q_id": q, "content": "answer"} for q in problems]}}
    # This test isolates expert scheduling after question-level preparation.
    # Freeze real plans so the fake provider is never asked to interpret rules.
    from backend.models import ProblemInfo
    from backend.services.math_grading_context import math_plans_context
    from backend.services.math_rubric_plan import fallback_math_rubric_plan
    with math_plans_context({qid: fallback_math_rubric_plan(ProblemInfo(**raw))
                             for qid, raw in problems.items()}):
        task = asyncio.create_task(grade_batch(student_store=students, problem_store=problems,
            registry=_FakeRegistry([fast, slow]), reporter=reporter, aggregation_method="weighted_average"))
    try:
        await asyncio.wait_for(fast_done.wait(), 2)
        await asyncio.sleep(0)
        models = events[-1]["model_progress"]
        assert models[0]["succeeded"] == models[0]["total"] == 3
        assert models[1]["succeeded"] == 0 and models[1]["responding"] == 3
        assert (await reporter.snapshot()).completed_units == 0
    finally:
        release_slow.set()
    results = await task
    assert [item.score for item in results[0]["corrections"]] == [8, 8, 8]
    assert (await reporter.snapshot()).completed_units == 3
    assert "secret-key" not in json.dumps(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["rate_limit", "timeout", "unavailable"])
async def test_retry_does_not_complete_or_recount_assessment_and_cancellation_is_distinct(reason):
    from backend.tools.structured_llm import _before_retry_sleep, RateLimitError, TransientLLMError
    from backend.llm.providers import ProviderRequestError
    p = provider("Gemini")
    reporter, events = recorder()
    async with observe_expert(reporter, p, [(p, 0)]) as unit:
        await report_provider_state(p, "responding")
        error = RateLimitError("PRIVATE_ERROR") if reason == "rate_limit" else TransientLLMError("PRIVATE_ERROR")
        if reason == "timeout":
            error.__cause__ = ProviderRequestError("provider_timeout")
        state = Obj(args=[p], outcome=Obj(exception=lambda: error),
                    next_action=Obj(sleep=1), attempt_number=1)
        await _before_retry_sleep(state)
        model = events[-1]["model_progress"][0]
        assert model["retrying"] == 1 and model["succeeded"] == model["failed"] == 0
        assert events[-1]["grading_activity"]["attempt"] == 2
        assert events[-1]["grading_activity"]["reason"] == reason
        await report_provider_state(p, "responding")
        unit["successful"] = True
    assert events[-1]["model_progress"][0]["succeeded"] == 1
    with pytest.raises(asyncio.CancelledError):
        async with observe_expert(reporter, p, [(p, 0)]):
            raise asyncio.CancelledError
    model = events[-1]["model_progress"][0]
    assert model["succeeded"] == model["cancelled"] == 1
    assert model["responding"] == model["retrying"] == 0
    assert "PRIVATE_ERROR" not in json.dumps(events)


@pytest.mark.asyncio
async def test_capacity_reports_admission_separately_from_response(monkeypatch):
    from contextlib import asynccontextmanager
    from backend.llm.providers import BaseProvider
    from backend.llm import providers
    p = provider("Gemini")
    p._endpoint_breaker = lambda: Obj(is_open=False)
    entered, admit = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def lease(**kwargs):
        entered.set()
        await admit.wait()
        yield

    monkeypatch.setattr(providers, "get_scheduler", lambda: Obj(lease=lease))
    reporter, events = recorder(1)

    async def call():
        async with observe_expert(reporter, p, [(p, 0)]) as unit:
            async with BaseProvider._call_capacity(p, []):
                assert events[-1]["grading_activity"]["code"] == "responding"
            unit["successful"] = True

    task = asyncio.create_task(call())
    await asyncio.wait_for(entered.wait(), 2)
    assert events[-1]["grading_activity"]["code"] == "queued"
    admit.set()
    await task
    assert events[-1]["model_progress"][0]["succeeded"] == 1
    count = len(events)
    await report_provider_state(p, "responding")  # no grading scope
    assert len(events) == count


@pytest.mark.asyncio
async def test_multisample_failure_and_observer_failure_do_not_change_outputs():
    p = provider("Gemini")
    reporter, events = recorder(1)
    async with observe_expert(reporter, p, [(p, 1), (p, 2)]):
        pass  # normalized invalid result
    assert events[-1]["model_progress"][0]["total"] == 2
    assert events[-1]["model_progress"][0]["failed"] == 1
    async def broken(_):
        raise ValueError("telemetry unavailable")
    reporter.publish_grading_activity = broken
    async with observe_expert(reporter, p, [(p, 1), (p, 2)]) as unit:
        unit["successful"] = True


@pytest.mark.asyncio
@pytest.mark.parametrize("samples", [1, 2])
async def test_filtered_retry_counts_only_selected_answers_and_samples(monkeypatch, samples):
    from backend.tests.test_multi_expert_robustness import _patch_skill_returning, _expert_result, _problem
    p = provider("Gemini")
    _patch_skill_returning(monkeypatch, {p.provider_id: _expert_result(p.provider_id, 8, 1, "ok")})
    reporter, events = recorder(2)
    await grade_batch(question_ids_by_student={"s": {"q1"}},
        student_store={"s": {"stu_id": "s", "stu_ans": [{"q_id": "q1", "content": "4"}, {"q_id": "q2", "content": "4"}]}},
        problem_store={q: _problem(q).model_dump() for q in ("q1", "q2")},
        registry=_FakeRegistry([p]), reporter=reporter, multi_sample_n=samples,
        aggregation_method="weighted_average")
    models = next(event["model_progress"] for event in reversed(events) if "model_progress" in event)
    assert models[0]["total"] == models[0]["succeeded"] == samples
    assert (await reporter.snapshot()).completed_units == 1


def test_eta_tracks_slow_model_and_withholds_unreliable_estimates():
    fast = {"total": 14, "succeeded": 14, "failed": 0, "retrying": 0, "completion_times": [5, 6, 7]}
    slow = {"total": 14, "succeeded": 3, "failed": 0, "retrying": 0, "completion_times": [10, 30, 50]}
    assert model_eta_seconds([fast, slow], now=55) == 220
    assert model_eta_seconds([fast, {**slow, "retrying": 1}], now=55) is None
    assert model_eta_seconds([fast, {**slow, "completion_times": [50]}], now=55) is None
    assert model_eta_seconds([fast, slow], now=120) is None
    assert model_eta_seconds([fast, {**slow, "completion_times": [1, 151, 301]}], now=400) == 1650


def test_public_projection_rejects_free_form_and_malformed_events():
    from backend.progress.grading_activity import public_event, public_models, public_waits
    row = {"sequence": 1, "created_at": 5, "message": "PRIVATE_LOG"}
    assert public_event({**row, "payload": {"grading_activity": {"code": "PRIVATE_STEP"}}}) is None
    assert public_event({**row, "payload": {"grading_activity": {"code": {"prompt": "PRIVATE_PROMPT"}}}}) is None
    assert public_event({**row, "payload": {"grading_activity": {"code": "retrying", "reason": {"error": "PRIVATE_ERROR"}, "attempt": float("nan")}}}) == {"sequence": 1, "ts": 5, "code": "retrying"}
    models = public_models([{"id": "model-1", "label": {"prompt": "PRIVATE_PROMPT"}, "raw": "PRIVATE_RAW"}])
    assert "PRIVATE" not in json.dumps(models)
    assert public_waits([{"model": {"key": "PRIVATE_KEY"}, "reason": "provider_rate_limited", "attempt": 1, "max_attempts": 3, "retry_at": 10}]) == []


def test_history_is_owner_scoped_paginated_durable_and_filters_internal_payloads(monkeypatch):
    from backend.services import task_facade
    from backend.db import grading_repository as repo
    from backend.domain.errors import DomainError
    from backend.tests.test_grading_source_readiness import _seed_legacy_structured_case
    case = _seed_legacy_structured_case(question_count=2)
    revision = case["workflow_repository"].get_live_workflow(case["assignment_id"], owner_id=case["owner_id"]).workflow_revision
    run_id = task_facade.start_task_grading(task_id=case["assignment_id"], owner_id=case["owner_id"], request_id="activity-test", expected_workflow_revision=revision)["job_id"]
    for i in range(205):
        repo.record_event(run_id=run_id, level="info", message="PRIVATE_STEP",
            payload={"prompt": "PRIVATE_PROMPT", "grading_activity": {"code": "responding", "model_id": "model-1", "raw_error": "PRIVATE_KEY"},
                     "model_progress": [{"id": "model-1", "label": "Gemini", "total": 2, "responding": 1, "prompt": "PRIVATE_PROMPT"}]})
    repo.record_event(run_id=run_id, level="info", message="PRIVATE_LOG", payload={})
    history, after = [], 0
    while True:
        page = task_facade.grading_activity_history(task_id=case["assignment_id"], owner_id=case["owner_id"], run_id=run_id, after=after, limit=100)
        assert page["cursor"] > after
        history += page["events"]
        after = page["cursor"]
        if not page["has_more"]:
            break
    assert len(history) == 205 and len({item["sequence"] for item in history}) == 205
    projection = task_facade._grading_progress(run_id, case["owner_id"])
    assert projection["model_progress"][0]["responding"] == 1
    assert projection["activity_cursor"] == after
    assert "PRIVATE" not in json.dumps({"history": history, "progress": projection})
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api import tasks
    app = FastAPI()
    app.include_router(tasks.router)
    app.dependency_overrides[tasks.require_teacher] = lambda: Obj(id=case["owner_id"])
    client = TestClient(app)
    url = f"/tasks/{case['assignment_id']}/grading/activity"
    response = client.get(url, params={"run_id": run_id, "limit": 100})
    assert response.status_code == 200 and response.json()["has_more"]
    assert "PRIVATE" not in response.text
    assert client.get(url, params={"run_id": run_id, "limit": 201}).status_code == 422
    assert client.get(url, params={"run_id": run_id, "after": -1}).status_code == 422
    app.dependency_overrides[tasks.require_teacher] = lambda: Obj(id="other-owner")
    assert client.get(url, params={"run_id": run_id}).status_code == 404
    for task_id, owner in [(case["assignment_id"], "other-owner"), ("other-task", case["owner_id"])]:
        with pytest.raises(DomainError):
            task_facade.grading_activity_history(task_id=task_id, owner_id=owner, run_id=run_id, after=0, limit=100)
    real_get = repo.get_run
    monkeypatch.setattr(repo, "get_run", lambda *a, **kw: real_get(*a, **kw).model_copy(update={"status": "cancelled", "error_message": "operation_cancelled"}))
    assert task_facade._grading_progress(run_id, case["owner_id"])["model_progress"][0]["responding"] == 0
