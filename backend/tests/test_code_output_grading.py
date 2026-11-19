"""Ordinary question admission, deterministic stdout scoring, and leased reuse."""

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents import ingest_agent
from backend.agents.question_preparation_agent import requested_major_question_materials
from backend.db import assignment_repository, grading_repository, workflow_repository
from backend.db.provider_repository import upsert_provider_config
from backend.domain.code_output import bind_code_output
from backend.models import ProviderConfig, TaskGradingSetup
from backend.services import code_output_grading, grading_runs, task_facade
from backend.services.programming_runs import QUESTION_OUTPUT
from backend.services.question_preparation_artifacts import ArtifactProblemV1
from backend.services.question_structure import annotate_major_question_structures
from backend.services.rubric_drafts import RubricDefinition, generated_rubric
from backend.skills import math_grading
from backend.tests.test_task_background_workflows import _seed_task
from backend.tools import oci_grading_runner
from backend.tools.code_interpreter import TestResult as RunResult
from backend.models import TestCase as Case


PROGRAM = "n = int(input())\nprint(n * 2 + 1)\n"


def source_question(form, *, code=PROGRAM, input_quote="Input: 3", stdin="3", language="python"):
    prompt = "What is printed by the given program?"
    if form == "explanation":
        prompt += " Explain how the program reaches the output."
    options = {"A": "6", "B": "7"} if form == "choice" else {}
    stem = f"{prompt}\n```{language}\n{code}```\n{input_quote}"
    if options:
        stem += "\n" + "\n".join(f"{label}. `{text}`" for label, text in options.items())
    candidate = dict(form=form, language=language, code=code, stdin=stdin,
                     input_quote=input_quote, choices=options)
    return stem, candidate


async def recognize(monkeypatch, source, candidate, *, partial=True, question_type="推理题"):
    reply = {"problems": [
        {"q_id": "q1", "number": "1", "type": question_type, "stem": source,
         "criterion": "", "code_output_candidate": candidate, "recognition_confidence": 1}
    ]}
    llm = AsyncMock(return_value=SimpleNamespace(content=json.dumps(reply)))
    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", llm)
    result = await ingest_agent._extract_problems_call(
        source, SimpleNamespace(provider_id="fixture"), partial_source=partial
    )
    llm.assert_awaited_once()
    for row in result.values():
        assert not row.pop("source_incomplete", False)
    return annotate_major_question_structures(result)


def rubric_definition(problem, form):
    maximum = problem["max_score"]
    if form == "explanation":
        steps = [
            {"id": "trace", "criterion": "Trace the multiplication before the addition", "total_pts": 6},
            {"id": "stdout", "criterion": "Report the actual printed output", "total_pts": 4, "code_output": True},
        ]
    else:
        steps = [{"id": "stdout", "criterion": "Identify the actual printed output", "total_pts": maximum}]
    return RubricDefinition(part_rubric={
        "id": "q1", "label": "1", "stem": problem["stem"], "total_pts": maximum,
        "parts": [{"id": "p1", "total_pts": maximum, "steps": steps, "alternative": {"deduct_per_error": 1}}],
    })


def confirmed_problem(problem, criterion, draft):
    return {**problem, "criterion": criterion, "rubric_draft": draft,
            "ai_completion_provenance": {"criterion": {"source_kind": "ai_generated"}},
            "review_status": "confirmed", "max_score_review_status": "confirmed"}


def reviewed_problem(problem, form):
    criterion, draft = generated_rubric(problem, rubric_definition(problem, form), language="en")
    return confirmed_problem(problem, criterion, draft.model_dump(mode="json"))


