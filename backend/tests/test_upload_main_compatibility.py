"""Preserve main's existing inputs without bypassing upload security boundaries."""
import hashlib
import io
import tarfile

import pytest
from fastapi import UploadFile
from PIL import Image
from starlette.datastructures import Headers

from backend.api.task_preparation import _read_source
from backend.domain.errors import RecognitionError
from backend.services.upload_contract import UploadContractError, validate_submission_upload, validate_upload
from backend.tests.test_question_score_contract import _seed_question_task, _VisionRegistry
from backend.tools.file_processing import extract_text_from_upload


def image_bytes(format):
    stream = io.BytesIO()
    Image.new("RGB", (24, 32), "white").save(stream, format)
    return stream.getvalue()


@pytest.mark.parametrize("role", ["problem_reference", "rubric", "programming_tests", "student_submission"])
@pytest.mark.parametrize("extension,format", [("bmp", "BMP"), ("tiff", "TIFF"), ("webp", "WEBP")])
def test_existing_image_formats_keep_original_hash_and_role_validation(role, extension, format):
    body = image_bytes(format)
    result = validate_upload(role, "source." + extension, body)
    assert result.sha256 == hashlib.sha256(body).hexdigest()
    with pytest.raises(UploadContractError):
        validate_upload(role, "source." + extension, b"not an image")


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["rubric", "programming_tests"])
@pytest.mark.parametrize("opt_in", [False, True])
async def test_existing_teacher_ocr_requires_explicit_opt_in_and_preserves_original(role, opt_in):
    owner, task = "compat-owner", "compat-task"
    _seed_question_task(owner, task)
    registry = _VisionRegistry()
    body = image_bytes("PNG")
    upload = UploadFile(file=io.BytesIO(body), filename="teacher.png",
                        headers=Headers({"content-type": "image/png"}))
    args = dict(file=upload, library_material_id=None, inline_text=None, owner_id=owner,
                task_id=task, registry=registry, role=role, enable_material_ocr=opt_in)
    if not opt_in:
        with pytest.raises(RecognitionError) as caught:
            await _read_source(**args)
        assert caught.value.code == "material_ocr_confirmation_required"
        assert registry.provider.calls == []
        return
    text, descriptor = await _read_source(**args)
    assert text == "Question 1: OCR result"
    assert len(registry.provider.calls) == 1
    assert descriptor["sha256"] == hashlib.sha256(body).hexdigest()
    from backend.db.file_repository import get_file
    from backend.storage import get_storage
    stored = get_file(file_id=descriptor["stored_file_id"], owner_id=owner)
    with get_storage().open(stored.storage_key) as original:
        assert original.read() == body


@pytest.mark.asyncio
@pytest.mark.parametrize("name,purpose", [
    ("teacher.markdown", "rubric"), ("tests.markdown", "test_cases"),
    ("tests.csv", "test_cases"), ("legacy.rst", "problems"),
    ("answer.txt", "submissions"),
])
async def test_existing_native_text_inputs_are_read_as_data_without_execution(name, purpose):
    body = "原始输入与预期，不执行\n".encode()
    assert await extract_text_from_upload(body, name, purpose=purpose) == body.decode()


@pytest.mark.parametrize("name,mode", [("answers.tar", "w"), ("answers.tgz", "w:gz"),
                                        ("answers.tar.bz2", "w:bz2")])
def test_existing_non_zip_archives_keep_bounded_signature_validation(name, mode, monkeypatch):
    body = io.BytesIO()
    with tarfile.open(fileobj=body, mode=mode) as archive:
        member = tarfile.TarInfo("1001.txt")
        member.size = 6
        archive.addfile(member, io.BytesIO(b"answer"))
    content = body.getvalue()
    validate_submission_upload(name, content)
    with pytest.raises(UploadContractError):
        validate_submission_upload(name, b"not an archive")
    from backend.tools import file_processing
    monkeypatch.setattr(file_processing, "SUBMISSION_UPLOAD_MAX_BYTES", len(content) - 1)
    with pytest.raises(UploadContractError) as caught:
        validate_submission_upload(name, content)
    assert caught.value.code == "submission_source_too_large"


def test_native_text_compatibility_does_not_grant_binary_or_other_roles_permission():
    for role, name, content in [
        ("student_submission", "answer.data", b"MZ\x00\xffbinary"),
        ("course_knowledge", "answer.csv", b"a,b\n1,2"),
        ("rubric", "answer.py", b"print(1)"),
    ]:
        with pytest.raises(UploadContractError):
            validate_upload(role, name, content)


