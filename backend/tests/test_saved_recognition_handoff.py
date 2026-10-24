"""Structured drafts restore to the original matrix; historical AI recovery is retired."""
import io
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from backend.api import task_preparation
from backend.db import file_repository, workflow_repository
from backend.services import task_facade
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
@pytest.mark.parametrize("with_targets", [False, True])
async def test_raw_only_questions_enter_original_matrix_without_ai_or_source_loss(monkeypatch, stopped, with_targets):
    owner, task = _seed_task()
    registry = _RecoveryRegistry()
    data, mime = source_file(pdf=True, pages=8)
    draft = await task_preparation.preflight_problem_source(task_id=task,
        file=UploadFile(io.BytesIO(data), filename="eight-pages.pdf", headers=Headers({"content-type": mime})),
        inline_text=None, library_material_id=None, stored_file_id=None, structure_mode="organized", role="problem",
        extraction_hint="", recognition_options=json.dumps({"targets": [str(i) for i in range(1, 8)]}) if with_targets else None,
        save_to_library=False, recognition_provider_id="test-provider", current=SimpleNamespace(id=owner), registry=registry)
    queued = await task_preparation._start_question_preparation(task_id=task,
        request=task_preparation.StartQuestionPreparationRequest(source_tokens=[draft["source_token"]],
            expected_workflow_revision=0, recognition_provider_id="test-provider"),
        current=SimpleNamespace(id=owner), registry=registry, background_tasks=None, allow_prepared_source_reuse=False)
    origin = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    stored = file_repository.get_file(file_id=origin.payload["source_refs"][0]["stored_file_id"], owner_id=owner)
    assembly = await save_pages(owner, task, stored, data, final=not stopped)
    assert assembly.document.coverage.failed_pages == [8]
    evidence = {item.id for item in file_repository.list_files(owner_id=owner, assignment_id=task)}
    if stopped:
        stop_task_run(task_id=task, owner_id=owner, job_id=origin.id, expected_revision=queued["workflow_revision"])
    else:
        task_facade._fail_operation(task, owner, origin.id, origin.attempt, "question_source_incomplete")
    forbidden = AsyncMock(side_effect=AssertionError("manual review cannot read sources or call a provider"))
    monkeypatch.setattr(task_preparation, "_recognize_saved_question_sources", forbidden)
    monkeypatch.setattr(task_facade, "_registry_for_owner", forbidden)
    revision = workflow_repository.get_workflow(task, owner_id=owner).workflow_revision
    response = await task_preparation.manually_complete_question_preparation(task, origin.id,
        task_preparation.ManualQuestionPreparationRequest(expected_workflow_revision=revision), current=SimpleNamespace(id=owner))
    assert response["status"] == "ready_for_review"
    rows = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
    if with_targets:
        assert [row["number"] for row in rows.values()] == [str(i) for i in range(1, 8)]
        assert all(row["stem"] == "" and row["type"] == "" for row in rows.values())
    else:
        assert rows == {} and response["first_question_id"] is None
        # Reopening an empty matrix is idempotent; raw text never launches AI.
        repeated = await task_preparation.manually_complete_question_preparation(task, origin.id,
            task_preparation.ManualQuestionPreparationRequest(expected_workflow_revision=response["workflow_revision"]), current=SimpleNamespace(id=owner))
        assert repeated == response
    assert forbidden.call_count == 0
    assert evidence <= {item.id for item in file_repository.list_files(owner_id=owner, assignment_id=task)}
    retained = file_repository.get_file(file_id=stored.id, owner_id=owner)
    assert retained.availability_status == "available"
    with get_storage().open(retained.storage_key) as original:
        assert original.read() == data
    assert workflow_repository.get_operation(origin.id, owner_id=owner).status == "error"



