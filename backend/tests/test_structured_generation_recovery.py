import json
import pytest

from backend.agents.ingest_agent import AI_COMPLETION_SYSTEM_PROMPT, AICompletionOutput
from backend.tools.structured_llm import (
    StructuredOutputInvalidError,
    _collapse_duplicate_json_colons,
    _escape_latex_backslashes,
    extract_and_parse_json,
)


@pytest.mark.parametrize("invalid_escape", [r"\uffa", r"\u4f", r"\u鍚ng"])
def test_invalid_unicode_cannot_turn_a_whole_response_into_literal_escapes(invalid_escape):
    # The test1 incident mixed valid Chinese/TeX escapes with malformed Unicode.
    # Doubling every slash makes this parse, but corrupts all otherwise valid text.
    raw = (r'{"candidates":[{"target_id":"q1:reference_answer","q_id":"q1",'
           r'"target":"reference_answer","text_value":"\u8bbe $\\mathbb{Z}$ '
           + invalid_escape + r'"}]}')
    with pytest.raises(StructuredOutputInvalidError):
        extract_and_parse_json(raw, AICompletionOutput)


def test_valid_unicode_and_algebra_commands_decode_once_during_tex_repair():
    raw = (r'{"candidates":[{"target_id":"q1:reference_answer","q_id":"q1",'
           r'"target":"reference_answer","text_value":"\u8bbe '
           r'$\\bar{a} \\in \\mathbb{Z}/n\\mathbb{Z}$\n$\overline{ab}$"}]}')
    parsed = extract_and_parse_json(raw, AICompletionOutput)
    assert parsed.candidates[0].text_value == (
        '设 $\\bar{a} \\in \\mathbb{Z}/n\\mathbb{Z}$\n$\\overline{ab}$')


def test_literal_unicode_in_programming_candidates_is_not_decoded_again():
    code = r'print(r"\u8bbe\uffa")'
    raw = json.dumps({"candidates": [{"target_id": "q1:solution_code", "q_id": "q1",
        "target": "solution_code", "text_value": code}]})
    assert extract_and_parse_json(raw, AICompletionOutput).candidates[0].text_value == code


def test_mixed_tex_escapes_and_literal_newlines_preserve_formulas():
    raw = r'{"candidates":[{"target_id":"q1:reference_answer","q_id":"q1","target":"reference_answer","text_value":"$\\iota(G)$ and $\{g,g^{-1}\}$' + '\n' + r'$|\iota(G)|$"}]}'
    candidate = extract_and_parse_json(raw, AICompletionOutput).candidates[0]
    assert candidate.text_value == "$\\iota(G)$ and $\\{g,g^{-1}\\}$\n$|\\iota(G)|$"


def test_valid_escape_pairs_are_not_doubled():
    raw = json.dumps({"text": r'$\iota(G)$ and "quotes"', "code": "std::vector\n"})
    assert _escape_latex_backslashes(raw) == raw


def test_duplicate_colon_repair_does_not_touch_prose_or_code():
    raw = r'{"candidates"::[{"target_id":"q1:solution_code","q_id":"q1","target":"solution_code","text_value":"std::vector<int> v; // https://example.com/a::b"}]}'
    parsed = extract_and_parse_json(raw, AICompletionOutput)
    assert parsed.candidates[0].text_value == "std::vector<int> v; // https://example.com/a::b"
    valid = json.dumps({"text": 'quoted ": :" and \\"::', "object": {"key": "value"}})
    assert _collapse_duplicate_json_colons(valid) == valid


def test_generation_prompt_shows_literal_json_escape_examples():
    assert r'encode line breaks once as `\n`, never double-escape them as `\\n`' in AI_COMPLETION_SYSTEM_PROMPT


@pytest.mark.parametrize("command", ["neq", "nu", "nabla", "times", "theta", "text", "frac", "beta", "bar", "right", "rangle"])
def test_raw_tex_valid_json_escapes_do_not_silently_change_math(command):
    expression = f"$x \\{command} y$"
    raw = json.dumps({"candidates": [{"target_id": "q1:reference_answer", "q_id": "q1", "target": "reference_answer", "text_value": expression}]}).replace("\\\\" + command, "\\" + command)
    parsed = extract_and_parse_json(raw, AICompletionOutput)
    assert parsed.candidates[0].text_value == expression


def test_mixed_json_escapes_preserve_control_commands_and_real_separators():
    raw = r'{"candidates":[{"target_id":"q1:reference_answer","q_id":"q1","target":"reference_answer","text_value":"$x \neq 0, \\nu=1, \frac{1}{2}$\nNext line. `\\n`"}]}'
    parsed = extract_and_parse_json(raw, AICompletionOutput)
    assert parsed.candidates[0].text_value == '$x \\neq 0, \\nu=1, \\frac{1}{2}$\nNext line. `\\n`'


def test_solution_candidate_and_test_data_are_not_reinterpreted_as_math():
    code = 'print("$\\nu$")'
    raw = json.dumps({"candidates": [{"target_id": "q1:solution_code", "q_id": "q1", "target": "solution_code", "text_value": code}]})
    assert extract_and_parse_json(raw, AICompletionOutput).candidates[0].text_value == code
