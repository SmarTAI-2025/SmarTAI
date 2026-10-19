import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from backend.agents.recognition_agent import RecognitionAgent, RecognitionReadRequestV1
from backend.domain.errors import RecognitionError
from backend.progress.tracker import ProgressReporter
from backend.recognition.budget import RecognitionBudget
from backend.recognition.cache_identity import final_cache_identity
from backend.recognition.engine import EngineReadInputV1
from backend.recognition.models import RecognitionCandidateV1, RecognitionPolicyV1, RecognitionSourceRefV1, RecognitionUsageV1
from backend.recognition.planner import EngineCapabilitiesV1
from backend.recognition.runtime import RecognitionCapacity, run_initial_read
from backend.services.question_source_batches import read_question_pdf_batches
from backend.services.recognition_timeouts import SOURCE_TIMEOUT_PROFILE, apply_source_timeout_profile


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


class VisionEngine:
    def __init__(self, *, clock=None, **capabilities):
        self.clock = clock
        self.reads = []
        self._caps = EngineCapabilitiesV1(**{
            "route_id": "test-vision", "fingerprint": "test-profile",
            "visual_inputs": ["page_image"], "region_reads": True,
            "semantic_repair": True, "response_recheck": True,
            "target_location": True, "bounded_output_tokens": True,
            **capabilities,
        })

    @property
    def capabilities(self):
        return self._caps.model_copy(deep=True)

    async def recognize(self, request):
        self.reads.append(request.page_number)
        if self.clock is not None:
            self.clock.now += 125
        return RecognitionCandidateV1(kind="vision", status="ok", provider_route_id=self._caps.route_id,
                                       text=f"Question {request.page_number}", input_tokens=10, output_tokens=2)


def read_request(data=b"test-pdf", **changes):
    source = RecognitionSourceRefV1(
        owner_id="test-owner", scope="assignment_source", business_id="test-task", stored_file_id="test-file",
        original_name="test.pdf", content_type="application/pdf", input_sha256=hashlib.sha256(data).hexdigest(),
    )
    return RecognitionReadRequestV1(**{"source": source, "purpose": "problems", **changes})


def apply(request, engine, **kwargs):
    return apply_source_timeout_profile(request, engine=engine, timeout_profile=SOURCE_TIMEOUT_PROFILE, **kwargs)


def test_missing_profile_preserves_legacy_defaults_serialization_and_identity():
    request, engine = read_request(), VisionEngine()
    assert request.policy.model_dump(mode="json") == {
        "version": "faithful-v2", "max_locator_calls": 12, "locator_seconds": 360, "read_seconds": 600,
        "max_detail_pages": 24, "max_regions": 24, "max_initial_calls": 12, "max_calls": 18,
        "max_empty_recoveries": 2, "max_patches": 4, "per_call_seconds": 120, "total_seconds": 600,
        "max_output_tokens": 32768, "enable_repair": True, "force_visual": False,
    }
    original_bytes = request.model_dump_json()
    original_identity = final_cache_identity(request, capabilities=engine.capabilities, prompt_version="faithful-reader-v1")
    selected = apply_source_timeout_profile(request, engine=engine, timeout_profile=None)
    assert selected is request and selected.model_dump_json() == original_bytes
    assert final_cache_identity(selected, capabilities=engine.capabilities,
                                prompt_version="faithful-reader-v1") == original_identity


@pytest.mark.parametrize("profile", ["", "source-300-v2", 300])
def test_unknown_profile_fails_closed_even_without_a_model(profile):
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        apply_source_timeout_profile(read_request(), engine=None, timeout_profile=profile)


@pytest.mark.parametrize("kind", ["native", "baidu", "knowledge"])
def test_profile_does_not_change_native_baidu_or_knowledge_policies(kind):
    request, engine = read_request(), VisionEngine()
    if kind == "native":
        engine = None
    elif kind == "baidu":
        engine = SimpleNamespace(capabilities=EngineCapabilitiesV1(
            route_id="test-baidu", fingerprint="test-ocr", visual_inputs=["document", "page_image"],
            candidate_kind="ocr", document_batching=True,
        ))
    else:
        request = read_request(purpose="knowledge", policy=RecognitionPolicyV1(
            version="knowledge-economy-v1", total_seconds=300, read_seconds=280,
        ))
    assert apply(request, engine) is request


def test_source_budget_covers_legal_crop_calls_and_preserves_non_time_limits():
    request = read_request(scope="pages", pages=[1])
    selected = apply(request, VisionEngine(), total_pages=1)
    # A single PDF page can have several crop reads; reserve all 12 legal
    # initial calls and six bounded rechecks rather than counting one page.
    assert selected.policy.per_call_seconds == 300
    assert selected.policy.read_seconds == 18 * 300 + 600
    assert selected.policy.total_seconds == selected.policy.read_seconds + 60
    assert selected.policy.locator_seconds == 360
    assert request.policy.per_call_seconds == 120  # The caller's request is not mutated.
    time_fields = {"per_call_seconds", "read_seconds", "locator_seconds", "total_seconds"}
    assert {key: value for key, value in selected.policy.model_dump().items() if key not in time_fields} == {
        key: value for key, value in request.policy.model_dump().items() if key not in time_fields
    }


