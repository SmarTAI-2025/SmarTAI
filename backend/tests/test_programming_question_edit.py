"""Programming edits through the reference selector, generator and development runner."""
import json
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest

from backend.domain.errors import ValidationError
from backend.models import TestCase
from backend.services import programming_question_edit as edits
from backend.services.programming_materials import material_problem, resolve_generated_outputs
from backend.services.programming_runs import _reference
from backend.skills.programming import _teacher_material
from backend.tests.test_question_material_format_repair import Provider
from backend.tools import code_interpreter

DOUBLE = "def scale(x): return x * 2"
TRIPLE = "def scale(x): return x * 3"
BINDING = "programming_reference_binding"


def question(**changes):
    return {"q_id": "q1", "number": "1", "type": "编程题",
            "stem": "Implement scale(x). x is an integer. Return twice x.",
            "reference_answer": "Return twice x.", "criterion": "Correctness.",
            "solution_code": DOUBLE, "test_cases": [], "material_provenance": {},
            "ai_completion_provenance": {}, "programming_scoring": {"test_points": 0, "input_rules": ""},
            **changes}


def case(value, expected, **changes):
    return {"input": value, "expected_output": expected, **changes}


def provider_for(*values):
    return Provider([json.dumps({"candidates": [
        {"target_id": f"q1:{k}", "target": k, "q_id": "q1",
         ("test_cases" if k == "test_cases" else "text_value"): v}
        for k, v in value.items()]}) for value in values])


async def save(before, *, provider=None, fields=None, **changes):
    candidate = {**deepcopy(before), **deepcopy(changes)}
    original = deepcopy(candidate)
    patch = await edits.prepare_programming_edit(
        before=before, candidate=candidate, edited_fields=fields or changes.keys(),
        get_provider=(lambda: provider) if provider else Mock(side_effect=AssertionError("unexpected provider")),
    )
    assert candidate == original
    return {**candidate, **patch}


@pytest.mark.asyncio
@pytest.mark.parametrize("inp,out", [("5", "8"), ("4", "10")])
async def test_reordered_or_removed_rows_cannot_hide_one_sided_edits(monkeypatch, inp, out):
    before = question(test_cases=[case("4", "8", visibility="hidden"), case("2", "4"), case("3", "6")])
    run = AsyncMock()
    monkeypatch.setattr(code_interpreter, "run_sandbox", run)
    with pytest.raises(ValidationError) as error:
        await save(before, fields=before.keys(), test_cases=[before["test_cases"][2], case(inp, out)])
    assert error.value.code == "invalid_programming_test_cases"
    assert error.value.details == {"case_number": 2, "issue": "paired_edit"}
    run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("expected,accept", [("10", True), ("11", False)])
async def test_changed_pair_and_new_hidden_case_run_only_the_touched_rows(monkeypatch, expected, accept):
    before = question(test_cases=[case("4", "8", source="llm_generated"), case("2", "4")])
    run = AsyncMock(wraps=code_interpreter.run_sandbox)
    monkeypatch.setattr(code_interpreter, "run_sandbox", run)
    changed = [before["test_cases"][1], case("5", expected, source="llm_generated", visibility="hidden"),
               case("9", "18", visibility="hidden")]
    if accept:
        saved = await save(before, test_cases=changed)
        assert [c["expected_return"] for c in saved["test_cases"]] == ["4", "10", "18"]
        assert saved["test_cases"][1]["source"] == "teacher"
    else:
        with pytest.raises(ValidationError) as error:
            await save(before, test_cases=changed)
        assert error.value.code == "programming_reference_conflict"
        assert error.value.details["case_number"] == 2
    assert [c.function_args for c in run.await_args.args[1]] == [[5], [9]]


@pytest.mark.asyncio
@pytest.mark.parametrize(("display", "expected", "args"), [
    ("[4]", "12", [4]), ("[[4]]", "[4, 4, 4]", [[4]]),
])
async def test_new_editor_row_uses_displayed_argument_vector_even_without_prior_cases(display, expected, args):
    before = question(solution_code=TRIPLE, stem="Implement scale(x). Return x * 3.")
    saved = await save(before, test_cases=[case(display, expected, io_mode="stdin")], test_case_origins=[None])
    assert saved["test_cases"][0]["function_args"] == args
    assert saved["test_cases"][0]["expected_return"] == expected


