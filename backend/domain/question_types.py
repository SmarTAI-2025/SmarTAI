"""Canonical question types shared by recognition, teacher edits and grading."""
import unicodedata


QUESTION_TYPES = ("计算题", "编程题", "证明题", "推理题", "概念题", "选择题", "多选题", "填空题", "其他")
_ALIASES = {
    "calculation": "计算题", "programming": "编程题", "coding": "编程题",
    "proof": "证明题", "reasoning": "推理题", "concept": "概念题",
    "choice": "选择题", "单选题": "选择题", "multiple_choice": "多选题", "fill_blank": "填空题",
    "short_answer": "概念题", "short": "概念题", "简答题": "概念题", "问答题": "概念题",
    "计算": "计算题", "编程": "编程题", "证明": "证明题",
    "other": "其他", "其它": "其他",
}


def normalize_question_type(value: object) -> str:
    """Unknown/missing types stay unselected; only an explicit Other is Other."""
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value).strip().lower()
    return value if value in QUESTION_TYPES else _ALIASES.get(value, "")
