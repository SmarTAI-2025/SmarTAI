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
from backend.services.execution_control import current_execution, run_controlled
from backend.services.question_source_batches import read_question_pdf_batches
from backend.services.recognition_timeouts import LEGACY_SOURCE_TIMEOUT_PROFILE, apply_source_timeout_profile


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
    return apply_source_timeout_profile(request, engine=engine, timeout_profile=LEGACY_SOURCE_TIMEOUT_PROFILE, **kwargs)


@pytest.mark.parametrize("profile", [None, LEGACY_SOURCE_TIMEOUT_PROFILE, "source-response-300-v1", "source-response-600-v1"])
@pytest.mark.parametrize("kind", ["vision", "ocr", "native", "knowledge"])
def test_all_product_sources_share_one_response_policy(profile, kind):
    engine = None if kind == "native" else VisionEngine(candidate_kind="ocr" if kind == "ocr" else "vision")
    request = read_request(purpose="knowledge" if kind == "knowledge" else "problems")
    before = request.model_dump_json()
    selected = apply_source_timeout_profile(request, engine=engine, timeout_profile=profile)
    assert selected.policy.per_call_seconds == 600 and selected.policy.timeout_mode == "response_only"
    ignored = {"timeout_mode", "per_call_seconds"}
    assert {k: v for k, v in selected.policy.model_dump().items() if k not in ignored} == {
        k: v for k, v in request.policy.model_dump().items() if k not in ignored}
    clock = Clock()
    budget = RecognitionBudget(selected.source, selected.policy, engine.capabilities if engine else None, clock=clock)
    clock.now = 100000
    assert budget.remaining("read") is None and budget.remaining("locator") is None
    assert request.model_dump_json() == before


@pytest.mark.asyncio
async def test_legacy_deadline_only_attributes_its_own_429_wait():
    engine = VisionEngine()
    engine.provider = SimpleNamespace(provider_id="test-provider", model="test-model")
    waiting, release = asyncio.Event(), asyncio.Event()
    async def recognize(request):
        if request.page_number == 1:
            await current_execution().waiting(engine.provider, 2, 3, 65)
            waiting.set()
            await release.wait()
        raise TimeoutError()
    engine.recognize = recognize

    async def read(page):
        request = read_request()
        return await run_initial_read(
            engine, EngineReadInputV1(purpose="problems", input_mode="page_image", page_number=page,
                                      content_type="image/png", payload=b"image"),
            source=request.source, policy=request.policy, capabilities=engine.capabilities,
            region_keys=(f"p{page}",), budget=RecognitionBudget(request.source, request.policy, engine.capabilities),
            capacity=RecognitionCapacity(),
        )

    async def job():
        first = asyncio.create_task(read(1))
        try:
            await waiting.wait()
            other = await read(2)
            assert other.submission_may_exist and other.candidate.safe_error_code == "provider_submit_uncertain"
        finally:
            release.set()
            result = await first
        assert not result.submission_may_exist and result.candidate.safe_error_code == "provider_rate_limited"
        await current_execution().check(engine.provider)  # Only one rejection; the next file may still run.
    await asyncio.wait_for(run_controlled("legacy-sibling-deadlines", lambda: False, job()), 5)




@pytest.mark.parametrize("profile", ["", "source-300-v2", 300])
def test_unknown_profile_fails_closed_even_without_a_model(profile):
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        apply_source_timeout_profile(read_request(), engine=None, timeout_profile=profile)


@pytest.mark.asyncio
async def test_twelve_slow_pages_complete_without_cumulative_ocr_cutoff():
    import pymupdf
    from backend.tests.test_recognition_response_deadlines import AdmittedEngine
    with pymupdf.open() as document:
        for page in range(1, 13):
            document.new_page().insert_text((72, 72), f"Exercise {page}. Preserve the complete written argument.")
        data = document.tobytes()
    clock = Clock()
    engine = AdmittedEngine(VisionEngine(clock=clock))
    request = apply(read_request(data, policy=RecognitionPolicyV1(force_visual=True)), engine, total_pages=12)
    raw = await RecognitionAgent(engine, capacity=RecognitionCapacity(), clock=clock).read(
        request, data, authorized_owner_id=request.source.owner_id)
    assert engine.engine.reads == list(range(1, 13))
    assert raw.stop_codes == [] and raw.read_batch.unprocessed_pages == []
    assert raw.budget.initial_calls == 12 and raw.budget.duration_ms == 1_500_000
    assert raw.budget.read_remaining_seconds is raw.budget.global_remaining_seconds is None


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,per_call,total", [(None, 600, 600), (LEGACY_SOURCE_TIMEOUT_PROFILE, 600, 600)])
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
