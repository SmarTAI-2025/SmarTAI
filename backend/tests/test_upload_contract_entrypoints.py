"""Real source-reading boundaries; only provider calls use test doubles."""
from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from backend.tools.file_processing import extract_raw_files_from_archive, extract_text_from_upload
from backend.tests.test_file_processing import FakeOCRSkill, _valid_png_1x1
from backend.tests.test_ocr_mounted_routes import client_registry, headers


@pytest.mark.asyncio
async def test_student_reader_rejects_csv_instead_of_silently_reading_it():
    with pytest.raises(HTTPException) as caught:
        await extract_text_from_upload(b"student,answer\n1,2\n", "submission.csv", purpose="submissions")
    assert caught.value.status_code == 415
    assert caught.value.detail["code"] == "submission_source_type_not_allowed"


@pytest.mark.asyncio
async def test_rubric_reader_rejects_markdown_alias_not_in_its_matrix():
    with pytest.raises(HTTPException) as caught:
        await extract_text_from_upload(b"Teacher standard", "rubric.markdown", purpose="rubric")
    assert caught.value.detail["code"] == "source_type_not_allowed"


@pytest.mark.asyncio
async def test_programming_json_is_read_as_validated_data_without_ocr():
    original = b'{"cases":[{"input":"eval(x)","expected_output":"NaN"}]}'
    skill = FakeOCRSkill("must not replace teacher data")
    text = await extract_text_from_upload(original, "tests.json", purpose="test_cases", ocr_skill=skill, content_type="application/json")
    assert text == original.decode()
    assert skill.calls == []


@pytest.mark.asyncio
async def test_programming_json_non_finite_data_is_rejected_before_reading():
    with pytest.raises(HTTPException) as caught:
        await extract_text_from_upload(b'{"cases":[{"value":1e999}]}', "tests.json", purpose="test_cases")
    assert caught.value.detail["code"] == "programming_tests_json_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["rubric", "test_cases", "knowledge"])
async def test_restricted_image_inputs_do_not_reach_ocr(purpose):
    skill = FakeOCRSkill("must not be called")
    with pytest.raises(HTTPException) as caught:
        await extract_text_from_upload(_valid_png_1x1(), "source.png", purpose=purpose, ocr_skill=skill)
    assert caught.value.detail["code"] == "source_type_not_allowed"
    assert skill.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["submissions", "problems", "reference"])
async def test_allowed_image_inputs_keep_real_reader_ocr_dispatch(purpose):
    skill = FakeOCRSkill("Literal student error 1 + 1 = 3")
    text = await extract_text_from_upload(_valid_png_1x1(), "source.png", purpose=purpose, ocr_skill=skill)
    assert text == "Literal student error 1 + 1 = 3"
    assert len(skill.calls) == 1 and skill.calls[0]["purpose"] == purpose


@pytest.mark.asyncio
async def test_declared_pdf_cannot_override_a_plain_text_file():
    with pytest.raises(HTTPException) as caught:
        await extract_text_from_upload(b"plain text", "source.txt", purpose="submissions", content_type="application/pdf")
    assert caught.value.detail["code"] == "submission_source_content_type_mismatch"


def archive(items):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        for name, body in items:
            package.writestr(name, body)
    return output.getvalue()


def test_raw_zip_member_policy_preserves_legal_sibling_and_rejected_original():
    sources = extract_raw_files_from_archive(archive([("good.txt", b"valid student source"), ("bad.csv", b"name,answer")]), "class.zip")
    by_name = {source.filename: source for source in sources}
    assert set(by_name) == {"good.txt", "bad.csv"}
    assert by_name["good.txt"].content == b"valid student source"
    assert by_name["good.txt"].pre_error_code is None
    assert by_name["bad.csv"].content == b"name,answer"
    assert by_name["bad.csv"].pre_error_code == "submission_source_type_not_allowed"


def test_docx_is_one_source_not_an_archive_of_students():
    from backend.tests.test_upload_contract import office_package
    data = office_package()
    sources = extract_raw_files_from_archive(data, "answer.docx")
    assert len(sources) == 1
    assert sources[0].filename == "answer.docx"
    assert sources[0].content == data


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["rubric", "test_cases"])
async def test_native_teacher_text_remains_readable_without_ocr(purpose):
    skill = FakeOCRSkill("not teacher original")
    text = await extract_text_from_upload(b"Teacher original standard -2", "teacher.txt", purpose=purpose, ocr_skill=skill)
    assert text == "Teacher original standard -2"
    assert skill.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["rubric", "test_cases"])
async def test_shared_reader_cannot_opt_restricted_images_into_ocr(tmp_path, monkeypatch, purpose):
    from backend.domain.errors import RecognitionError
    from backend.services import question_sources
    from backend.services.stage_provider_routing import StageProviderRoute
    from backend.tests.test_recognition_reader import llm
    from backend.tests.test_recognition_v2_artifacts import setup
    _, request, data, results = setup(tmp_path)
    provider, _ = llm(text="Teacher literal -2")
    registry = SimpleNamespace(list_configs=lambda: [{"provider_id": provider.provider_id, "enabled": True}], pick_vision=lambda preferred=None: provider)
    route = StageProviderRoute(route_id=provider.provider_id, kind="llm", provider=provider)
    monkeypatch.setattr(question_sources, "get_storage", lambda: results.store.storage)
    with pytest.raises(RecognitionError, match="source_type_not_allowed"):
        await question_sources.read_question_source(
            owner_id=request.source.owner_id, task_id=request.source.business_id,
            content=data, filename="teacher.png", stored_file_id=request.source.stored_file_id,
            purpose=purpose, allow_vision=True, route=route, registry=registry,
        )
    provider.ainvoke_vision.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["rubric", "test_cases"])
