"""Local media reuse changes no model evidence or current-call accounting."""
import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from backend.agents.recognition_agent import RecognitionAgent, RecognitionReadRequestV1
from backend.domain.errors import RecognitionError
from backend.recognition.executor import read_pdf_plan
from backend.recognition.fusion import RecognitionAssemblyV1, assemble_recognition
from backend.recognition.image_executor import read_image_plan
from backend.recognition.models import RecognitionPolicyV1, RecognitionSourceRefV1
from backend.recognition.recheck import recheck_recognition
from backend.recognition.runtime import RecognitionCapacity
from backend.tests.test_recognition_agent import Engine as LocatorEngine, pdf
from backend.tests.test_recognition_executor import FakeEngine as DocumentEngine, setup as pdf_setup
from backend.tests.test_recognition_image_executor import setup as image_setup
from backend.tests.test_recognition_local_evidence import setup as stored_setup
from backend.tests.test_recognition_recheck import Engine as RepairEngine, source_file
from backend.tools.pdf_evidence import ImagePrepareRequest, read_image_evidence, read_pdf_evidence


class RecordingReader:
    """Exercise the injection using real killable workers and fake model calls."""

    def __init__(self, source, *, error=None, fail_operation=None, now=None):
        self.source = source.model_copy(deep=True)
        self.checks, self.calls = [], []
        self.error, self.fail_operation, self.now = error, fail_operation, now

    def assert_context(self, source, *, authorized_owner_id):
        self.checks.append((source.model_copy(deep=True), authorized_owner_id))
        if source != self.source or authorized_owner_id != self.source.owner_id:
            raise RecognitionError("recognition_source_mismatch")

    async def read(self, data, command, *, timeout_seconds):
        self.calls.append((command.operation, timeout_seconds))
        if self.error is not None and command.operation == self.fail_operation:
            raise self.error
        if self.now is not None:
            self.now[0] += 1
        tool = read_image_evidence if isinstance(command, ImagePrepareRequest) else read_pdf_evidence
        result = await tool(data, command, timeout_seconds=timeout_seconds)
        return SimpleNamespace(evidence=result)


def source_for(data, content_type="application/pdf"):
    return RecognitionSourceRefV1(owner_id="owner", scope="assignment_source", business_id="task",
                                   stored_file_id="original", input_sha256=hashlib.sha256(data).hexdigest(),
                                   content_type=content_type)


