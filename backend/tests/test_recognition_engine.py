from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.recognition.engine import EngineReadInputV1, RecognitionEngine
from backend.recognition.models import RecognitionCandidateV1
from backend.recognition.planner import EngineCapabilitiesV1


class FakeEngine:
    def __init__(self, route, kind, mode, semantic):
        self._capabilities = EngineCapabilitiesV1(
            route_id=route, fingerprint=route + "-v1", visual_inputs=[mode], semantic_repair=semantic,
        )
        self.kind = kind
        self.requests = []

    @property
    def capabilities(self):
        return self._capabilities

    async def recognize(self, request):
        self.requests.append(request)
        return RecognitionCandidateV1(
            kind=self.kind, status="ok", text="x = -2 (crossed out)",
            provider_route_id=self.capabilities.route_id,
        )


async def transcribe(engine: RecognitionEngine, request: EngineReadInputV1):
    return await engine.recognize(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("route,kind,mode,semantic", [
    ("existing-llm", "vision", "page_image", True),
    ("existing-document-ocr", "ocr", "document", False),
    ("future-engine-fixture", "ocr", "page_image", False),
])
async def test_engines_share_one_transcription_port(route, kind, mode, semantic):
    engine = FakeEngine(route, kind, mode, semantic)
    request = EngineReadInputV1(
        purpose="submissions", input_mode=mode, page_number=7,
        content_type="application/pdf" if mode == "document" else "image/png", payload=b"inspected-fixture",
    )
    result = await transcribe(engine, request)
    assert result.text == "x = -2 (crossed out)"
    assert result.kind == kind
    assert len(engine.requests) == 1
    assert engine.capabilities.semantic_repair is semantic


def test_payload_is_not_part_of_logs_or_persisted_evidence():
    request = EngineReadInputV1(
        purpose="knowledge", input_mode="page_image", page_number=1,
        content_type="image/png", payload=b"private-page-bytes",
    )
    assert "private-page-bytes" not in repr(request)
    assert "payload" not in request.model_dump()
    assert "private-page-bytes" not in request.model_dump_json()


def test_invalid_payload_does_not_leak_through_safe_validation_errors():
    payload = b"PRIVATE_STUDENT_ANSWER" + b"x" * (10 * 1024 * 1024) + b"PRIVATE_TAIL"
    with pytest.raises(ValidationError) as exc:
        EngineReadInputV1(
            purpose="submissions", input_mode="page_image", page_number=1,
            content_type="image/png", payload=payload,
        )
    assert "PRIVATE_STUDENT_ANSWER" not in str(exc.value)
    assert "PRIVATE_TAIL" not in str(exc.value)
    assert all("input" not in error for error in exc.value.errors(include_input=False))


@pytest.mark.parametrize("changes", [
    {"content_type": "application/pdf"}, {"payload": b""},
    {"payload": b"x" * (10 * 1024 * 1024 + 1)},
    {"input_mode": "document", "content_type": "application/pdf", "region": {"x1": 0.5}},
])
def test_invalid_call_units_rejected(changes):
    with pytest.raises(ValidationError):
        EngineReadInputV1(**{
            "purpose": "knowledge", "input_mode": "page_image", "page_number": 1,
            "content_type": "image/png", "payload": b"fixture", **changes,
        })
