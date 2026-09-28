import asyncio
import hashlib
from io import BytesIO

from PIL import Image
import pytest
from pydantic import ValidationError

from backend.domain.errors import RecognitionError
from backend.progress.tracker import ProgressReporter
from backend.recognition.budget import RecognitionBudget
from backend.recognition.engine import EngineReadInputV1
from backend.recognition.executor import RecognitionReadBatchV1
from backend.recognition.image_executor import read_image_plan
from backend.recognition.models import (
    NormalizedRegionV1, RecognitionCandidateV1, RecognitionPolicyV1, RecognitionSourceRefV1,
)
from backend.recognition.planner import (
    EngineCapabilitiesV1, PageObservationV1, RecognitionPlanRequestV1, RecognitionPlanV1, plan_recognition,
)
from backend.recognition.runtime import RecognitionCapacity, region_budget_key, run_initial_read


class ImageEngine:
    def __init__(self, *, ocr=False, known_usage=True, result="  x = -3\n~~x = 2~~\n", error=None):
        self.caps = EngineCapabilitiesV1(
            route_id="selected-route", fingerprint="frozen", visual_inputs=["page_image", "document"] if ocr else ["page_image"],
            region_reads=True, candidate_kind="ocr" if ocr else "vision", document_batching=ocr,
            max_document_pages=24 if ocr else 1, bounded_output_tokens=not ocr,
        )
        self.requests = []
        self.known_usage, self.result, self.error = known_usage, result, error

    @property
    def capabilities(self):
        return self.caps.model_copy(deep=True)

    async def recognize(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return RecognitionCandidateV1(
            kind=self.caps.candidate_kind, status="ok" if self.result else "empty", text=self.result,
            provider_route_id=self.caps.route_id, input_tokens=8 if self.known_usage else None,
            output_tokens=3 if self.known_usage else None,
        )


def image_data(*, orientation=1, format="PNG", size=(7, 11)):
    image = Image.new("RGB", size, "white")
    image.putpixel((0, 0), (255, 0, 0))
    exif = Image.Exif()
    exif[274] = orientation
    output = BytesIO()
    image.save(output, format, exif=exif)
    return output.getvalue()


def setup(data, engine, *, purpose="problems", content_type="image/png", regions=None, **policy):
    source = RecognitionSourceRefV1(owner_id="owner", scope="assignment_source", business_id="task",
                                   content_type=content_type, input_sha256=hashlib.sha256(data).hexdigest())
    plan = plan_recognition(
        RecognitionPlanRequestV1(purpose=purpose, source_kind="image", scope="document", total_pages=1,
                                 requested_pages=[1], observations=[PageObservationV1(page_number=1)]),
        engine=engine.capabilities if engine else None, policy=RecognitionPolicyV1(**policy),
    )
    if regions:
        raw = plan.model_dump()
        raw["decisions"][0]["regions"] = [region.model_dump() for region in regions]
        raw["decisions"][0]["initial_calls"] = raw["initial_calls"] = len(regions)
        plan = RecognitionPlanV1.model_validate(raw)
    return dict(authorized_owner_id="owner", source=source, plan=plan, engine=engine, capacity=RecognitionCapacity())


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["problems", "reference", "submissions", "rubric", "test_cases", "knowledge"])
async def test_every_purpose_keeps_literal_candidate_and_pixel_evidence(purpose):
    data, engine = image_data(), ImageEngine()
    progress = ProgressReporter("fixture")
    await progress.increment_stage_metrics(existing_counter=9)
    result = await read_image_plan(data, **setup(data, engine, purpose=purpose), progress=progress)
    assert len(engine.requests) == 1 and engine.requests[0].purpose == purpose
    assert result.units[0].candidate.text == engine.result
    assert result.pages[0].geometry_unit == "pixels" and result.pages[0].native_text == ""
    assert (result.pages[0].width_pixels, result.pages[0].height_pixels) == (7, 11)
    assert not result.native_only_pages and not result.unprocessed_pages and not result.recognition_complete
    assert progress._progress.stage_metrics["existing_counter"] == 9
    assert progress._progress.stage_metrics["recognition_initial_calls"] == 1
    serialized = result.model_dump_json()
    assert '"payload"' not in serialized and 'payload_b64' not in serialized and 'width_points' not in serialized
    assert RecognitionReadBatchV1.model_validate_json(serialized) == result


