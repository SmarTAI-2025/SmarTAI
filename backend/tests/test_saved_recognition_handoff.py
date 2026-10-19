"""Real saved OCR evidence -> durable organization -> original review DTOs."""
import io
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from backend.api import task_preparation
from backend.db import file_repository, workflow_repository
from backend.domain.errors import RecognitionError
from backend.services import task_facade
from backend.services.recognition_recovery import saved_recognition
from backend.services.task_execution import stop_task_run
from backend.storage import get_storage
from backend.tests.test_question_preparation_recovery import _RecoveryRegistry, _claim, _seed_task
from backend.tests.test_recognition_recheck import Engine, source_file


async def save_pages(owner, task, stored, data, *, purpose="problems", final=True, text=None):
    from backend.agents.recognition_agent import RecognitionAgent, RecognitionReadRequestV1
    from backend.recognition.models import RecognitionPolicyV1, RecognitionSourceRefV1
    from backend.recognition.runtime import RecognitionCapacity
    from backend.services.recognition_artifacts import RecognitionArtifactBindingV1, RecognitionArtifactStore
    from backend.services.recognition_calls import RecognitionCallService
    from backend.services.recognition_results import RecognitionResultService
    store = RecognitionArtifactStore(get_storage())
    binding = RecognitionArtifactBindingV1(link="assignment", business_id=task)
    calls = RecognitionCallService(store=store, binding=binding, authorized_owner_id=owner)
    results = RecognitionResultService(store=store, binding=binding, authorized_owner_id=owner)
    engine = Engine(initial=text or [f"{i}. Saved literal question {i}." for i in range(1, 8)] + [RuntimeError("failed page")])
    engine.capabilities = engine.capabilities.model_copy(update={"fingerprint": hashlib.sha256(str(text).encode()).hexdigest()})
    source = RecognitionSourceRefV1(owner_id=owner, scope="assignment_source", business_id=task,
        stored_file_id=stored.id, content_type=stored.content_type, input_sha256=stored.sha256)
    request = RecognitionReadRequestV1(source=source, purpose=purpose,
        policy=RecognitionPolicyV1(force_visual=True, enable_repair=False))
    agent = RecognitionAgent(engine, capacity=RecognitionCapacity(), call_service=calls)
    assembly = await agent.recognize(request, data, authorized_owner_id=owner, prompt_version="faithful-reader-v1")
    if final:
        await results.record(assembly)
    return assembly


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [False, True])
@pytest.mark.parametrize("legacy_empty_handoff", [False, True])
async def test_question_saved_pages_are_organized_without_rereading_or_generating(monkeypatch, stopped, legacy_empty_handoff):
    owner, task = _seed_task()
    registry = _RecoveryRegistry()
    data, mime = source_file(pdf=True, pages=8)
    draft = await task_preparation.preflight_problem_source(task_id=task,
        file=UploadFile(io.BytesIO(data), filename="eight-pages.pdf", headers=Headers({"content-type": mime})),
        inline_text=None, library_material_id=None, stored_file_id=None, structure_mode="organized", role="problem",
        extraction_hint="", save_to_library=False, recognition_provider_id="test-provider",
        current=SimpleNamespace(id=owner), registry=registry)
    queued = await task_preparation._start_question_preparation(task_id=task,
        request=task_preparation.StartQuestionPreparationRequest(source_tokens=[draft["source_token"]],
            expected_workflow_revision=0, recognition_provider_id="test-provider"),
        current=SimpleNamespace(id=owner), registry=registry, background_tasks=None, allow_prepared_source_reuse=False)
    origin = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    stored = file_repository.get_file(file_id=origin.payload["source_refs"][0]["stored_file_id"], owner_id=owner)
    source_example = "Example input: 10 20; expected output: 30."
    source_pairs = "1 → 2\n3 → 4"
    source_json_case = {"input": "7 8", "expected_output": "15"}
    cases = [
        {"input": "10 20", "expected_output": "30", "source_excerpt": source_example},
        source_json_case,  # No source or excerpt, but an exact case in uploaded text.
        {"input": "10 20", "expected_output": "30"},  # Plain labelled original; omitted source and excerpt.
        {"input": "2 3", "expected_output": "5"},  # Created; omitted provenance.
        {"input": "4 5", "expected_output": "9", "source": "llm_generated"},
        {"input": "10 20", "expected_output": "3", "source": "teacher", "source_excerpt": source_example},
        {"input": "1", "expected_output": "4", "source": "llm_generated", "source_excerpt": source_pairs},
        {"input": "1", "expected_output": "2", "source_excerpt": source_pairs},
        {"input": "3", "expected_output": "4"},
    ]
    assembly = await save_pages(owner, task, stored, data, final=not stopped, text=[
        "1. Saved literal question 1. (a) First part. (b) Second part. Rubric: (a) 3 points\n"
        + source_example + "\n" + json.dumps(source_json_case) + "\n" + source_pairs,
        *[f"{i}. Saved literal question {i}." for i in range(2, 8)], RuntimeError("failed page")])
    assert assembly.document.coverage.failed_pages == [8]
    if stopped:
        stop_task_run(task_id=task, owner_id=owner, job_id=origin.id, expected_revision=queued["workflow_revision"])
    else:
        task_facade._fail_operation(task, owner, origin.id, origin.attempt, "question_source_incomplete")
    provider_calls = []
    async def organize(messages):
        provider_calls.append(messages)
        assert "Saved literal question 1" in messages[-1].content
        assert "Saved literal question 7" in messages[-1].content
        return SimpleNamespace(content=json.dumps({"problems": [dict(q_id=f"q{i}", number=str(i), type="short",
            stem=f"Saved literal question {i}." + (" (a) First part. (b) Second part." if i == 1 else ""),
            criterion="(a) 3 points" if i == 1 else "", test_cases=cases if i == 1 else None,
            source_incomplete=(i == 7)) for i in range(1, 8)]}))
    registry.provider = SimpleNamespace(provider_id="test-provider", supports_vision=False, ainvoke=organize)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    forbidden = AsyncMock(side_effect=AssertionError("must not reread or generate"))
    monkeypatch.setattr(task_preparation, "_recognize_saved_question_sources", forbidden)
    monkeypatch.setattr(task_preparation, "prepare_question_packages", forbidden)
    revision = workflow_repository.get_workflow(task, owner_id=owner).workflow_revision
    if legacy_empty_handoff:
        from backend.services import question_preparation_manual
        with monkeypatch.context() as legacy:
            legacy.setattr(question_preparation_manual, "saved_question_sources", lambda _: None)
            empty = await task_preparation.manually_complete_question_preparation(task, origin.id,
                task_preparation.ManualQuestionPreparationRequest(expected_workflow_revision=revision), current=SimpleNamespace(id=owner))
            assert empty["status"] == "ready_for_review"
            revision = empty["workflow_revision"]
    response = await task_preparation.manually_complete_question_preparation(task, origin.id,
        task_preparation.ManualQuestionPreparationRequest(expected_workflow_revision=revision), current=SimpleNamespace(id=owner))
    assert isinstance(response, dict), getattr(response, "body", response)
    assert response["status"] == "started"
    assert task_facade.task_state(task_id=task, owner_id=owner)["recovering_saved_text"] is True
    await task_preparation.run_durable_question_preparation(_claim(owner, response["job_id"], "saved-text"))
    operation = workflow_repository.get_operation(response["job_id"], owner_id=owner)
    assert operation.status == "done", (operation.error_code, operation.checkpoint)
    assert len(provider_calls) == 1 and forbidden.await_count == 0
    for read in (task_facade.get_task(task_id=task, owner_id=owner), await task_facade.async_task_state(task_id=task, owner_id=owner)):
        assert read["status"] == "problems_ready" and read["problem_count"] == 7
    problems = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
    assert [case["source"] for case in problems["q1"]["test_cases"]] == [
        "teacher", "teacher", "teacher", "llm_generated", "llm_generated", "llm_generated",
        "llm_generated", "teacher", "teacher"]
    assert [case["input"] for case in problems["q1"]["test_cases"]] == [case["input"] for case in cases]
    assert all("source_excerpt" not in case for case in problems["q1"]["test_cases"])
    from sqlalchemy import select
    from backend.db.models import AssignmentQuestionRecord
    from backend.db.session import session_scope
    with session_scope() as session:
        stored_question = session.scalar(select(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == task, AssignmentQuestionRecord.q_id == "q1"))
        assert stored_question.test_cases == problems["q1"]["test_cases"]
    assert task_facade.task_state(task_id=task, owner_id=owner)["recovering_saved_text"] is False
    assert problems["q1"]["stem"] == "Saved literal question 1. (a) First part. (b) Second part."
    assert problems["q1"]["criterion"] == "(a) 3 points" and not problems["q1"].get("reference_answer")
    assert not problems["q1"]["rubric_point_summary"]["is_valid"]
    assert not any(issue["code"] == "recognition_partial" for issue in problems["q1"]["preparation_issues"])
    assert any(issue["code"] == "recognition_partial" for issue in problems["q7"]["preparation_issues"])
    assert task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after_save_failure", [False, True])