@pytest.mark.asyncio
async def test_label_visibility_and_reorder_preserve_authorship_with_no_io(monkeypatch):
    before = question(test_cases=[case("2", "4", source="llm_generated"), case("4", "8")])
    run = AsyncMock()
    monkeypatch.setattr(code_interpreter, "run_sandbox", run)
    saved = await save(before, test_cases=[before["test_cases"][1], {**before["test_cases"][0],
                                                                     "title": "Boundary", "visibility": "hidden",
                                                                     "source": "teacher"}])
    assert saved["test_cases"][1]["source"] == "llm_generated"
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_syntax_only_code_edit_adopts_teacher_authorship_without_execution(monkeypatch):
    before = question(ai_completion_provenance={"solution_code": {}, "criterion": {"source_kind": "ai_generated"}},
                      test_cases=[case("1", "2")])
    run = AsyncMock()
    monkeypatch.setattr(code_interpreter, "run_sandbox", run)
    after = await save(before, solution_code="# My implementation\n" + DOUBLE)
    assert after["solution_code"].startswith("# My")
    assert "solution_code" not in after["ai_completion_provenance"] and "criterion" in after["ai_completion_provenance"]
    assert after["material_provenance"]["solution_code"]["source_kind"] == "teacher_edited"
    assert _reference(material_problem(after)) == after["solution_code"]
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_code_edit_reruns_teacher_rows_and_derives_only_ai_expectations():
    before = question(test_cases=[case("0", "0"), case("3", "6", visibility="hidden", source="llm_generated")])
    after = await save(before, solution_code=TRIPLE)
    assert after["reference_answer"] == before["reference_answer"] and after["criterion"] == before["criterion"]
    assert after["solution_code"] == TRIPLE
    assert after["test_cases"][0]["expected_output"] == "0"
    assert after["test_cases"][1]["expected_output"] == "9"
    assert after["test_cases"][1]["visibility"] == "hidden"
    with pytest.raises(ValidationError, match="expected result"):
        await save(question(test_cases=[case("4", "8")]), solution_code=TRIPLE)


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [0, 6])
async def test_changed_spec_can_never_use_previous_teacher_code_or_answer_fence(budget):
    answer = f"Teacher's notes\n```python\n{DOUBLE}\n```"
    before = question(reference_answer=answer, material_provenance={"solution_code": {"source_kind": "uploaded_code"}},
                      programming_scoring={"test_points": budget, "input_rules": ""})
    targets = {"solution_code": TRIPLE}
    if budget:
        targets["test_cases"] = [dict(io_mode="function", function_args=[i], expected_return="999") for i in range(10)]
    provider = provider_for(targets)
    after = await save(before, stem=before["stem"].replace("twice x", "x*3"), provider=provider)
    assert len(provider.calls) == 1
    assert DOUBLE not in str(provider.calls)
    assert after["reference_answer"] == answer and after["solution_code"] == DOUBLE
    assert after["material_provenance"]["solution_code"] == before["material_provenance"]["solution_code"]
    assert after[BINDING]["field"] == "generated_code"
    assert set(after[BINDING]["stale_fields"]) == {"reference_answer", "solution_code"}
    assert _reference(material_problem(after)) == TRIPLE
    assert not _reference(material_problem(after), teacher_only=True)
    assert DOUBLE not in str(_teacher_material(material_problem(after)))
    if budget:
        assert [c["expected_return"] for c in after["test_cases"]] == [str(3*i) for i in range(10)]
    else:
        assert not after["test_cases"]
    probes = [TestCase(io_mode="function", function_args=[i]) for i in range(10)]
    derived = await resolve_generated_outputs(after, probes)
    assert derived[4].expected_return == "12"
    assert not _reference(material_problem({**after, "stem": "Now return x * 4."}))


@pytest.mark.asyncio
async def test_new_teacher_answer_fence_replaces_stale_dedicated_priority_without_generation():
    after = await save(question(), reference_answer=f"Revised implementation\n```python\n{TRIPLE}\n```")
    assert after[BINDING]["field"] == "reference_answer"
    assert after["solution_code"] == DOUBLE
    assert _reference(material_problem(after)).strip() == TRIPLE