def forbid_direct_tools(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("injected media must not fall back to the direct tool")
    for module in ("backend.agents.recognition_agent", "backend.recognition.executor",
                   "backend.recognition.image_executor", "backend.recognition.recheck"):
        for name in ("read_pdf_evidence", "read_image_evidence"):
            monkeypatch.setattr(f"{module}.{name}", forbidden, raising=False)


@pytest.mark.asyncio
async def test_native_index_target_detail_and_executor_detail_delegate(monkeypatch):
    data = pdf(["Sec. 1.1\n5. Describe the group."])
    source = source_for(data)
    reader = RecordingReader(source)
    forbid_direct_tools(monkeypatch)
    raw = await RecognitionAgent(None, capacity=RecognitionCapacity(), local_reader=reader).read(
        RecognitionReadRequestV1(source=source, purpose="problems", scope="targets", targets=["1.1.5"]),
        data, authorized_owner_id="owner",
    )
    assert [op for op, _ in reader.calls] == ["index", "detail", "detail"]
    assert len(reader.checks) == 2
    assert raw.selected_pages == [1] and raw.budget.total_calls == 0
    assert "Describe the group" in raw.read_batch.pages[0].native_text


@pytest.mark.asyncio
async def test_out_of_window_explicit_page_detail_delegates(monkeypatch):
    data = pdf(["First source.", "Second source.", "Third source."])
    source, engine = source_for(data), LocatorEngine()
    reader = RecordingReader(source)
    forbid_direct_tools(monkeypatch)
    raw = await RecognitionAgent(engine, capacity=RecognitionCapacity(), local_reader=reader).read(
        RecognitionReadRequestV1(source=source, purpose="problems", scope="pages", pages=[1, 3], search_window_pages=1),
        data, authorized_owner_id="owner",
    )
    assert [op for op, _ in reader.calls] == ["index", "detail", "detail"]
    assert raw.selected_pages == [1, 3] and raw.budget.total_calls == 0


@pytest.mark.asyncio
async def test_contact_sheet_and_read_render_share_original_budget(monkeypatch):
    data = pdf([None, None])
    source, engine, now = source_for(data), LocatorEngine({"Q7": [2]}), [0.0]
    reader = RecordingReader(source, now=now)
    forbid_direct_tools(monkeypatch)
    raw = await RecognitionAgent(engine, capacity=RecognitionCapacity(), local_reader=reader, clock=lambda: now[0]).read(
        RecognitionReadRequestV1(source=source, purpose="problems", scope="targets", targets=["Q7"],
                                 policy=RecognitionPolicyV1(total_seconds=8)), data, authorized_owner_id="owner",
    )
    assert [op for op, _ in reader.calls] == ["index", "contact_sheet", "detail", "render"]
    assert [seconds for _, seconds in reader.calls] == [8, 7, 6, 5]
    assert raw.budget.locator_calls == raw.budget.initial_calls == 1
    assert raw.budget.input_tokens == 16 and raw.budget.output_tokens == 2
    assert raw.locator_calls[0].result.candidate.input_tokens == 8
    assert raw.read_batch.units[0].candidate.input_tokens == 8
    assert raw.budget.global_remaining_seconds == 4


@pytest.mark.asyncio
async def test_pdf_document_export_delegates_without_hidden_image_or_repair(monkeypatch):
    data = pdf(["First source.", "Second source."])
    engine = DocumentEngine(document=True)
    kwargs = await pdf_setup(data, engine)
    reader = RecordingReader(kwargs["source"])
    forbid_direct_tools(monkeypatch)
    raw = await read_pdf_plan(data, **kwargs, local_reader=reader)
    assert [op for op, _ in reader.calls] == ["detail", "export_pages"]
    assert len(engine.requests) == 1 and raw.units[0].page_numbers == [1, 2]
    assert raw.units[0].candidate.kind == "ocr" and raw.usage.initial_calls == 1
    assert not raw.usage.patch_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("is_pdf", [False, True])
async def test_agent_recheck_reuses_injected_media_without_changing_call_evidence(monkeypatch, is_pdf):
    data, mime = source_file(pdf=is_pdf)
    source, engine = source_for(data, mime), RepairEngine()
    reader = RecordingReader(source)
    forbid_direct_tools(monkeypatch)
    result = await RecognitionAgent(engine, capacity=RecognitionCapacity(), local_reader=reader).recognize(
        RecognitionReadRequestV1(source=source, purpose="submissions"), data,
        authorized_owner_id="owner", prompt_version="test-reader-v1",
    )
    expected = ["index", "detail", "render", "render"] if is_pdf else ["image_prepare", "image_prepare"]
    assert [op for op, _ in reader.calls] == expected
    assert len(reader.checks) == 3
    assert len(engine.reads) == len(engine.repairs) == 1
    assert engine.reads[0].payload == engine.repairs[0].payload
    assert result.raw.budget.initial_calls == 1 and result.repair_execution.budget.patch_calls == 1
    assert result.document.usage.input_tokens == 14 and result.document.usage.output_tokens == 9
    assert RecognitionAssemblyV1.model_validate_json(result.model_dump_json()) == result
    assert "local_reader" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_reader_is_stable_for_read_and_recheck_if_agent_configuration_changes(monkeypatch):
    data, mime = source_file()
    source, engine = source_for(data, mime), RepairEngine()
    reader = RecordingReader(source)
    agent = RecognitionAgent(engine, capacity=RecognitionCapacity(), local_reader=reader)
    original_read = reader.read
    async def changing(*args, **kwargs):
        agent.local_reader = None
        return await original_read(*args, **kwargs)
    reader.read = changing
    forbid_direct_tools(monkeypatch)
    result = await agent.recognize(RecognitionReadRequestV1(source=source, purpose="submissions"), data,
                                   authorized_owner_id="owner", prompt_version="test-reader-v1")
    assert [op for op, _ in reader.calls] == ["image_prepare", "image_prepare"]
    assert result.repair_execution.budget.total_calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("business_id", "other-task"), ("stored_file_id", "other-file"),
                                         ("scope", "submission_source"), ("owner_id", "other-owner"),
                                         ("input_sha256", "f" * 64)])
