"""HTTP waits bound the complete synchronous operation, never job lifetimes."""
import asyncio
from inspect import signature

import pytest
from fastapi import HTTPException

from backend.api import request_deadlines


def test_deadline_covers_multiple_steps_and_cancels_work():
    events = []

    @request_deadlines.request_deadline(0.03)
    async def operation():
        try:
            await asyncio.sleep(0.02)
            events.append("first")
            await asyncio.sleep(0.02)
            events.append("second")
        finally:
            events.append("cleanup")

    with pytest.raises(HTTPException) as error:
        asyncio.run(operation())
    assert error.value.status_code == 504
    assert error.value.detail == {"code": "request_timeout"}
    assert events == ["first", "cleanup"]


def test_inner_timeout_is_not_reclassified():
    @request_deadlines.request_deadline(10)
    async def operation():
        raise TimeoutError("provider deadline")

    with pytest.raises(TimeoutError, match="provider deadline"):
        asyncio.run(operation())


def test_external_cancellation_is_not_reclassified():
    @request_deadlines.request_deadline(10)
    async def operation():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(operation())


def test_success_and_dependency_signature_are_preserved():
    @request_deadlines.request_deadline(10)
    async def operation(value: "int", optional: "str" = "ok") -> "str":
        return f"{value}:{optional}"

    assert asyncio.run(operation(3)) == "3:ok"
    assert signature(operation).parameters["value"].annotation is int
    assert signature(operation).parameters["optional"].default == "ok"


@pytest.mark.parametrize("module_name,function_name,seconds", [
    ("analytics", "_run_grounded_ask", 290),
    ("analytics", "per_question", 25),
    ("task_preparation", "preflight_material_import", 950),
    ("task_preparation", "preflight_problem_source", 950),
])
def test_real_synchronous_entrypoints_install_expected_budget(monkeypatch, module_name, function_name, seconds):
    import importlib
    endpoint = getattr(importlib.import_module(f"backend.api.{module_name}"), function_name)
    seen = []

    class Expired:
        async def __aenter__(self):
            raise TimeoutError()

        async def __aexit__(self, *args):
            return False

        def expired(self):
            return True

    def timeout(value):
        seen.append(value)
        return Expired()

    monkeypatch.setattr(request_deadlines.asyncio, "timeout", timeout)
    with pytest.raises(HTTPException) as error:
        asyncio.run(endpoint())
    assert error.value.detail == {"code": "request_timeout"}
    assert seen == [seconds]
    # Dependency introspection still sees the original declared arguments.
    assert "current" in signature(endpoint).parameters


def test_swallowed_cancellation_cannot_return_success():
    @request_deadlines.request_deadline(0.01)
    async def operation():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return "not a successful response"

    with pytest.raises(HTTPException) as error:
        asyncio.run(operation())
    assert error.value.status_code == 504
    assert error.value.detail == {"code": "request_timeout"}


def test_browser_budgets_include_reachable_cleanup_and_delivery_margin():
    from backend.tools.pdf_worker_lifecycle import PDF_CLEANUP_SECONDS
    from backend.llm.request_timeouts import _LOCAL_STEP_SECONDS
    # PDF transport and process cleanup can be serial. HTTP text-only paths
    # have no recognition observer, leaving only Gemini client close.
    assert request_deadlines.MATERIAL_PREFLIGHT_SECONDS + 2 * PDF_CLEANUP_SECONDS + 10 <= 1020
    assert request_deadlines.ASK_REQUEST_SECONDS + _LOCAL_STEP_SECONDS + 5 <= 305
    assert request_deadlines.QUESTION_ANALYSIS_SECONDS + _LOCAL_STEP_SECONDS + 5 <= 40
    assert request_deadlines.EXPERT_PROBE_SECONDS + _LOCAL_STEP_SECONDS + 5 <= 75


@pytest.mark.asyncio
async def test_near_deadline_success_is_unchanged_and_expiry_allows_bounded_cleanup(monkeypatch):
    # Scaled25s processing+10s cleanup behind40s browser wait. Deterministically
    # expire at the blocked stage rather than depending on machine speed.
    original = asyncio.timeout
    scopes = []
    def timeout(seconds):
        scope = original(seconds)
        scopes.append(scope)
        return scope
    monkeypatch.setattr(request_deadlines.asyncio, "timeout", timeout)
    cleaned = []
    @request_deadlines.request_deadline(.25)
    async def operation(expire):
        if not expire:
            return {"complete": True, "unchanged": "result"}
        scopes[-1].reschedule(asyncio.get_running_loop().time())
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(.01)
            cleaned.append(True)
    assert await operation(False) == {"complete": True, "unchanged": "result"}
    with pytest.raises(HTTPException) as error:
        await operation(True)
    assert error.value.detail == {"code": "request_timeout"}
    assert cleaned == [True]
