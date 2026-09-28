import asyncio
import hashlib
import io
import json

import fitz
from PIL import Image
import pytest
from pydantic import ValidationError

from backend.agents.recognition_agent import RecognitionAgent, RecognitionReadRequestV1
from backend.domain.errors import RecognitionError
from backend.progress.tracker import ProgressReporter
from backend.recognition.budget import RecognitionBudget
from backend.recognition.fusion import RecognitionAssemblyV1, assemble_recognition
from backend.recognition.models import RecognitionCandidateV1, RecognitionPolicyV1, RecognitionSourceRefV1
from backend.recognition.planner import EngineCapabilitiesV1
from backend.recognition.recheck import recheck_recognition
from backend.recognition.runtime import RecognitionCapacity


class Engine:
    def __init__(self, *, initial=None, decision="replace", text="1 + 1 = 3", error=None, raw=None):
        self.capabilities = EngineCapabilitiesV1(
            route_id="selected", fingerprint="fixed", visual_inputs=["page_image"],
            semantic_repair=True, response_recheck=True, bounded_output_tokens=True,
        )
        self.initial = initial if initial is not None else ["[unclear] + 1 = 3"]
        self.decision, self.text, self.error, self.raw = decision, text, error, raw
        self.reads, self.repairs = [], []
        self.entered, self.release = asyncio.Event(), None

    async def recognize(self, request):
        self.reads.append(request)
        item = self.initial[min(request.page_number - 1, len(self.initial) - 1)]
        if isinstance(item, Exception):
            raise item
        return RecognitionCandidateV1(kind="vision", status="ok" if item else "empty", text=item,
                                       provider_route_id="selected", input_tokens=10, output_tokens=2)

    async def repair(self, request):
        self.repairs.append(request)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.error:
            raise self.error
        if self.raw is not None:
            return self.raw
        text = (request.repair_context.native_text if self.decision == "keep_native" else
                request.repair_context.visual_text if self.decision == "keep_visual" else
                "" if self.decision == "still_unknown" else self.text)
        return RecognitionCandidateV1(kind="vision", status="ok", provider_route_id="selected",
                                       text=json.dumps({"decision": self.decision, "text": text, "source_evidence": "Visible literal symbols"}),
                                       input_tokens=4, output_tokens=7)


def source_file(*, pdf=False, pages=1):
    if pdf:
        document = fitz.open()
        for number in range(1, pages + 1):
            document.new_page(width=160, height=180).insert_text((20, 40), f"Q{number}. x = -2")
        payload = document.tobytes()
        document.close()
        return payload, "application/pdf"
    stream = io.BytesIO()
    Image.new("RGB", (80, 120), "white").save(stream, "PNG")
    return stream.getvalue(), "image/png"