@pytest.mark.asyncio
@pytest.mark.parametrize("orientation", range(1, 9))
async def test_oriented_crop_retains_requested_and_actual_pixel_geometry(orientation):
    data, engine = image_data(orientation=orientation), ImageEngine()
    region = NormalizedRegionV1(x0=0.13, y0=0.21, x1=0.67, y1=0.79)
    result = await read_image_plan(data, **setup(data, engine, regions=[region]))
    unit = result.units[0]
    metadata = unit.image_preparation
    assert metadata.exif_orientation == orientation and metadata.source_sha256 == hashlib.sha256(data).hexdigest()
    assert unit.region == region and metadata.effective_region != region.as_tuple()
    assert engine.requests[0].region.as_tuple() == metadata.effective_region
    with Image.open(BytesIO(engine.requests[0].payload)) as decoded:
        assert decoded.size == (metadata.width, metadata.height)
        assert not decoded.getexif()
    expected = (11, 7) if orientation >= 5 else (7, 11)
    assert (result.pages[0].width_pixels, result.pages[0].height_pixels) == expected


@pytest.mark.asyncio
async def test_dual_input_ocr_uses_image_without_companion_or_fake_token_reserve():
    data, engine = image_data(), ImageEngine(ocr=True, known_usage=False)
    kwargs = setup(data, engine, max_output_tokens=1)
    assert kwargs["plan"].decisions[0].input_mode == "page_image"
    assert kwargs["plan"].engine_capabilities == engine.capabilities
    budget = RecognitionBudget(kwargs["source"], kwargs["plan"].policy, engine.capabilities)
    result = await read_image_plan(data, **kwargs, budget=budget)
    assert len(engine.requests) == 1 and result.units[0].candidate.kind == "ocr"
    assert engine.requests[0].content_type == "image/png"
    snapshot = budget.snapshot()
    assert snapshot.output_tokens is None and snapshot.charged_output_tokens is None
    assert snapshot.reserved_output_tokens == 0 and snapshot.unknown_output_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("variant,code", [
    ("malformed", "image_invalid"), ("mime", "image_format_mismatch"),
    ("oversize", "image_input_too_large"),
])
async def test_invalid_images_never_dispatch(variant, code):
    data = b"not an image" if variant == "malformed" else (b"x" * (10 * 1024 * 1024 + 1) if variant == "oversize" else image_data())
    engine = ImageEngine()
    kwargs = setup(data, engine, content_type="image/jpeg" if variant == "mime" else "image/png")
    result = await read_image_plan(data, **kwargs)
    assert not engine.requests and result.unprocessed_pages == [1]
    assert result.stop_codes == [code] and not result.pages


@pytest.mark.asyncio
async def test_white_pixels_and_empty_response_are_not_verified_blank():
    output = BytesIO()
    Image.new("RGB", (8, 8), "white").save(output, "PNG")
    data, engine = output.getvalue(), ImageEngine(result="")
    result = await read_image_plan(data, **setup(data, engine))
    assert result.units[0].candidate.status == "empty"
    assert not result.pages[0].observation.verified_blank and not result.recognition_complete
    assert not result.native_only_pages and len(engine.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["owner", "bytes", "route", "budget_source", "budget_policy", "pdf_plan"])
async def test_image_context_guard_is_before_engine_call(variant):
    data, engine = image_data(), ImageEngine()
    kwargs = setup(data, engine)
    budget_source = kwargs["source"].model_copy(deep=True)
    budget_policy = kwargs["plan"].policy.model_copy(deep=True)
    if variant == "owner":
        kwargs["authorized_owner_id"] = "other-owner"
    elif variant == "bytes":
        data += b"changed"
    elif variant == "route":
        engine.caps.fingerprint = "changed"
    elif variant == "budget_source":
        budget_source.business_id = "other-task"
    elif variant == "budget_policy":
        budget_policy.max_calls -= 1
    else:
        kwargs["plan"].source_kind = "pdf"
    budget = RecognitionBudget(budget_source, budget_policy, kwargs["plan"].engine_capabilities)
    with pytest.raises(RecognitionError):
        await read_image_plan(data, **kwargs, budget=budget)
    assert not engine.requests


@pytest.mark.parametrize("observation", [
    PageObservationV1(page_number=1, native_char_count=1, native_quality="clean"),
    PageObservationV1(page_number=1, verified_blank=True),
])
def test_image_plan_cannot_invent_native_or_blank_observation(observation):
    with pytest.raises(ValidationError):
        RecognitionPlanRequestV1(purpose="knowledge", source_kind="image", scope="document", total_pages=1,
                                 requested_pages=[1], observations=[observation])