async def test_compatibility_pdf_reader_cannot_enable_teacher_ocr(purpose):
    import fitz
    with fitz.open() as document:
        document.new_page()
        data = document.tobytes()
    skill = FakeOCRSkill("Forbidden teacher OCR")
    with pytest.raises(HTTPException) as caught:
        await extract_text_from_upload(data, "scan.pdf", purpose=purpose, ocr_skill=skill)
    assert caught.value.detail["code"] == "material_ocr_not_allowed_for_role"
    assert skill.calls == []


@pytest.mark.parametrize("role,extensions", [
    ("problem", {".pdf", ".txt", ".md", ".markdown", ".docx", ".png", ".jpg", ".jpeg"}),
    ("reference_answer", {".pdf", ".txt", ".md", ".markdown", ".docx", ".png", ".jpg", ".jpeg"}),
    ("rubric", {".pdf", ".txt", ".md"}),
    ("programming_tests", {".pdf", ".txt", ".md", ".json"}),
])
def test_draft_selectable_types_match_the_business_role(role, extensions):
    from backend.api import task_preparation
    assert set(task_preparation._accepted_source_extensions(role, has_vision=True)) == extensions
    assert set(task_preparation._accepted_source_extensions(role, has_vision=False)) == extensions


@pytest.mark.asyncio
async def test_knowledge_upload_observes_lower_canonical_limit_before_publication(monkeypatch):
    from backend.knowledge.service import ingest_document
    from backend.services import upload_contract
    from backend.tests.test_task_background_workflows import _seed_task
    owner, _ = _seed_task()
    monkeypatch.setattr(upload_contract, "KNOWLEDGE_MAX_BYTES", 3)
    with pytest.raises(upload_contract.UploadContractError) as caught:
        await ingest_document(owner_id=owner, original_name="notes.txt", content=b"four", native_only=True)
    assert caught.value.code == "source_too_large"
    from backend.db.knowledge_repository import list_documents
    assert list_documents(owner) == []


@pytest.mark.parametrize("filename,content,code", [
    ("source.csv", b"teacher,question", "source_type_not_allowed"),
    ("source.png", b"not an image", "submission_source_content_type_mismatch"),
])
def test_problem_queue_rejects_invalid_file_before_publishing_source(filename, content, code):
    from backend.domain.errors import ValidationError
    from backend.db import file_repository
    from backend.services import task_facade
    from backend.tests.test_task_background_workflows import _seed_task
    from backend.tests.test_question_preparation_recovery import _RecoveryRegistry
    owner, task = _seed_task()
    with pytest.raises(ValidationError) as caught:
        task_facade.queue_task_problem_extraction(task_id=task, owner_id=owner,
            filename=filename, content=content, content_type=None, registry=_RecoveryRegistry())
    assert caught.value.code == code
    assert file_repository.list_files(owner_id=owner, assignment_id=task) == []


@pytest.mark.parametrize("filename,content", [
    ("answers.rar", b"Rar!\x1a\x07\x00invalid"),
    ("answers.7z", b"7z\xbc\xaf\x27\x1cinvalid"),
    ("answers.zip", b"not a ZIP"),
])
def test_submission_queue_denies_non_zip_or_invalid_container(filename, content):
    from backend.domain.errors import ValidationError
    from backend.db import file_repository
    from backend.services import task_facade
    from backend.tests.test_task_background_workflows import _seed_task
    from backend.tests.test_question_preparation_recovery import _RecoveryRegistry
    owner, task = _seed_task(with_question=True)
    with pytest.raises(ValidationError) as caught:
        task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
            filename=filename, content=content, content_type=None, registry=_RecoveryRegistry())
    assert caught.value.code in {"submission_source_type_not_allowed", "submission_archive_invalid"}
    assert file_repository.list_files(owner_id=owner, assignment_id=task) == []


@pytest.mark.parametrize("alias", ["problem-sources/preflight", "question-preparation/sources/preflight"])
@pytest.mark.parametrize("role", ["rubric", "programming_tests"])
def test_mounted_old_aliases_cannot_opt_images_into_teacher_ocr(client_registry, monkeypatch, alias, role):
    from unittest.mock import AsyncMock
    from backend.db import file_repository
    from backend.tests.test_task_background_workflows import _seed_task
    client, registry = client_registry
    provider = registry.provider
    monkeypatch.setattr(provider, "supports_vision", True)
    invoke = AsyncMock(side_effect=AssertionError("Teacher image must never reach OCR"))
    monkeypatch.setattr(provider, "ainvoke_vision", invoke, raising=False)
    owner, task = _seed_task()
    response = client.post(f"/tasks/{task}/{alias}", headers=headers(owner),
        data={"role": role, "enable_material_ocr": "true"},
        files={"file": ("teacher.png", _valid_png_1x1(), "image/png")})
    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "source_type_not_allowed"
    assert file_repository.list_files(owner_id=owner, assignment_id=task) == []
    invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_json_is_rejected_before_creating_source_records():
    from fastapi import UploadFile
    from backend.api import task_preparation as api
    from backend.db import file_repository
    from backend.tests.test_task_background_workflows import _seed_task
    from backend.tests.test_question_preparation_recovery import _RecoveryRegistry
    owner, task = _seed_task()
    with pytest.raises(HTTPException) as caught:
        await api.preflight_problem_source(task_id=task,
            file=UploadFile(filename="tests.json", file=io.BytesIO(b'{"nested":[NaN]}')),
            library_material_id=None, inline_text=None, structure_mode="organized",
            extraction_hint="", save_to_library=False, recognition_options=None,
            role="programming_tests", enable_material_ocr=True,
            current=SimpleNamespace(id=owner), registry=_RecoveryRegistry())
    assert caught.value.detail["code"] == "programming_tests_json_invalid"
    assert file_repository.list_files(owner_id=owner, assignment_id=task) == []