async def generate_problem(monkeypatch, questions, form, *, definition=None):
    targets = requested_major_question_materials(questions)
    target, = targets
    assert target["target"] == "criterion"
    question = questions["q1"]
    reply = {"candidates": [{**target, "rubric_definition":
                           (definition or rubric_definition(question, form)).model_dump(mode="json", exclude_unset=True)}]}
    call = AsyncMock(return_value=SimpleNamespace(content=json.dumps(reply), duration_ms=1))
    monkeypatch.setattr(ingest_agent, "ainvoke_with_retry", call)
    feedback = {}
    drafts = await ingest_agent.generate_missing_question_materials(
        questions, targets, 1, SimpleNamespace(provider_id="fixture"),
        generation_language="en", allow_partial=True, repair_feedback=feedback,
    )
    assert len(drafts) == 1, feedback
    call.assert_awaited_once()
    draft, = drafts
    return confirmed_problem(question, draft.text_value, draft.rubric_draft)


def stub_oci(monkeypatch, *, stdout="7\n", program=PROGRAM):
    monkeypatch.setattr(code_output_grading.settings, "program_runner_mode", "oci")
    monkeypatch.setattr(code_output_grading.settings, "runtime_environment", "production")

    async def execute(code, stdin, **limits):
        assert code == program
        assert stdin == "3"
        assert limits["timeout"] == 5.0 and limits["memory_mb"] == 256
        assert limits["max_output_chars"] == 8192 and limits["function_call"] is None
        return RunResult(Case(input=stdin), True, stdout, "", 1,
                         execution_id="synthetic-run", request_digest="a" * 64,
                         exit_reason="success", isolation_mode="oci_container")

    dispatched = AsyncMock(side_effect=execute)
    monkeypatch.setattr(oci_grading_runner, "run_python_oci", dispatched)
    return dispatched


