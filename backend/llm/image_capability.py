"""Evidence from one real image request, independent of provider/model names."""
from __future__ import annotations

import io
import json
import re
import secrets

from PIL import Image, ImageDraw, ImageFont


def make_image_challenge() -> tuple[bytes, str]:
    # Fresh, memory-only pixels. Neither prompt, filename nor PNG metadata
    # contains this answer. A fixed fixture would allow memorized responses.
    answer = "".join(secrets.choice("23456789") for _ in range(8))
    image = Image.new("RGB", (320, 88), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=42)
    draw.text((16, 16), answer, font=font, fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue(), answer


IMAGE_CHALLENGE_PROMPT = (
    "Read the eight digits printed in the image. Reply with only those digits."
)


def explicitly_rejects_images(status: object, body: object) -> bool:
    # Do not reinterpret authentication, quota, timeout, corrupt files, or model
    # lookup failures as capability evidence. Only an upstream input rejection.
    if status not in (400, 422):
        return False
    text = json.dumps(body, ensure_ascii=False).lower()
    if re.search(r"(?:invalid|unsupported|not supported).{0,30}(?:format|mime|dimension|size|resolution)|(?:format|mime|dimension|size|resolution).{0,30}(?:invalid|unsupported|not supported)", text):
        return False
    return bool(re.search(
        r"(?:does not support|doesn't support|not supported|unsupported|not capable|only supports? text|不支持)"
        r".{0,90}(?:image(?:_url)?|vision|multimodal|图片|图像|视觉)"
        r"|(?:image(?:_url)?|vision|multimodal|图片|图像|视觉).{0,90}"
        r"(?:not supported|unsupported|not allowed|does not support|不支持)",
        text,
    ))


def is_explicit_image_rejection(exc: BaseException) -> bool:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if getattr(exc, "code", None) == "provider_vision_not_supported":
            return True
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        body = getattr(exc, "body", None) or getattr(exc, "details", None)
        response = getattr(exc, "response", None)
        if response is not None:
            status = getattr(response, "status_code", status)
            try:
                body = response.json()
            except (ValueError, AttributeError):
                pass
        if explicitly_rejects_images(status, body if body is not None else str(exc)):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def can_attempt_images(provider) -> bool:
    allowed = getattr(provider, "can_attempt_vision", None)
    return allowed if isinstance(allowed, bool) else getattr(provider, "supports_vision", None) is not False


def image_quality_failure(code: str, document) -> str:
    # Reuse actual recognition-plan/evidence selection, never the extension or
    # a new numeric confidence threshold. Network/auth errors retain their code.
    if code in {"question_source_incomplete", "ocr_empty_result", "recognition_response_invalid"} and document is not None:
        if any(getattr(page, "selected_for_visual", False) or any(getattr(span, "visual", None) is not None for span in getattr(page, "spans", [])) for page in document.pages):
            return "image_recognition_unconfirmed"
    return code