@pytest.mark.asyncio
async def test_new_independent_example_can_confirm_unchanged_teacher_rule():
    before = question()
    after = await save(before, stem=before["stem"] + "\nExample: scale(4) -> 8")
    assert _reference(material_problem(after)) == DOUBLE
    assert after[BINDING]["field"] == "solution_code"
    # An added zero example can't corroborate the old implementation after
    # changing the general multiplier. Both the old and new functions pass it.
    provider = provider_for({"solution_code": TRIPLE})
    revised = await save(before, stem=before["stem"].replace("twice x", "three times x") +
                         "\nExample: scale(0) -> 0", provider=provider)
    assert _reference(material_problem(revised)) == TRIPLE


@pytest.mark.asyncio
@pytest.mark.parametrize("row,allowed", [
    (case("", ""), True), ({"expected_output": ""}, False), ({"input": ""}, False),
])
async def test_empty_stdin_and_output_are_distinct_from_absent_fields(row, allowed):
    before = question(stem="Read stdin and echo it exactly.", solution_code="import sys\nsys.stdout.write(sys.stdin.read())")
    if allowed:
        after = await save(before, test_cases=[row])
        assert after["test_cases"][0]["input"] == "" and after["test_cases"][0]["expected_output"] == ""
    else:
        with pytest.raises(ValidationError, match="input|output"):
            await save(before, test_cases=[row])


@pytest.mark.asyncio
async def test_function_display_and_structured_edits_compare_effective_pairs():
    base = dict(input="[4]", expected_output="8", io_mode="function", function_args=[4], expected_return="8")
    before = question(test_cases=[base])
    for changed in [dict(input="[5]", expected_output="10"), dict(function_args=[5], expected_return="10")]:
        saved = await save(before, test_cases=[{**base, **changed}])
        row = saved["test_cases"][0]
        assert row["function_args"] == [5] and row["expected_return"] == "10"
    with pytest.raises(ValidationError) as err:
        await save(before, test_cases=[{**base, "function_args": [5]}])
    assert err.value.details["issue"] == "paired_edit"


@pytest.mark.asyncio
async def test_metadata_only_and_rule_only_edits_make_no_derived_patch():
    row = question()
    for field, value, semantic in [("stem", row["stem"] + ".", []), ("criterion", "Changed rules", [])]:
        patch = await edits.prepare_programming_edit(before=row, candidate={**row, field: value,
            "programming_semantic_fields": semantic}, edited_fields={field}, get_provider=None)
        assert patch == {}


@pytest.mark.asyncio
async def test_legacy_embedded_teacher_implementation_validates_first_sample():
    before = question(solution_code=None, reference_answer=f"Teacher's implementation\n```python\n{TRIPLE}\n```")
    saved = await save(before, test_cases=[case("4", "12")])
    assert saved["test_cases"][0]["expected_return"] == "12"


@pytest.mark.asyncio
async def test_empty_args_and_empty_string_none_and_stdout_preserve_authored_spelling():
    for expr, expected in [("''", "''"), ("None", "None"), ("[]", "[]")]:
        before = question(stem="Implement answer().", solution_code=f"def answer(): return {expr}")
        after = await save(before, test_cases=[dict(function_args=[], expected_return=expected,
                                                   io_mode="function", visibility="hidden")])
        assert after["test_cases"][0]["function_args"] == [] and after["test_cases"][0]["expected_return"] == expected
    text = "  a, b\n  "
    before = question(stem="Read stdin and echo it verbatim.", solution_code="import sys\nsys.stdout.write(sys.stdin.read())")
    after = await save(before, test_cases=[case(text, text, comparison="exact")])
    assert after["test_cases"][0]["input"] == after["test_cases"][0]["expected_output"] == text


@pytest.mark.asyncio
async def test_code_cosmetic_edit_cannot_reinstate_obsolete_teacher_code():
    before = question()
    new_stem = before["stem"].replace("twice x", "x*3")
    new_code = "# teacher comment\n" + DOUBLE
    after = await save(before, stem=new_stem, solution_code=new_code, provider=provider_for({"solution_code": TRIPLE}))
    assert after["solution_code"] == new_code
    assert _reference(material_problem(after)) == TRIPLE
    relabeled = await save(after, solution_code=new_code + "\n")
    assert _reference(material_problem(relabeled)) == TRIPLE