@pytest.mark.asyncio
async def test_prepared_source_reuse_revalidates_original_against_current_role():
    from fastapi import UploadFile
    from backend.api import task_preparation as api
    from backend.db import file_repository, workflow_repository
    from backend.domain.errors import ValidationError
    from backend.tests.test_task_background_workflows import _seed_task
    from backend.tests.test_question_preparation_recovery import _RecoveryRegistry
    owner, task = _seed_task()
    response = await api.preflight_problem_source(task_id=task,
        file=UploadFile(filename="teacher.markdown", file=io.BytesIO(b"1. Original teacher standard")),
        library_material_id=None, inline_text=None, structure_mode="organized",
        extraction_hint="", save_to_library=False, recognition_options=None,
        role="problem", current=SimpleNamespace(id=owner), registry=_RecoveryRegistry())
    assert isinstance(response, dict), getattr(response, "body", response)
    operation = workflow_repository.get_operation(response["source_token"], owner_id=owner)
    # A historical prepared source with a newly restricted role cannot reuse
    # previously extracted text to grant its original extension permission.
    payload = {**operation.payload, "role": "rubric"}
    with pytest.raises(ValidationError) as caught:
        api._validated_question_source_ref(operation=operation, payload=payload, task_id=task, owner_id=owner)
    assert caught.value.code == "source_type_not_allowed"
    assert len(file_repository.list_files(owner_id=owner, assignment_id=task)) == 1


@pytest.mark.parametrize("path", ["extract_problems", "assignment-import", "problem-sources/preflight"])
def test_problem_upload_routes_bound_reads_by_effective_contract(client_registry, monkeypatch, path):
    from backend.services import upload_contract
    from backend.tests.test_task_background_workflows import _seed_task
    client, _ = client_registry
    owner, task = _seed_task()
    monkeypatch.setattr(upload_contract, "QUESTION_SOURCE_MAX_BYTES", 3)
    url = (f"/assignments/{task}/questions/import-file" if path == "assignment-import"
           else f"/tasks/{task}/{path}")
    response = client.post(url, headers=headers(owner), files={"file": ("source.txt", b"four", "text/plain")})
    assert response.status_code == 413, response.text


def test_capabilities_explicitly_deny_restricted_ocr_without_disabling_other_roles(client_registry):
    from backend.tests.test_task_background_workflows import _seed_task
    client, _ = client_registry
    owner, task = _seed_task()
    response = client.get(f"/tasks/{task}/question-preparation/capabilities", headers=headers(owner))
    assert response.status_code == 200
    roles = response.json()["source_roles"]
    assert roles["rubric"]["allow_vision"] is False
    assert roles["programming_tests"]["allow_vision"] is False
    assert roles["problem"]["allow_vision"] is True
    assert roles["reference_answer"]["allow_vision"] is True
    assert response.json()["reader"]["docx"] is False


@pytest.mark.asyncio
async def test_cached_submission_source_cannot_bypass_current_type_policy():
    from backend.services import submission_source_pipeline as pipeline
    from backend.db import workflow_repository, source_outcome_repository, file_repository
    from backend.tests.test_submission_source_pipeline_integrity import _seed_task, _create_operation
    owner, task = _seed_task()
    queued = _create_operation(owner, task)
    running = workflow_repository.claim_operation(queued.id, owner_id=owner, worker_id="policy-reuse", lease_seconds=60)
    reused = []
    def reuse(raw, source_id, stored_file_id):
        reused.append(source_id)
        return True
    result = await pipeline.prepare_submission_sources(content=b"student,answer", filename="answer.csv",
        content_type="text/csv", owner_id=owner, task_id=task, job_id=running.id,
        job_attempt=running.attempt, operation_lease_token=running.lease_token,
        ocr_skill=None, reuse_source=reuse)
    assert len(result) == 1
    assert result[0].pre_error_code == "submission_source_type_not_allowed"
    assert result[0].failure_phase == "source_read"
    assert reused == []
    source = source_outcome_repository.get_source(result[0].source_id, owner_id=owner)
    stored = file_repository.get_file(file_id=source.stored_file_id, owner_id=owner)
    assert stored.original_name == "answer.csv"
    assert stored.sha256 == __import__("hashlib").sha256(b"student,answer").hexdigest()


@pytest.mark.parametrize("actor,alias", [("student", "upload"), ("teacher", "teacher-upload")])
def test_personal_submission_denies_non_zip_container_before_original_storage(client_registry, actor, alias):
    from backend.tests.test_ocr_normalized_ingest import _published_assignment
    from backend.db import file_repository
    client, _ = client_registry
    teacher, student, task = _published_assignment("policy-" + alias)
    response = client.post(f"/submissions/{alias}", headers=headers(teacher if actor == "teacher" else student, actor),
        data={"assignment_id": task, "student_id": student},
        files={"file": ("answer.rar", b"Rar!\x1a\x07\x00invalid", "application/octet-stream")})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "submission_source_type_not_allowed"
    assert file_repository.list_files(owner_id=teacher, assignment_id=task) == []
    assert file_repository.list_files(owner_id=student) == []