def stub_process_grading(monkeypatch, fail_answers=()):
    async def grade(_provider, *, user_prompt, output_model, validate_output, **kwargs):
        answer = json.loads(user_prompt.split("\nINPUT DATA:\n")[1])["student_answer"]
        if answer in fail_answers:
            raise RuntimeError("synthetic provider failure")
        reasoning = "Multiplication gives 6." in answer
        proof_line = next((f"student:{i}" for i, line in enumerate(answer.splitlines(), 1)
                           if "Multiplication gives 6." in line), "student:1")
        # Both verdicts are deliberately wrong. The runner owns this row.
        result = output_model.model_validate({
            "confidence": 1, "comment": "Evaluate the multiplication trace.",
            "part_decisions": [{
                "part_id": "p1", "target_quote": "part:p1",
                "student_quote": proof_line,
                "relevance": "partial" if reasoning else "incorrect",
                "method": "reference", "progress": "The trace was checked.",
                "inferences": [
                    {"step_ids": ["trace"], "evidence": proof_line if reasoning else "",
                     "reasoning": "The trace is shown." if reasoning else "The multiplication step is missing.",
                     "result": "written" if reasoning else "gap"},
                    {"step_ids": ["stdout"], "evidence": "student:1",
                     "reasoning": "Conflicting model verdict", "result": "gap" if "Output: 7" in answer else "written"},
                ],
            }],
        })
        validate_output(result)
        return result, SimpleNamespace(content=result.model_dump_json(), duration_ms=1)

    called = AsyncMock(side_effect=grade)
    monkeypatch.setattr(math_grading, "structured_llm_call", called)
    monkeypatch.setattr(math_grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    return called


def registry_for(provider):
    registry = SimpleNamespace(count=lambda: 1, list_available=lambda: [provider],
                               get=lambda _: provider, pick_default=lambda: provider)
    registry.select = lambda *args, **kwargs: registry
    return registry


def task_with_problem(problem, responses):
    owner, task = _seed_task()
    task_facade._replace_draft_questions(task, owner, {"q1": problem}, "synthetic.md")
    provider = upsert_provider_config(owner, ProviderConfig(provider_type="openai", model="fixture",
        api_key="fixture-no-network"), master_key=os.environ["SMARTAI_PROVIDER_ENCRYPTION_KEY"])
    setup = TaskGradingSetup(selected_provider_ids=[provider.id], primary_provider_id=provider.id,
                             knowledge_scope="none", feedback_language="en")
    workflow_repository.update_workflow(task, owner_id=owner, grading_setup=setup.model_dump(mode="json"),
                                        grading_setup_fingerprint="confirmed")
    task_facade._commit_imported_submissions(task_id=task, owner_id=owner,
        course_id=assignment_repository.get_assignment(task, actor_id=owner).course_id,
        students=[{"stu_id": str(i), "stu_name": f"Student {i}", "source_id": "",
                   "identity_status": "matched", "identity_match_method": "filename",
                   "stu_ans": [{"q_id": "q1", "number": "1", "type": "推理题", "content": answer, "flag": []}]}
                  for i, answer in enumerate(responses, 1)],
        expected_workflow_revision=workflow_repository.get_live_workflow(task, owner_id=owner).workflow_revision,
        submission_file_name="synthetic.txt")
    return owner, task, provider


def start(owner, task, *, request_id="stdout-first", retry_scope="full_batch"):
    return task_facade.start_task_grading(task_id=task, owner_id=owner,
        expected_workflow_revision=workflow_repository.get_live_workflow(task, owner_id=owner).workflow_revision,
        request_id=request_id, retry_scope=retry_scope)["job_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("form,answers,expected", [
    ("choice", ["答案：B", "A"], [10, 0]),
    ("blank", ["`7`", "6"], [1.24, 0]),
    ("explanation", ["Output: 7\nMultiplication gives 6.", "Output: 6\nMultiplication gives 6.", "Output: 7"],
     [10, 6, 4]),
])
async def test_question_preparation_to_saved_grades(monkeypatch, form, answers, expected):
    stem, candidate = source_question(form)
    questions = await recognize(monkeypatch, stem, candidate)
    extracted = questions["q1"]
    extracted["max_score"] = expected[0]
    assert ArtifactProblemV1.model_validate(extracted).model_dump()["code_output"] == extracted["code_output"]
    problem = await generate_problem(monkeypatch, questions, form)
    owner, task, provider = task_with_problem(problem, answers)
    stored, = assignment_repository.list_questions(task, teacher_id=owner)
    assert stored.source["presentation"]["code_output"] == extracted["code_output"]
    assert stored.type == "推理题" and not stored.test_cases
    registry = registry_for(SimpleNamespace(provider_id=provider.id, model="fixture"))
    dispatched = stub_oci(monkeypatch)
    scored = stub_process_grading(monkeypatch)
    revision = workflow_repository.get_live_workflow(task, owner_id=owner).workflow_revision
    run_id = start(owner, task)
    await grading_runs.process_run(run_id=run_id, worker_id="synthetic-stdout", registry=registry)
    identities = workflow_repository.list_student_presentations(task)
    rows = sorted(grading_repository.list_results_for_run(run_id),
                  key=lambda row: identities[row.student_id].display_student_id)
    assert [row.ai_score for row in rows] == expected
    assert scored.await_count == (len(answers) if form == "explanation" else 0)
    assert sum(step["score"] for step in rows[0].ai_steps) == expected[0]
    if form == "explanation":
        assert {step["item_id"]: step["score"] for step in rows[1].ai_steps} == {"trace": 6, "stdout": 0}
    dispatched.assert_awaited_once()
    event, = grading_repository.list_events(run_id, actor_id=owner, messages=(QUESTION_OUTPUT,))
    assert event["payload"]["value"] == {
        "q_id": "q1", "stdout": "7\n", "isolation_mode": "oci_container", "request_digest": "a" * 64,
        "execution_id": "synthetic-run", "duration_ms": 1,
    }
    assert QUESTION_OUTPUT not in {row["message"] for row in grading_runs.get_run(
        run_id=run_id, actor_id=owner, role="teacher")["events"]}
    repeated = task_facade.start_task_grading(task_id=task, owner_id=owner,
        expected_workflow_revision=revision, request_id="stdout-first")
    assert repeated["job_id"] == run_id


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "rewritten_input", "edited_stem", "runner_disabled", "runtime", "truncated",
                                      "unavailable", "ambiguous_choice"])
