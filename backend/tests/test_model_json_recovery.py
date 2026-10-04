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
from backend.tools.structured_llm import StructuredOutputInvalidError


def test_markdown_property_keys_recover_without_changing_content():
    class Output(BaseModel):
        stem: str
        score: int
    raw = '{\n * `stem`: "Line 1\\n* `literal`: remains in content; $x^2$",\n * `score`: 3\n}'
    parsed = extract_and_parse_json(raw, Output)
    assert parsed.stem == 'Line 1\n* `literal`: remains in content; $x^2$'
    assert parsed.score == 3



def test_invalid_structured_output_has_safe_specific_code():
    class Output(BaseModel):
        score: int
    with pytest.raises(StructuredOutputInvalidError) as caught:
        extract_and_parse_json('PRIVATE_ORIGINAL_OUTPUT', Output)
    assert 'PRIVATE_ORIGINAL_OUTPUT' not in str(caught.value)
    assert classify_background_error(caught.value, 'problem_extraction_failed') == 'provider_response_invalid'



def test_unquoted_extra_metadata_does_not_destroy_valid_generated_materials():
    from backend.agents.ingest_agent import AICompletionOutput
    parsed = extract_and_parse_json('{"candidates":[{tspans:[],"q_id":"q1",'
        '"target_id":"q1:reference_answer","target":"reference_answer","text_value":"x = 2"}]}', AICompletionOutput)
    assert parsed.candidates[0].text_value == "x = 2"

