"""Canonical question types shared by recognition, teacher edits and grading."""
import unicodedata


QUESTION_TYPES = ("计算题", "概念题", "推理题", "编程题", "其他")
_ALIASES = {
    "calculation": "计算题", "programming": "编程题", "coding": "编程题",
    "reasoning": "推理题",
    "concept": "概念题", "概念": "概念题", "推理": "推理题",
    "计算": "计算题", "编程": "编程题",
    "other": "其他", "其它": "其他",
}


def normalize_question_type(value: object) -> str:
    """Unknown/missing types stay unselected; only an explicit Other is Other."""
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value).strip().lower()
    return value if value in QUESTION_TYPES else _ALIASES.get(value, "")
