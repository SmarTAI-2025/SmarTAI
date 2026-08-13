import pytest

from backend.agents.ingest_agent import (
    MATERIAL_IMPORT_SYSTEM_PROMPT,
    PROB_SYSTEM_PROMPT,
    REFERENCE_SYSTEM_PROMPT,
)


@pytest.mark.parametrize(
    ("prompt", "required_contract"),
    [
        (
            PROB_SYSTEM_PROMPT,
            "never leave commands such as `\\int`, `\\mu`, or `\\times` bare in prose",
        ),
        (
            REFERENCE_SYSTEM_PROMPT,
            "never leave LaTeX commands bare or add math delimiters inside code",
        ),
        (
            MATERIAL_IMPORT_SYSTEM_PROMPT,
            "never add math delimiters inside code or test data",
        ),
    ],
)
def test_ingest_prompts_require_renderable_latex_without_rewriting_code(
    prompt: str,
    required_contract: str,
):
    assert "inline LaTeX" in prompt
    assert "`$...$`" in prompt
    assert "`$$...$$`" in prompt
    assert required_contract in prompt