def test_oversized_native_text_is_rejected_before_decoding(monkeypatch):
    from backend.tools import file_processing
    monkeypatch.setattr(file_processing, "SUBMISSION_UPLOAD_MAX_BYTES", 3)

    class DecodeForbidden(bytes):
        def decode(self, *args, **kwargs):
            raise AssertionError("Oversized input must be rejected before text decoding")

    with pytest.raises(UploadContractError) as caught:
        validate_upload("student_submission", "answer.txt", DecodeForbidden(b"four"))
    assert caught.value.code == "submission_source_too_large"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["rubric", "programming_tests"])
@pytest.mark.parametrize("pending", [False, True])
async def test_saved_teacher_ocr_is_reused_but_pending_read_honors_opt_in(role, pending):
    import fitz
    import time
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from backend.api import task_preparation as api
    from backend.models import ProblemSourceDraft
    from backend.progress.tracker import ProgressReporter
    from backend.services.source_files import persist_problem_source
    from backend.services.stage_provider_routing import StageProviderRoute
    from backend.storage import get_storage
    owner, task = "saved-ocr-owner", "saved-ocr-task"
    _seed_question_task(owner, task)
    with fitz.open() as document:
        page = document.new_page()
        # Non-text visible ink: a truly blank PDF is correctly classified as
        # verified blank and would not exercise the OCR dispatch/reuse path.
        page.draw_rect(fitz.Rect(80, 80, 250, 150), color=(0, 0, 0), fill=(0, 0, 0))
        body = document.tobytes()
    stored, _ = persist_problem_source(storage=get_storage(), owner_id=owner, task_id=task,
        original_name="teacher.pdf", content=body, content_type="application/pdf")
    draft = ProblemSourceDraft(source_token="saved", task_id=task, owner_id=owner,
        role=role, source_kind="upload", structure_mode="organized", filename="teacher.pdf",
        content_type="application/pdf", size_bytes=len(body), content_sha256=stored.sha256,
        expires_at=time.time() + 60)
    registry = _VisionRegistry()
    route = StageProviderRoute(route_id=registry.provider.provider_id, kind="llm", provider=registry.provider)
    payload = dict(recognition_pending=pending, enable_material_ocr=True, source_ref={"stored_file_id": stored.id})
    operation = SimpleNamespace(owner_id=owner, assignment_id=task, operation_id="saved",
                                payload={}, attempt=1, update_progress=AsyncMock())
    resolved = await api._recognize_saved_question_sources(
        [(draft, "Frozen teacher transcript", payload)], operation=operation, route=route,
        registry=registry, reporter=ProgressReporter("saved"))
    assert resolved[0][1] == ("Question 1: OCR result" if pending else "Frozen teacher transcript")
    assert len(registry.provider.calls) == int(pending)


@pytest.mark.parametrize("kind", ["7z", "tgz", "rar"])
def test_archive_preflight_does_not_decompress_legacy_container_metadata(monkeypatch, kind):
    from backend.tools import file_processing as files
    stream = io.BytesIO()
    if kind == "7z":
        with files.py7zr.SevenZipFile(stream, "w") as archive:
            archive.writestr("answer", "1001.txt")
        content = stream.getvalue()
    elif kind == "tgz":
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            member = tarfile.TarInfo("1001.txt")
            member.size = 6
            archive.addfile(member, io.BytesIO(b"answer"))
        content = stream.getvalue()
    else:
        # Empty RAR4: marker plus the standard 13-byte main header. No member
        # payload is necessary to check the outer format/security boundary.
        content = b"Rar!\x1a\x07\x00\xcf\x90s\x00\x00\x0d\x00" + b"\x00" * 6

    def forbidden(*args, **kwargs):
        raise AssertionError("Outer validation must not decompress archive metadata")

    monkeypatch.setattr(files.py7zr, "SevenZipFile", forbidden)
    monkeypatch.setattr(files.rarfile, "RarFile", forbidden)
    monkeypatch.setattr(files.tarfile, "open", forbidden)
    validate_submission_upload("answers." + kind, content)
    if kind == "7z":
        corrupt = bytearray(content)
        corrupt[12] ^= 1
        with pytest.raises(UploadContractError):
            validate_submission_upload("answers.7z", bytes(corrupt))