@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [False, True])
@pytest.mark.parametrize("target_source", ["options", "hint"])
async def test_structured_partial_questions_restore_known_missing_rows_into_existing_editor(monkeypatch, stopped, target_source):
    from backend.agents.incremental_question_preparation import IncrementalQuestionPreparation, _rows
    from backend.db import assignment_repository
    from backend.domain.errors import ValidationError
    from backend.progress.tracker import ProgressReporter

    owner, task = _seed_task()
    registry = _RecoveryRegistry()
    numbers = ["1.1.5", "1.1.7", "1.1.20", "1.1.29", "1.1.31", "1.2.3", "1.2.16"]
    recognized = [numbers[0], numbers[1], numbers[5]]
    literal = "\n".join(f"{number}. Saved literal question {number}." for number in recognized)
    draft = await task_preparation.preflight_problem_source(task_id=task, file=None,
        inline_text=literal, library_material_id=None, stored_file_id=None, structure_mode="organized", role="problem",
        extraction_hint="题号: " + ", ".join(numbers) if target_source == "hint" else "",
        recognition_options=json.dumps({"targets": numbers}) if target_source == "options" else None, save_to_library=False,
        recognition_provider_id="test-provider", current=SimpleNamespace(id=owner), registry=registry)
    queued = await task_preparation._start_question_preparation(task_id=task,
        request=task_preparation.StartQuestionPreparationRequest(source_tokens=[draft["source_token"]],
            expected_workflow_revision=0, recognition_provider_id="test-provider"),
        current=SimpleNamespace(id=owner), registry=registry, background_tasks=None, allow_prepared_source_reuse=False)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    operation = _claim(owner, queued["job_id"], "persist-structured")
    inputs = task_preparation._rehydrate_question_preparation_inputs(operation)
    pipeline = IncrementalQuestionPreparation(operation=operation, sources=inputs["sources"],
        provider=registry.provider, provider_id="test-provider", reporter=ProgressReporter(operation.operation_id),
        score_policy=inputs["score_policy"], read_source=AsyncMock(side_effect=AssertionError("already structured")))
    rows = _rows([dict(number=number, type="concept", stem=f"Saved literal question {number}.",
        criterion="Teacher rubric: award 10 points." if index == 0 else "", max_score=10,
        reference_answer="Teacher answer" if index == 0 else "") for index, number in enumerate(recognized)])
    await pipeline.finish("extract:" + draft["source_token"] + ":0", rows)
    if stopped:
        stop_task_run(task_id=task, owner_id=owner, job_id=operation.operation_id,
            expected_revision=workflow_repository.get_workflow(task, owner_id=owner).workflow_revision)
    else:
        task_facade._fail_operation(task, owner, operation.operation_id, operation.attempt,
            "question_targets_incomplete", expected_lease_token=operation.lease_token)
    forbidden = AsyncMock(side_effect=AssertionError("manual review cannot invoke AI"))
    monkeypatch.setattr(task_facade, "_registry_for_owner", forbidden)
    revision = workflow_repository.get_workflow(task, owner_id=owner).workflow_revision
    response = await task_preparation.manually_complete_question_preparation(task, operation.operation_id,
        task_preparation.ManualQuestionPreparationRequest(expected_workflow_revision=revision), current=SimpleNamespace(id=owner))
    assert response["status"] == "ready_for_review" and forbidden.call_count == 0
    problems = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
    assert len(assignment_repository.list_questions(task, teacher_id=owner)) == 7
    assert {row["number"] for row in problems.values()} == set(numbers)
    assert [problems[f"q{i}"]["stem"] for i in range(1, 4)] == [f"Saved literal question {number}." for number in recognized]
    assert problems["q1"]["criterion"] == "Teacher rubric: award 10 points."
    assert problems["q1"]["reference_answer"] == "Teacher answer"
    for q_id in ("q4", "q5", "q6", "q7"):
        row = problems[q_id]
        assert not row["stem"] and not row["criterion"] and not row.get("reference_answer")
        assert row["question_structure"]["major_number"] == row["number"]
        assert row["number"] in set(numbers) - set(recognized)
        marker = next(issue for issue in row["preparation_issues"] if issue["code"] == "manual_completion_required")
        assert marker["details"]["required_fields"] == ["type", "stem", "criterion"]
    with pytest.raises(ValidationError):
        task_facade.update_problem(task_id=task, owner_id=owner, q_id="q4", patch={"review_status": "confirmed"})
    task_facade.update_problem(task_id=task, owner_id=owner, q_id="q4",
        patch={"type": "concept", "stem": "Teacher-filled question 4", "criterion": "Award 10 points for the solution."})
    task_facade.update_problem(task_id=task, owner_id=owner, q_id="q4", patch={"review_status": "confirmed"})
    assert task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]["q4"]["review_status"] == "confirmed"
    rejected = await task_preparation.retry_question_preparation(task_id=task, job_id=operation.operation_id,
        request=task_preparation.RetryQuestionPreparationRequest(expected_workflow_revision=response["workflow_revision"]),
        background_tasks=None, current=SimpleNamespace(id=owner), registry=registry)
    assert rejected.status_code == 409
    assert json.loads(rejected.body)["error"]["code"] == "question_preparation_retry_not_available"






