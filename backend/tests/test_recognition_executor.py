import asyncio
import hashlib

import fitz
import pytest

from backend.domain.errors import RecognitionError
from backend.progress.tracker import ProgressReporter
from backend.recognition.executor import RecognitionCapacity, RecognitionReadBatchV1, read_pdf_plan
from backend.recognition.models import RecognitionCandidateV1, RecognitionPolicyV1, RecognitionSourceRefV1
from backend.recognition.planner import EngineCapabilitiesV1, RecognitionPlanRequestV1, plan_recognition
from backend.tools.pdf_evidence import PdfIndexRequest, read_pdf_evidence


def pdf(count=3, text="A literal source paragraph."):
    with fitz.open() as document:
        for _ in range(count):
            page = document.new_page(width=240, height=320)
            page.insert_text((20, 30), text)
        return document.tobytes()


class FakeEngine:
    def __init__(self, *, document=False, result="visual source", error=None, wait=0, known_usage=True):
        self._capabilities = EngineCapabilitiesV1(
            route_id="frozen-route", fingerprint="frozen-fp",
            visual_inputs=["document", "page_image"] if document else ["page_image"],
            region_reads=not document, candidate_kind="ocr" if document else "vision",
            document_batching=document, max_document_pages=24 if document else 1,
            bounded_output_tokens=not document,
        )
        self.requests = []
        self.result, self.error, self.wait, self.known_usage = result, error, wait, known_usage
        self.active = 0

    @property
    def capabilities(self):
        return self._capabilities.model_copy(deep=True)

    async def recognize(self, request):
        self.requests.append(request)
        self.active += 1
        try:
            if self.wait:
                await asyncio.sleep(self.wait)
            if self.error:
                raise self.error
            return RecognitionCandidateV1(
                kind=self._capabilities.candidate_kind, status="ok" if self.result else "empty", text=self.result,
                provider_route_id=self._capabilities.route_id,
                input_tokens=10 if self.known_usage else None, output_tokens=1 if self.known_usage else None,
            )
        finally:
            self.active -= 1


async def setup(data, engine, **policy):
    index = await read_pdf_evidence(data, PdfIndexRequest())
    source = RecognitionSourceRefV1(owner_id="owner", scope="assignment_source", business_id="task",
                                    content_type="application/pdf", input_sha256=hashlib.sha256(data).hexdigest())
    plan = plan_recognition(
        RecognitionPlanRequestV1(purpose="problems", scope="document", total_pages=index.total_pages,
                                 requested_pages=[p.page_number for p in index.pages],
                                 observations=[p.observation for p in index.pages]),
        engine=engine.capabilities if engine else None,
        policy=RecognitionPolicyV1(force_visual=engine is not None, **policy),
    )
    return {"authorized_owner_id": "owner", "source": source, "plan": plan, "engine": engine,
            "capacity": RecognitionCapacity()}


@pytest.mark.asyncio
async def test_clean_native_is_zero_call_and_preserved_verbatim():
    data = pdf()
    result = await read_pdf_plan(data, **await setup(data, None))
    assert result.native_only_pages == [1, 2, 3]
    assert result.units == [] and not result.unprocessed_pages
    assert all(page.native_text == "A literal source paragraph.\n" for page in result.pages)
    assert result.usage.total_calls == 0 and result.usage.usage_complete
    assert result.recognition_complete is False


@pytest.mark.asyncio
async def test_native_and_visual_kept_separately_with_exact_mapping_and_progress():
    data = pdf(2, "x = -2")
    engine = FakeEngine(result="x = -3")
    progress = ProgressReporter("fixture")
    await progress.increment_stage_metrics(existing_counter=7)
    result = await read_pdf_plan(data, progress=progress, **await setup(data, engine))
    assert [unit.page_numbers for unit in result.units] == [[1], [2]]
    assert all("-2" in page.native_text for page in result.pages)
    assert all(unit.candidate.text == "x = -3" for unit in result.units)
    assert result.usage.initial_calls == 2 and result.usage.input_tokens == 20
    assert not result.failed_pages and not result.unprocessed_pages
    assert progress._progress.stage_metrics["existing_counter"] == 7
    assert progress._progress.stage_metrics["recognition_initial_calls"] == 2
    assert '"payload":' not in result.model_dump_json()
    assert '"payload_b64":' not in result.model_dump_json()
    assert RecognitionReadBatchV1.model_validate_json(result.model_dump_json()) == result