async def test_requested_missing_questions_remain_editable_after_organization_and_retry(monkeypatch, retry_after_save_failure):
    from backend.db import assignment_repository
    from backend.domain.errors import ValidationError

    owner, task = _seed_task()
    registry = _RecoveryRegistry()
    data, mime = source_file(pdf=True, pages=1)
    numbers = [str(i) for i in range(1, 8)]
    draft = await task_preparation.preflight_problem_source(task_id=task,
        file=UploadFile(io.BytesIO(data), filename="questions.pdf", headers=Headers({"content-type": mime})),
        inline_text=None, library_material_id=None, stored_file_id=None, structure_mode="organized", role="problem",
        extraction_hint="", recognition_options=json.dumps({"targets": numbers}), save_to_library=False,
        recognition_provider_id="test-provider", current=SimpleNamespace(id=owner), registry=registry)
    queued = await task_preparation._start_question_preparation(task_id=task,
        request=task_preparation.StartQuestionPreparationRequest(source_tokens=[draft["source_token"]],
            expected_workflow_revision=0, recognition_provider_id="test-provider"),
        current=SimpleNamespace(id=owner), registry=registry, background_tasks=None, allow_prepared_source_reuse=False)
    origin = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    stored = file_repository.get_file(file_id=origin.payload["source_refs"][0]["stored_file_id"], owner_id=owner)
    literal = "\n".join(f"{i}. Saved literal question {i}." for i in range(1, 4))
    await save_pages(owner, task, stored, data, text=[literal])
    task_facade._fail_operation(task, owner, origin.id, origin.attempt, "question_source_incomplete")
    organize = AsyncMock(return_value=SimpleNamespace(content=json.dumps({"problems": [
        dict(q_id=f"q{i}", number=str(i), type="short", stem=f"Saved literal question {i}.", criterion="")
        for i in range(1, 4)]})))
    registry.provider = SimpleNamespace(provider_id="test-provider", supports_vision=False, ainvoke=organize)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    forbidden = AsyncMock(side_effect=AssertionError("must not reread or generate"))
    monkeypatch.setattr(task_preparation, "_recognize_saved_question_sources", forbidden)
    monkeypatch.setattr(task_preparation, "prepare_question_packages", forbidden)
    revision = workflow_repository.get_workflow(task, owner_id=owner).workflow_revision
    response = await task_preparation.manually_complete_question_preparation(task, origin.id,
        task_preparation.ManualQuestionPreparationRequest(expected_workflow_revision=revision), current=SimpleNamespace(id=owner))
    assert response["status"] == "started"
    if retry_after_save_failure:
        # Keep the three extracted rows, but fail before the final package can be saved.
        with monkeypatch.context() as failed_save:
            def fail_save(**_):
                raise RuntimeError("temporary package save failure")
            failed_save.setattr(task_preparation, "save_final_question_packages_artifact", fail_save)
            await task_preparation.run_durable_question_preparation(_claim(owner, response["job_id"], "failed-save"))
        failed = workflow_repository.get_operation(response["job_id"], owner_id=owner)
        assert failed.status == "error" and failed.checkpoint["questions_extracted_artifact_id"]
        assert not assignment_repository.list_questions(task, teacher_id=owner)
        revision = workflow_repository.get_workflow(task, owner_id=owner).workflow_revision
        response = await task_preparation.retry_question_preparation(task_id=task, job_id=failed.id,
            request=task_preparation.RetryQuestionPreparationRequest(expected_workflow_revision=revision),
            background_tasks=None, current=SimpleNamespace(id=owner), registry=registry)
        assert response["status"] == "started"
    await task_preparation.run_durable_question_preparation(_claim(owner, response["job_id"], "complete-seven"))
    operation = workflow_repository.get_operation(response["job_id"], owner_id=owner)
    assert operation.status == "done", (operation.error_code, operation.checkpoint)
    assert organize.await_count == 1 and forbidden.await_count == 0
    assert len(assignment_repository.list_questions(task, teacher_id=owner)) == 7
    for read in (task_facade.get_task(task_id=task, owner_id=owner), await task_facade.async_task_state(task_id=task, owner_id=owner)):
        assert read["status"] == "problems_ready" and read["problem_count"] == 7
    problems = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
    assert [row["number"] for row in problems.values()] == numbers
    assert [problems[f"q{i}"]["stem"] for i in range(1, 4)] == [f"Saved literal question {i}." for i in range(1, 4)]
    for i in range(4, 8):
        row = problems[f"q{i}"]
        assert not row["stem"] and not row["criterion"] and not row.get("reference_answer")
        assert row["question_structure"]["major_number"] == str(i)
        assert row["review_status"] == "needs_review"
        marker = next(issue for issue in row["preparation_issues"] if issue["code"] == "manual_completion_required")
        assert marker["details"]["required_fields"] == ["stem", "criterion"]
    assert "question_manual_completion_required" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
    with pytest.raises(ValidationError):
        task_facade.update_problem(task_id=task, owner_id=owner, q_id="q4", patch={"review_status": "confirmed"})
    task_facade.update_problem(task_id=task, owner_id=owner, q_id="q4",
        patch={"stem": "Teacher-filled question 4", "criterion": "Award 10 points for the solution."})
    task_facade.update_problem(task_id=task, owner_id=owner, q_id="q4", patch={"review_status": "confirmed"})
    assert task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]["q4"]["review_status"] == "confirmed"