def test_saved_submission_retry_revalidates_original_format_without_deleting_it():
    from backend.services import workflow_inputs
    from backend.db import file_repository
    from backend.domain.errors import ValidationError
    from backend.storage import get_storage
    from backend.tests.test_task_background_workflows import _seed_task
    owner, task = _seed_task()
    stored = file_repository.save_file(storage=get_storage(), owner_id=owner,
        kind="submission_source", original_name="old.csv", content=b"student,answer",
        content_type="text/csv", assignment_id=task)
    with pytest.raises(ValidationError) as caught:
        workflow_inputs.load_submission_input(task_id=task, owner_id=owner, stored_file_id=stored.id)
    assert caught.value.code == "submission_source_type_not_allowed"
    assert get_storage().exists(stored.storage_key)
    assert file_repository.get_file(file_id=stored.id, owner_id=owner).sha256 == stored.sha256


def test_knowledge_worker_revalidates_historical_original_before_reusing_it():
    from backend.knowledge import ingestion
    from backend.services.knowledge_storage import persist_knowledge_upload
    from backend.db import file_repository
    from backend.db.knowledge_repository import get_document
    from backend.domain.errors import RecognitionError
    from backend.storage import get_storage
    from backend.tests.test_task_background_workflows import _seed_task
    owner, _ = _seed_task()
    upload = persist_knowledge_upload(storage=get_storage(), owner_id=owner,
        original_name="old.csv", content=b"topic,answer", content_type="text/csv", retention_policy="retained")
    with pytest.raises(RecognitionError, match="source_type_not_allowed"):
        ingestion._read_source(SimpleNamespace(document_id=upload.document_id, owner_id=owner))
    document = get_document(upload.document_id, owner)
    stored = file_repository.get_file(file_id=document.stored_file_id, owner_id=owner)
    assert get_storage().exists(stored.storage_key)


@pytest.mark.parametrize("path,limit", [
    ("/knowledge/documents", "KNOWLEDGE_MAX_BYTES"),
    ("/course-materials/", "KNOWLEDGE_MAX_BYTES"),
    ("/tasks/{task}/kb", "KNOWLEDGE_MAX_BYTES"),
    ("/submissions/upload", "INDIVIDUAL_SUBMISSION_MAX_BYTES"),
    ("/submissions/teacher-upload", "INDIVIDUAL_SUBMISSION_MAX_BYTES"),
    ("/tasks/{task}/parse_submissions", "batch"),
])
def test_all_remaining_http_uploads_read_only_effective_limit_plus_one(client_registry, monkeypatch, path, limit):
    from starlette.datastructures import UploadFile
    from backend.services import upload_contract
    from backend.tools import file_processing
    from backend.tests.test_task_background_workflows import _seed_task
    from backend.tests.test_ocr_normalized_ingest import _published_assignment
    client, _ = client_registry
    actor = "teacher"
    data = {}
    if path.startswith("/submissions"):
        teacher, student, task = _published_assignment("bounded" + limit + path.rsplit("/", 1)[-1])
        actor = "student" if path.endswith("/upload") else "teacher"
        owner = student if actor == "student" else teacher
        data = {"assignment_id": task, "student_id": student}
    else:
        owner, task = _seed_task(with_question=True)
    if limit == "batch":
        monkeypatch.setattr(file_processing, "SUBMISSION_UPLOAD_MAX_BYTES", 3)
    else:
        monkeypatch.setattr(upload_contract, limit, 3)
    original_read = UploadFile.read
    async def read_bounded(self, size=-1):
        assert 0 <= size <= 4, "Upload read exceeds the effective ceiling before validation"
        return await original_read(self, size)
    monkeypatch.setattr(UploadFile, "read", read_bounded)
    response = client.post(path.format(task=task), headers=headers(owner, actor), data=data,
        files={"file": ("source.txt", b"four", "text/plain")})
    assert response.status_code >= 400


@pytest.mark.asyncio
async def test_background_submission_recovery_does_not_unpack_legacy_tar():
    import tarfile
    from backend.services import submission_source_pipeline as pipeline
    from backend.db import workflow_repository
    from backend.tests.test_submission_source_pipeline_integrity import _seed_task, _create_operation
    owner, task = _seed_task()
    queued = _create_operation(owner, task)
    running = workflow_repository.claim_operation(queued.id, owner_id=owner, worker_id="policy-tar", lease_seconds=60)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as package:
        member = tarfile.TarInfo("student.txt")
        member.size = 6
        package.addfile(member, io.BytesIO(b"answer"))
    skill = FakeOCRSkill("never OCR a forbidden container")
    result = await pipeline.prepare_submission_sources(content=archive.getvalue(), filename="legacy.tar",
        content_type="application/x-tar", owner_id=owner, task_id=task, job_id=running.id,
        job_attempt=running.attempt, operation_lease_token=running.lease_token, ocr_skill=skill)
    assert len(result) == 1
    assert result[0].filename == "legacy.tar"
    assert result[0].pre_error_code == "submission_source_type_not_allowed"
    assert skill.calls == []


