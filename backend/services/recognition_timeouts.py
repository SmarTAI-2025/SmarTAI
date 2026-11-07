"""One response policy for source OCR; the owning workflow limits total time."""
from backend.config import settings
from backend.domain.errors import RecognitionError
from backend.recognition.models import RecognitionPolicyV1

SOURCE_TIMEOUT_PROFILE = "source-response-180-v1"


def require_current_source_timeout_profile(timeout_profile):
    """Reject retired or missing contracts before saved work can be resumed."""
    if timeout_profile != SOURCE_TIMEOUT_PROFILE:
        raise RecognitionError("recognition_request_invalid")


def apply_source_timeout_profile(request, *, engine, timeout_profile, total_pages=None):
    require_current_source_timeout_profile(timeout_profile)
    if total_pages is not None and (type(total_pages) is not int or not 1 <= total_pages <= 10000):
        raise RecognitionError("recognition_request_invalid")
    policy = RecognitionPolicyV1.model_validate({
        **request.policy.model_dump(), "per_call_seconds": settings.llm_timeout,
        "timeout_mode": "response_only",
    })
    return request.model_copy(update={"policy": policy})