@pytest.mark.parametrize("policy,caps,read_calls", [
    (RecognitionPolicyV1(max_initial_calls=3, max_calls=4), {}, 4),
    (RecognitionPolicyV1(enable_repair=False), {}, 12),
    (RecognitionPolicyV1(), {"response_recheck": False}, 12),
    (RecognitionPolicyV1(), {"semantic_repair": False}, 12),
])
def test_budget_respects_existing_call_and_repair_limits(policy, caps, read_calls):
    selected = apply(read_request(policy=policy), VisionEngine(**caps))
    assert selected.policy.read_seconds == read_calls * 300 + 600


@pytest.mark.parametrize("start,window,total,images,expected_calls", [
    (1, 500, 10, 1, 2),
    (9, 500, 10, 1, 1),
    (1, 8, 100, 1, 1),
    (1, 500, 20, 2, 2),
    (1, 500, None, 1, 12),
])
def test_locator_budget_uses_search_window_known_pages_and_contact_sheet_capacity(start, window, total, images, expected_calls):
    request = read_request(scope="targets", targets=["1.1"], search_start_page=start, search_window_pages=window)
    selected = apply(request, VisionEngine(max_locator_images=images), total_pages=total)
    assert selected.policy.locator_seconds == expected_calls * 300 + 360
    assert selected.policy.total_seconds == selected.policy.read_seconds + selected.policy.locator_seconds + 60


@pytest.mark.asyncio
async def test_twelve_125_second_pages_complete_with_the_new_phase_and_total_budgets(monkeypatch):
    import pymupdf
    from backend.recognition import runtime

    with pymupdf.open() as document:
        for page in range(1, 13):
            document.new_page().insert_text((72, 72), f"Exercise {page}. Preserve the complete written argument.")
        data = document.tobytes()
    clock = Clock()
    engine = VisionEngine(clock=clock)
    request = apply(read_request(data, policy=RecognitionPolicyV1(force_visual=True)), engine, total_pages=12)
    deadlines = []
    original_timeout = asyncio.timeout

    def observe_timeout(seconds):
        deadlines.append(seconds)
        return original_timeout(seconds)

    monkeypatch.setattr(runtime.asyncio, "timeout", observe_timeout)
    raw = await RecognitionAgent(engine, capacity=RecognitionCapacity(), clock=clock).read(
        request, data, authorized_owner_id=request.source.owner_id,
    )
    assert engine.reads == list(range(1, 13))
    assert raw.stop_codes == [] and raw.read_batch.unprocessed_pages == []
    assert raw.budget.initial_calls == 12 and raw.budget.duration_ms == 1_500_000
    assert raw.budget.read_remaining_seconds > 0 and raw.budget.global_remaining_seconds > 0
    assert deadlines.count(300) == 12


@pytest.mark.asyncio
async def test_existing_explicit_short_call_and_phase_deadlines_still_apply():
    request, engine, clock = read_request(policy=RecognitionPolicyV1(
        per_call_seconds=0.01, read_seconds=1, total_seconds=2,
    )), VisionEngine(), Clock()
    selected = apply_source_timeout_profile(request, engine=engine, timeout_profile=None)
    assert selected.policy is request.policy
    budget = RecognitionBudget(selected.source, selected.policy, engine.capabilities, clock=clock)
    calls = []

    async def blocked(unit):
        calls.append(unit.page_number)
        await asyncio.Event().wait()

    engine.recognize = blocked
    result = await run_initial_read(
        engine, EngineReadInputV1(purpose="problems", input_mode="page_image", page_number=1,
                                 content_type="image/png", payload=b"image"),
        source=selected.source, policy=selected.policy, capabilities=engine.capabilities,
        region_keys=("p1",), budget=budget, capacity=RecognitionCapacity(),
    )
    assert calls == [1] and result.submission_may_exist
    assert result.candidate.safe_error_code == "provider_submit_uncertain"
    clock.now = 1
    with pytest.raises(RecognitionError, match="recognition_timeout"):
        budget.remaining("read")
    assert budget.remaining() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,per_call,total", [(None, 120, 600), (SOURCE_TIMEOUT_PROFILE, 300, 6060)])
async def test_each_pdf_batch_gets_the_selected_frozen_policy(profile, per_call, total):
    seen = []

    async def run(request, *_args, **_kwargs):
        seen.append(request)
        document = SimpleNamespace(
            pages=[SimpleNamespace(page_number=page, spans=[SimpleNamespace(final_text=str(page))]) for page in request.pages],
            coverage=SimpleNamespace(processed_pages=request.pages, unprocessed_pages=[], failed_pages=[]),
            confidence="high", confidence_reasons=[], warning_codes=[],
        )
        return SimpleNamespace(status="completed", operation_id=f"batch-{request.pages[0]}",
                               assembly=SimpleNamespace(document=document), safe_error_code=None, artifact_ids=[],
                               current_usage=RecognitionUsageV1(), operation_usage=RecognitionUsageV1())

    original = read_request()
    _, summary = await read_question_pdf_batches(
        service=SimpleNamespace(run=run), request=original, content=b"test-pdf", engine=VisionEngine(),
        total_pages=20, reporter=ProgressReporter("timeout-batches"), timeout_profile=profile,
    )
    assert [request.pages for request in seen] == [list(range(1, 13)), list(range(13, 21))]
    assert all(request.policy.per_call_seconds == per_call and request.policy.total_seconds == total for request in seen)
    assert original.policy.per_call_seconds == 120
    assert summary["coverage"]["processed_pages"] == list(range(1, 21))
