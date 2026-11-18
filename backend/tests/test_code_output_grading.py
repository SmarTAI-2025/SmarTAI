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
    if form == "explanation":
        steps = [
            {"id": "trace", "criterion": "Trace the multiplication before the addition", "total_pts": 6},
            {"id": "stdout", "criterion": "Report the actual printed output", "total_pts": 4, "code_output": True},
        ]
    else:
        steps = [{"id": "stdout", "criterion": "Identify the actual printed output", "total_pts": 10}]
    return RubricDefinition(part_rubric={
        "id": "q1", "label": "1", "stem": problem["stem"], "total_pts": 10,
        "parts": [{"id": "p1", "total_pts": 10, "steps": steps, "alternative": {"deduct_per_error": 1}}],
    })


def confirmed_problem(problem, criterion, draft):
    return {**problem, "criterion": criterion, "rubric_draft": draft,
            "ai_completion_provenance": {"criterion": {"source_kind": "ai_generated"}},
            "review_status": "confirmed", "max_score_review_status": "confirmed"}


def reviewed_problem(problem, form):
    criterion, draft = generated_rubric(problem, rubric_definition(problem, form), language="en")
    return confirmed_problem(problem, criterion, draft.model_dump(mode="json"))


async def generate_problem(monkeypatch, questions, form):
    targets = requested_major_question_materials(questions)
    target, = targets
    assert target["target"] == "criterion"
    question = questions["q1"]
    reply = {"candidates": [{**target, "rubric_definition":
                           rubric_definition(question, form).model_dump(mode="json", exclude_unset=True)}]}
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


def stub_oci(monkeypatch, *, stdout="7\n"):
    monkeypatch.setattr(code_output_grading.settings, "program_runner_mode", "oci")
    monkeypatch.setattr(code_output_grading.settings, "runtime_environment", "production")

    async def execute(code, stdin, **limits):
        assert code == PROGRAM
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
        # Both verdicts are deliberately wrong. The runner owns this row.
        result = output_model.model_validate({
            "confidence": 1, "comment": "Evaluate the multiplication trace.",
            "part_decisions": [{
                "part_id": "p1", "target_quote": "part:p1",
                "student_quote": "student:2" if reasoning else "student:1",
                "relevance": "partial" if reasoning else "incorrect",
                "method": "reference", "progress": "The trace was checked.",
                "inferences": [
                    {"step_ids": ["trace"], "evidence": "student:2" if reasoning else "",
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
    ("blank", ["`7`", "6"], [10, 0]),
    ("explanation", ["Output: 7\nMultiplication gives 6.", "Output: 6\nMultiplication gives 6.", "Output: 7"],
     [10, 6, 4]),
])
async def test_question_preparation_to_saved_grades(monkeypatch, form, answers, expected):
    stem, candidate = source_question(form)
    questions = await recognize(monkeypatch, stem, candidate)
    extracted = questions["q1"]
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
    assert sum(step["score"] for step in rows[0].ai_steps) == 10
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
