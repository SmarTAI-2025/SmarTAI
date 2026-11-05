"""Upload-format and library boundaries must fail before paid parsing/storage."""
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers
import io

from backend.api import task_preparation
from backend.knowledge import service


@pytest.mark.parametrize("filename", ["tests.json", "tests.csv", "slides.pptx"])
def test_unsupported_library_original_has_actionable_error(filename):
    with pytest.raises(HTTPException) as caught:
        task_preparation._validate_source_library_format(save=True, filename=filename)
    assert caught.value.status_code == 415
    assert caught.value.detail == {"code": "source_library_type_not_allowed", "accepted_extensions": sorted(service.KNOWLEDGE_UPLOAD_EXTENSIONS)}
    task_preparation._validate_source_library_format(save=False, filename=filename)


@pytest.mark.parametrize("filename", ["notes.pdf", "notes.TXT", "notes.MD", "notes.markdown", "scan.png", "scan.JPG", "scan.tiff", "scan.webp"])
def test_supported_library_originals_are_preserved(filename):
    task_preparation._validate_source_library_format(save=True, filename=filename)


@pytest.mark.asyncio
async def test_library_save_rejects_before_ingestion(monkeypatch):
    ingest = AsyncMock()
    monkeypatch.setattr(task_preparation, "ingest_document", ingest)
    with pytest.raises(HTTPException) as caught:
        await task_preparation._save_source_to_library(save=True, descriptor={"filename": "tests.json", "_body": b"{}"}, owner_id="o", task_id="t", role="programming_tests", existing_material_id=None)
    assert caught.value.detail["code"] == "source_library_type_not_allowed"
    ingest.assert_not_called()


@pytest.mark.parametrize("targets,accepted", [(["criterion"], False), (["reference_answer"], False), (["criterion", "reference_answer"], False), (["test_cases"], True), (["criterion", "test_cases"], True)])
def test_bulk_material_json_policy_follows_selected_targets(targets, accepted):
    role = task_preparation._material_import_source_role(targets)
    assert (".json" in task_preparation._accepted_source_extensions(role, has_vision=False)) is accepted
    assert (".csv" in task_preparation._accepted_source_extensions(role, has_vision=False)) is accepted
    assert ".png" in task_preparation._accepted_source_extensions(role, has_vision=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("filename,body,status,code", [
    ("empty.txt", b"", 400, "source_empty"),
    ("notes.zip", b"PK archive", 415, "source_type_not_allowed"),
    ("not-really.pdf", b"plain text", 415, "source_content_type_not_allowed"),
    ("hidden-image.txt", b"\x89PNG\r\n\x1a\n" + bytes(32), 415, "source_content_type_not_allowed"),
])
async def test_all_knowledge_entrypoints_get_structured_validation(filename, body, status, code):
    with pytest.raises(HTTPException) as caught:
        await service.ingest_document(owner_id="o", original_name=filename, content=body)
    assert caught.value.status_code == status
    assert caught.value.detail["code"] == code


@pytest.mark.asyncio
async def test_knowledge_size_limit_is_structured_before_storage(monkeypatch):
    monkeypatch.setattr(service, "MAX_FILE_BYTES", 3)
    with pytest.raises(HTTPException) as caught:
        await service.ingest_document(owner_id="o", original_name="notes.txt", content=b"four")
    assert caught.value.status_code == 413
    assert caught.value.detail == {"code": "source_too_large", "max_bytes": 3}


@pytest.mark.parametrize("mime", ["text/csv", "application/csv", "text/plain"])
def test_csv_test_material_accepts_normal_declared_mimes(mime):
    file = UploadFile(file=io.BytesIO(b"input,expected\n1,2\n"), filename="cases.csv", headers=Headers({"content-type": mime}))
    assert task_preparation._validate_source_upload(file, role="programming_tests", has_vision=False) == ".csv"
    for role in ("problem", "reference_answer", "rubric"):
        with pytest.raises(HTTPException) as caught:
            task_preparation._validate_source_upload(file, role=role, has_vision=False)
        assert caught.value.detail["code"] == "source_type_not_allowed"


@pytest.mark.asyncio
async def test_csv_test_material_is_read_as_text_without_conversion_claim():
    from backend.services.question_sources import read_question_source
    from backend.services.stage_provider_routing import StageProviderRoute
    content = b'q_id,input,expected\n1,"1,2","3"\n'
    result = await read_question_source(owner_id="owner", task_id="task", content=content,
        filename="cases.csv", content_type="text/csv", purpose="test_cases", registry=None,
        route=StageProviderRoute(route_id="unused", kind="llm", provider=None))
    assert result.text == content.decode("utf-8")
    assert result.recognition is None
