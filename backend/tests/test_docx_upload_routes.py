"""A real Word document through the mounted routes, immutable originals and recovery."""
import hashlib
import io
import json
import zipfile

from docx import Document
from fastapi import HTTPException
import pytest

from backend.db import file_repository, knowledge_repository, workflow_repository
from backend.domain.source_outcomes import source_requires_reupload
from backend.storage import get_storage
from backend.tests.test_ocr_mounted_routes import client_registry, headers
from backend.tests.test_programming_input_mapping_regressions import DOCX_MIME, docx, prepare
from backend.tests.test_task_background_workflows import _seed_task
from backend.tests.test_upload_contract import office_package
from backend.tools.file_processing import extract_text_from_upload


@pytest.fixture
def word():
    document = Document()
    document.add_paragraph("Question 1: Trace the code.")
    document.add_paragraph("def inc(x):\n\treturn x + 1")
    row = document.add_table(rows=1, cols=2).rows[0]
    row.cells[0].text, row.cells[1].text = "输入 2", "Output 3"
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


TEXT = "Question 1: Trace the code.\ndef inc(x):\n\treturn x + 1\n输入 2\tOutput 3"


def assert_original(owner, file_id, body):
    stored = file_repository.get_file(file_id=file_id, owner_id=owner)
    assert stored.content_type == DOCX_MIME and stored.sha256 == hashlib.sha256(body).hexdigest()
    with get_storage().open(stored.storage_key) as stream:
        assert stream.read() == body


@pytest.mark.parametrize("role", ["problem", "reference_answer", "rubric", "programming_tests"])
def test_teacher_preflight_preserves_docx_bytes_and_literal_text(client_registry, word, role):
    client, _ = client_registry
    owner, task = _seed_task()
    route = f"/tasks/{task}/question-preparation/sources/preflight"
    response = client.post(route, headers=headers(owner), data={"role": role, "save_to_library": "true"},
                           files={"file": ("课件.DOCX", word, "application/zip")})
    assert response.status_code == 200, response.text
    operation = workflow_repository.get_operation(response.json()["source_token"], owner_id=owner)
    assert operation.payload["text"] == TEXT
    assert_original(owner, operation.payload["source_ref"]["stored_file_id"], word)
    assert operation.payload["library_material_id"]
    saved = client.post(route, headers=headers(owner), data={"role": role,
        "stored_file_id": operation.payload["source_ref"]["stored_file_id"]})
    assert saved.status_code == 200, saved.text
    derived = client.post(route, headers=headers(owner), data={"role": role,
        "library_material_id": operation.payload["library_material_id"]})
    assert derived.status_code == 200, derived.text
    assert workflow_repository.get_operation(derived.json()["source_token"], owner_id=owner).payload["text"] == TEXT


@pytest.mark.parametrize("target", ["criterion", "reference_answer", "test_cases"])
def test_bulk_import_preflight_keeps_material_original(client_registry, word, target):
    client, _ = client_registry
    owner, task = _seed_task(with_question=True)
    response = client.post(f"/tasks/{task}/material-imports/preflight", headers=headers(owner),
        data={"targets": json.dumps([target])}, files={"file": ("materials.DOCX", word, DOCX_MIME)})
    assert response.status_code == 200, response.text
    operation = workflow_repository.get_operation(response.json()["source_token"], owner_id=owner)
    assert_original(owner, operation.payload["original_stored_file_id"], word)
    artifact = file_repository.get_file(file_id=operation.payload["text_artifact_id"], owner_id=owner)
    with get_storage().open(artifact.storage_key) as stream:
        assert stream.read().decode() == TEXT


@pytest.mark.parametrize("path,field", [("/knowledge/documents", "id"), ("/course-materials/", "document_id"),
                                         ("/tasks/{task}/kb", "doc_id")])
