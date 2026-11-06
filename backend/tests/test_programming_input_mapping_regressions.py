"""P1 reads original evidence; it never chooses a function or runs a submission."""
import hashlib
import asyncio
import io
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.sax.saxutils import escape
import zipfile

import pytest
from fastapi import HTTPException

from backend.agents import ingest_agent
from backend.db import assignment_repository, file_repository, source_outcome_repository, workflow_repository
from backend.domain.errors import DomainError
from backend.services import submission_source_pipeline as pipeline, task_facade
from backend.services.submission_incremental import split_submission_question_units
from backend.storage import get_storage
from backend.tests.test_submission_source_pipeline_integrity import _create_operation
from backend.tests.test_submission_incremental import _claim, _registry
from backend.tests.test_task_background_workflows import _BackgroundTasks, _seed_task
from backend.tests.test_upload_contract import office_package
from backend.tests.test_ocr_mounted_routes import headers
from backend.tools import file_processing as files
from backend.tests.test_ocr_mounted_routes import client_registry  # noqa: F401 - pytest fixture

CODE = "# 辅助函数在前\ndef helper(x):\n\treturn x + 1\n\ndef add(a, b):\n\treturn helper(a) + b\n"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture(autouse=True)
def no_student_execution(monkeypatch):
    from backend.tools import code_interpreter
    runner = AsyncMock(side_effect=AssertionError("Ingestion must not execute student code"))
    monkeypatch.setattr(code_interpreter, "run_sandbox", runner)
    yield
    runner.assert_not_awaited()