async def test_unverified_output_has_no_grade_or_model_fallback(monkeypatch, failure):
    stem, candidate = source_question("choice")
    if failure == "missing":
        candidate = {}
    elif failure == "rewritten_input":
        candidate["stdin"] = "30"
    problem = reviewed_problem((await recognize(monkeypatch, stem, candidate, partial=False))["q1"], "choice")
    if failure == "edited_stem":
        changed = annotate_major_question_structures({"q1": {**problem,
            "stem": stem.replace("Input: 3", "Input: 4"), "criterion": "", "rubric_draft": None}})
        problem = reviewed_problem(changed["q1"], "choice")
    dispatched = stub_oci(monkeypatch)
    if failure == "runner_disabled":
        monkeypatch.setattr(code_output_grading.settings, "program_runner_mode", "disabled")
    elif failure == "runtime":
        dispatched.side_effect = None
        dispatched.return_value = RunResult(Case(), False, "7\n", "", 0, exit_reason="runtime_error",
                                             isolation_mode="oci_container", execution_id="rt", request_digest="a" * 64)
    elif failure == "truncated":
        dispatched.side_effect = None
        dispatched.return_value = RunResult(Case(), True, "7\n", "", 0, exit_reason="success", stdout_truncated=True,
                                             isolation_mode="oci_container", execution_id="rt", request_digest="a" * 64)
    elif failure == "unavailable":
        dispatched.side_effect = oci_grading_runner.IsolatedRunnerUnavailable()
    elif failure == "ambiguous_choice":
        stem, candidate = source_question("choice")
        stem = stem.replace("A. `6`", "A. `7`")
        candidate["choices"]["A"] = "7"
        problem = reviewed_problem((await recognize(monkeypatch, stem, candidate))["q1"], "choice")
    scored = stub_process_grading(monkeypatch)
    owner, task, provider = task_with_problem(problem, ["B", "B"])
    run_id = start(owner, task)
    await grading_runs.process_run(run_id=run_id, worker_id="synthetic-stdout",
        registry=registry_for(SimpleNamespace(provider_id=provider.id, model="fixture")))
    rows = grading_repository.list_results_for_run(run_id)
    assert len(rows) == 2
    assert all(row.ai_score is None and "code_output_unavailable" in row.review_reasons for row in rows)
    observed = grading_repository.list_events(run_id, actor_id=owner, messages=(QUESTION_OUTPUT,))
    assert len(observed) == (1 if failure == "ambiguous_choice" else 0)
    assert dispatched.await_count == (1 if failure in {"runtime", "truncated", "unavailable", "ambiguous_choice"} else 0)
    scored.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_explanation_retry_reuses_actual_output(monkeypatch):
    stem, candidate = source_question("explanation")
    problem = reviewed_problem((await recognize(monkeypatch, stem, candidate))["q1"], "explanation")
    answers = ["Output: 7\nMultiplication gives 6.", "Output: 6\nMultiplication gives 6."]
    owner, task, provider = task_with_problem(problem, answers)
    registry = registry_for(SimpleNamespace(provider_id=provider.id, model="fixture"))
    dispatched = stub_oci(monkeypatch)
    stub_process_grading(monkeypatch, fail_answers=[answers[1]])
    run_id = start(owner, task)
    await grading_runs.process_run(run_id=run_id, worker_id="synthetic-stdout", registry=registry)
    assert sorted((row.ai_score for row in grading_repository.list_results_for_run(run_id)), key=str) == [10, None]
    stub_process_grading(monkeypatch)
    retry = start(owner, task, request_id="stdout-failed-only", retry_scope="failed_only")
    await grading_runs.process_run(run_id=retry, worker_id="synthetic-stdout-retry", registry=registry)
    assert sorted(row.ai_score for row in grading_repository.list_results_for_run(retry)) == [6, 10]
    dispatched.assert_awaited_once()
    assert not grading_repository.list_events(retry, actor_id=owner, messages=(QUESTION_OUTPUT,))