def test_knowledge_upload_indexes_native_word_and_downloads_original(client_registry, word, path, field):
    client, _ = client_registry
    owner, task = _seed_task()
    response = client.post(path.format(task=task), headers=headers(owner), data={"native_only": "true"},
                           files={"file": ("book.DOCX", word, DOCX_MIME)})
    assert response.status_code in {200, 201}, response.text
    document_id = response.json()[field]
    document = knowledge_repository.get_document(document_id, owner)
    assert document.status == "ready" and document.ingestion_summary["coverage_complete"]
    assert document.ingestion_summary["usage"].get("initial_calls", 0) == 0
    assert knowledge_repository.list_chunks([document_id])[0].content == TEXT
    chunks = knowledge_repository.list_chunks([document_id])
    download = f"/knowledge/documents/{document_id}"
    if field == "doc_id":  # Task-only documents require an owner-bound citation.
        download += f"/citations/{chunks[0].id}"
    assert client.get(download + "/download", headers=headers(owner)).content == word


def test_complex_word_in_library_remains_downloadable_without_partial_index_or_retry(client_registry):
    client, _ = client_registry
    owner, _ = _seed_task()
    body = docx("<w:p><w:r><w:t>Partial text</w:t><w:footnoteReference w:id='1'/></w:r></w:p>")
    response = client.post("/knowledge/documents", headers=headers(owner), data={"native_only": "true"},
                           files={"file": ("book.docx", body, DOCX_MIME)})
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["status"] == "failed" and result["ingestion"]["error_code"] == "source_docx_incomplete"
    assert not knowledge_repository.list_chunks([result["id"]])
    assert client.get(f"/knowledge/documents/{result['id']}/download", headers=headers(owner)).content == body
    retry = client.post(f"/knowledge/documents/{result['id']}/retry-failed", headers=headers(owner))
    assert retry.status_code == 422 and retry.json()["error"]["code"] == "source_docx_incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["main_type", "root_target", "external_link", "no_body", "duplicate", "unsafe",
                                   "utf16_dtd", "secondary_crc", "local_flags", "compression", "oversized", "directory_count"])
async def test_abnormal_package_never_returns_partial_text(fault):
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(office_package())) as src, zipfile.ZipFile(out, "w") as dst:
        for part in src.namelist():
            data = src.read(part)
            if fault == "main_type" and part == "[Content_Types].xml":
                data = data.replace(b"document.main+xml", b"template.main+xml")
            if part == "_rels/.rels":
                if fault == "root_target":
                    data = data.replace(b'Target="word/document.xml"', b'Target="wrong.xml"')
                if fault == "external_link":
                    data = data.replace(b'Target=', b'TargetMode="External" Target=')
            if part == "word/document.xml" and fault == "no_body":
                data = data.replace(b"w:body", b"w:missing")
            if part == "word/document.xml" and fault == "utf16_dtd":
                data = ('<!DOCTYPE doc [<!ENTITY test "hidden">]>' + data.decode()).encode("utf-16")
            dst.writestr(part, data)
        if fault == "duplicate":
            dst.writestr("WORD/DOCUMENT.XML", b"x")
        if fault == "unsafe":
            dst.writestr("../hidden", b"x")
        if fault == "secondary_crc":
            dst.writestr("custom/part", b"SECONDARY_CRC_SENTINEL")
        if fault == "compression":
            dst.writestr("custom/part", b"x", compress_type=zipfile.ZIP_BZIP2)
        if fault == "oversized":
            dst.writestr("word/styles.xml", b" " * (5 * 1024 * 1024 + 1), compress_type=zipfile.ZIP_DEFLATED)
    data = out.getvalue()
    if fault == "secondary_crc":
        data = data.replace(b"SECONDARY_CRC_SENTINEL", b"!" * len(b"SECONDARY_CRC_SENTINEL"))
    if fault == "local_flags":
        data = data[:6] + b"\x01\x00" + data[8:]  # Local-only encryption flag.
    if fault == "directory_count":
        data = data[:-12] + b"\xff\xff" + data[-10:]
    with pytest.raises(HTTPException) as caught:
        await extract_text_from_upload(data, "source.DOCX", purpose="rubric")
    expected = "source_docx_limit_exceeded" if fault == "oversized" else "source_docx_invalid"
    assert caught.value.detail["code"] == expected


