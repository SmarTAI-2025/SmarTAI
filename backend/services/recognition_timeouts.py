"""One response policy for source OCR; the owning workflow limits total time."""
from backend.config import settings
from backend.domain.errors import RecognitionError
from backend.recognition.models import RecognitionPolicyV1

SOURCE_TIMEOUT_PROFILE = "source-response-180-v1"
LEGACY_SOURCE_TIMEOUT_PROFILE = "source-300-v1"


def apply_source_timeout_profile(request, *, engine, timeout_profile, total_pages=None):
    if timeout_profile not in (None, SOURCE_TIMEOUT_PROFILE, "source-response-600-v1", "source-response-300-v1", LEGACY_SOURCE_TIMEOUT_PROFILE):
        raise RecognitionError("recognition_request_invalid")
    if total_pages is not None and (type(total_pages) is not int or not 1 <= total_pages <= 10000):
        raise RecognitionError("recognition_request_invalid")
    policy = RecognitionPolicyV1.model_validate({
        **request.policy.model_dump(), "per_call_seconds": settings.llm_timeout,
        "timeout_mode": "response_only",
    })
    return request.model_copy(update={"policy": policy})