@pytest.mark.asyncio
async def test_document_group_is_one_call_and_does_not_invent_page_output_alignment():
    data = pdf(3)
    engine = FakeEngine(document=True, result="Entire document", known_usage=False)
    result = await read_pdf_plan(data, **await setup(data, engine))
    assert len(engine.requests) == 1
    request = engine.requests[0]
    assert request.document_pages == [1, 2, 3] and request.content_type == "application/pdf"
    with fitz.open(stream=request.payload, filetype="pdf") as exported:
        assert len(exported) == 3
    assert result.units[0].output_mapping == "document_only"
    assert result.usage.total_calls == 1 and not result.usage.usage_complete
    assert result.usage.output_tokens is None


@pytest.mark.asyncio
async def test_document_twenty_four_pages_are_one_planned_call_not_twenty_four():
    data = pdf(24)
    engine = FakeEngine(document=True, known_usage=False)
    kwargs = await setup(data, engine, max_initial_calls=1)
    assert kwargs["plan"].initial_calls == 1
    assert [page.initial_calls for page in kwargs["plan"].decisions] == [1] + [0] * 23
    result = await read_pdf_plan(data, **kwargs)
    assert len(engine.requests) == 1 and not result.unprocessed_pages
    assert engine.requests[0].document_pages == list(range(1, 25))


@pytest.mark.asyncio
async def test_document_group_limit_is_an_explicit_extra_call_not_silent_fanout():
    data = pdf(5)
    engine = FakeEngine(document=True, known_usage=False)
    engine._capabilities.max_document_pages = 2
    kwargs = await setup(data, engine, max_initial_calls=2)
    assert kwargs["plan"].initial_calls == 2
    result = await read_pdf_plan(data, **kwargs)
    assert [unit.page_numbers for unit in result.units] == [[1, 2], [3, 4]]
    assert result.unprocessed_pages == [5]


@pytest.mark.asyncio
async def test_mixed_mode_keeps_noncontiguous_frozen_document_group():
    from backend.recognition.planner import RecognitionPlanV1

    data = pdf(3)
    engine = FakeEngine(document=True, known_usage=False)
    kwargs = await setup(data, engine)
    plan = kwargs["plan"].model_dump()
    plan["decisions"][1].update(input_mode="page_image", document_call_group=None, initial_calls=1)
    plan["initial_calls"] = 2
    kwargs["plan"] = RecognitionPlanV1.model_validate(plan)
    result = await read_pdf_plan(data, **kwargs)
    assert [(unit.input_mode, unit.page_numbers) for unit in result.units] == [("document", [1, 3]), ("page_image", [2])]
    assert len(engine.requests) == 2 and not result.unprocessed_regions and not result.unprocessed_pages


@pytest.mark.asyncio
async def test_empty_visual_never_erases_native_and_never_retries():
    data = pdf(1)
    engine = FakeEngine(result="")
    result = await read_pdf_plan(data, **await setup(data, engine))
    assert result.units[0].candidate.status == "empty"
    assert result.pages[0].native_text == "A literal source paragraph.\n"
    assert len(engine.requests) == 1 and not result.recognition_complete


@pytest.mark.asyncio
@pytest.mark.parametrize("error,code,uncertain", [
    (RecognitionError("provider_auth_failed"), "provider_auth_failed", False),
    (RecognitionError("provider_submit_uncertain", submission_may_exist=True), "provider_submit_uncertain", True),
    (RuntimeError("PRIVATE_BODY KEY"), "recognition_response_invalid", True),
])
async def test_failed_call_stops_remaining_pages_without_replay(error, code, uncertain):
    data = pdf()
    engine = FakeEngine(error=error)
    result = await read_pdf_plan(data, **await setup(data, engine))
    assert len(engine.requests) == 1
    assert result.units[0].candidate.safe_error_code == code
    assert result.units[0].submission_may_exist is uncertain
    assert result.failed_pages == [1] and result.unprocessed_pages == [2, 3]
    assert "PRIVATE_BODY" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_call_timeout_is_uncertain_and_capacity_is_released():
    data = pdf(2)
    engine = FakeEngine(wait=10)
    kwargs = await setup(data, engine, per_call_seconds=0.02)
    result = await read_pdf_plan(data, **kwargs)
    assert result.units[0].candidate.safe_error_code == "provider_submit_uncertain"
    assert result.unprocessed_pages == [2] and engine.active == 0
    assert kwargs["capacity"]._owners == {}


@pytest.mark.asyncio
async def test_cancelled_provider_call_propagates_and_releases_capacity():
    data = pdf(1)
    engine = FakeEngine(wait=10)
    kwargs = await setup(data, engine)
    task = asyncio.create_task(read_pdf_plan(data, **kwargs))
    for _ in range(500):
        if engine.active:
            break
        await asyncio.sleep(0.01)
    assert engine.active == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine.active == 0 and kwargs["capacity"]._owners == {}
    assert len(engine.requests) == 1


