from types import SimpleNamespace
from unittest.mock import AsyncMock
import io

import pytest
from PIL import Image

from backend.domain.errors import RecognitionError
from backend.services import question_sources
from backend.services.stage_provider_routing import StageProviderRoute
from backend.tests.test_recognition_reader import llm
from backend.tests.test_recognition_v2_artifacts import setup
from backend.tests.test_recognition_agent import pdf


@pytest.mark.asyncio
@pytest.mark.parametrize("count,options,code", [
    (2, {"pages": [3]}, "question_source_pages_out_of_range"),
])
async def test_unsupported_pdf_scope_stops_before_provider(monkeypatch, count, options, code):
    run = AsyncMock()
    monkeypatch.setattr(question_sources.RecognitionRunService, "run", run)
    with pytest.raises(RecognitionError) as caught:
        await question_sources.read_question_source(
            owner_id="owner", task_id="task", content=pdf(["Exercise 1. Explain A."] * count),
            filename="exercises.pdf", options=options,
            route=StageProviderRoute(route_id="ocr:unused", kind="ocr", credential_id="never-read"), registry=None,
        )
    assert caught.value.code == code
    run.assert_not_awaited()


def test_scope_hint_is_bounded_and_does_not_treat_decimal_as_question():
    result = question_sources.question_recognition_options(extraction_hint="题目 1.1.5、1.1.7，页码: 3-5, 8")
    assert result.targets == ["1.1.5", "1.1.7"] and result.pages == [3, 4, 5, 8]
    assert question_sources.question_recognition_options(extraction_hint="value 1.23 with chapter 4").targets == []
    assert question_sources.question_recognition_options(extraction_hint="题号: 1-3, 1.2.3").targets == ["1", "2", "3", "1.2.3"]
    for value in ({"pages": [2, 1]}, {"pages": [True]}, {"targets": ["1\n2"]}):
        with pytest.raises(RecognitionError):
            question_sources.question_recognition_options(value)
    with pytest.raises(RecognitionError):
        question_sources.question_recognition_options(extraction_hint="pages 1-1000000000")


def test_partial_issues_use_existing_final_review_and_do_not_duplicate():
    packages = {"q1": {"stem": "literal wrong condition"}}
    summary = dict(operation_id="op", confidence="low", coverage=dict(unprocessed_pages=[2]), artifact_ids=["a"])
    question_sources.attach_recognition_review(packages, [{"recognition": summary}])
    question_sources.attach_recognition_review(packages, [{"recognition": summary}])
    assert packages["q1"]["stem"] == "literal wrong condition"
    issues = packages["q1"]["preparation_issues"]
    assert len(issues) == 1 and issues[0]["code"] == "recognition_partial"
    assert issues[0]["details"]["coverage"]["unprocessed_pages"] == [2]


@pytest.mark.asyncio
async def test_real_adapter_keeps_raw_tokens_and_reuses_without_second_model(tmp_path, monkeypatch):
    _agent, request, data, results = setup(tmp_path)
    provider, _engine = llm(text="1.1.5. Let x = -2. Prove the stated identity.")
    registry = SimpleNamespace(list_configs=lambda: [dict(provider_id=provider.provider_id, enabled=True, model="fixture")])
    route = StageProviderRoute(route_id=provider.provider_id, kind="llm", provider=provider)
    monkeypatch.setattr(question_sources, "get_storage", lambda: results.store.storage)
    args = dict(owner_id=request.source.owner_id, task_id=request.source.business_id, content=data,
                filename="synthetic.png", content_type="image/png", route=route, registry=registry,
                stored_file_id=request.source.stored_file_id)
    first = await question_sources.read_question_source(**args)
    second = await question_sources.read_question_source(**args)
    assert first.text == second.text and "x = -2" in first.text
    assert first.recognition["usage"]["input_tokens"] == 50
    assert second.recognition["usage"]["input_tokens"] == 0
    assert second.recognition["operation_usage"]["input_tokens"] == 50
    assert provider.ainvoke_vision.await_count == 1
    prompt = provider.ainvoke_vision.call_args.args[0]
    assert "without repairing erroneous source mathematics" in prompt and "1.1.5" not in prompt