@pytest.mark.asyncio
async def test_library_category_and_cached_chunks_cannot_grant_disallowed_format():
    from backend.api import task_preparation as api
    from backend.db import course_library_repository
    from backend.tests.test_personal_knowledge import _persist_ready_document
    from backend.tests.test_task_background_workflows import _seed_task
    from backend.tests.test_question_preparation_recovery import _RecoveryRegistry
    owner, task = _seed_task()
    document = _persist_ready_document(owner_id=owner, filename="historical.csv",
        content=b"topic,criterion", chunks=["Cached teacher standard"])
    material, _ = course_library_repository.create_material(owner_id=owner, document_id=document.id,
        filename="historical.csv", category="rubric", labels=[], course_id=None, group_id=None)
    with pytest.raises(HTTPException) as caught:
        await api._select_source(file=None, library_material_id=material.material_id, inline_text=None,
            owner_id=owner, task_id=task, registry=_RecoveryRegistry(), role="rubric")
    assert caught.value.detail["code"] == "source_type_not_allowed"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["rubric", "programming_tests"])
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("pending", [False, True])
async def test_deferred_recovery_ignores_legacy_true_for_teacher_pdf(tmp_path, monkeypatch, role, native, pending):
    import hashlib
    import time
    import fitz
    from unittest.mock import AsyncMock
    from backend.api import task_preparation as api
    from backend.domain.errors import RecognitionError
    from backend.models import ProblemSourceDraft
    from backend.progress.tracker import ProgressReporter
    from backend.services import question_sources
    from backend.services.stage_provider_routing import StageProviderRoute
    from backend.tests import test_recognition_v2_artifacts as fixtures
    from backend.tests.test_recognition_reader import llm
    with fitz.open() as document:
        page = document.new_page()
        if native:
            page.insert_text((72, 72), "Original teacher data -2. " * 3)
        body = document.tobytes()
    monkeypatch.setattr(fixtures, "source_file", lambda **_: (body, "application/pdf"))
    _, request, body, results = fixtures.setup(tmp_path)
    provider, _ = llm(text="Forbidden OCR replacement")
    registry = SimpleNamespace(list_configs=lambda: [{"provider_id": provider.provider_id, "enabled": True}], pick_vision=lambda preferred=None: provider)
    route = StageProviderRoute(route_id=provider.provider_id, kind="llm", provider=provider)
    monkeypatch.setattr(api, "get_storage", lambda: results.store.storage)
    monkeypatch.setattr(question_sources, "get_storage", lambda: results.store.storage)
    owner, task = request.source.owner_id, request.source.business_id
    from backend.services.source_files import persist_problem_source
    stored, _ = persist_problem_source(storage=results.store.storage, owner_id=owner, task_id=task,
        original_name="teacher.pdf", content=body, content_type="application/pdf")
    draft = ProblemSourceDraft(source_token="historical", task_id=task, owner_id=owner,
        role=role, source_kind="upload", structure_mode="organized", filename="teacher.pdf",
        content_type="application/pdf", size_bytes=len(body), content_sha256=hashlib.sha256(body).hexdigest(),
        expires_at=time.time() + 60)
    payload = {"recognition_pending": pending, "enable_material_ocr": True,
               "source_ref": {"stored_file_id": stored.id}}
    operation = SimpleNamespace(owner_id=owner, assignment_id=task, update_progress=AsyncMock(),
                                operation_id="historical", payload={}, attempt=1)
    kwargs = dict(operation=operation, route=route, registry=registry, reporter=ProgressReporter("historical"))
    cached = "Original frozen teacher transcript" if not pending else ""
    if native:
        resolved = await api._recognize_saved_question_sources([(draft, cached, payload)], **kwargs)
        assert ("Original teacher data -2" if pending else cached) in resolved[0][1]
    else:
        with pytest.raises(RecognitionError, match="material_ocr_not_allowed_for_role"):
            await api._recognize_saved_question_sources([(draft, cached, payload)], **kwargs)
    material = {"targets": ["criterion" if role == "rubric" else "test_cases"],
                "source_kind": "upload", "original_stored_file_id": stored.id,
                "original_sha256": stored.sha256, "enable_material_ocr": True}
    if native:
        await api._revalidate_material_original(material, owner_id=owner, task_id=task)
    else:
        with pytest.raises(RecognitionError, match="material_ocr_not_allowed_for_role"):
            await api._revalidate_material_original(material, owner_id=owner, task_id=task)
    provider.ainvoke_vision.assert_not_awaited()


@pytest.mark.asyncio
async def test_durable_submission_checks_effective_limit_before_unbounded_storage_read(monkeypatch):
    from contextlib import contextmanager
    from backend.db import workflow_repository
    from backend.services import task_facade
    from backend.services.workflow_worker import LeasedOperation
    from backend.storage import get_storage
    from backend.tools import file_processing
    from backend.tests.test_task_background_workflows import _seed_task, _Registry
    owner, task = _seed_task(with_question=True)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="answer.txt", content=b"four", content_type="text/plain", registry=_Registry())
    claimed = workflow_repository.claim_operation(queued["job_id"], owner_id=owner, worker_id="bounded-recovery", lease_seconds=60)
    storage = get_storage()
    real_open = storage.open
    @contextmanager
    def bounded_open(key):
        with real_open(key) as original:
            class BoundedStream:
                def read(self, size=-1):
                    assert 0 <= size <= 4, "Stored original read exceeds the effective byte ceiling"
                    return original.read(size)
            yield BoundedStream()
    monkeypatch.setattr(storage, "open", bounded_open)
    monkeypatch.setattr(file_processing, "SUBMISSION_UPLOAD_MAX_BYTES", 3)
    await task_facade.run_durable_submission_recognition(LeasedOperation(claimed, worker_id="bounded-recovery", lease_seconds=60))
    final = workflow_repository.get_operation(claimed.id, owner_id=owner)
    assert final.status == "error"
    assert final.error_code == "submission_source_too_large"


