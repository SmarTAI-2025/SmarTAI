import json
import pytest

from backend.agents.ingest_agent import AI_COMPLETION_SYSTEM_PROMPT, AICompletionOutput
from backend.tools.structured_llm import (
    StructuredOutputInvalidError,
    _collapse_duplicate_json_colons,
    _escape_latex_backslashes,
    _protect_json_math_escapes,
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


@pytest.mark.parametrize("command", ["underbrace", "underline", "uparrow", "underset", "upsilon", "uplus",
    "updownarrow", "upharpoonleft", "upharpoonright", "upuparrows", "underbracket", "undergroup",
    "underlinesegment", "underleftarrow", "underrightarrow", "underleftrightarrow", "utilde", "unlhd", "unrhd"])
@pytest.mark.parametrize("fences", [("$", "$"), ("$$", "$$"), (r"\(", r"\)"), (r"\[", r"\]")])
def test_raw_u_tex_commands_preserve_unicode_and_existing_escapes(command, fences):
    expression = fences[0] + "\\" + command + r"{x} + \frac{1}{2}" + fences[1]
    text = "设 " + expression + "\nNext line."
    raw = json.dumps({"candidates": [{"target_id": "q1:reference_answer", "q_id": "q1",
        "target": "reference_answer", "text_value": text}]}).replace("\\\\" + command, "\\" + command)
    expected = text.replace(r"\(", "$").replace(r"\)", "$").replace(r"\[", "$$").replace(r"\]", "$$")
    assert extract_and_parse_json(raw, AICompletionOutput).candidates[0].text_value == expected


def test_raw_u_tex_repair_composes_with_raw_delimiters_and_literal_newlines():
    raw = (r'{"candidates":[{"target_id":"q1:reference_answer","q_id":"q1",'
           r'"target":"reference_answer","text_value":"\u8bbe \[\underbrace{x}_{n}\]'
           + '\n' + r'\(\uparrow\)"}]}')
    assert extract_and_parse_json(raw, AICompletionOutput).candidates[0].text_value == (
        '设 $$\\underbrace{x}_{n}$$\n$\\uparrow$')


@pytest.mark.parametrize("bad", [r"\u", r"\uffa", r"\u4f", r"\u鍚ng", r"\underbracex"])
def test_known_u_command_does_not_mask_another_bad_unicode_escape(bad):
    raw = (r'{"candidates":[{"target_id":"q1:reference_answer","q_id":"q1",'
           r'"target":"reference_answer","text_value":"\u8bbe $\underbrace{x} '
           + bad + r'$"}]}')
    with pytest.raises(StructuredOutputInvalidError):
        extract_and_parse_json(raw, AICompletionOutput)


@pytest.mark.parametrize("field", ["quote", "context_quote", "source_quote", "solution_code",
    "reference_code", "student_code", "code", "input", "expected_output", "expected_return",
    r"\u0073ource_quote", r"\u0063ode"])
def test_u_command_repair_never_rewrites_verbatim_fields(field):
    raw = '{"' + field + r'":"$\underbrace{x}$"}'
    assert _protect_json_math_escapes(raw) == raw


@pytest.mark.parametrize("value", [r"\underbrace{x}", r"`$\underbrace{x}$`",
    r"``$\underbrace{x}$``", r"``a ` $\underbrace{x}$``",
    r"````a ``` $\underbrace{x}$````", "```tex\n$\\underbrace{x}$\n````",
    "```tex\n$\\underbrace{x}$\n```", "~~~tex\n$\\underbrace{x}$\n~~~",
    r"$\u8bbe \uD835\uDC9C \\underbrace{x}$"])
def test_u_command_repair_leaves_ambiguous_prose_code_and_valid_escapes(value):
    raw = '{"text_value":"' + value + '"}'
    assert _protect_json_math_escapes(raw) == raw


@pytest.mark.parametrize("code_first", [False, True])
def test_math_repair_is_scoped_to_its_candidate_when_code_is_also_returned(code_first):
    # The target may follow text_value; a sibling code candidate must neither
    # suppress the answer's repair nor have its own escapes reinterpreted.
    answer = {"text_value": r"设 $\underbrace{x}$", "target_id": "q1:reference_answer",
        "q_id": "q1", "target": "reference_answer"}
    code = {"text_value": 'print("$\\underbrace{x}$")', "target_id": "q1:solution_code",
        "q_id": "q1", "target": "solution_code"}
    candidates = [code, answer] if code_first else [answer, code]
    raw = json.dumps({"candidates": candidates}).replace(r"\u8bbe $\\underbrace", r"\u8bbe $\underbrace")
    actual = extract_and_parse_json(raw, AICompletionOutput).candidates
    assert [item.text_value for item in actual] == [item["text_value"] for item in candidates]


@pytest.mark.parametrize("target_key,target", [("target", "solution_code"), (r"\u0074arget", r"\u0073olution_code")])
def test_code_candidate_scope_preserves_raw_escape_semantics(target_key, target):
    # Valid JSON newline inside a code string must stay a newline, even though
    # the same bytes would denote a TeX command inside a mathematical answer.
    raw = (r'{"candidates":[{"text_value":"$\nu$", "target_id":"q1:solution_code",'
           r'"q_id":"q1","' + target_key + '":"' + target + '"}]}')
    assert _protect_json_math_escapes(raw) == raw
    assert extract_and_parse_json(raw, AICompletionOutput).candidates[0].text_value == "$\nu$"


@pytest.mark.parametrize("raw", [
    r'{"candidates":[{"target":"\uffa", "text_value":"$\underbrace{x}$"}]}',
    r'{"candidates":[{"\uffa":"x", "text_value":"$\underbrace{x}$"}]}',
])
def test_invalid_context_tokens_still_use_the_structured_failure_contract(raw):
    with pytest.raises(StructuredOutputInvalidError):
        extract_and_parse_json(raw, AICompletionOutput)