@pytest.mark.asyncio
async def test_missing_or_mismatched_binding_prevents_using_old_program():
    first = await save(question(), reference_answer=f"```python\n{TRIPLE}\n```")
    broken = {**first, "reference_answer": "different answer"}
    assert _reference(material_problem(broken)) is None
    with pytest.raises(ValidationError) as err:
        await save(broken, test_cases=[case("4", "8")])
    assert err.value.code == "programming_cases_unavailable"


@pytest.mark.asyncio
async def test_zero_weight_still_checks_the_current_function_entry():
    with pytest.raises(ValidationError) as err:
        await save(question(), solution_code="def unrelated(n): return n")
    assert err.value.code == "programming_reference_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["c", "cpp"])
async def test_native_stdin_preserves_literal_input_and_uses_native_runner(monkeypatch, language):
    from backend.tools.code_interpreter import ExecutionReport, TestResult
    source = 'int main(void) { return 0; }'
    before = question(stem="Read stdin and produce no stdout.", solution_code=f"```{language}\n{source}\n```")
    async def native(code, rows, **kwargs):
        assert code.strip() == source and kwargs["language"] == language
        assert rows[0].input == " 1, 3\n"
        return ExecutionReport(1, 1, 1.0, [TestResult(test=rows[0], passed=True, duration_ms=1,
                               actual_output="", stdout="", error="")])
    run = AsyncMock(side_effect=native)
    monkeypatch.setattr(code_interpreter, "run_sandbox", run)
    after = await save(before, test_cases=[case(" 1, 3\n", "", visibility="hidden")])
    assert after["test_cases"][0]["visibility"] == "hidden"
    run.assert_awaited_once()
    with pytest.raises(ValidationError):
        await save(before, test_cases=[dict(io_mode="function", function_args=[1], expected_return="1")])
    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_row_origins_distinguish_replacement_from_one_sided_change():
    before = question(stem="Implement sign(x).", solution_code="def sign(x): return x > 0",
                      test_cases=[case("1", "True"), case("-1", "False", source="llm_generated")])
    rows = [before["test_cases"][1], case("2", "True", source="llm_generated")]
    # Insert a complete new row, delete the old first row, shift row 2 up.
    after = await save(before, test_cases=rows, test_case_origins=[1, None])
    assert [r["source"] for r in after["test_cases"]] == ["llm_generated", "teacher"]
    for origins in [None, [1, 0]]:
        with pytest.raises(ValidationError) as err:
            await save(before, test_cases=rows, test_case_origins=origins)
        assert err.value.details["issue"] == "paired_edit"
    for origins in [[1, 1], [0, 3], [1, -1], [True, None], [0]]:
        with pytest.raises(ValidationError) as err:
            await save(before, test_cases=rows, test_case_origins=origins)
        assert err.value.code == "invalid_programming_test_cases"


@pytest.mark.asyncio
async def test_minimal_patch_preserves_teacher_raw_rows_and_touches_only_generated_expectations():
    before = question(test_cases=[case("0", "0", title="Zero")])
    saved = await save(before, solution_code=TRIPLE)
    assert saved["test_cases"] == before["test_cases"]


@pytest.mark.asyncio
async def test_mixed_function_display_and_structured_cannot_disguise_whitespace_only_expected_change():
    before = question(test_cases=[case("1", "True")], stem="Implement positive(x).",
                      solution_code="def positive(x): return x > 0")
    with pytest.raises(ValidationError) as err:
        await save(before, test_cases=[case("2", "True ")])
    assert err.value.details["issue"] == "paired_edit"


@pytest.mark.asyncio
async def test_valid_generated_inputs_reused_across_semantic_change_without_generating_inputs():
    before = question(test_cases=[case("4", "8", source="llm_generated", visibility="hidden")],
                      ai_completion_provenance={"test_cases": {"source_kind": "ai_generated"}})
    new_stem = before["stem"].replace("twice x", "x*3")
    provider = provider_for({"solution_code": TRIPLE})
    after = await save(before, stem=new_stem, provider=provider)
    assert len(provider.calls) == 1
    assert [(c["input"], c["expected_output"], c["visibility"]) for c in after["test_cases"]] == [("4", "12", "hidden")]
    assert after["ai_completion_provenance"]["test_cases"]["input_review_fingerprint"]


