"""Opt-in source OCR deadlines, frozen before a durable run is identified.

The parent operation persists the profile. A missing profile must keep the old
policy unchanged so recovery cannot bypass an existing pending model submission.
"""
from __future__ import annotations

from math import ceil
from typing import TYPE_CHECKING

from backend.domain.errors import RecognitionError
from backend.recognition.models import RecognitionPolicyV1

if TYPE_CHECKING:
    from backend.agents.recognition_agent import RecognitionReadRequestV1
    from backend.recognition.engine import RecognitionEngine


SOURCE_TIMEOUT_PROFILE = "source-response-300-v1"
LEGACY_SOURCE_TIMEOUT_PROFILE = "source-300-v1"
_PER_CALL_SECONDS = 300
_READ_ALLOWANCE_SECONDS = 600
_LOCATOR_ALLOWANCE_SECONDS = 360
_FINALIZE_ALLOWANCE_SECONDS = 60


def apply_source_timeout_profile(
    request: RecognitionReadRequestV1,
    *,
    engine: RecognitionEngine | None,
    timeout_profile: str | None,
    total_pages: int | None = None,
) -> RecognitionReadRequestV1:
    """Select frozen response timing without changing calls or older profiles.

    The legacy source profile keeps its original cumulative budgets because it
    may already own pending submissions. New operations time each provider
    response independently, with bounded local waits and processing elsewhere.
    """
    if timeout_profile is None:
        return request
    if timeout_profile not in (SOURCE_TIMEOUT_PROFILE, LEGACY_SOURCE_TIMEOUT_PROFILE):
        raise RecognitionError("recognition_request_invalid")
    if engine is None or request.purpose == "knowledge" or request.source.scope == "knowledge_document":
        return request
    caps = engine.capabilities
    if caps.candidate_kind != "vision" or "page_image" not in caps.visual_inputs:
        return request
    if total_pages is not None and (type(total_pages) is not int or not 1 <= total_pages <= 10000):
        raise RecognitionError("recognition_request_invalid")

    policy = request.policy
    if timeout_profile == SOURCE_TIMEOUT_PROFILE:
        selected = RecognitionPolicyV1.model_validate({
            **policy.model_dump(), "per_call_seconds": _PER_CALL_SECONDS,
            "timeout_mode": "response_only",
        })
        return request.model_copy(update={"policy": selected})

    # Preserve the first source profile exactly when an old parent resumes.
    # One page may require several crops, so its original allowance used the
    # legal call bounds instead of treating pages as an exact call count.
    initial_calls = min(policy.max_initial_calls, policy.max_calls, policy.max_regions)
    extra_calls = 0
    if caps.response_recheck and caps.semantic_repair and policy.enable_repair:
        extra_calls = min(
            initial_calls, policy.max_empty_recoveries + policy.max_patches,
            policy.max_calls - initial_calls, 6,
        )
    locator_calls = 0
    if request.source.content_type == "application/pdf" and request.scope == "targets" and caps.target_location:
        search_pages = request.search_window_pages
        if total_pages is not None:
            search_pages = min(search_pages, max(0, total_pages - request.search_start_page + 1))
        locator_calls = min(policy.max_locator_calls, ceil(search_pages / (8 * caps.max_locator_images)))

    read_seconds = (initial_calls + extra_calls) * _PER_CALL_SECONDS + _READ_ALLOWANCE_SECONDS
    locator_seconds = locator_calls * _PER_CALL_SECONDS + _LOCATOR_ALLOWANCE_SECONDS
    selected = RecognitionPolicyV1.model_validate({
        **policy.model_dump(),
        "per_call_seconds": _PER_CALL_SECONDS,
        "read_seconds": read_seconds,
        "locator_seconds": locator_seconds,
        "total_seconds": read_seconds + (locator_seconds if locator_calls else 0) + _FINALIZE_ALLOWANCE_SECONDS,
    })
    return request.model_copy(update={"policy": selected})