def docx(body, *, extra=None):
    """Hand-built real OOXML, independent of the production text reader."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(office_package())) as base, zipfile.ZipFile(out, "w") as target:
        for item in base.infolist():
            content = base.read(item)
            if item.filename == "word/document.xml":
                content = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                           '<w:body>' + body + '</w:body></w:document>').encode()
            target.writestr(item.filename, content)
        for name, content in (extra or {}).items():
            target.writestr(name, content)
    return out.getvalue()


async def prepare(content, filename):
    owner, task = _seed_task()
    job = _create_operation(owner, task)
    sources = await pipeline.prepare_submission_sources(content=content, filename=filename,
        content_type=None, owner_id=owner, task_id=task, job_id=job.id, job_attempt=job.attempt,
        ocr_skill=None)
    return owner, task, sources


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["utf-8", "gbk"])
async def test_source_preserves_helpers_and_function_order(encoding):
    body = CODE.encode(encoding)
    owner, task, sources = await prepare(body, "S1_Alice.txt")
    assert len(sources) == 1 and sources[0].pre_error_code is None
    assert sources[0].text == CODE
    stored = file_repository.get_file(file_id=sources[0].stored_file_id, owner_id=owner)
    assert stored.assignment_id == task and stored.sha256 == hashlib.sha256(body).hexdigest()
    with get_storage().open(stored.storage_key) as original:
        assert original.read() == body
    if encoding == "gbk":
        assert stored.sha256 != hashlib.sha256(sources[0].text.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_ambiguous_question_mapping_keeps_all_blocks():
    text = "Question 1: def helper(x): return x + 1\nQuestion 1: def add(a,b): return helper(a) + b"
    known = {"q1": dict(q_id="q1", number="Section 1.1 / 1", type="programming", stem="Implement add."),
             "q2": dict(q_id="q2", number="Section 1.2 / 1", type="programming", stem="Implement multiply.")}
    _owner, _task, sources = await prepare(text.encode(), "S1_Alice.txt")
    assert split_submission_question_units(sources[0], known) == [(sources[0], {"q1", "q2"})]
    parsed = (await ingest_agent.parse_student_answer_sources_from_ocr_markdown(sources, known))[0]
    assert parsed.status == "no_matching_answer" and parsed.student is None
    assert parsed.unknown_question_ids == ("number:1",)
    assert sources[0].text == text


@pytest.mark.asyncio
async def test_confirmed_identity_and_mapping_survive_retry(monkeypatch):
    from backend.api import tasks
    owner, task = _seed_task()
    for i, name in enumerate(("add",), 1):
        assignment_repository.add_question(task, teacher_id=owner, q_id=f"q{i}", order_index=i-1,
            number=str(i), type="programming", stem=f"Implement {name}.", criterion="Teacher rubric", max_score=10)
    async def invoke(_messages):
        return SimpleNamespace(content=json.dumps(dict(stu_id="S1", stu_name="Alice", stu_ans=[
            dict(q_id="q1", number="1", type="programming", content=CODE, flag=[])])))
    registry = _registry(monkeypatch, invoke)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="S1_Alice.txt", content=CODE.encode(), content_type="text/plain", registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    before = task_facade.get_task(task_id=task, owner_id=owner)
    answer = next(a for a in before["student_data"]["S1"]["stu_ans"] if a["q_id"] == "q1")
    task_facade.update_student_answer(task_id=task, owner_id=owner, display_student_id="S1", q_id="q1",
        patch={"answer_id": answer["answer_id"], "target_q_id": "q1", "content": CODE, "review_status": "confirmed"},
        expected_revision=before["workflow_revision"])
    current = task_facade.get_task(task_id=task, owner_id=owner)
    task_facade.update_student_identity(task_id=task, owner_id=owner, current_display_id="S1",
        new_display_id="Confirmed-1", new_display_name="Confirmed learner", expected_revision=current["workflow_revision"])
    confirmed = task_facade.get_task(task_id=task, owner_id=owner)
    calls = registry.provider.ainvoke.await_count
    retry = await tasks.retry_submission_recognition_endpoint(task_id=task, job_id=queued["job_id"],
        request=tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=confirmed["workflow_revision"]),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=registry)
    assert retry.status_code == 409
    assert json.loads(retry.body)["error"]["code"] == "submission_retry_not_available"
    with pytest.raises(DomainError):
        task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner, filename="S1_Alice.txt",
            content=CODE.encode(), content_type="text/plain", registry=registry, replace_confirmed=False)
    after = task_facade.get_task(task_id=task, owner_id=owner)
    assert after["student_data"] == confirmed["student_data"]
    assert after["workflow_revision"] == confirmed["workflow_revision"]
    saved = after["student_data"]["Confirmed-1"]
    assert saved["stu_name"] == "Confirmed learner"
    matching = next(a for a in saved["stu_ans"] if a["q_id"] == "q1")
    assert matching["content"] == CODE and matching["review_status"] == "confirmed"
    assert registry.provider.ainvoke.await_count == calls


@pytest.mark.asyncio
async def test_bad_archive_member_keeps_valid_sibling():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("group/S1.txt", CODE.encode())
        archive.writestr("bad.bin", b"\x00binary")
    owner, _task, sources = await prepare(out.getvalue(), "answers.zip")
    by_name = {s.filename: s for s in sources}
    assert set(by_name) == {"group/S1.txt", "bad.bin"}
    assert by_name["group/S1.txt"].text == CODE
    assert by_name["bad.bin"].pre_error_code == "submission_source_unsupported"
    assert by_name["bad.bin"].text is None
    stored = file_repository.get_file(file_id=by_name["bad.bin"].stored_file_id, owner_id=owner)
    with get_storage().open(stored.storage_key) as original:
        assert original.read() == b"\x00binary"


@pytest.mark.asyncio
async def test_mixed_document_is_not_implicitly_executable(tmp_path):
    marker = tmp_path / "must-not-exist"
    snippet = "from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()"
    body = docx('<w:p><w:r><w:t>Student ID: S1</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>' + escape(snippet) + '</w:t></w:r></w:p>'
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Input</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>Expected -2</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        '<w:p><w:r><w:t>Teacher literal error stays -2.</w:t></w:r></w:p>')
    owner, task, sources = await prepare(body, "S1_Alice.docx")
    assert len(sources) == 1 and sources[0].pre_error_code is None
    assert sources[0].text == "Student ID: S1\n" + snippet + "\nInput\tExpected -2\nTeacher literal error stays -2."
    assert not marker.exists()
    stored = file_repository.get_file(file_id=sources[0].stored_file_id, owner_id=owner)
    assert stored.assignment_id == task and stored.sha256 == hashlib.sha256(body).hexdigest()
    with get_storage().open(stored.storage_key) as original:
        assert original.read() == body


@pytest.mark.asyncio
async def test_docx_explicit_tabs_and_line_breaks_preserve_code_indentation():
    body = docx('<w:p><w:r><w:t>def helper(x):</w:t><w:br/><w:tab/><w:t>return x + 1</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>def add(a,b):</w:t><w:br/><w:tab/><w:t>return helper(a) + b</w:t></w:r></w:p>')
    assert await files.extract_text_from_upload(body, "answer.docx") == (
        "def helper(x):\n\treturn x + 1\ndef add(a,b):\n\treturn helper(a) + b")


@pytest.mark.asyncio
async def test_standard_word_package_keeps_code_and_plain_table_without_ocr():
    from docx import Document
    document = Document()
    document.add_paragraph("Student ID: S1")
    document.add_paragraph(CODE)
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Input", "Expected"
    table.cell(1, 0).text, table.cell(1, 1).text = "2, 3", "-2"
    stream = io.BytesIO()
    document.save(stream)
    text = await files.extract_text_from_upload(stream.getvalue(), "answer.docx")
    assert text == "Student ID: S1\n" + CODE + "\nInput\tExpected\n2, 3\t-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("body,code", [(b"", "submission_source_empty"),
    (b"\x00answer", "submission_source_content_type_mismatch"),
    (b"\xff\xff\xff", "submission_source_content_type_mismatch")])
async def test_unreadable_source_keeps_original_and_stable_failure(body, code):
    owner, _task, sources = await prepare(body, "S1.txt")
    assert sources[0].text is None and sources[0].pre_error_code == code
    assert sources[0].failure_phase == "source_read"
    stored = file_repository.get_file(file_id=sources[0].stored_file_id, owner_id=owner)
    assert stored.sha256 == hashlib.sha256(body).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"word/media/image.png": b"visual"}, {"word/header1.xml": b"supplementary"}])
async def test_docx_unread_native_evidence_is_not_silently_accepted(extra):
    body = docx('<w:p><w:r><w:t>Some text is not the whole document.</w:t></w:r></w:p>', extra=extra)
    _owner, _task, sources = await prepare(body, "answer.docx")
    assert sources[0].text is None
    assert sources[0].pre_error_code == "source_docx_incomplete"
    assert sources[0].failure_phase == "source_read"


@pytest.mark.asyncio
async def test_docx_text_budget_rejects_instead_of_truncating(monkeypatch):
    monkeypatch.setattr(files, "DOCX_MAX_CHARACTERS", 16, raising=False)
    body = docx('<w:p><w:r><w:t>Seventeen-letters!</w:t></w:r></w:p>')
    with pytest.raises(HTTPException) as caught:
        await files.extract_text_from_upload(body, "answer.docx")
    assert caught.value.status_code == 413
    assert caught.value.detail["code"] == "source_text_too_large"
    _owner, _task, sources = await prepare(body, "answer.docx")
    assert sources[0].text is None and sources[0].pre_error_code == "source_text_too_large"


@pytest.mark.asyncio
async def test_docx_office_chunks_do_not_insert_characters_inside_long_source():
    literal = "x" * 80010
    body = docx('<w:p><w:r><w:t>' + literal + '</w:t></w:r></w:p>')
    assert await files.extract_text_from_upload(body, "answer.docx") == literal


@pytest.mark.asyncio
async def test_docx_reaches_real_durable_mapping_as_one_original_source(monkeypatch):
    owner, task = _seed_task()
    assignment_repository.add_question(task, teacher_id=owner, q_id="q1", order_index=0,
        number="1", type="programming", stem="Implement add.", criterion="Teacher rubric", max_score=10)
    code = "def helper(x):\n\treturn x + 1\ndef add(a,b):\n\treturn helper(a) + b"
    body = docx('<w:p><w:r><w:t>Student ID: S1</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Question 1:</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>def helper(x):</w:t><w:br/><w:tab/><w:t>return x + 1</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>def add(a,b):</w:t><w:br/><w:tab/><w:t>return helper(a) + b</w:t></w:r></w:p>')
    async def invoke(messages):
        assert code in messages[-1].content
        return SimpleNamespace(content=json.dumps(dict(stu_id="S1", stu_name="Alice", stu_ans=[
            dict(q_id="q1", number="1", type="programming", content=code, flag=[])])))
    registry = _registry(monkeypatch, invoke)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner, filename="S1_Alice.docx",
        content=body, content_type=DOCX_MIME, registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued["job_id"]))
    saved = task_facade.get_task(task_id=task, owner_id=owner)
    assert saved["status"] == "submissions_ready", saved["error"]
    assert len(saved["submission_sources"]) == 1
    assert saved["student_data"]["S1"]["stu_ans"][0]["content"] == code
    source = saved["submission_sources"][0]
    operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    originals = source_outcome_repository.list_sources(operation_id=operation.id, owner_id=owner, attempt=operation.attempt)
    assert len(originals) == 1 and originals[0].id == source["source_id"]
    stored = file_repository.get_file(file_id=originals[0].stored_file_id, owner_id=owner)
    assert stored.sha256 == hashlib.sha256(body).hexdigest()
    with get_storage().open(stored.storage_key) as original:
        assert original.read() == body


def test_docx_capability_matches_native_teacher_preflight(client_registry):
    client, _registry = client_registry
    owner, task = _seed_task()
    capability = client.get(f"/tasks/{task}/question-preparation/capabilities", headers=headers(owner))
    assert capability.status_code == 200 and capability.json()["reader"]["docx"] is True
    body = docx('<w:p><w:r><w:t>1. Implement add(a,b).</w:t></w:r></w:p>')
    result = client.post(f"/tasks/{task}/question-preparation/sources/preflight", headers=headers(owner),
        data={"role": "problem", "structure_mode": "organized"}, files={"file": ("problem.docx", body, DOCX_MIME)})
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("node", ["drawing", "altChunk", "oMath", "ins", "gridSpan", "vMerge", "sdt", "AlternateContent"])
async def test_docx_unprocessed_body_parts_fail_closed(node):
    paragraph = '<w:p><w:r><w:t>Visible fragment</w:t></w:r></w:p>'
    if node in {"gridSpan", "vMerge"}:
        fragment = '<w:tbl><w:tr><w:tc><w:tcPr><w:' + node + ' w:val="2"/></w:tcPr>' + paragraph + '</w:tc></w:tr></w:tbl>'
    elif node == "sdt":
        fragment = '<w:sdt><w:sdtContent>' + paragraph + '</w:sdtContent></w:sdt>'
    elif node == "AlternateContent":
        fragment = ('<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
            '<mc:Choice Requires="w">' + paragraph + '</mc:Choice><mc:Fallback>' + paragraph + '</mc:Fallback></mc:AlternateContent>')
    else:
        fragment = '<w:p><w:r><w:t>Visible fragment</w:t><w:' + node + '/></w:r></w:p>'
    body = docx(fragment)
    _owner, _task, sources = await prepare(body, "answer.docx")
    assert sources[0].text is None and sources[0].pre_error_code == "source_docx_incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize("during_launch", [False, True])
async def test_docx_cancellation_reaps_before_releasing_shared_native_slot(monkeypatch, during_launch):
    from backend.tests.test_pdf_worker_lifecycle import Process
    process, launched, allow = Process(), asyncio.Event(), asyncio.Event()
    slots = threading.BoundedSemaphore(1)
    async def launch(*_args, **_kwargs):
        launched.set()
        if during_launch:
            await allow.wait()
        return process
    monkeypatch.setattr(files.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(files, "_PDF_EXTRACTION_SLOTS", slots)
    task = asyncio.create_task(files.extract_text_from_upload(docx('<w:p/>'), "answer.docx"))
    await launched.wait()
    if not during_launch:
        await process.started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not slots.acquire(blocking=False)
    allow.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode == -9 and process.drained and process.reaped
    assert slots.acquire(blocking=False)
    slots.release()


@pytest.mark.asyncio
async def test_docx_timeout_keeps_retryable_source_reason_and_reaps(monkeypatch):
    from backend.tests.test_pdf_worker_lifecycle import Process
    process = Process()
    async def launch(*_args, **_kwargs):
        return process
    monkeypatch.setattr(files.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(files, "DOCX_EXTRACTION_TIMEOUT_SECONDS", .01)
    _owner, _task, sources = await prepare(docx('<w:p/>'), "answer.docx")
    assert sources[0].pre_error_code == "source_docx_timeout" and sources[0].retryable is True
    assert process.returncode == -9 and process.drained and process.reaped


@pytest.mark.asyncio
async def test_docx_busy_is_retryable_without_starting_another_process(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(files, "_PDF_EXTRACTION_SLOTS", slots)
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("No process may start without an extraction slot")
    monkeypatch.setattr(files.asyncio, "create_subprocess_exec", forbidden)
    try:
        _owner, _task, sources = await prepare(docx('<w:p/>'), "answer.docx")
        assert sources[0].pre_error_code == "source_docx_busy" and sources[0].retryable is True
    finally:
        slots.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("fragment", [
    '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="5"/></w:numPr></w:pPr><w:r><w:t>Implement add.</w:t></w:r></w:p>',
    '<w:p><w:r><w:t>A</w:t><w:sym w:font="Wingdings" w:char="F041"/><w:t>B</w:t></w:r></w:p>',
    '<w:p><w:moveFrom w:id="1"><w:r><w:t>old code</w:t></w:r></w:moveFrom><w:moveTo w:id="2"><w:r><w:t>new code</w:t></w:r></w:moveTo></w:p>',
    '<w:p><w:fldSimple w:instr="SEQ Question"><w:r><w:t>3</w:t></w:r></w:fldSimple><w:r><w:t>Implement add.</w:t></w:r></w:p>',
    '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>SEQ Question</w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>3</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>',
    '<w:p><w:commentRangeStart w:id="0"/><w:r><w:t>Body text</w:t></w:r><w:commentRangeEnd w:id="0"/><w:r><w:commentReference w:id="0"/></w:r></w:p>',
])
async def test_review_docx_semantics_cannot_disappear_without_incomplete_reason(fragment):
    _owner, _task, sources = await prepare(docx(fragment), "answer.docx")
    assert sources[0].text is None and sources[0].pre_error_code == "source_docx_incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize("style_case", ["used", "inherited", "default_numbered", "unused"])
async def test_review_only_actually_used_numbered_word_styles_block_native_read(style_case):
    from copy import deepcopy
    from docx import Document
    from docx.enum.style import WD_STYLE_TYPE
    document = Document()
    if style_case == "used":
        document.add_paragraph("Implement add.", style="List Number")
    elif style_case == "inherited":
        custom = document.styles.add_style("InheritedNumbered", WD_STYLE_TYPE.PARAGRAPH)
        custom.base_style = document.styles["List Number"]
        document.add_paragraph("Implement add.", style=custom)
    else:
        document.add_paragraph("Implement add.")
        if style_case == "default_numbered":
            num = next(node for node in document.styles["List Number"]._element.iter()
                       if node.tag.rsplit("}", 1)[-1] == "numPr")
            document.styles["Normal"]._element.get_or_add_pPr().append(deepcopy(num))
    body = io.BytesIO()
    document.save(body)
    _owner, _task, sources = await prepare(body.getvalue(), "answer.docx")
    if style_case == "unused":
        assert sources[0].text == "Implement add." and sources[0].pre_error_code is None
    else:
        assert sources[0].text is None and sources[0].pre_error_code == "source_docx_incomplete"


@pytest.mark.asyncio
async def test_review_native_word_character_tokens_preserve_unicode_characters():
    body = docx('<w:p><w:r><w:t>A</w:t><w:noBreakHyphen/><w:t>B</w:t><w:softHyphen/><w:t>C</w:t></w:r></w:p>')
    assert await files.extract_text_from_upload(body, "answer.docx") == "A\u2011B\u00adC"


@pytest.mark.asyncio
async def test_review_real_word_comments_are_not_ignored_as_complete():
    from docx import Document
    document = Document()
    paragraph = document.add_paragraph("Visible body.")
    document.add_comment(paragraph.runs[0], text="Student evidence -2", author="Synthetic reviewer")
    body = io.BytesIO()
    document.save(body)
    _owner, _task, sources = await prepare(body.getvalue(), "answer.docx")
    assert sources[0].text is None and sources[0].pre_error_code == "source_docx_incomplete"