async def test_agent_rejects_wrong_reader_binding_before_tools_or_model(monkeypatch, field, value):
    data = pdf(["Source."])
    source, engine = source_for(data), LocatorEngine()
    reader = RecordingReader(source.model_copy(update={field: value}))
    forbid_direct_tools(monkeypatch)
    with pytest.raises(RecognitionError, match="recognition_source_mismatch"):
        await RecognitionAgent(engine, capacity=RecognitionCapacity(), local_reader=reader).read(
            RecognitionReadRequestV1(source=source, purpose="problems"), data, authorized_owner_id="owner",
        )
    assert not reader.calls and not engine.reads and not engine.locates


@pytest.mark.asyncio
@pytest.mark.parametrize("is_pdf", [False, True])
async def test_standalone_executor_rejects_wrong_reader_binding(monkeypatch, is_pdf):
    data, mime = source_file(pdf=is_pdf)
    engine = RepairEngine()
    kwargs = await pdf_setup(data, engine) if is_pdf else image_setup(data, engine)
    reader = RecordingReader(kwargs["source"].model_copy(update={"business_id": "other-task"}))
    forbid_direct_tools(monkeypatch)
    executor = read_pdf_plan if is_pdf else read_image_plan
    with pytest.raises(RecognitionError, match="recognition_source_mismatch"):
        await executor(data, **kwargs, local_reader=reader)
    assert not reader.calls and not engine.reads


@pytest.mark.asyncio
async def test_standalone_recheck_rejects_wrong_reader_before_extra(monkeypatch):
    data, mime = source_file()
    source, engine = source_for(data, mime), RepairEngine()
    capacity = RecognitionCapacity()
    raw, budget = await RecognitionAgent(engine, capacity=capacity)._read(
        RecognitionReadRequestV1(source=source, purpose="submissions"), data, authorized_owner_id="owner",
    )
    reader = RecordingReader(source.model_copy(update={"stored_file_id": "other-file"}))
    forbid_direct_tools(monkeypatch)
    with pytest.raises(RecognitionError, match="recognition_source_mismatch"):
        await recheck_recognition(assemble_recognition(raw, prompt_version="test-reader-v1"), data,
                                 authorized_owner_id="owner", engine=engine, budget=budget, capacity=capacity,
                                 local_reader=reader)
    assert not reader.calls and not engine.repairs and budget.snapshot().total_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["index", "detail", "contact_sheet", "render", "image_prepare", "export_pages"])