@pytest.mark.asyncio
async def test_mixed_zip_keeps_one_docx_per_member_and_permanent_repair_classification(word):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("group/OK.DOCX", word)
        archive.writestr("group/complex.docx", docx("<w:p><w:r><w:t>x</w:t><w:ptab/></w:r></w:p>"))
        archive.writestr("group/bad.docx", b"PK\x03\x04broken")
    owner, _, sources = await prepare(out.getvalue(), "class.zip")
    good, complex_source, invalid = sources
    assert good.text == TEXT and good.filename == "group/OK.DOCX"
    assert_original(owner, good.stored_file_id, word)
    for item, code in [(complex_source, "source_docx_incomplete"), (invalid, "source_docx_invalid")]:
        assert item.pre_error_code == code and item.text is None
        assert source_requires_reupload(code, item.retryable)


@pytest.mark.asyncio
async def test_worker_launch_failure_preserves_retryable_original(word, monkeypatch):
    from backend.tools import file_processing

    async def unavailable(*args, **kwargs):
        raise OSError("synthetic worker shortage")

    monkeypatch.setattr(file_processing.asyncio, "create_subprocess_exec", unavailable)
    owner, _, (source,) = await prepare(word, "answer.docx")
    assert source.pre_error_code == "source_docx_unavailable" and source.retryable
    assert not source_requires_reupload(source.pre_error_code, source.retryable)
    assert_original(owner, source.stored_file_id, word)


def test_word_cannot_be_used_as_a_roster_or_disguised_as_a_class_zip(word):
    from backend.domain.errors import ValidationError
    from backend.services.roster_import import parse_roster
    from backend.tools.file_processing import extract_raw_files_from_archive

    with pytest.raises(ValidationError) as caught:
        parse_roster(word, filename="roster.DOCX")
    assert caught.value.code == "submission_roster_unsupported"
    (source,) = extract_raw_files_from_archive(word, "class.zip")
    assert source.pre_error_code == "submission_source_content_type_mismatch" and source.content == word


@pytest.mark.asyncio
async def test_durable_retry_reuses_word_original_then_requires_replacement_for_remaining_gap(word, monkeypatch):
    from backend.services import task_facade
    from backend.tests.test_submission_incremental import _registry, _response
    from backend.tests.test_submission_upload_retry_policy import finish, retry
    from backend.tools import file_processing

    owner, task = _seed_task(with_question=True)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("S1.DOCX", word)
        archive.writestr("S2.DOCX", docx("<w:p><w:r><w:drawing/></w:r></w:p>"))
    body = stream.getvalue()

    async def invoke(messages):
        assert "return x + 1" in messages[-1].content
        return _response(content=TEXT)

    registry = _registry(monkeypatch, invoke)
    original_reader = file_processing._extract_docx_text

    async def busy_once(data, **kwargs):
        if data == word:
            raise HTTPException(429, detail={"code": "source_docx_busy"})
        return await original_reader(data, **kwargs)

    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner, filename="class.zip",
        content=body, content_type="application/zip", registry=registry)
    with monkeypatch.context() as initial:
        initial.setattr(file_processing, "_extract_docx_text", busy_once)
        await finish(queued["job_id"], owner)
    snapshot = task_facade.get_task(task_id=task, owner_id=owner)
    reasons = {item["file_name"]: item for item in snapshot["submission_sources"]}
    assert reasons["S1.DOCX"]["reason_code"] == "source_docx_busy" and reasons["S1.DOCX"]["retryable"]
    assert reasons["S2.DOCX"]["reason_code"] == "source_docx_incomplete" and reasons["S2.DOCX"]["requires_reupload"]
    registry.provider.ainvoke.assert_not_awaited()
    restarted = await retry(task, owner, snapshot)
    assert restarted["reused_original_upload"]
    await finish(restarted["job_id"], owner)
    after = task_facade.get_task(task_id=task, owner_id=owner)
    assert after["submission_source_summary"]["parsed"] == 1
    assert after["submission_source_summary"]["failed"] == 1
    registry.provider.ainvoke.assert_awaited_once()
    blocked = await retry(task, owner, after)
    assert blocked.status_code == 409 and "submission_retry_requires_reupload" in blocked.body.decode()