@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [False, True])
async def test_submission_raw_text_handoff_opens_manual_rows_without_model_calls(monkeypatch, stopped):
    from backend.db import assignment_repository
    from backend.services.submission_manual_review import continue_manually
    owner, task = _seed_task(with_question=True)
    assignment_repository.add_question(task, teacher_id=owner, q_id="q2", order_index=1, number="2",
        type="concept", stem="Second", criterion="Award 10 points.", max_score=10)
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
        raise AssertionError("Manual recovery cannot dispatch a model")
    registry.provider = SimpleNamespace(provider_id="test-provider", supports_vision=False, ainvoke=organize)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    forbidden = AsyncMock(side_effect=AssertionError("must not reread the original"))
    monkeypatch.setattr(task_facade, "read_question_source", forbidden)
    response = await continue_manually(task_id=task, owner_id=owner, job_id=origin.id,
        expected_revision=workflow_repository.get_workflow(task, owner_id=owner).workflow_revision)
    assert response["status"] == "ready_for_review"
    assert (await task_facade.async_task_state(task_id=task, owner_id=owner))["recovering_saved_text"] is False
    assert not calls and forbidden.await_count == 0
    assert file_repository.get_file(file_id=stored.id, owner_id=owner).availability_status == "available"
    for _ in range(2):
        for read in (task_facade.get_task(task_id=task, owner_id=owner), await task_facade.async_task_state(task_id=task, owner_id=owner),
                     task_facade.list_tasks(owner_id=owner)[task]):
            assert read["status"] == "submissions_ready" and not read["error"]
    student = next(iter(task_facade.get_task(task_id=task, owner_id=owner)["student_data"].values()))
    answers = {answer["q_id"]: answer for answer in student["stu_ans"]}
    assert all(answer["content"] == "" and "recognition_manual_pending" in answer["flag"]
               for answer in answers.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["start", "retry", "worker", "manual"])
async def test_retired_question_text_recovery_never_dispatches_provider(monkeypatch, entrypoint):
    from backend.db.session import session_scope
    from backend.tests.test_question_preparation_recovery import _queue_question_preparation
    owner, task = _seed_task()
    source, queued, _ = await _queue_question_preparation(owner, task)
    registry = _RecoveryRegistry()
    forbidden = AsyncMock(side_effect=AssertionError("retired recovery cannot resolve a provider"))
    monkeypatch.setattr(task_facade, "_registry_for_owner", forbidden)
    legacy = {"artifacts": {source["source_token"]: ["retained-original"]}}
    if entrypoint == "start":
        result = await task_preparation._start_question_preparation(task_id=task,
            request=task_preparation.StartQuestionPreparationRequest(source_tokens=[source["source_token"]],
                expected_workflow_revision=queued["workflow_revision"], recognition_provider_id="test-provider"),
            current=SimpleNamespace(id=owner), registry=registry, background_tasks=None,
            allow_prepared_source_reuse=True, manual_saved_sources=legacy)
    else:
        with session_scope() as session:
            record = session.get(workflow_repository.WorkflowOperationRecord, queued["job_id"])
            record.payload = {**record.payload, "manual_saved_sources": legacy}
        if entrypoint == "worker":
            await task_preparation.run_durable_question_preparation(_claim(owner, queued["job_id"], "retired-worker"))
            result = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
            assert result.status == "error" and result.error_code == "saved_text_recovery_retired"
        else:
            operation = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
            task_facade._fail_operation(task, owner, operation.id, operation.attempt, "provider_rate_limited")
            revision = workflow_repository.get_workflow(task, owner_id=owner).workflow_revision
            if entrypoint == "retry":
                result = await task_preparation.retry_question_preparation(task_id=task, job_id=operation.id,
                    request=task_preparation.RetryQuestionPreparationRequest(expected_workflow_revision=revision),
                    background_tasks=None, current=SimpleNamespace(id=owner), registry=registry)
            else:
                result = await task_preparation.manually_complete_question_preparation(task, operation.id,
                    task_preparation.ManualQuestionPreparationRequest(expected_workflow_revision=revision),
                    current=SimpleNamespace(id=owner))
    if entrypoint != "worker":
        assert result.status_code == 409
        assert json.loads(result.body)["error"]["code"] == "saved_text_recovery_retired"
    assert forbidden.call_count == 0



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
