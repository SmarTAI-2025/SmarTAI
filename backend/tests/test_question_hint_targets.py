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


@pytest.mark.parametrize("hint", [
    "1.1.5, 1.1.7, 1.1.20, 1.1.29, 1.1.31, 1.2.3, 1.2.16",
    "请提取题目1.1.5、1.1.7、1.1.20、1.1.29、1.1.31、1.2.3、1.2.16，保留子问",
])
def test_legacy_teacher_hint_selects_targets_before_any_ocr(hint):
    assert question_recognition_options(extraction_hint=hint).targets == [
        "1.1.5", "1.1.7", "1.1.20", "1.1.29", "1.1.31", "1.2.3", "1.2.16"]



def test_explicit_target_field_wins_over_older_hint():
    assert question_recognition_options({"targets": ["2.1.1"]}, extraction_hint="1.1.5, 1.2.3").targets == ["2.1.1"]
    assert question_recognition_options(extraction_hint="version 1.2.3; pi is 3.14").targets == []