@pytest.mark.asyncio
async def test_current_teacher_answer_code_can_supply_generated_tests_and_keep_its_source():
    before = question(programming_scoring={"test_points": 6, "input_rules": ""})
    new_stem = before["stem"].replace("twice x", "x*3")
    provider = provider_for({"test_cases": [dict(io_mode="function", function_args=[i]) for i in range(10)]})
    new_answer = f"Use the following implementation.\n```python\n{TRIPLE}\n```"
    after = await save(before, stem=new_stem, reference_answer=new_answer, provider=provider)
    assert after[BINDING]["field"] == "reference_answer"
    assert [c["expected_return"] for c in after["test_cases"]] == [str(3*i) for i in range(10)]
    assert _reference(material_problem(after), teacher_only=True).strip() == TRIPLE


@pytest.mark.asyncio
async def test_grading_preparation_and_manual_test_generation_both_execute_bound_code():
    from backend.agents.ingest_agent import generate_missing_question_materials
    from backend.services import programming_runs

    before = question()
    after = await save(before, stem=before["stem"].replace("twice x", "x*3"),
                       provider=provider_for({"solution_code": TRIPLE}))
    # Manual preparation receives a serialized question containing BOTH the
    # original teacher source and the independently selected current source.
    proposals = [dict(io_mode="function", function_args=[i]) for i in range(10)]
    provider = provider_for({"test_cases": proposals})
    generated, = await generate_missing_question_materials(
        {"q1": after}, [dict(q_id="q1", target="test_cases", target_id="q1:test_cases")],
        10, provider)
    assert generated.test_cases[4].expected_return == "12"
    assert DOUBLE not in str(provider.calls)
    row = await save(after, test_cases=[case("4", "12", visibility="hidden")])
    run = AsyncMock(wraps=code_interpreter.run_sandbox)
    _, suite, reason = await programming_runs._prepare(
        programming_runs.ProgrammingRun(), material_problem(row), None, None, run, None)
    assert not reason and suite[0].expected_return == "12"
    assert run.await_args.args[0] == TRIPLE


@pytest.mark.asyncio
async def test_test_runner_outage_rejects_edit_instead_of_claiming_success(monkeypatch):
    monkeypatch.setattr(code_interpreter, "run_sandbox", AsyncMock(side_effect=TimeoutError()))
    with pytest.raises(ValidationError) as err:
        await save(question(), test_cases=[case("4", "8")])
    assert err.value.code == "programming_execution_unavailable"


@pytest.mark.asyncio
async def test_generated_replacement_retains_a_corroborated_teacher_implementation():
    # No previous output is used as an oracle for the new problem. The fresh
    # generation sees no old code, returns an independently identical module
    # and the explicit example is verified on the actual teacher original.
    before = question(solution_code="# teacher original\n" + TRIPLE)
    after = await save(before, stem=before["stem"].replace("twice x", "x*3") + "\nscale(4) -> 12",
                       provider=provider_for({"solution_code": TRIPLE}))
    assert after[BINDING]["field"] == "solution_code"
    assert _reference(material_problem(after), teacher_only=True) == before["solution_code"]


@pytest.mark.asyncio
async def test_old_upload_with_mixed_provenance_survives_new_ai_supplement():
    before = question(material_provenance={"solution_code": {"source_kind": "uploaded"}},
                      ai_completion_provenance={"solution_code": {"source_kind": "ai_generated"}})
    after = await save(before, stem=before["stem"].replace("twice x", "x*3"),
                       provider=provider_for({"solution_code": TRIPLE}))
    assert after["solution_code"] == DOUBLE and after[BINDING]["generated_code"] == TRIPLE
    assert after[BINDING]["source_kind"] == "ai_generated"


@pytest.mark.asyncio
async def test_ai_code_appended_to_teacher_text_is_not_an_independent_teacher_oracle():
    before = question(reference_answer="(a) Teacher's answer")
    new_answer = before["reference_answer"] + f"\n(b)\n```python\n{TRIPLE}\n```"
    after = await save(before, stem=before["stem"].replace("twice x", "x*3"), reference_answer=new_answer,
                       teacher_material_supplements={"reference_answer": {"original": before["reference_answer"],
                                                                         "parts": ["b"]}},
                       fields={"stem"})
    problem = material_problem(after)
    assert _reference(problem).strip() == TRIPLE
    assert _reference(problem, teacher_only=True) is None
    assert after["reference_answer"].startswith(before["reference_answer"])