@pytest.mark.asyncio
async def test_saved_evidence_keeps_distinct_fragments_and_rejects_other_original(monkeypatch):
    owner, task = _seed_task()
    data, mime = source_file(pdf=True, pages=1)
    stored = file_repository.save_file(storage=get_storage(), owner_id=owner, assignment_id=task,
        kind="problem_source", original_name="source.pdf", content=data, content_type=mime)
    await save_pages(owner, task, stored, data, final=False, text=["1. First saved fragment"])
    # Different artifact identity simulates a separately authorized retry.
    # Use another request's saved final evidence rather than relying on a success cache.
    await save_pages(owner, task, stored, data, text=["1. Later saved fragment"])
    recovered = saved_recognition(owner_id=owner, task_id=task, stored_file_id=stored.id,
        purpose="problems", before=time.time())
    assert "First saved fragment" in recovered.text and "Later saved fragment" in recovered.text
    other = file_repository.save_file(storage=get_storage(), owner_id=owner, assignment_id=task,
        kind="problem_source", original_name="other.pdf", content=data + b"\n", content_type=mime)
    with pytest.raises(RecognitionError):
        saved_recognition(owner_id=owner, task_id=task, stored_file_id=other.id, purpose="problems",
            before=time.time(), artifact_ids=recovered.artifact_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [False, True])