def test_retry_loader_uses_original_filename_not_legacy_payload_permission():
    from backend.db import file_repository, workflow_repository
    from backend.domain.errors import ValidationError
    from backend.services import task_facade
    from backend.storage import get_storage
    from backend.tests.test_submission_source_pipeline_integrity import _seed_task, _create_operation
    owner, task = _seed_task()
    operation = _create_operation(owner, task)
    stored = file_repository.save_file(storage=get_storage(), owner_id=owner, assignment_id=task,
        kind="submission_container", original_name="legacy.csv", content=b"student,answer", content_type="text/csv")
    operation = workflow_repository.save_operation_checkpoint(operation.id, owner_id=owner,
        expected_attempt=operation.attempt, expected_checkpoint_revision=operation.checkpoint_revision,
        stage="submission_input_saved", checkpoint={}, artifact_refs=[stored.id])
    workflow_repository.update_operation(operation.id, owner_id=owner, expected_attempt=operation.attempt,
        status="error", payload={"filename": "promoted.txt"})
    with pytest.raises(ValidationError) as caught:
        task_facade.load_submission_retry_upload(task_id=task, owner_id=owner, job_id=operation.id)
    assert caught.value.code == "submission_source_type_not_allowed"
    assert get_storage().exists(stored.storage_key)


@pytest.mark.asyncio
async def test_prepared_source_reuse_observes_lower_effective_limit(monkeypatch):
    from contextlib import contextmanager
    from fastapi import UploadFile
    from backend.api import task_preparation as api
    from backend.db import workflow_repository
    from backend.domain.errors import ValidationError
    from backend.services import upload_contract
    from backend.storage import get_storage
    from backend.tests.test_task_background_workflows import _seed_task
    from backend.tests.test_question_preparation_recovery import _RecoveryRegistry
    owner, task = _seed_task()
    response = await api.preflight_problem_source(task_id=task,
        file=UploadFile(filename="teacher.txt", file=io.BytesIO(b"four")),
        library_material_id=None, inline_text=None, structure_mode="organized",
        extraction_hint="", save_to_library=False, recognition_options=None,
        role="problem", current=SimpleNamespace(id=owner), registry=_RecoveryRegistry())
    operation = workflow_repository.get_operation(response["source_token"], owner_id=owner)
    monkeypatch.setattr(upload_contract, "QUESTION_SOURCE_MAX_BYTES", 3)
    storage = get_storage()
    real_open = storage.open
    @contextmanager
    def bounded_open(key):
        with real_open(key) as original:
            class BoundedStream:
                def read(self, size=-1):
                    assert 0 <= size <= 4, "Prepared-source read exceeds the effective byte ceiling"
                    return original.read(size)
            yield BoundedStream()
    monkeypatch.setattr(storage, "open", bounded_open)
    with pytest.raises(ValidationError) as caught:
        api._validated_question_source_ref(operation=operation, payload=operation.payload, task_id=task, owner_id=owner)
    assert caught.value.code == "source_too_large"


@pytest.mark.asyncio
async def test_material_text_keeps_original_bytes_for_later_role_revalidation():
    import hashlib
    from fastapi import UploadFile
    from backend.api import task_preparation as api
    from backend.db import workflow_repository, file_repository
    from backend.storage import get_storage
    from backend.tests.test_task_background_workflows import _seed_task
    from backend.tests.test_question_preparation_recovery import _RecoveryRegistry
    owner, task = _seed_task(with_question=True)
    body = b"\xef\xbb\xbfTeacher original criterion -2\r\n"
    response = await api.preflight_material_import(task_id=task,
        file=UploadFile(filename="criterion.txt", file=io.BytesIO(body)), library_material_id=None,
        targets='["criterion"]', structure_mode="organized", extraction_hint="", save_to_library=False,
        current=SimpleNamespace(id=owner), registry=_RecoveryRegistry())
    assert response["status"] == "ready"
    source = workflow_repository.get_operation(response["source_token"], owner_id=owner)
    original_id = source.payload.get("original_stored_file_id")
    assert original_id, "Decoded text artifact cannot replace the original upload"
    stored = file_repository.get_file(file_id=original_id, owner_id=owner)
    assert stored.assignment_id == task and stored.kind == "problem_source"
    assert stored.sha256 == hashlib.sha256(body).hexdigest()
    assert source.payload["original_sha256"] == stored.sha256
    with get_storage().open(stored.storage_key) as stream:
        assert stream.read() == body


@pytest.mark.asyncio
@pytest.mark.parametrize("case,code", [
    ("missing_original", "recognition_source_unavailable"),
    ("forbidden_role", "source_type_not_allowed"),
    ("lower_limit", "source_too_large"),
    ("invalid_targets", "invalid_source_role"),
    ("artifact_limit", "source_text_too_large"),
])
async def test_material_background_revalidates_original_before_parser_or_cache(monkeypatch, case, code):
    import hashlib
    from fastapi import UploadFile
    from backend.api import task_preparation as api
    from backend.db import workflow_repository
    from backend.domain.errors import RecognitionError, ValidationError
    from backend.services import task_facade, upload_contract
    from backend.services.workflow_worker import LeasedOperation
    from backend.tests.test_task_background_workflows import _seed_task
    from backend.tests.test_question_preparation_recovery import _RecoveryRegistry
    owner, task = _seed_task(with_question=True)
    registry = _RecoveryRegistry()
    filename = "teacher.markdown" if case == "forbidden_role" else "teacher.txt"
    response = await api.preflight_material_import(task_id=task,
        file=UploadFile(filename=filename, file=io.BytesIO(b"four")), library_material_id=None,
        targets='["reference_answer"]', structure_mode="organized", extraction_hint="", save_to_library=False,
        current=SimpleNamespace(id=owner), registry=registry)
    source = workflow_repository.get_operation(response["source_token"], owner_id=owner)
    payload = {**source.payload, "targets": ["criterion"]}
    if case == "missing_original":
        payload.pop("original_stored_file_id", None)
        payload.pop("recognition_stored_file_id", None)
    if case == "invalid_targets":
        payload["targets"] = [{"unexpected": "target"}]
    workflow_repository.update_operation(source.id, owner_id=owner, expected_attempt=source.attempt, payload=payload)
    queued, _ = workflow_repository.create_operation(assignment_id=task, owner_id=owner,
        operation_type="material_import", input_hash=hashlib.sha256(source.id.encode()).hexdigest(),
        payload={"source_token": source.id, "base_workflow_revision": 0, "provider_id": "test-provider"})
    claimed = workflow_repository.claim_operation(queued.id, owner_id=owner, worker_id="material-policy", lease_seconds=60)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    if case == "lower_limit":
        monkeypatch.setattr(upload_contract, "QUESTION_SOURCE_MAX_BYTES", 3)
    if case == "artifact_limit":
        monkeypatch.setattr(api, "MAX_SOURCE_CHARACTERS", 3)
    with pytest.raises(ValidationError if case == "artifact_limit" else RecognitionError) as caught:
        await api.run_durable_material_import(LeasedOperation(claimed, worker_id="material-policy", lease_seconds=60))
    assert caught.value.code == code