@pytest.mark.parametrize("mode", ["native", "blank", "document"])
def test_image_serialized_plan_cannot_invent_pdf_modes(mode):
    kwargs = setup(image_data(), ImageEngine(ocr=True))
    raw = kwargs["plan"].model_dump()
    if mode == "document":
        raw["decisions"][0].update(input_mode="document", document_call_group="d0000")
    else:
        raw["decisions"][0].update(action=mode, input_mode=None, regions=[], initial_calls=0)
        raw["initial_calls"] = 0
    with pytest.raises(ValidationError):
        RecognitionPlanV1.model_validate(raw)


@pytest.mark.asyncio
async def test_locator_and_partial_reader_share_one_output_budget():
    data, engine = image_data(), ImageEngine(known_usage=False)
    regions = [NormalizedRegionV1(x1=0.5), NormalizedRegionV1(x0=0.5)]
    kwargs = setup(data, engine, regions=regions, max_output_tokens=4100)
    budget = RecognitionBudget(kwargs["source"], kwargs["plan"].policy, engine.capabilities)
    ticket = budget.reserve("locator", max_output_tokens=4090)
    budget.settle(ticket, input_tokens=None, output_tokens=None, outcome="ok")
    result = await read_image_plan(data, **kwargs, budget=budget)
    assert [request.max_output_tokens for request in engine.requests] == [10]
    assert result.partial_pages == [1] and result.unprocessed_regions[0].region == regions[1]
    assert result.stop_codes == ["recognition_budget_exhausted"]
    assert budget.snapshot().total_calls == 2 and result.usage.total_calls == 1
    assert budget.snapshot().charged_output_tokens == 4100


@pytest.mark.asyncio
async def test_expired_workflow_does_not_get_new_deadline_at_reader_entry():
    data, engine, now = image_data(), ImageEngine(), [0.0]
    kwargs = setup(data, engine, total_seconds=4)
    budget = RecognitionBudget(kwargs["source"], kwargs["plan"].policy, engine.capabilities, clock=lambda: now[0])
    budget.start_phase("locator")
    now[0] = 5.0
    result = await read_image_plan(data, **kwargs, budget=budget)
    assert not engine.requests and result.stop_codes == ["recognition_timeout"]
    assert result.unprocessed_pages == [1]


def runtime_setup(engine, **policy):
    kwargs = setup(image_data(), engine, **policy)
    request = EngineReadInputV1(purpose="submissions", input_mode="page_image", page_number=1,
                               content_type="image/png", payload=b"already-inspected-by-caller", max_output_tokens=16)
    budget = RecognitionBudget(kwargs["source"], kwargs["plan"].policy, engine.capabilities)
    return request, dict(source=kwargs["source"], policy=kwargs["plan"].policy, capabilities=engine.capabilities,
                         region_keys=(region_budget_key(1, request.region),), budget=budget, capacity=kwargs["capacity"])


class WaitingEngine(ImageEngine):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()

    async def recognize(self, request):
        self.requests.append(request)
        self.entered.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_ambiguous_dispatch_keeps_reservation_and_cannot_replay(cancel):
    engine = WaitingEngine()
    request, kwargs = runtime_setup(engine, per_call_seconds=0.02 if not cancel else 120)
    task = asyncio.create_task(run_initial_read(engine, request, **kwargs))
    await engine.entered.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result.submission_may_exist and result.candidate.safe_error_code == "provider_submit_uncertain"
    snapshot = kwargs["budget"].snapshot()
    assert snapshot.pending_calls == 1 and snapshot.reserved_output_tokens == 16
    assert not kwargs["capacity"]._owners
    with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
        await run_initial_read(engine, request, **kwargs)
    assert len(engine.requests) == 1


@pytest.mark.asyncio
async def test_waiting_for_capacity_never_reserves_or_dispatches():
    engine = ImageEngine()
    request, kwargs = runtime_setup(engine, read_seconds=0.02)
    kwargs["capacity"] = RecognitionCapacity(global_limit=1)
    async with kwargs["capacity"].lease("other-owner"):
        with pytest.raises(RecognitionError, match="recognition_timeout"):
            await run_initial_read(engine, request, **kwargs)
    assert kwargs["budget"].snapshot().total_calls == 0 and not engine.requests
    assert not kwargs["capacity"]._owners


@pytest.mark.asyncio
async def test_explicit_rejection_is_settled_but_never_eligible_for_empty_recovery():
    engine = ImageEngine(error=RecognitionError("provider_auth_failed"))
    engine.caps.response_recheck = True
    request, kwargs = runtime_setup(engine)
    result = await run_initial_read(engine, request, **kwargs)
    assert not result.submission_may_exist and result.candidate.safe_error_code == "provider_auth_failed"
    assert kwargs["budget"].snapshot().settled_calls == 1
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        kwargs["budget"].reserve("empty_recovery", max_output_tokens=16, region_keys=kwargs["region_keys"])