@pytest.mark.asyncio
async def test_unknown_output_usage_reserves_request_budget_and_leaves_rest_unprocessed():
    data = pdf(3)
    engine = FakeEngine(known_usage=False)
    result = await read_pdf_plan(data, **await setup(data, engine, max_output_tokens=4100))
    assert [request.max_output_tokens for request in engine.requests] == [4096, 4]
    assert result.unprocessed_pages == [3]
    assert result.stop_codes == ["recognition_budget_exhausted"]
    assert result.usage.output_tokens is None and not result.usage.usage_complete


@pytest.mark.asyncio
async def test_initial_call_budget_preserves_tail_not_fake_complete():
    data = pdf(3)
    engine = FakeEngine()
    result = await read_pdf_plan(data, **await setup(data, engine, max_initial_calls=1))
    assert len(engine.requests) == 1 and result.unprocessed_pages == [2, 3]
    assert not result.recognition_complete


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "hash", "route", "mutated_budget"])
async def test_scope_and_route_guards_before_paid_call(change):
    data = pdf(1)
    engine = FakeEngine()
    kwargs = await setup(data, engine)
    if change == "owner":
        kwargs["authorized_owner_id"] = "other-owner"
    elif change == "hash":
        data += b"changed"
    elif change == "route":
        engine._capabilities.fingerprint = "changed"
    else:
        kwargs["plan"].initial_calls = 999
    with pytest.raises(RecognitionError):
        await read_pdf_plan(data, **kwargs)
    assert not engine.requests


@pytest.mark.asyncio
async def test_serialized_plan_cannot_duplicate_a_region_to_repeat_billing():
    data = pdf(1)
    engine = FakeEngine()
    kwargs = await setup(data, engine)
    decision = kwargs["plan"].decisions[0]
    decision.regions.append(decision.regions[0].model_copy())
    decision.initial_calls = 2
    kwargs["plan"].initial_calls = 2
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        await read_pdf_plan(data, **kwargs)
    assert not engine.requests


@pytest.mark.asyncio
async def test_native_plan_must_agree_with_actual_visual_risks():
    data = pdf(1, "x = -2")
    kwargs = await setup(data, None)
    decision = kwargs["plan"].decisions[0]
    decision.action = "native"
    decision.reason_codes = ["clean_native_text"]
    result = await read_pdf_plan(data, **kwargs)
    assert result.stop_codes == ["recognition_plan_changed"]
    assert result.unprocessed_pages == [1] and result.units == []


@pytest.mark.asyncio
async def test_capacity_is_shared_by_owner_and_cleans_cancelled_waiters():
    capacity = RecognitionCapacity(global_limit=3, owner_limit=2)
    release = asyncio.Event()
    active = []

    async def lease(owner):
        async with capacity.lease(owner):
            active.append(owner)
            try:
                await release.wait()
            finally:
                active.remove(owner)

    tasks = [asyncio.create_task(lease("same-owner")) for _ in range(3)]
    tasks.append(asyncio.create_task(lease("other-owner")))
    await asyncio.sleep(0)
    assert active.count("same-owner") == 2 and active.count("other-owner") == 1
    tasks[2].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[2]
    release.set()
    await asyncio.gather(tasks[0], tasks[1], tasks[3])
    assert not capacity._owners


@pytest.mark.asyncio
async def test_partial_region_budget_stop_retains_pending_geometry():
    from backend.recognition.models import NormalizedRegionV1
    from backend.recognition.planner import PageObservationV1

    data = pdf(1, "x = -2")
    engine = FakeEngine(known_usage=False)
    kwargs = await setup(data, engine)
    kwargs["plan"] = plan_recognition(
        RecognitionPlanRequestV1(purpose="problems", scope="pages", total_pages=1, requested_pages=[1], observations=[
            PageObservationV1(page_number=1, native_char_count=7, native_quality="clean", risks=["math"],
                              regions_cover_all_risks=True,
                              regions=[NormalizedRegionV1(x1=0.5), NormalizedRegionV1(x0=0.5)]),
        ]), engine=engine.capabilities, policy=RecognitionPolicyV1(max_output_tokens=4096),
    )
    result = await read_pdf_plan(data, **kwargs)
    assert result.partial_pages == [1] and len(result.units) == 1
    assert result.unprocessed_regions[0].region == NormalizedRegionV1(x0=0.5)
    assert result.stop_codes == ["recognition_budget_exhausted"]
    assert RecognitionReadBatchV1.model_validate_json(result.model_dump_json()).partial_pages == [1]
