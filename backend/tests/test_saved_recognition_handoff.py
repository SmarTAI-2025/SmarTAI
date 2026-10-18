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
    assembly = await save_pages(owner, task, stored, data, final=not stopped, text=[
        "1. Saved literal question 1. (a) First part. (b) Second part. Rubric: (a) 3 points",
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
            criterion="(a) 3 points" if i == 1 else "", source_incomplete=(i == 7)) for i in range(1, 8)]}))
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
    await task_preparation.run_durable_question_preparation(_claim(owner, response["job_id"], "saved-text"))
    operation = workflow_repository.get_operation(response["job_id"], owner_id=owner)
    assert operation.status == "done", (operation.error_code, operation.checkpoint)
    assert len(provider_calls) == 1 and forbidden.await_count == 0
    for read in (task_facade.get_task(task_id=task, owner_id=owner), await task_facade.async_task_state(task_id=task, owner_id=owner)):
        assert read["status"] == "problems_ready" and read["problem_count"] == 7
    problems = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
    assert problems["q1"]["stem"] == "Saved literal question 1. (a) First part. (b) Second part."
    assert problems["q1"]["criterion"] == "(a) 3 points" and not problems["q1"].get("reference_answer")
    assert not problems["q1"]["rubric_point_summary"]["is_valid"]
    assert not any(issue["code"] == "recognition_partial" for issue in problems["q1"]["preparation_issues"])
    assert any(issue["code"] == "recognition_partial" for issue in problems["q7"]["preparation_issues"])
    assert task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]


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