@pytest.mark.asyncio
async def test_text_bypasses_vision_even_when_route_is_ocr_only():
    route = StageProviderRoute(route_id="ocr:unused", kind="ocr", credential_id="never-read")
    result = await question_sources.read_question_source(owner_id="owner", task_id="task", content=b"1. x = -2",
                                                        filename="questions.txt", route=route, registry=None)
    assert result.text == "1. x = -2" and result.recognition is None


@pytest.mark.asyncio
async def test_text_only_pdf_keeps_all_native_pages_for_compound_question_targets(tmp_path, monkeypatch):
    _agent, request, _data, results = setup(tmp_path)
    provider, _engine = llm()
    provider.supports_vision = False
    monkeypatch.setattr(question_sources, "get_storage", lambda: results.store.storage)
    data = pdf(["Chapter 1. Section 1. Exercise 5. First target. " * 8,
                "Exercise 31. Later target on another page. " * 8])
    result = await question_sources.read_question_source(
        owner_id=request.source.owner_id, task_id=request.source.business_id,
        content=data, filename="native.pdf", options={"targets": ["1.1.5", "1.1.31"]},
        route=StageProviderRoute(route_id=provider.provider_id, kind="llm", provider=provider), registry=None,
    )
    assert "First target" in result.text and "Later target" in result.text
    assert result.recognition["coverage"]["processed_pages"] == [1, 2]
    assert result.recognition["usage"]["initial_calls"] == 0
    provider.ainvoke_vision.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_only_mixed_pdf_keeps_native_text_and_marks_missing_scan(tmp_path, monkeypatch):
    _agent, request, _data, results = setup(tmp_path)
    provider, _engine = llm()
    provider.supports_vision = False
    monkeypatch.setattr(question_sources, "get_storage", lambda: results.store.storage)
    args = dict(owner_id=request.source.owner_id, task_id=request.source.business_id,
                filename="mixed.pdf", route=StageProviderRoute(route_id=provider.provider_id, kind="llm", provider=provider),
                registry=None)
    result = await question_sources.read_question_source(
        **args, content=pdf(["Exercise 1. Explain the stated property in words.", None]),
    )
    assert "Exercise 1" in result.text
    assert result.recognition["coverage"]["failed_pages"] == [2]
    assert question_sources.recognition_needs_review(result.recognition)
    with pytest.raises(RecognitionError, match="provider_vision_not_supported"):
        await question_sources.read_question_source(**args, content=pdf([None]))
    provider.ainvoke_vision.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_visual_locator_falls_back_to_all_native_math_pages(tmp_path, monkeypatch):
    from backend.llm.providers import ProviderRequestError
    _agent, request, _data, results = setup(tmp_path)
    provider, _engine = llm()
    provider.ainvoke_vision.side_effect = ProviderRequestError("provider_vision_not_supported", status_code=400)
    registry = SimpleNamespace(list_configs=lambda: [dict(provider_id=provider.provider_id, enabled=True, model="fixture")])
    monkeypatch.setattr(question_sources, "get_storage", lambda: results.store.storage)
    data = pdf(["First target: x = y + z; a < b; c > d; f(x) = x * x / 2.",
                "Later target: x = y + z; a < b; c > d; f(x) = x * x / 3."])
    result = await question_sources.read_question_source(
        owner_id=request.source.owner_id, task_id=request.source.business_id,
        content=data, filename="math.pdf", options={"targets": ["1.1.5", "1.1.31"]},
        route=StageProviderRoute(route_id=provider.provider_id, kind="llm", provider=provider), registry=registry,
    )
    assert "First target" in result.text and "Later target" in result.text
    assert result.recognition["coverage"]["processed_pages"] == [1, 2]
    provider.ainvoke_vision.assert_awaited_once()


@pytest.mark.asyncio
async def test_unauthorized_visual_source_rejected_before_model(tmp_path, monkeypatch):
    _agent, request, data, results = setup(tmp_path)
    provider, _ = llm(text="should not run")
    monkeypatch.setattr(question_sources, "get_storage", lambda: results.store.storage)
    registry = SimpleNamespace(list_configs=lambda: [dict(provider_id=provider.provider_id, enabled=True)])
    with pytest.raises(Exception):
        await question_sources.read_question_source(owner_id="other", task_id=request.source.business_id, content=data,
                                                    filename="source.png", route=StageProviderRoute(
                                                        route_id=provider.provider_id, kind="llm", provider=provider),
                                                    registry=registry, stored_file_id=request.source.stored_file_id)
    provider.ainvoke_vision.assert_not_awaited()