async def test_submission_saved_text_survives_handoff_and_repeated_task_reads(monkeypatch, stopped):
    from backend.db import assignment_repository
    from backend.services.submission_manual_review import continue_manually
    owner, task = _seed_task(with_question=True)
    assignment_repository.add_question(task, teacher_id=owner, q_id="q2", order_index=1,
        type="short", stem="Second", criterion="Award 10 points.", max_score=10)
    registry = _RecoveryRegistry()
    data, mime = source_file(pdf=True, pages=8)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner, filename="S1.pdf",
        content=data, content_type=mime, registry=registry)
    origin = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    stored = file_repository.get_file(file_id=origin.payload["input_file_id"], owner_id=owner)
    await save_pages(owner, task, stored, data, purpose="submissions", final=not stopped)
    if stopped:
        stop_task_run(task_id=task, owner_id=owner, job_id=origin.id, expected_revision=queued["workflow_revision"])
    else:
        task_facade._fail_operation(task, owner, origin.id, origin.attempt, "question_source_incomplete")
    calls = []
    async def organize(messages):
        calls.append(messages)
        assert "Saved literal question 1" in messages[-1].content
        return SimpleNamespace(content=json.dumps(dict(stu_id="S1", stu_name="Student One", stu_ans=[
            dict(q_id="q1", number="1", type="short", content="Saved wrong answer -2", flag=[]),
            dict(q_id="q2", number="2", type="short", content="", flag=[])])))
    registry.provider = SimpleNamespace(provider_id="test-provider", supports_vision=False, ainvoke=organize)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    forbidden = AsyncMock(side_effect=AssertionError("must not reread the original"))
    monkeypatch.setattr(task_facade, "read_question_source", forbidden)
    response = await continue_manually(task_id=task, owner_id=owner, job_id=origin.id,
        expected_revision=workflow_repository.get_workflow(task, owner_id=owner).workflow_revision)
    assert response["status"] == "started"
    assert (await task_facade.async_task_state(task_id=task, owner_id=owner))["recovering_saved_text"] is True
    await task_facade.run_durable_submission_recognition(_claim(owner, response["job_id"], "saved-answers"))
    operation = workflow_repository.get_operation(response["job_id"], owner_id=owner)
    assert operation.status == "done", (operation.error_code, operation.checkpoint)
    assert len(calls) == 1 and forbidden.await_count == 0
    for _ in range(2):
        for read in (task_facade.get_task(task_id=task, owner_id=owner), await task_facade.async_task_state(task_id=task, owner_id=owner),
                     task_facade.list_tasks(owner_id=owner)[task]):
            assert read["status"] == "submissions_ready" and not read["error"]
    student = next(iter(task_facade.get_task(task_id=task, owner_id=owner)["student_data"].values()))
    answers = {answer["q_id"]: answer for answer in student["stu_ans"]}
    assert answers["q1"]["content"] == "Saved wrong answer -2"
    assert not task_facade._answer_recognition_failures(answers["q1"]["flag"])
    assert "recognition_manual_pending" in answers["q2"]["flag"]