async def initial_read(engine=None, *, pdf=False, pages=1, policy=None, purpose="submissions", clock=None):
    engine = engine or Engine()
    data, content_type = source_file(pdf=pdf, pages=pages)
    source = RecognitionSourceRefV1(owner_id="owner", scope="submission_source", business_id="submission",
                                     content_type=content_type, input_sha256=hashlib.sha256(data).hexdigest())
    request = RecognitionReadRequestV1(source=source, purpose=purpose, policy=policy or RecognitionPolicyV1(force_visual=True))
    capacity = RecognitionCapacity()
    agent = RecognitionAgent(engine, capacity=capacity, **({"clock": clock} if clock else {}))
    raw, budget = await agent._read(request, data, authorized_owner_id="owner")
    initial = assemble_recognition(raw, prompt_version="test-reader-v1")
    return initial, data, dict(authorized_owner_id="owner", engine=engine, budget=budget, capacity=capacity)


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["problems", "submissions", "reference", "rubric", "test_cases", "knowledge"])
async def test_agent_rechecks_original_image_without_rewriting_raw_or_correcting_source(purpose):
    engine = Engine(text="1 + 1 = 3\n[crossed-out: 2]")
    data, content_type = source_file()
    source = RecognitionSourceRefV1(owner_id="owner", scope="submission_source", business_id="submission",
                                     content_type=content_type, input_sha256=hashlib.sha256(data).hexdigest())
    progress = ProgressReporter("recheck-test")
    await progress.increment_stage_metrics(existing=5)
    result = await RecognitionAgent(engine, capacity=RecognitionCapacity(), progress=progress).recognize(
        RecognitionReadRequestV1(source=source, purpose=purpose), data,
        authorized_owner_id="owner", prompt_version="test-reader-v1",
    )
    assert len(engine.reads) == len(engine.repairs) == 1
    assert engine.reads[0].payload == engine.repairs[0].payload
    assert engine.repairs[0].repair_context.before_text == "[unclear] + 1 = 3"
    span = result.document.pages[0].spans[0]
    assert span.visual.text == "[unclear] + 1 = 3" and span.patch.before_text == span.visual.text
    assert span.adopted_from == "repair" and span.final_text == "1 + 1 = 3\n[crossed-out: 2]"
    assert span.patch.status == "applied" and span.confidence == "low" and result.document.confidence == "low"
    assert result.raw.budget.total_calls == 1 and result.repair_execution.budget.total_calls == 2
    assert result.document.usage.input_tokens == 14 and result.document.usage.output_tokens == 9
    assert result.document.usage.patch_calls == 1 and not result.recognition_complete
    assert progress._progress.stage_metrics["existing"] == 5
    assert progress._progress.stage_metrics["recognition_rechecks_finished"] == 1
    assert RecognitionAssemblyV1.model_validate_json(result.model_dump_json()) == result
    assert "payload_b64" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("pdf", [False, True])
async def test_empty_recovery_retains_initial_empty_and_recovers_only_page_read_coverage(pdf):
    initial, data, kwargs = await initial_read(Engine(initial=[""]), pdf=pdf)
    assert initial.document.coverage.failed_pages == [1]
    result = await recheck_recognition(initial, data, **kwargs)
    assert result.document.pages[0].spans[0].visual.status == "empty"
    assert result.document.pages[0].spans[0].patch.attempt_count == 1
    assert result.document.coverage.processed_pages == [1] and not result.document.coverage.failed_pages
    assert result.document.usage.empty_recovery_calls == 1 and result.document.confidence == "low"
    assert initial.document.coverage.failed_pages == [1]
    assert RecognitionAssemblyV1.model_validate_json(result.model_dump_json()) == result


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["keep_native", "keep_visual", "still_unknown"])
async def test_explicit_decisions_preserve_exact_candidate_and_never_gain_high_confidence(decision):
    initial, data, kwargs = await initial_read(Engine(decision=decision), pdf=True)
    result = await recheck_recognition(initial, data, **kwargs)
    before = initial.document.pages[0].spans[0]
    after = result.document.pages[0].spans[0]
    expected = before.native.text if decision == "keep_native" else before.visual.text if decision == "keep_visual" else before.final_text
    assert after.final_text == expected and after.confidence == "low"
    assert after.patch.decision == decision
    assert after.patch.status == ("still_unknown" if decision == "still_unknown" else "applied")


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_text", ["1 + 1 = 3", "x = (2", "unfinished student step =", "[blank]"])
async def test_mathematical_wrongness_or_blank_marker_does_not_trigger_extra(initial_text):
    initial, data, kwargs = await initial_read(Engine(initial=[initial_text]))
    result = await recheck_recognition(initial, data, **kwargs)
    assert not kwargs["engine"].repairs and not result.repair_execution.calls
    assert result.document.final_markdown == initial_text and result.document.usage.total_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RecognitionError("provider_auth_failed"), RecognitionError("provider_quota_exceeded"),
                                  RecognitionError("provider_submit_uncertain")])