@pytest.mark.asyncio
async def test_baidu_uses_same_durable_boundary_and_retains_unknown_usage(tmp_path, monkeypatch):
    _agent, request, data, results = setup(tmp_path)
    client = SimpleNamespace(
        recognize_document=AsyncMock(return_value=SimpleNamespace(markdown="1. x = -2", duration_ms=2)),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(question_sources, "get_storage", lambda: results.store.storage)
    monkeypatch.setattr(question_sources, "stage_provider_configuration_fingerprint", lambda **_: "baidu-frozen")
    monkeypatch.setattr(question_sources, "build_owner_baidu_ocr_skill", lambda *args: SimpleNamespace(client=client))
    args = dict(owner_id=request.source.owner_id, task_id=request.source.business_id, content=data,
                filename="source.png", route=StageProviderRoute(route_id="ocr:baidu_unlimited_ocr:owned", kind="ocr", credential_id="owned"),
                registry=None, stored_file_id=request.source.stored_file_id)
    first = await question_sources.read_question_source(**args)
    second = await question_sources.read_question_source(**args)
    assert first.text == "1. x = -2" and second.text == first.text
    assert first.recognition["usage"]["input_tokens"] is None
    assert not first.recognition["usage"]["usage_complete"]
    assert second.recognition["usage"]["input_tokens"] == 0
    assert second.recognition["operation_usage"]["input_tokens"] is None
    assert client.recognize_document.await_count == 1 and client.aclose.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("format,mime", [("BMP", "image/bmp"), ("TIFF", "image/tiff")])
async def test_existing_single_frame_formats_use_same_safe_worker(tmp_path, monkeypatch, format, mime):
    from backend.db import file_repository
    _agent, request, _data, results = setup(tmp_path)
    stream = io.BytesIO()
    Image.new("RGB", (80, 120), "white").save(stream, format=format)
    data = stream.getvalue()
    stored = file_repository.save_file(storage=results.store.storage, owner_id=request.source.owner_id,
                                      assignment_id=request.source.business_id, kind="problem_source",
                                      original_name="source." + format.lower(), content=data, content_type=mime)
    provider, _engine = llm(text="1. x = -2")
    registry = SimpleNamespace(list_configs=lambda: [dict(provider_id=provider.provider_id, enabled=True)])
    monkeypatch.setattr(question_sources, "get_storage", lambda: results.store.storage)
    result = await question_sources.read_question_source(
        owner_id=request.source.owner_id, task_id=request.source.business_id, content=data,
        filename=stored.original_name, content_type=mime, stored_file_id=stored.id,
        route=StageProviderRoute(route_id=provider.provider_id, kind="llm", provider=provider), registry=registry,
    )
    assert result.text == "1. x = -2" and provider.ainvoke_vision.await_count == 1
    assert provider.ainvoke_vision.call_args.args[1][0].media_type == "image/png"


def test_library_clone_preview_uses_assignment_source_fences(monkeypatch):
    from backend.services import source_files
    clone = SimpleNamespace(id="clone", sha256="sha")
    resolved = SimpleNamespace(stored=clone)
    material = SimpleNamespace(sha256="sha")
    monkeypatch.setattr(source_files.course_library_repository, "get_material", lambda *args: material)
    monkeypatch.setattr(source_files.file_repository, "get_file", lambda **kwargs: clone)
    calls = []
    def resolve(**kwargs):
        calls.append(kwargs)
        return resolved
    monkeypatch.setattr(source_files, "_problem_from_workflow_source", resolve)
    selected = dict(role="problem", source_kind="library", stored_file_id="clone", source_id="source",
                    source_operation_id="source-op", source_attempt=1, library_material_id="material")
    operation = SimpleNamespace(payload=dict(source_refs=[selected]))
    assert source_files._problem_from_question_preparation(storage=None, task_id="task", owner_id="owner",
                                                           operation=operation, workflow=None) is resolved
    assert calls[0]["expected_operation_id"] == "source-op" and calls[0]["expected_operation_type"] == "problem_source"
    material.sha256 = "different"
    with pytest.raises(source_files.SourcePreviewNotFound):
        source_files._problem_from_question_preparation(storage=None, task_id="task", owner_id="owner",
                                                        operation=operation, workflow=None)