def test_source_requires_exact_code_and_input_boundaries():
    stem, candidate = source_question("blank", input_quote="Input:  30", stdin=" 30")
    assert bind_code_output(candidate, stem=stem, document=stem).source.stdin == " 30"
    assert bind_code_output({**candidate, "stdin": " 3", "input_quote": "Input:  3"},
                            stem=stem, document=stem).source is None
    changed = stem.replace(PROGRAM, PROGRAM.replace("2 + 1", "4 + 1"))
    assert bind_code_output(candidate, stem=stem, document=changed).source is None
    assert bind_code_output(candidate, stem=stem, document=stem, incomplete=True).source is None
    inline, quoted = source_question("blank", input_quote="If the input is `30`, what does it print?", stdin="30")
    assert bind_code_output(quoted, stem=inline, document=inline).source.stdin == "30"


@pytest.mark.asyncio
async def test_incidental_code_does_not_change_grading(monkeypatch):
    stem, candidate = source_question("blank")
    questions = await recognize(monkeypatch, stem, candidate, question_type="概念题")
    assert "code_output" not in questions["q1"]
    assert {item["target"] for item in requested_major_question_materials(questions)} == {"criterion", "reference_answer"}


async def grade_fixture(monkeypatch, problem, answers, *, stdout, program=PROGRAM):
    dispatched = stub_oci(monkeypatch, stdout=stdout, program=program)
    scored = stub_process_grading(monkeypatch)
    owner, task, provider = task_with_problem(problem, answers)
    run_id = start(owner, task)
    await grading_runs.process_run(run_id=run_id, worker_id="synthetic-stdout",
        registry=registry_for(SimpleNamespace(provider_id=provider.id, model="fixture")))
    identities = workflow_repository.list_student_presentations(task)
    rows = sorted(grading_repository.list_results_for_run(run_id),
                  key=lambda row: int(identities[row.student_id].display_student_id))
    dispatched.assert_awaited_once()
    assert all("code_output_unavailable" in row.review_reasons for row in rows if row.ai_score is None)
    return rows, scored


@pytest.mark.asyncio
async def test_choice_with_content_markings_and_conflicting_selection(monkeypatch):
    stem, candidate = source_question("choice")
    question = (await recognize(monkeypatch, stem, candidate))["q1"]
    rows, scored = await grade_fixture(monkeypatch, reviewed_problem(question, "choice"), [
        "B", "B. 7", "b. 七", "选项：ｂ. SEVEN", "B. ７", "seven", "笔迹无法辨认\n(b)",
        "B and A", "b, a", "B. 6", "A. 7", "B. 77", "B seven", "B six",
        "B\n不相关涂写", "A. 6", "", "B. 7 A. 6", "B\nA stray line of notes.", "B a note",
    ], stdout="7\n")
    assert [row.ai_score for row in rows] == [10]*7 + [0, 0, None, None, None, 10, None, 10, 0, 0, 0, 10, 10]
    scored.assert_not_awaited()


@pytest.mark.asyncio
async def test_correct_option_is_bound_to_printed_text_before_reading_student_aliases(monkeypatch):
    stem, candidate = source_question("choice")
    stem = stem.replace("A. `6`", "A. `7.0`")
    candidate["choices"]["A"] = "7.0"
    question = (await recognize(monkeypatch, stem, candidate))["q1"]
    rows, scored = await grade_fixture(monkeypatch, reviewed_problem(question, "choice"), [
        "B", "A", "b. 七", "B. seven", "7", "7.0", "B. 7.0", "A. 7", "七", "B. 7.5",
    ], stdout="7\n")
    assert [row.ai_score for row in rows] == [10, 0, 10, 10, 10, 0, None, None, None, None]
    scored.assert_not_awaited()


@pytest.mark.asyncio
async def test_option_discussed_in_an_explanation_is_not_an_additional_selection(monkeypatch):
    stem, candidate = source_question("choice")
    question = (await recognize(monkeypatch, stem, candidate))["q1"]
    rows, scored = await grade_fixture(monkeypatch, reviewed_problem(question, "choice"), [
        "答案：B\nA 是错误选项",
        "A 是错误选项\n答案：B",
        "答案：B，A 是错误选项",
        "答案：B\n（A）是错误选项",
        "答案：B\n选项 A 不正确",
        "答案：B\nOption A is incorrect.",
        "答案：B\nA is not the correct answer (A).",
        "答案：B\n说明：先检查（A），它不是答案。",
        "答案：B\nA",
        "B 和 A",
        "B. 7\nA. 6",
    ], stdout="7\n")
    assert [row.ai_score for row in rows] == [10] * 8 + [0, 0, 0]
    scored.assert_not_awaited()