@pytest.mark.parametrize("error", ["recognition_artifact_invalid", "recognition_source_unavailable", "recognition_timeout"])
async def test_local_failure_never_falls_back_or_submits_reader(monkeypatch, operation, error):
    data, mime = source_file() if operation == "image_prepare" else (pdf([None]), "application/pdf")
    source = source_for(data, mime)
    engine = DocumentEngine(document=True) if operation == "export_pages" else LocatorEngine({"Q7": [1]})
    reader = RecordingReader(source, error=RecognitionError(error), fail_operation=operation)
    forbid_direct_tools(monkeypatch)
    extra = {"scope": "targets", "targets": ["Q7"]} if operation == "contact_sheet" else {}
    raw = await RecognitionAgent(engine, capacity=RecognitionCapacity(), local_reader=reader).read(
        RecognitionReadRequestV1(source=source, purpose="problems", **extra), data, authorized_owner_id="owner",
    )
    assert error in raw.stop_codes
    assert not (engine.requests if operation == "export_pages" else engine.reads)
    assert raw.budget.pending_calls == raw.budget.initial_calls == 0
    assert reader.calls[-1][0] == operation


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["index", "detail", "contact_sheet", "render", "image_prepare", "export_pages"])
async def test_local_cancellation_propagates_without_model_dispatch(monkeypatch, operation):
    data, mime = source_file() if operation == "image_prepare" else (pdf([None]), "application/pdf")
    source = source_for(data, mime)
    engine = DocumentEngine(document=True) if operation == "export_pages" else LocatorEngine({"Q7": [1]})
    reader = RecordingReader(source, error=asyncio.CancelledError(), fail_operation=operation)
    forbid_direct_tools(monkeypatch)
    extra = {"scope": "targets", "targets": ["Q7"]} if operation == "contact_sheet" else {}
    with pytest.raises(asyncio.CancelledError):
        await RecognitionAgent(engine, capacity=RecognitionCapacity(), local_reader=reader).read(
            RecognitionReadRequestV1(source=source, purpose="problems", **extra), data, authorized_owner_id="owner",
        )
    assert not (engine.requests if operation == "export_pages" else engine.reads)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_local_recheck_failure_keeps_initial_without_extra_submission(monkeypatch, cancel):
    data, mime = source_file()
    source, engine, capacity = source_for(data, mime), RepairEngine(), RecognitionCapacity()
    raw, budget = await RecognitionAgent(engine, capacity=capacity)._read(
        RecognitionReadRequestV1(source=source, purpose="submissions"), data, authorized_owner_id="owner",
    )
    error = asyncio.CancelledError() if cancel else RecognitionError("recognition_artifact_invalid")
    reader = RecordingReader(source, error=error, fail_operation="image_prepare")
    forbid_direct_tools(monkeypatch)
    initial = assemble_recognition(raw, prompt_version="test-reader-v1")
    call = recheck_recognition(initial, data, authorized_owner_id="owner", engine=engine,
                              budget=budget, capacity=capacity, local_reader=reader)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await call
    else:
        result = await call
        assert result.document.final_markdown == initial.document.final_markdown
        assert result.repair_execution.calls[0].safe_error_code == "recognition_artifact_invalid"
    assert not engine.repairs and budget.snapshot().total_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("is_image", [False, True])
async def test_real_local_cache_avoids_workers_but_not_model_calls_on_second_invocation(tmp_path, monkeypatch, is_image):
    from backend.services import recognition_local_evidence as service

    reader, data, source, _, _ = stored_setup(tmp_path, image=is_image)
    engine = RepairEngine()
    agent = RecognitionAgent(engine, capacity=RecognitionCapacity(), local_reader=reader)
    request = RecognitionReadRequestV1(source=source, purpose="submissions")
    forbid_direct_tools(monkeypatch)
    first = await agent.recognize(request, data, authorized_owner_id=source.owner_id, prompt_version="test-reader-v1")
    async def forbidden(*args, **kwargs):
        pytest.fail("second invocation media should reuse the authorized local cache")
    monkeypatch.setattr(service, "read_pdf_evidence", forbidden)
    monkeypatch.setattr(service, "read_image_evidence", forbidden)
    second = await agent.recognize(request, data, authorized_owner_id=source.owner_id, prompt_version="test-reader-v1")
    assert len(engine.reads) == len(engine.repairs) == 2
    assert first.document.final_markdown == second.document.final_markdown
    assert first.document.usage.input_tokens == second.document.usage.input_tokens == 14
    assert first.document.usage.output_tokens == second.document.usage.output_tokens == 9
    assert first.repair_execution.budget.total_calls == second.repair_execution.budget.total_calls == 2
    assert first.raw.read_batch.units[0].payload_sha256 == second.raw.read_batch.units[0].payload_sha256
