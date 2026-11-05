"""The same response-file policy applies before OCR, including retained inputs."""
import io
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile
from PIL import Image

from backend.api import tasks
from backend.services import workflow_inputs
from backend.tools.file_processing import extract_raw_files_from_archive, extract_text_from_upload, inspect_upload_content, validate_upload_content


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return output.getvalue()


@pytest.mark.parametrize("name", ["response.csv", "response.rst", "response.json", "response.html", "response.doc"])
def test_unsupported_response_suffix_is_rejected_directly_and_in_archives(name):
    for data, filename in [(b"answer", name), (archive({name: b"answer", "healthy.markdown": b"# answer"}), "responses.zip")]:
        sources = extract_raw_files_from_archive(data, filename)
        assert sources[0].pre_error_code == "submission_source_unsupported"
        assert sources[0].content == b"answer"  # Retain evidence, not silently skip it.
        if len(sources) > 1:
            assert sources[1].pre_error_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["response.txt", "response.md", "response.markdown", "response.MARKDOWN"])
async def test_response_text_formats_accept_real_utf8_and_markdown_html(name):
    content = "# 作答\n<div>1+1=2</div>".encode()
    assert await extract_text_from_upload(content, name, purpose="submissions") == content.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("name,body", [
    ("response.png", b"\x89PNG\r\n\x1a\nnot an image"),
    ("response.pdf", b"%PDF-1.7\nnot a PDF"),
    ("response.txt", b"\x89PNG\r\n\x1a\nnot text"),
    ("response.png", b"ordinary readable text"),
])
async def test_spoofed_or_corrupt_response_never_reaches_ocr(name, body):
    ocr = SimpleNamespace(recognize_images=Mock(side_effect=AssertionError("must not call a model")))
    with pytest.raises(HTTPException) as caught:
        await extract_text_from_upload(body, name, ocr_skill=ocr)
    assert caught.value.status_code in {400, 415}
    ocr.recognize_images.assert_not_called()


@pytest.mark.asyncio
async def test_real_image_decodes_and_is_not_rejected_by_extension_only():
    output = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(output, "PNG")
    inspection = await validate_upload_content(output.getvalue(), "response.PNG", purpose="submissions")
    assert inspection.content_type == "image/png"
    with pytest.raises(HTTPException):
        await validate_upload_content(output.getvalue(), "response.jpg", purpose="submissions")


def test_v7_tar_has_a_real_checked_header_without_ustar():
    output = io.BytesIO()
    import tarfile
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as z:
        item = tarfile.TarInfo("answer.txt")
        item.size = 2
        z.addfile(item, io.BytesIO(b"ok"))
    body = bytearray(output.getvalue())
    body[257:265] = b"\x00" * 8
    body[148:156] = b" " * 8
    checksum = sum(body[:512])
    body[148:156] = f"{checksum:06o}\0 ".encode()
    assert inspect_upload_content(bytes(body), "responses.tar").content_type == "application/x-tar"
    assert extract_raw_files_from_archive(bytes(body), "responses.tar")[0].content == b"ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [False, True])
async def test_new_and_reused_csv_inputs_cannot_bypass_submission_policy(monkeypatch, stored):
    queue = Mock(side_effect=AssertionError("must not enqueue"))
    monkeypatch.setattr(tasks.task_facade, "queue_task_submission_parsing", queue)
    monkeypatch.setattr(tasks.workflow_repository, "get_live_workflow", lambda *a, **k: SimpleNamespace(workflow_revision=1, last_failed_job_id=None, parse_job_id=None))
    monkeypatch.setattr(workflow_inputs, "load_submission_input", lambda **k: (SimpleNamespace(original_name="old.csv", content_type="text/csv"), b"id,answer\n1,2"))
    upload = None if stored else UploadFile(file=io.BytesIO(b"id,answer\n1,2"), filename="new.csv")
    with pytest.raises(HTTPException) as caught:
        await tasks.parse_submissions_endpoint(task_id="task", background_tasks=BackgroundTasks(), file=upload,
            stored_file_id="stored" if stored else None, expected_workflow_revision=1,
            current=SimpleNamespace(id="teacher"), registry=object())
    assert caught.value.detail["code"] == "submission_source_unsupported"
    queue.assert_not_called()

@pytest.mark.asyncio
async def test_pdf_header_quoted_in_text_is_not_misreported_as_a_corrupt_pdf():
    content = b"%PDF-1.7 is an example header, not a document.\n"
    assert await extract_text_from_upload(content, "format-notes.txt", purpose="submissions") == content.decode()
    source = extract_raw_files_from_archive(archive({"format-notes.txt": content}), "responses.zip")[0]
    assert source.pre_error_code is None


@pytest.mark.asyncio
async def test_retry_of_old_csv_input_is_rejected_before_queue(monkeypatch):
    queue = Mock(side_effect=AssertionError("must not enqueue"))
    monkeypatch.setattr(tasks.task_facade, "queue_task_submission_parsing", queue)
    monkeypatch.setattr(tasks.task_facade, "require_current_submission_pipeline", lambda payload: None)
    monkeypatch.setattr(tasks.workflow_repository, "get_live_workflow", lambda *a, **k: SimpleNamespace(workflow_revision=1, last_failed_job_id="job", parse_job_id="job"))
    monkeypatch.setattr(tasks.workflow_repository, "get_operation", lambda *a, **k: SimpleNamespace(assignment_id="task", operation_type="submission_recognition", attempt=1, payload={}, checkpoint={}, error_code="workflow_failed"))
    monkeypatch.setattr(tasks.source_outcome_repository, "summarize_sources", lambda **k: SimpleNamespace(failed_count=1))
    monkeypatch.setattr(tasks.task_facade, "load_submission_retry_upload", lambda **k: {"filename": "old.csv", "content": b"id,answer\n1,2", "content_type": "text/csv"})
    with pytest.raises(HTTPException) as caught:
        await tasks.retry_submission_recognition_endpoint(task_id="task", job_id="job",
            request=tasks.RetrySubmissionRecognitionRequest(expected_workflow_revision=1),
            background_tasks=BackgroundTasks(), current=SimpleNamespace(id="teacher"), registry=object())
    assert caught.value.detail["code"] == "submission_source_unsupported"
    queue.assert_not_called()


@pytest.mark.asyncio
async def test_real_pdf_with_printable_preamble_cannot_be_disguised_as_text():
    import fitz
    document = fitz.open()
    document.new_page()
    content = b"Printed report\n" + document.tobytes()
    document.close()
    assert (await validate_upload_content(content, "report.pdf", purpose="submissions")).content_type == "application/pdf"
    with pytest.raises(HTTPException) as caught:
        await validate_upload_content(content, "report.txt", purpose="submissions")
    assert caught.value.detail["code"] == "submission_source_content_type_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("name,content", [
    ("notes.txt", b"Example signature: %PDF-1.7 is only text."),
    ("cases.csv", b"input,expected\n%PDF-1.7,text\n"),
    ("cases.json", b'{"input":"%PDF-1.7","expected":"text"}'),
])
async def test_text_materials_can_quote_a_pdf_marker(name, content):
    assert await extract_text_from_upload(content, name, purpose="test_cases") == content.decode()
