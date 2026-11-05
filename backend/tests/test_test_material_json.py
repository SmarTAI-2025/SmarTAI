"""JSON test materials must be validated/read before models or persistent writes."""
import base64
import io
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from backend.api import task_preparation
from backend.services import question_sources, source_files
from backend.services.stage_provider_routing import StageProviderRoute
from backend.tools.file_processing import extract_text_from_upload


@pytest.fixture
def no_model_or_storage(monkeypatch):
    engine = Mock(side_effect=AssertionError("JSON must not invoke a recognition model"))
    store = Mock(side_effect=AssertionError("JSON validation must precede persistent writes"))
    pdf = AsyncMock(side_effect=AssertionError("JSON must not route to the PDF reader"))
    monkeypatch.setattr(question_sources, "recognition_engine", engine)
    monkeypatch.setattr(source_files, "persist_problem_source", store)
    monkeypatch.setattr(question_sources, "read_pdf_evidence", pdf)
    yield
    engine.assert_not_called()
    store.assert_not_called()
    pdf.assert_not_called()


async def read_json(content, content_type, path):
    if path == "text_reader":
        return await extract_text_from_upload(content, "tests.JSON", purpose="test_cases", content_type=content_type)
    result = await question_sources.read_question_source(
        owner_id="owner", task_id="task", content=content, filename="tests.JSON",
        content_type=content_type, purpose="test_cases", registry=None,
        route=StageProviderRoute(route_id="unused", kind="llm", provider=None),
    )
    assert result.recognition is None
    return result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["text_reader", "question_source"])
@pytest.mark.parametrize("content_type", ["application/json", "text/plain", "", None])
async def test_json_test_material_is_read_verbatim(content_type, path, no_model_or_storage):
    content = '{\n  "题号": "1", "cases": [{"input": "1,2", "expected": "3"}]\n}\n'.encode("utf-8")
    assert await read_json(content, content_type, path) == content.decode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["text_reader", "question_source"])
@pytest.mark.parametrize("content", [
    b'{"cases": [}',
    b'{"input": 1,}',
    b'not json at all',
    b'\x00\xff\x01\x02binary',
    base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkAAMAASsJTYQAAAAASUVORK5CYII="),
], ids=["malformed-array", "trailing-comma", "ordinary-text", "binary", "png-disguised-as-json"])
async def test_invalid_json_test_material_fails_before_model_or_storage(content, path, no_model_or_storage):
    with pytest.raises(HTTPException) as caught:
        await read_json(content, "application/json", path)
    assert caught.value.status_code in {400, 415, 422}
    assert isinstance(caught.value.detail, dict)
    assert caught.value.detail.get("code")


@pytest.mark.parametrize("role", ["problem", "rubric", "reference_answer"])
def test_json_remains_rejected_for_other_material_roles(role):
    file = UploadFile(file=io.BytesIO(b'{"cases": []}'), filename="tests.json", headers=Headers({"content-type": "application/json"}))
    with pytest.raises(HTTPException) as caught:
        task_preparation._validate_source_upload(file, role=role, has_vision=True)
    assert caught.value.status_code == 415
    assert caught.value.detail["code"] == "source_type_not_allowed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mime", ["application/json", "text/json", "text/plain", "application/octet-stream"])
async def test_json_content_type_is_assigned_only_after_successful_parse(mime):
    from backend.tools.file_processing import validate_upload_content
    result = await validate_upload_content(b'{"input":"%PDF-1.7"}', "cases.json", content_type=mime, purpose="test_cases")
    assert result.content_type == "application/json"
