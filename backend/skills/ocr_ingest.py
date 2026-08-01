"""OCR ingest skill: turn images/scanned pages into Markdown text.

This is intentionally separate from ``GradingSkill``. Grading skills operate
after problems and student answers have already been parsed; this skill sits
before ingest_agent and converts non-text uploads into text that the existing
pipeline can review and grade.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from backend.config import settings
from backend.llm.providers import BaseProvider, VisionImage

OCRPurpose = Literal["problems", "submissions", "reference", "test_cases"]


@dataclass
class OCRImage:
    data: bytes
    media_type: str
    label: str | None = None


@dataclass
class OCRResult:
    text: str
    provider: str
    model: str | None = None
    duration_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    warnings: list[str] = field(default_factory=list)


class OCRIngestSkill(Protocol):
    async def recognize_images(
        self,
        images: list[OCRImage],
        purpose: OCRPurpose,
    ) -> OCRResult:
        ...


_PROMPTS: dict[OCRPurpose, str] = {
    "problems": """你是一个数理题目 OCR 转写器。请把图片中的题目内容转写为纯文本 Markdown。

要求：
1. 保留题号、题干、选项、已给条件、评分标准、附图说明。
2. 数学公式使用 LaTeX。
3. 不要解题，不要补充图片中没有的信息。
4. 无法辨认的内容标记为 [unclear]。
5. 如果有多页，按页面顺序输出。""",
    "submissions": """你是一个手写数理作答 OCR 转写器。请把图片中的学生作答转写为纯文本 Markdown。

要求：
1. 保留学生姓名、学号、班级等身份信息。
2. 保留题号和每道题的作答步骤。
3. 数学公式使用 LaTeX。
4. 不要批改，不要推断学生未写出的步骤。
5. 看不清的字、公式或数字标记为 [unclear]。
6. 尽量保留划改、箭头、补充说明等作答痕迹。""",
    "reference": """你是一个数理参考答案 OCR 转写器。请把图片中的参考答案或解题过程转写为纯文本 Markdown。

要求：
1. 只转写图片中真实存在的参考答案、公式、推导和说明。
2. 数学公式使用 LaTeX。
3. 不要生成新答案，不要补充图片中没有的信息。
4. 无法辨认的内容标记为 [unclear]。
5. 如果有多页，按页面顺序输出。""",
    "test_cases": """你是一个编程题测试点 OCR 转写器。请把图片中的测试点说明转写为纯文本 Markdown。

要求：
1. 保留输入、期望输出、样例编号、表格内容和备注。
2. 不要推断或生成图片中没有的测试点。
3. 代码、stdin/stdout 和 JSON 片段请放入 Markdown 代码块。
4. 无法辨认的内容标记为 [unclear]。
5. 如果有多页，按页面顺序输出。""",
}


def build_ocr_prompt(images: list[OCRImage], purpose: OCRPurpose) -> str:
    labels = [
        f"{idx + 1}. {image.label or f'image-{idx + 1}'}"
        for idx, image in enumerate(images)
    ]
    label_block = "\n".join(labels)
    return (
        _PROMPTS[purpose]
        + "\n\n输入图片顺序如下，请按这个顺序转写：\n"
        + label_block
        + "\n\n输出要求：只输出转写后的 Markdown 文本，不要解释你的 OCR 过程。"
    )


class LLMVisionOCRSkill:
    name = "LLMVisionOCRSkill"

    def __init__(self, provider: BaseProvider):
        if not getattr(provider, "supports_vision", False):
            raise ValueError(f"{provider.provider_id} does not support vision OCR.")
        self.provider = provider

    async def recognize_images(
        self,
        images: list[OCRImage],
        purpose: OCRPurpose,
    ) -> OCRResult:
        if not images:
            return OCRResult(text="", provider=self.provider.provider_id)
        prompt = build_ocr_prompt(images, purpose)
        vision_images = [
            VisionImage(data=image.data, media_type=image.media_type, filename=image.label)
            for image in images
        ]
        response = await self.provider.ainvoke_vision(prompt, vision_images)
        return OCRResult(
            text=(response.content or "").strip(),
            provider=response.provider,
            model=response.model,
            duration_ms=response.duration_ms,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            warnings=[],
        )


class MathpixOCRSkill:
    """Placeholder for a future paid/high-precision OCR path."""

    name = "MathpixOCRSkill"

    @property
    def available(self) -> bool:
        return bool(settings.mathpix_app_id and settings.mathpix_app_key)

    async def recognize_images(
        self,
        images: list[OCRImage],
        purpose: OCRPurpose,
    ) -> OCRResult:
        raise NotImplementedError("Mathpix OCR is not implemented yet.")