@pytest.mark.asyncio
async def test_numeric_option_labels_accept_each_number_notation(monkeypatch):
    stem, candidate = source_question("choice")
    stem = stem.replace("A. `6`", "1. `6`").replace("B. `7`", "2. `7`")
    candidate["choices"] = {"1": "6", "2": "7"}
    question = (await recognize(monkeypatch, stem, candidate))["q1"]
    rows, _ = await grade_fixture(monkeypatch, reviewed_problem(question, "choice"), [
        "2", "二", "TWO", "２. 7", "二. 七", "1", "one", "1. 七", "one and two",
    ], stdout="7\n")
    assert [row.ai_score for row in rows] == [10, 10, 10, 10, 10, 0, 0, None, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("subset_award", ["2 points", "full credit", "0 points"])
async def test_multiple_choice_applies_the_frozen_missing_option_award(monkeypatch, subset_award):
    program = PROGRAM + "print(8)\n"
    stem, candidate = source_question("choice", code=program)
    stem = stem.replace("What is printed by the given program?", "Which values appear in the output? Select all that apply.")
    stem = stem.replace("A. `6`", "A. `7.0`") + "\nC. `8`"
    candidate["choices"].update(A="7.0", C="8")
    questions = await recognize(monkeypatch, stem, candidate)
    definition = rubric_definition(questions["q1"], "choice").model_dump()
    definition["part_rubric"]["parts"][0]["steps"][0]["criterion"] = (
        f"All correct options: full credit; Any wrong option: 0 points; Partial selection: {subset_award}"
    )
    problem = await generate_problem(monkeypatch, questions, "choice", definition=RubricDefinition.model_validate(definition))
    rows, scored = await grade_fixture(monkeypatch, problem, [
        "BC", "b and c", "b, c", "Ｂ. 七\nｃ. Eight", "B", "C",
        "ABC", "A and B", "B\nA", "B. 8", "",
    ], stdout="7\n8\n", program=program)
    partial = 10 if subset_award == "full credit" else int(subset_award[0])
    assert [row.ai_score for row in rows] == [10]*4 + [partial, partial, 0, 0, 0, None, 0]
    scored.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("multi", [False, True])
async def test_explanation_compares_the_full_stdout_block(monkeypatch, multi):
    program = PROGRAM + "print(8)\n" if multi else PROGRAM
    stem, candidate = source_question("explanation", code=program)
    question = (await recognize(monkeypatch, stem, candidate))["q1"]
    correct = "7\n8" if multi else "7"
    wrong = "7\n9" if multi else "6"
    answers = [
        f"Output: {correct}\nMultiplication gives 6.",
        f"Output:\n{correct}\nMultiplication gives 6.",
        f"Multiplication gives 6.\n输出：\n{correct}",
        f"Output:\n```text\n{correct}\n```\nMultiplication gives 6.",
        f"Output:\n{wrong}\nExplanation: Multiplication gives 6.",
        f"Output:\n{correct}\n9\nMultiplication gives 6.",
        f"Output:\n{correct}\nundecidable note\nMultiplication gives 6.",
        f"Output: {correct}\nOutput: {wrong}\nMultiplication gives 6.",
        "Output:\nExplanation: Multiplication gives 6.",
    ]
    rows, scored = await grade_fixture(monkeypatch, reviewed_problem(question, "explanation"), answers,
                                       stdout=correct + "\n", program=program)
    assert [row.ai_score for row in rows] == [10, 10, 10, 10, 6, 6, None, None, None]
    assert scored.await_count == 6
    assert {step["item_id"]: step["score"] for step in rows[2].ai_steps} == {"trace": 6, "stdout": 4}


@pytest.mark.asyncio
async def test_two_blanks_keep_frozen_teacher_awards_and_never_shift_missing_values(monkeypatch):
    program = PROGRAM + "print(8)\n"
    stem, candidate = source_question("blank", code=program)
    stem = stem.replace("What is printed by the given program?", "依次填写两个空。第一空____，第二空____。")
    question = (await recognize(monkeypatch, stem, candidate))["q1"]
    question["criterion"] = "第一空4分；第二空6分。"
    definition = rubric_definition(question, "blank").model_dump()
    part = definition["part_rubric"]["parts"][0]
    with pytest.raises(ValueError, match="exactly 2 independent point steps"):
        generated_rubric({**question, "criterion": ""}, RubricDefinition.model_validate(definition))
    part["steps"] = [
        {"id": "blank1", "total_pts": 4, "criterion": "第一空正确", "source_quote": "第一空4分"},
        {"id": "blank2", "total_pts": 6, "criterion": "第二空正确", "source_quote": "第二空6分"},
    ]
    part["steps"].reverse()
    with pytest.raises(ValueError, match="must refer only to blank 1"):
        generated_rubric(question, RubricDefinition.model_validate(definition))
    part["steps"].reverse()
    criterion, draft = generated_rubric(question, RubricDefinition.model_validate(definition))
    problem = confirmed_problem(question, criterion, draft.model_dump(mode="json"))
    problem["ai_completion_provenance"] = {}
    rows, scored = await grade_fixture(monkeypatch, problem, [
        "7 9", "7\n9", "0 8", "7 8", "7\n8", "7", "8", "7 9 8", "__ 8", "",
    ], stdout="7\n8\n", program=program)
    assert [row.ai_score for row in rows] == [4, 4, 6, 10, 10, None, None, None, 6, 0]
    assert [(step["item_id"], step["score"]) for step in rows[0].ai_steps] == [("blank1", 4), ("blank2", 0)]
    scored.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_blank_keeps_a_multiline_output_as_one_answer(monkeypatch):
    program = PROGRAM + "print(8)\n"
    stem, candidate = source_question("blank", code=program)
    stem = stem.replace("What is printed by the given program?", "Write the exact output in one blank: ____.")
    question = (await recognize(monkeypatch, stem, candidate))["q1"]
    rows, _ = await grade_fixture(monkeypatch, reviewed_problem(question, "blank"),
                                   ["7\n8", "7 8", "7\n9"], stdout="7\n8\n", program=program)
    assert [row.ai_score for row in rows] == [10, 0, 0]


@pytest.mark.asyncio
async def test_teacher_multi_selection_policy_is_one_funded_step(monkeypatch):
    program = PROGRAM + "print(8, 9)\n"
    stem, candidate = source_question("choice", code=program)
    stem = stem.replace("What is printed by the given program?", "Select all printed values.") + "\nC. `8`"
    candidate["choices"]["C"] = "8"
    question = (await recognize(monkeypatch, stem, candidate))["q1"]
    question["criterion"] = "全部选对满分；错选0分；少选但没有错误选项得四分。"
    criterion, draft = generated_rubric(question, rubric_definition(question, "choice"))
    assert criterion == question["criterion"]
    step, = draft.plan.part_rubric.parts[0].steps
    assert step.code_output and step.total_pts == 10 and step.criterion == criterion
    problem = confirmed_problem(question, criterion, draft.model_dump(mode="json"))
    problem["ai_completion_provenance"] = {}
    rows, scored = await grade_fixture(monkeypatch, problem, ["B", "BC", "AB", ""],
                                      stdout="7\n8 9\n", program=program)
    assert [row.ai_score for row in rows] == [4, 10, 0, 0]
    scored.assert_not_awaited()


@pytest.mark.asyncio
async def test_objective_retry_reuses_recorded_stdout_and_preserves_accepted_results(monkeypatch):
    stem, candidate = source_question("choice")
    question = (await recognize(monkeypatch, stem, candidate))["q1"]
    owner, task, provider = task_with_problem(reviewed_problem(question, "choice"), ["B. 7", "B. 6"])
    registry = registry_for(SimpleNamespace(provider_id=provider.id, model="fixture"))
    dispatched = stub_oci(monkeypatch)
    first = start(owner, task)
    await grading_runs.process_run(run_id=first, worker_id="stdout-first", registry=registry)
    assert sorted((row.ai_score for row in grading_repository.list_results_for_run(first)), key=str) == [10, None]
    retry = start(owner, task, request_id="stdout-retry", retry_scope="failed_only")
    await grading_runs.process_run(run_id=retry, worker_id="stdout-retry", registry=registry)
    assert sorted((row.ai_score for row in grading_repository.list_results_for_run(retry)), key=str) == [10, None]
    dispatched.assert_awaited_once()


def test_annotated_selection_keeps_commas_in_the_selected_literal():
    choices = {"A": "6", "B": "7, 8", "C": "8"}
    from backend.services.code_output_answers import submitted_choices

    assert submitted_choices("B. 7, 8", choices) == {"B"}
    assert submitted_choices("B. 7, 8; A", choices) == {"A", "B"}
    assert submitted_choices("B annotation, A", choices) == {"A", "B"}
    assert submitted_choices("B和A", choices) == {"A", "B"}
    assert submitted_choices("答案是 b", choices) == {"B"}
    assert submitted_choices("正确选项为 B", choices) == {"B"}
    assert submitted_choices("The correct answer is B", choices) == {"B"}
    from backend.services.code_output_answers import CodeOutputUnavailable
    with pytest.raises(CodeOutputUnavailable):
        submitted_choices("B or A", choices)
    assert submitted_choices("A stray note.\n`B`", choices) == {"B"}


def test_explanation_labels_inside_an_explicit_output_block_are_literal():
    from backend.services.code_output_answers import explanation_stdout

    result, evidence = explanation_stdout(
        "Output:\n```text\nOutput: 7\nExplanation: 8\n```\nExplanation: here is the trace.",
        "Output: 7\nExplanation: 8\n",
    )
    assert result == "Output: 7\nExplanation: 8\n"
    assert evidence == "student:3"
    assert explanation_stdout("Output: Explanation: 8", "Explanation: 8\n")[0] == "Explanation: 8"


def test_number_spellings_preserve_scalar_option_identity():
    from backend.services.code_output_answers import submitted_choices

    choices = {"A": "22", "B": "-27", "C": "123"}
    assert submitted_choices("A. twenty-two", choices) == {"A"}
    assert submitted_choices("B. negative twenty seven", choices) == {"B"}
    assert submitted_choices("C. one hundred and twenty-three", choices) == {"C"}
    assert submitted_choices("C. 一百二十三", choices) == {"C"}


@pytest.mark.asyncio
async def test_four_blanks_follow_order_and_ignore_answer_line_wrapping(monkeypatch):
    program = "input()\nprint('1 2')\nprint('3 4')\n"
    stem, candidate = source_question("blank", code=program)
    stem = stem.replace("What is printed by the given program?", "Fill in four blanks in order: ___ ___ ___ ___.")
    questions = await recognize(monkeypatch, stem, candidate)
    definition = rubric_definition(questions["q1"], "blank").model_dump()
    definition["part_rubric"]["parts"][0]["steps"] = [
        {"id": f"blank{i}", "criterion": f"Blank {i}", "total_pts": i} for i in range(1, 5)
    ]
    problem = await generate_problem(monkeypatch, questions, "blank", definition=RubricDefinition.model_validate(definition))
    rows, _ = await grade_fixture(monkeypatch, problem, ["1 2 3 4", "1 x\n3 4", "1 3 4"],
                                   stdout="1 2\n3 4\n", program=program)
    assert [row.ai_score for row in rows] == [10, 8, None]
    assert [(step["item_id"], step["score"]) for step in rows[1].ai_steps] == [
        ("blank1", 1), ("blank2", 0), ("blank3", 3), ("blank4", 4),
    ]