@pytest.mark.asyncio
async def test_completed_organization_chunks_are_retained_on_stop_and_skipped_on_resume():
    import asyncio
    from copy import deepcopy
    from backend.models import QuestionScorePolicy
    from backend.progress.tracker import ProgressReporter
    from backend.services.question_preparation_manual import prepare_saved_question_packages
    text = "1. First problem. " + "a" * 70 + "\n\n2. Second problem. " + "b" * 70
    draft = SimpleNamespace(role="problem", extraction_hint="")
    calls, saved, submitted = [], [], []
    async def invoke(messages):
        calls.append(messages[-1].content)
        if len(calls) == 2:
            raise asyncio.CancelledError()
        number = "1" if "First problem" in messages[-1].content else "2"
        return SimpleNamespace(content=json.dumps({"problems": [dict(q_id="q1", number=number,
            type="short", stem="First problem" if number == "1" else "Second problem", criterion="", source_incomplete=False)]}))
    async def checkpoint(packages, *, completed_chunks):
        saved.append((deepcopy(packages), completed_chunks))
    async def submitting():
        submitted.append(True)
    async def failed(*_):
        raise AssertionError("Cancellation must retain the in-flight fence")
    options = dict(provider=SimpleNamespace(provider_id="synthetic", ainvoke=invoke),
        reporter=ProgressReporter("saved-chunks"), score_policy=QuestionScorePolicy(), ocr_only=False,
        on_started=submitting, on_extracted=checkpoint, on_failed=failed,
        chunk_settings={"chunk_chars": 110, "chunk_overlap": 0})
    with pytest.raises(asyncio.CancelledError):
        await prepare_saved_question_packages([(draft, text, {})], recovered=None, **options)
    assert len(saved) == 1 and saved[0][0]["q1"]["stem"] == "First problem"
    # An explicitly resumed worker consumes the last verified chunk checkpoint.
    recovered, completed = saved[0]
    result = await prepare_saved_question_packages([(draft, text, {})], recovered=recovered,
        completed_chunks=completed, **options)
    assert len(calls) == 3 and calls[0] != calls[2] and calls[1] == calls[2]
    assert [item["stem"] for item in result.values()] == ["First problem", "Second problem"]