async def test_any_initial_failure_halts_rechecks_of_other_successful_pages(error):
    engine = Engine(initial=["[unclear] + 1 = 3", error])
    initial, data, kwargs = await initial_read(engine, pdf=True, pages=2)
    result = await recheck_recognition(initial, data, **kwargs)
    assert result.repair_execution.selection.targets and not result.repair_execution.calls and not engine.repairs
    assert result.repair_execution.stop_codes
    assert result.document.usage.total_calls == initial.document.usage.total_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,error", [
    (RecognitionCandidateV1(kind="vision", status="ok", text="bad JSON", provider_route_id="selected"), None),
    (RecognitionCandidateV1(kind="vision", status="empty", provider_route_id="selected"), None),
    (RecognitionCandidateV1(kind="vision", status="ok", text="refusal", finish_reason="refused", provider_route_id="selected"), None),
    (RecognitionCandidateV1(kind="vision", status="ok", text='{"decision":', finish_reason="length", provider_route_id="selected"), None),
    (None, RecognitionError("provider_auth_failed")), (None, RecognitionError("provider_submit_uncertain")),
])
async def test_failed_recheck_keeps_before_charges_once_and_halts_other_rechecks(raw, error):
    engine = Engine(raw=raw, error=error)
    initial, data, kwargs = await initial_read(engine, pdf=True, pages=2)
    result = await recheck_recognition(initial, data, **kwargs)
    assert len(engine.repairs) == len(result.repair_execution.calls) == 1
    assert result.document.final_markdown == initial.document.final_markdown
    assert result.document.usage.total_calls == 3 and result.repair_execution.stop_codes
    assert result.repair_execution.budget.pending_calls == int(error is not None and error.code == "provider_submit_uncertain")
    assert RecognitionAssemblyV1.model_validate_json(result.model_dump_json()) == result


@pytest.mark.asyncio
async def test_shared_extra_limits_apply_to_actual_dispatches_not_just_selection():
    engine = Engine(initial=["", "", "", "[unclear]", "[unclear]", "[unclear]", "[unclear]", "[unclear]"])
    initial, data, kwargs = await initial_read(engine, pdf=True, pages=8)
    result = await recheck_recognition(initial, data, **kwargs)
    assert len(engine.repairs) == 6
    assert result.document.usage.empty_recovery_calls == 2 and result.document.usage.patch_calls == 4
    assert result.document.usage.total_calls == 14
    assert result.document.coverage.failed_pages == [3]
    assert result.document.pages[7].spans[0].patch.attempt_count == 0


@pytest.mark.asyncio
async def test_elapsed_deadline_is_not_restarted_before_preparing_repair():
    now = [0.0]
    initial, data, kwargs = await initial_read(policy=RecognitionPolicyV1(total_seconds=10), clock=lambda: now[0])
    now[0] = 11
    result = await recheck_recognition(initial, data, **kwargs)
    assert not kwargs["engine"].repairs
    call = result.repair_execution.calls[0]
    assert call.result is None and call.safe_error_code == "recognition_timeout"
    assert result.document.usage.total_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["route", "tokens", "disabled", "no_capability"])
async def test_route_budget_and_policy_gates_prevent_extra_dispatch(change):
    engine = Engine()
    policy = RecognitionPolicyV1(max_output_tokens=2 if change == "tokens" else 32768,
                                  enable_repair=change != "disabled")
    if change == "no_capability":
        engine.capabilities.semantic_repair = False
    initial, data, kwargs = await initial_read(engine, policy=policy)
    if change == "route":
        engine.capabilities.fingerprint = "different"
    result = await recheck_recognition(initial, data, **kwargs)
    assert not engine.repairs and result.document.usage.total_calls == 1
    if change in {"route", "tokens"}:
        assert result.repair_execution.calls[0].result is None
    else:
        assert not result.repair_execution.selection.targets


