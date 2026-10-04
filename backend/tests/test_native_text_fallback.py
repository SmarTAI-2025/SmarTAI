"""Contracts reproduced against the official providers on 2026-10-04."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from backend.llm.image_capability import explicitly_rejects_images
from backend.llm.providers import LLMResponse, SafeRelayProvider, _response_metadata
from backend.models import ProviderConfig
from backend.services.background_errors import classify_background_error
from backend.services.question_sources import question_recognition_options
from backend.tests.test_recognition_reader import llm, request
from backend.tools.structured_llm import PermanentLLMError, ainvoke_with_retry
from backend.tools.structured_llm import extract_and_parse_json
from pydantic import BaseModel


def test_official_glm_text_only_allowlist_is_explicit_image_rejection():
    body = {"error": {"code": "1210", "message": "messages.content.type 参数非法，取值范围 ['text']"}}
    assert explicitly_rejects_images(400, body)
    assert not explicitly_rejects_images(429, body)
    assert not explicitly_rejects_images(400, {"error": {"message": "messages.content.type 参数非法，取值范围 ['text', 'image_url']"}})



def test_native_fallback_policy_preserves_old_serialization_and_separates_new_cache():
    from backend.recognition.models import RecognitionPolicyV1
    from backend.recognition.cache_identity import canonical_digest
    legacy = RecognitionPolicyV1().model_dump(mode="json")
    enabled = RecognitionPolicyV1(allow_native_fallback=True).model_dump(mode="json")
    assert "allow_native_fallback" not in legacy
    assert enabled["allow_native_fallback"] is True
    assert canonical_digest(enabled) != canonical_digest(legacy)



@pytest.mark.parametrize("purpose", ["problems", "submissions"])
def test_unavailable_vision_preserves_native_pdf_text_but_not_empty_scan(purpose):
    from backend.recognition.models import RecognitionPolicyV1
    from backend.recognition.planner import PageObservationV1, plan_recognition
    from backend.tests.test_recognition_planner import native, request
    plan = plan_recognition(request(native(risks=["math"]), PageObservationV1(page_number=2), purpose=purpose),
                            engine=None, policy=RecognitionPolicyV1(allow_native_fallback=True))
    assert plan.decisions[0].action == "native"
    assert "visual_evidence_missing" in plan.decisions[0].reason_codes
    assert plan.decisions[1].action == "blocked"
    assert plan.initial_calls == 0

