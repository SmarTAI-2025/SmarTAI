import json
import pytest

from backend.agents.ingest_agent import AI_COMPLETION_SYSTEM_PROMPT, AICompletionOutput
from backend.tools.structured_llm import (
    _collapse_duplicate_json_colons,
    _escape_latex_backslashes,
    extract_and_parse_json,
)


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