@pytest.mark.asyncio
async def test_actual_repair_output_overrun_is_retained_not_clamped_to_budget():
    initial, data, kwargs = await initial_read(policy=RecognitionPolicyV1(max_output_tokens=3))
    result = await recheck_recognition(initial, data, **kwargs)
    assert kwargs["engine"].repairs[0].max_output_tokens == 1
    assert result.document.usage.output_tokens == 9
    assert result.repair_execution.budget.output_limit_exceeded
    assert result.repair_execution.budget.charged_output_tokens == 9
    assert RecognitionAssemblyV1.model_validate_json(result.model_dump_json()) == result


@pytest.mark.asyncio
@pytest.mark.parametrize("pdf", [False, True])
async def test_wrong_prepared_image_is_rejected_before_model_dispatch(monkeypatch, pdf):
    from backend.recognition import recheck

    initial, data, kwargs = await initial_read(pdf=pdf)
    prepare = recheck._prepare_image

    async def wrong(*args, **kw):
        payload, image = await prepare(*args, **kw)
        image.payload_sha256 = "b" * 64
        return payload, image

    monkeypatch.setattr(recheck, "_prepare_image", wrong)
    result = await recheck_recognition(initial, data, **kwargs)
    assert not kwargs["engine"].repairs
    assert result.repair_execution.calls[0].safe_error_code == "recognition_source_mismatch"
    assert result.document.usage.total_calls == 1


@pytest.mark.asyncio
async def test_fresh_budget_wrong_owner_wrong_bytes_and_second_recheck_are_rejected():
    initial, data, kwargs = await initial_read()
    fresh = RecognitionBudget(initial.raw.request.source, initial.raw.execution_policy, initial.raw.engine_capabilities)
    for changes in ({"budget": fresh}, {"authorized_owner_id": "other"}):
        with pytest.raises(RecognitionError):
            await recheck_recognition(initial, data, **(kwargs | changes))
    with pytest.raises(RecognitionError):
        await recheck_recognition(initial, data + b"changed", **kwargs)
    result = await recheck_recognition(initial, data, **kwargs)
    with pytest.raises(RecognitionError):
        await recheck_recognition(result, data, **kwargs)
    with pytest.raises(RecognitionError):
        await recheck_recognition(initial, data, **kwargs)
    assert len(kwargs["engine"].repairs) == 1


@pytest.mark.asyncio
async def test_cancelled_workflow_never_refunds_extra_or_permits_replay():
    initial, data, kwargs = await initial_read()
    engine = kwargs["engine"]
    engine.release = asyncio.Event()
    task = asyncio.create_task(recheck_recognition(initial, data, **kwargs))
    await asyncio.wait_for(engine.entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert kwargs["budget"].snapshot().pending_calls == 1 and not kwargs["capacity"]._owners
    with pytest.raises(RecognitionError):
        await recheck_recognition(initial, data, **kwargs)
    assert len(engine.repairs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path,value", [
    (("document", "final_markdown"), "invented"),
    (("repair_execution", "calls"), []),
    (("repair_execution", "budget", "patch_calls"), 0),
    (("repair_execution", "budget", "output_tokens"), 0),
    (("repair_execution", "budget", "known_input_tokens"), 0),
    (("repair_execution", "budget", "global_remaining_seconds"), 900),
    (("repair_execution", "calls", 0, "image", "source_sha256"), "b" * 64),
    (("repair_execution", "calls", 0, "image", "payload_sha256"), "b" * 64),
    (("repair_execution", "calls", 0, "result", "context", "before_text"), "other"),
    (("repair_execution", "selection", "targets", 0, "context", "purpose"), "knowledge"),
])
async def test_roundtrip_rejects_lost_paid_call_changed_scope_or_synthetic_summary(path, value):
    initial, data, kwargs = await initial_read()
    result = await recheck_recognition(initial, data, **kwargs)
    payload = result.model_dump()
    cursor = payload
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    with pytest.raises((ValidationError, RecognitionError)):
        RecognitionAssemblyV1.model_validate(payload)