@pytest.mark.asyncio
async def test_mixed_zip_records_rejected_source_and_publishes_valid_sibling(monkeypatch):
    import hashlib
    import json
    from unittest.mock import AsyncMock
    from backend.db import workflow_repository, source_outcome_repository, file_repository
    from backend.services import task_facade
    from backend.services.workflow_worker import LeasedOperation
    from backend.storage import get_storage
    from backend.tests.test_task_background_workflows import _seed_task, _Registry
    owner, task = _seed_task(with_question=True)
    registry = _Registry()
    provider = SimpleNamespace(provider_id="test-provider", supports_vision=False,
        ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps({
            "stu_id": "S001", "stu_name": "Synthetic learner",
            "stu_ans": [{"q_id": "q1", "number": "1", "type": "short", "content": "Wrong -2", "flag": []}],
        }))))
    registry.provider = provider
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("S001.txt", b"Wrong -2")
        archive.writestr("rejected.csv", b"student,answer")
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename="answers.zip", content=buffer.getvalue(), content_type="application/zip", registry=registry)
    claimed = workflow_repository.claim_operation(queued["job_id"], owner_id=owner, worker_id="mixed-policy", lease_seconds=60)
    await task_facade.run_durable_submission_recognition(LeasedOperation(claimed, worker_id="mixed-policy", lease_seconds=60))
    rows = source_outcome_repository.list_source_results(operation_id=claimed.id, owner_id=owner, attempt=claimed.attempt)
    results = {row.source.original_name: row for row in rows}
    assert results["S001.txt"].outcome.status == "parsed"
    assert results["rejected.csv"].outcome.stable_error_code == "submission_source_type_not_allowed"
    assert results["rejected.csv"].outcome.failure_phase == "source_read"
    stored = file_repository.get_file(file_id=results["rejected.csv"].source.stored_file_id, owner_id=owner)
    assert stored.sha256 == hashlib.sha256(b"student,answer").hexdigest()
    with get_storage().open(stored.storage_key) as stream:
        assert stream.read() == b"student,answer"
    assert provider.ainvoke.await_count == 1
    snapshot = task_facade.get_task(task_id=task, owner_id=owner)
    assert snapshot["submission_source_summary"]["parsed"] == 1
    assert snapshot["submission_source_summary"]["failed"] == 1


def test_invalid_replacement_does_not_retire_previous_problem_original():
    from backend.db import workflow_repository, file_repository
    from backend.domain.errors import ValidationError
    from backend.services import task_facade
    from backend.storage import get_storage
    from backend.tests.test_task_background_workflows import _seed_task
    from backend.tests.test_question_preparation_recovery import _RecoveryRegistry
    owner, task = _seed_task()
    registry = _RecoveryRegistry()
    queued = task_facade.queue_task_problem_extraction(task_id=task, owner_id=owner,
        filename="valid.txt", content=b"1. Original question", content_type="text/plain", registry=registry)
    before = file_repository.list_files(owner_id=owner, assignment_id=task)
    with pytest.raises(ValidationError) as caught:
        task_facade.queue_task_problem_extraction(task_id=task, owner_id=owner,
            filename="invalid.csv", content=b"teacher,question", content_type="text/csv",
            replace_confirmed=True, registry=registry)
    assert caught.value.code == "source_type_not_allowed"
    after = file_repository.list_files(owner_id=owner, assignment_id=task)
    assert [(row.id, row.sha256, row.availability_status) for row in after] == [(row.id, row.sha256, row.availability_status) for row in before]
    assert all(get_storage().exists(row.storage_key) for row in after)
    assert workflow_repository.get_workflow(task, owner_id=owner).extract_job_id == queued["job_id"]


@pytest.mark.parametrize("alias", ["problem-sources/preflight", "question-preparation/sources/preflight"])
def test_teacher_preflight_keeps_existing_signature_mismatch_code(client_registry, alias):
    from backend.tests.test_task_background_workflows import _seed_task
    from backend.tests.test_upload_contract import payload
    client, _ = client_registry
    owner, task = _seed_task()
    response = client.post(f"/tasks/{task}/{alias}", headers=headers(owner), data={"role": "problem"},
        files={"file": ("renamed.txt", payload("pdf"), "text/plain")})
    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "source_content_type_not_allowed"


def test_course_material_capability_has_no_independent_extension_list(client_registry, monkeypatch):
    from backend.services import upload_contract
    from backend.tests.test_task_background_workflows import _seed_task
    client, _ = client_registry
    owner, _ = _seed_task()
    # Change only the policy owner in this test. The actual validator and the
    # existing capability response must agree without a second list update.
    monkeypatch.setattr(upload_contract, "ROLE_EXTENSIONS", {
        **upload_contract.ROLE_EXTENSIONS, "course_knowledge": frozenset({"pdf"})})
    with pytest.raises(upload_contract.UploadContractError):
        upload_contract.validate_upload("course_knowledge", "notes.txt", b"notes")
    response = client.get("/course-materials/", headers=headers(owner))
    assert response.status_code == 200
    assert response.json()["capabilities"]["accepted_types"] == ["pdf"]