@pytest.mark.parametrize("case,excerpt,text,teacher", [
    ({"function_name": "add", "function_args": [2, 3], "expected_return": "5"},
     "add([2, 3]) returns 5", "Example: add([2, 3]) returns 5", True),
    ({"input": "2 3", "expected_output": "5"}, "input: 2 3; output: 5", "Question only", False),
    ({"input": "30", "expected_output": "10 20"}, "input: 10 20; output: 30",
     "input: 10 20; output: 30", False),
    ({"input": "", "expected_output": "0"}, "", '{"input": "", "expected_output": "0"}', True),
    ({"input": "2 3", "expected_output": "5"}, "", "题目2有3分，作业有5题", False),
    ({"input": "1", "expected_output": "4"}, "", "Input: 1; Output: 4 5", False),
    ({"input": "A", "expected_output": "b"}, "", "Input: a; Output: b", False),
    ({"input": "A", "expected_output": "b"}, "", "a → b", False),
    ({"input": "1 2", "expected_output": "3"}, "", "输入：\n1\n2\n输出：\n3", True),
    ({"function_name": "Add", "function_args": [2, 3], "expected_return": "5"},
     "", "add([2, 3]) returns 5", False),
])
def test_saved_test_case_evidence(case, excerpt, text, teacher):
    from backend.agents.ingest_agent import _saved_test_case_supported
    assert _saved_test_case_supported(case, excerpt, text) is teacher


@pytest.mark.parametrize("with_excerpt", [False, True])
@pytest.mark.parametrize("source,first,second", [
    ("1 → 2\n3 → 4", {"input": "1", "expected_output": "2"}, {"input": "3", "expected_output": "4"}),
    ("input: 1; output: 2\ninput: 3; output: 4",
     {"input": "1", "expected_output": "2"}, {"input": "3", "expected_output": "4"}),
    ('[{"input": "1", "expected_output": "2"}, {"input": "3", "expected_output": "4"}]',
     {"input": "1", "expected_output": "2"}, {"input": "3", "expected_output": "4"}),
    ("f([1]) returns 2\nf([3]) returns 4",
     {"function_name": "f", "function_args": [1], "expected_return": "2"},
     {"function_name": "f", "function_args": [3], "expected_return": "4"}),
])
def test_saved_test_case_requires_one_original_pair(source, first, second, with_excerpt):
    from backend.agents.ingest_agent import _saved_test_case_supported
    excerpt = source if with_excerpt else ""
    for original in (first, second):
        assert _saved_test_case_supported(original, excerpt, source)
    output_field = "expected_return" if "function_name" in first else "expected_output"
    for inputs, outputs in ((first, second), (second, first)):
        mixed = {**inputs, output_field: outputs[output_field]}
        assert not _saved_test_case_supported(mixed, excerpt, source)


@pytest.mark.asyncio
async def test_saved_organization_respects_targets_and_does_not_invent_missing_material():
    from backend.models import QuestionScorePolicy
    from backend.progress.tracker import ProgressReporter
    from backend.services.question_preparation_manual import prepare_saved_question_packages
    response = {"problems": [dict(q_id=f"q{i}", number=str(i), type="short",
        stem=f"Saved question {i}", criterion="Invented rubric", reference_answer="Invented solution") for i in (1, 2)]}
    provider = SimpleNamespace(provider_id="synthetic", ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(response))))
    source = (SimpleNamespace(role="problem", extraction_hint=""), "Saved question 1\nSaved question 2",
              {"recognition_options": {"targets": ["1", "3"]}})
    result = await prepare_saved_question_packages([source], provider=provider, reporter=ProgressReporter("scope"),
        score_policy=QuestionScorePolicy(), ocr_only=False, recovered=None,
        on_started=AsyncMock(), on_extracted=AsyncMock(), on_failed=AsyncMock())
    assert [row["number"] for row in result.values()] == ["1", "3"]
    assert result["q1"]["criterion"] == result["q1"]["reference_answer"] == ""
    assert result["q2"]["stem"] == "" and result["q2"]["type"] == "other"
