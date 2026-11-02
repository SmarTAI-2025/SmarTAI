"""Failed material retries reuse verified source units without question deduping."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents import incremental_question_preparation as inc
from backend.api import task_preparation as api
from backend.db import workflow_repository
from backend.db.session import session_scope
from backend.domain.errors import ValidationError
from backend.services import task_facade
from backend.tests.test_incremental_question_preparation import generated, row
from backend.tests.test_question_manual_completion import manual
from backend.tests.test_question_preparation_recovery import (
    _BackgroundTasks, _RecoveryRegistry, _claim, _queue_question_preparation, _seed_task,
)


NUMBERS = ["1.1.5", "1.1.7", "1.1.20", "1.1.29", "1.1.31", "1.2.3", "1.2.16"]


async def queue(monkeypatch, targets=NUMBERS):
    preflight = api.preflight_problem_source

    async def targeted(**kwargs):
        kwargs["recognition_options"] = json.dumps({"targets": targets})
        return await preflight(**kwargs)

    monkeypatch.setattr(api, "preflight_problem_source", targeted)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: _RecoveryRegistry())
    owner, task = _seed_task()
    _, queued, _ = await _queue_question_preparation(owner, task, source_text="Selected exercises.")
    return owner, task, queued["job_id"]


def full_rows():
    return inc._rows([row(number) for number in NUMBERS])


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_provider", [False, True])
@pytest.mark.parametrize("from_units", [False, True])
async def test_retry_failed_materials_never_rereads_or_reextracts_completed_questions(monkeypatch, changed_provider, from_units):
    owner, task, job = await queue(monkeypatch)
    reads, extracts, generations = [], [], []

    async def read(sources, **kwargs):
        # A repeated reader may produce different text, even with the same
        # frozen original. It must not run for already verified question units.
        reads.append(kwargs["operation"].attempt)
        return [(draft, text + f"\nReader pass {len(reads)}", payload) for draft, text, payload in sources]

    async def extract(*args, **kwargs):
        extracts.append(True)
        return full_rows()

    failed_numbers = set(NUMBERS[3:5])

    async def generate(data, targets, *args, **kwargs):
        number = data["q1"]["number"]
        generations.append((number, tuple(target["target"] for target in targets)))
        if number in failed_numbers:
            raise ValidationError("Synthetic failure", code="provider_response_invalid")
        return [generated(target["target"], f"Saved {target['target']} for {number}.") for target in targets]

    monkeypatch.setattr(api, "_recognize_saved_question_sources", read)
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    await api.run_durable_question_preparation(_claim(owner, job, "first"))
    first = workflow_repository.get_operation(job, owner_id=owner)
    assert first.status == "error" and first.checkpoint["preparation_counts"]["questions"] == 7
    before = len(generations)
    failed_numbers.remove(NUMBERS[3])
    registry = _RecoveryRegistry("changed-model") if changed_provider else _RecoveryRegistry()
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    retry = await api.retry_question_preparation(task, job,
        api.RetryQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision,
            use_current_configuration=changed_provider),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=registry)
    assert isinstance(retry, dict), getattr(retry, "body", retry)
    await api.run_durable_question_preparation(_claim(owner, retry["job_id"], "retry"))
    assert len(reads) == len(extracts) == 1
    assert {number for number, _ in generations[before:]} == set(NUMBERS[3:5])
    saved = workflow_repository.get_operation(retry["job_id"], owner_id=owner)
    assert saved.status == "error"
    assert saved.checkpoint["preparation_counts"] == dict(questions=7, stems=7, reference_answers=6, rubrics=6)
    if from_units:
        with session_scope() as session:
            operation = session.get(workflow_repository.WorkflowOperationRecord, retry["job_id"])
            operation.checkpoint = {**operation.checkpoint, "incremental_view_from_units": True}
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    before_calls = (len(reads), len(extracts), len(generations))
    result = await manual(owner, task, retry["job_id"], workflow.workflow_revision)
    assert isinstance(result, dict), getattr(result, "body", result)
    rows = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
    assert [item["number"] for item in rows.values()] == NUMBERS
    for item in rows.values():
        number = item["number"]
        assert item["stem"] == row(number)["stem"]
        assert bool(item["criterion"]) == (number != NUMBERS[4])
        assert bool(item["reference_answer"]) == (number != NUMBERS[4])
    assert (len(reads), len(extracts), len(generations)) == before_calls


@pytest.mark.asyncio
async def test_retry_preserves_different_questions_with_the_same_chapter_local_number(monkeypatch):
    owner, task, job = await queue(monkeypatch, targets=[])
    rows = inc._rows([row("1", stem="Chapter one: explain a cyclic group."),
                     row("1", stem="Chapter two: explain an integral domain.")])
    extract = AsyncMock(return_value=rows)
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    calls = []
    failing = True

    async def generate(data, targets, *args, **kwargs):
        stem = data["q1"]["stem"]
        calls.append(stem)
        if failing and stem == rows["q2"]["stem"]:
            raise ValidationError("Synthetic failure", code="provider_response_invalid")
        return [generated(target["target"], f"Saved {target['target']}.") for target in targets]

    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    await api.run_durable_question_preparation(_claim(owner, job, "repeated-number"))
    assert workflow_repository.get_operation(job, owner_id=owner).status == "error"
    failing = False
    forbidden = AsyncMock(side_effect=AssertionError("Completed source must not be read again"))
    monkeypatch.setattr(api, "_recognize_saved_question_sources", forbidden)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    retry = await api.retry_question_preparation(task, job,
        api.RetryQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=_RecoveryRegistry())
    assert isinstance(retry, dict), getattr(retry, "body", retry)
    await api.run_durable_question_preparation(_claim(owner, retry["job_id"], "repeated-number-retry"))
    assert workflow_repository.get_operation(job, owner_id=owner).status == "done"
    published = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
    assert [item["number"] for item in published.values()] == ["1", "1"]
    assert [item["stem"] for item in published.values()] == [item["stem"] for item in rows.values()]
    assert calls.count(rows["q1"]["stem"]) == 1
    assert calls.count(rows["q2"]["stem"]) == 2
    extract.assert_awaited_once()
    forbidden.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read", "missing_target", "partial_stem"])
async def test_retry_still_reads_and_repairs_unfinished_question_sources(monkeypatch, failure):
    from backend.domain.errors import RecognitionError

    owner, task, job = await queue(monkeypatch)
    reads = []
    repairing = False

    async def read(sources, **kwargs):
        reads.append(True)
        if not repairing and failure == "read":
            raise RecognitionError("ocr_incomplete")
        if repairing and failure == "partial_stem":
            return [(draft, text + " Recovered continuation.", payload) for draft, text, payload in sources]
        return sources

    async def extract(*args, **kwargs):
        rows = inc._rows([row(number, criterion="Teacher rubric.", reference_answer="Teacher answer.")
                         for number in NUMBERS])
        if not repairing and failure == "missing_target":
            rows.pop("q7")
        if not repairing and failure == "partial_stem":
            rows["q7"]["source_incomplete"] = True
        return rows

    monkeypatch.setattr(api, "_recognize_saved_question_sources", read)
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    await api.run_durable_question_preparation(_claim(owner, job, "unfinished"))
    assert workflow_repository.get_operation(job, owner_id=owner).status == "error"
    repairing = True
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    retry = await api.retry_question_preparation(task, job,
        api.RetryQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=_RecoveryRegistry())
    assert isinstance(retry, dict), getattr(retry, "body", retry)
    await api.run_durable_question_preparation(_claim(owner, retry["job_id"], "unfinished-retry"))
    assert len(reads) == 2
    saved = workflow_repository.get_operation(job, owner_id=owner)
    assert saved.status == "done", saved.error_code
    rows = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
    assert len(rows) == 7 and all(item["stem"] for item in rows.values())