@pytest.mark.parametrize("characters", [240, 241])
def test_course_material_filename_limit_matches_actual_boundary(client_registry, characters):
    from backend.services.upload_contract import get_upload_contract
    from backend.tests.test_task_background_workflows import _seed_task
    client, _ = client_registry
    owner, _ = _seed_task()
    filename = "n" * (characters - 4) + ".txt"
    response = client.post("/course-materials/", headers=headers(owner), data={"native_only": "true"},
        files={"file": (filename, b"original notes", "text/plain")})
    if characters == 240:
        assert response.status_code == 200, response.text
    else:
        assert response.status_code == 422
        assert response.json()["detail"] == {"code": "material_filename_too_long", "max_length": 240}
    assert get_upload_contract()["limits"]["course_material_filename_max_characters"] == 240


@pytest.mark.parametrize("alias", ["problem-sources/preflight", "question-preparation/sources/preflight"])
@pytest.mark.parametrize("role", ["rubric", "programming_tests"])
def test_teacher_scanned_pdf_is_actionable_role_error_not_provider_failure(client_registry, monkeypatch, alias, role):
    import fitz
    from unittest.mock import AsyncMock
    from backend.db import file_repository
    from backend.storage import get_storage
    from backend.tests.test_task_background_workflows import _seed_task
    client, registry = client_registry
    with fitz.open() as document:
        document.new_page()
        body = document.tobytes()
    invoke = AsyncMock(side_effect=AssertionError("Restricted PDF must not reach OCR"))
    monkeypatch.setattr(registry.provider, "supports_vision", True)
    monkeypatch.setattr(registry.provider, "ainvoke_vision", invoke, raising=False)
    owner, task = _seed_task()
    response = client.post(f"/tasks/{task}/{alias}", headers=headers(owner),
        data={"role": role, "enable_material_ocr": "true"},
        files={"file": ("teacher.pdf", body, "application/pdf")})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "material_ocr_not_allowed_for_role"
    originals = [row for row in file_repository.list_files(owner_id=owner, assignment_id=task) if row.kind == "problem_source"]
    assert len(originals) == 1 and get_storage().exists(originals[0].storage_key)
    invoke.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["problems", "reference", "submissions"])
async def test_shared_reader_keeps_allowed_roles_visual_transcription(tmp_path, monkeypatch, purpose):
    from backend.services import question_sources
    from backend.services.stage_provider_routing import StageProviderRoute
    from backend.tests.test_recognition_reader import llm
    from backend.tests.test_recognition_v2_artifacts import setup
    _, request, data, results = setup(tmp_path)
    provider, _ = llm(text="Literal source error 1+1=3")
    registry = SimpleNamespace(list_configs=lambda: [{"provider_id": provider.provider_id, "enabled": True}], pick_vision=lambda preferred=None: provider)
    route = StageProviderRoute(route_id=provider.provider_id, kind="llm", provider=provider)
    monkeypatch.setattr(question_sources, "get_storage", lambda: results.store.storage)
    result = await question_sources.read_question_source(
        owner_id=request.source.owner_id, task_id=request.source.business_id,
        content=data, filename="answer.png", stored_file_id=request.source.stored_file_id,
        purpose=purpose, allow_vision=True, route=route, registry=registry,
    )
    assert "1+1=3" in result.text
    assert result.recognition["requires_review"] is True
    assert provider.ainvoke_vision.await_count > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["rubric", "test_cases"])
@pytest.mark.parametrize("native", [False, True])
async def test_shared_reader_teacher_pdf_never_opted_into_ocr(tmp_path, monkeypatch, purpose, native):
    import fitz
    from backend.domain.errors import RecognitionError
    from backend.services import question_sources
    from backend.services.stage_provider_routing import StageProviderRoute
    from backend.tests import test_recognition_v2_artifacts as fixtures
    from backend.tests.test_recognition_reader import llm
    with fitz.open() as document:
        page = document.new_page()
        if native:
            page.insert_text((72, 72), "Teacher original standard -2. " * 3)
        data = document.tobytes()
    # Only the synthetic fixture changes; production PDF/read/role logic is real.
    monkeypatch.setattr(fixtures, "source_file", lambda **kwargs: (data, "application/pdf"))
    _, request, content, results = fixtures.setup(tmp_path)
    provider, _ = llm(text="OCR output must not replace original data")
    registry = SimpleNamespace(list_configs=lambda: [{"provider_id": provider.provider_id, "enabled": True}], pick_vision=lambda preferred=None: provider)
    route = StageProviderRoute(route_id=provider.provider_id, kind="llm", provider=provider)
    monkeypatch.setattr(question_sources, "get_storage", lambda: results.store.storage)
    kwargs = dict(owner_id=request.source.owner_id, task_id=request.source.business_id,
                  content=content, filename="teacher.pdf", stored_file_id=request.source.stored_file_id,
                  purpose=purpose, allow_vision=True, route=route, registry=registry)
    if native:
        result = await question_sources.read_question_source(**kwargs)
        assert "Teacher original standard -2" in result.text
    else:
        with pytest.raises(RecognitionError, match="material_ocr_not_allowed_for_role"):
            await question_sources.read_question_source(**kwargs)
    provider.ainvoke_vision.assert_not_awaited()