@pytest.mark.asyncio
async def test_engine_output_overrun_is_retained_and_blocks_next_call():
    engine = ImageEngine()
    request, kwargs = runtime_setup(engine)
    request.max_output_tokens = 1
    result = await run_initial_read(engine, request, **kwargs)
    assert result.candidate.output_tokens == 3
    snapshot = kwargs["budget"].snapshot()
    assert snapshot.output_tokens == 3 and snapshot.output_limit_exceeded
    kwargs["region_keys"] = ("different-region",)
    with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
        await run_initial_read(engine, request, **kwargs)
    assert len(engine.requests) == 1


def test_region_identity_normalizes_int_float_and_negative_zero():
    a = NormalizedRegionV1(x0=0, y0=0)
    b = NormalizedRegionV1(x0=-0.0, y0=0.0)
    assert region_budget_key(1, a) == region_budget_key(1, b)
    assert region_budget_key(1, a) != region_budget_key(2, a)


@pytest.mark.asyncio
async def test_unbounded_ocr_can_use_twelve_regions_without_fictional_token_exhaustion():
    data, engine = image_data(size=(120, 80)), ImageEngine(ocr=True, known_usage=False)
    regions = [NormalizedRegionV1(x0=i / 12, x1=(i + 1) / 12) for i in range(12)]
    kwargs = setup(data, engine, regions=regions, max_output_tokens=1)
    budget = RecognitionBudget(kwargs["source"], kwargs["plan"].policy, engine.capabilities)
    result = await read_image_plan(data, **kwargs, budget=budget)
    assert len(engine.requests) == 12 and not result.stop_codes and not result.unprocessed_regions
    assert budget.snapshot().initial_calls == 12 and budget.snapshot().output_tokens is None


@pytest.mark.asyncio
async def test_document_only_route_is_explicitly_blocked_for_image():
    data, engine = image_data(), ImageEngine(ocr=True)
    engine.caps.visual_inputs = ["document"]
    engine.caps.region_reads = False
    kwargs = setup(data, engine)
    assert kwargs["plan"].decisions[0].action == "blocked"
    result = await read_image_plan(data, **kwargs)
    assert result.unprocessed_pages == [1] and not engine.requests


@pytest.mark.asyncio
async def test_image_provenance_cannot_be_reassigned_to_another_source():
    data, engine = image_data(), ImageEngine()
    result = await read_image_plan(data, **setup(data, engine))
    raw = result.model_dump()
    raw["source"]["input_sha256"] = "b" * 64
    with pytest.raises(ValidationError):
        RecognitionReadBatchV1.model_validate(raw)


@pytest.mark.asyncio
async def test_engine_mutating_aliased_capabilities_cannot_bypass_frozen_route():
    class AliasingEngine(ImageEngine):
        @property
        def capabilities(self):
            return self.caps

        async def recognize(self, request):
            result = await super().recognize(request)
            self.caps.fingerprint = "different-route-configuration"
            return result

    engine = AliasingEngine()
    request, kwargs = runtime_setup(engine)
    result = await run_initial_read(engine, request, **kwargs)
    assert result.submission_may_exist and result.candidate.safe_error_code == "recognition_route_changed"
    assert kwargs["budget"].snapshot().pending_calls == 1
    assert kwargs["budget"].snapshot().settled_calls == 0


@pytest.mark.asyncio
async def test_route_change_while_queued_never_dispatches_or_reserves():
    engine = ImageEngine()
    request, kwargs = runtime_setup(engine)
    kwargs["capacity"] = RecognitionCapacity(global_limit=1)
    async with kwargs["capacity"].lease("other-owner"):
        task = asyncio.create_task(run_initial_read(engine, request, **kwargs))
        await asyncio.sleep(0)
        engine.caps.fingerprint = "changed-while-queued"
    with pytest.raises(RecognitionError, match="recognition_route_changed"):
        await task
    assert not engine.requests and kwargs["budget"].snapshot().total_calls == 0


@pytest.mark.asyncio
async def test_request_snapshot_is_unchanged_by_caller_while_waiting():
    engine = ImageEngine()
    request, kwargs = runtime_setup(engine)
    kwargs["capacity"] = RecognitionCapacity(global_limit=1)
    async with kwargs["capacity"].lease("other-owner"):
        task = asyncio.create_task(run_initial_read(engine, request, **kwargs))
        await asyncio.sleep(0)
        request.purpose = "problems"
        request.payload = b"different-image"
        kwargs["source"].business_id = "different-task"
    result = await task
    assert result.candidate.status == "ok"
    assert engine.requests[0].purpose == "submissions"
    assert engine.requests[0].payload == b"already-inspected-by-caller"
