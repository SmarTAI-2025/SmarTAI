"""Independent U3 synthetic recovery probes. No product edits or live model calls.

These are fault-injected backend integration checks, not real OCR/GUI acceptance.
"""
from audit_guard import require_probe_run
require_probe_run()

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents import incremental_question_preparation as inc
from backend.api import task_preparation as api
from backend.db import workflow_repository
from backend.domain.errors import LeaseLost, RecognitionError, ValidationError
from backend.services import task_facade
from backend.services.task_execution import stop_task_run
from backend.tests.test_incremental_question_materials import material_setup
from backend.tests.test_incremental_question_preparation import row, persisted, generated, setup
from backend.tests.test_question_preparation_recovery import (
    _RecoveryRegistry, _claim, _queue_question_preparation,
)
from backend.tests.test_task_background_workflows import _seed_task, _BackgroundTasks


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_role", ["rubric", "reference_answer"])
async def test_three_materials_reordered_numbers_gap_pause_and_manual_keep_every_issue(monkeypatch, broken_role):
    """Q3,Q1,Q2; teacher material has another order; page2 failure survives pause."""
    rubrics = {"1": "Rule A: one exact condition.", "2": "Rule B: two exact conditions.", "3": "Rule C: three exact conditions."}
    answers = {"1": "Answer A: alpha.", "2": "Answer B: beta.", "3": "Answer C: gamma."}
    rubric_text = "\n".join(f"Question {n}\n{rubrics[n]}" for n in ("2", "1", "3"))
    answer_text = "\n".join(f"Question {n}\n{answers[n]}" for n in ("1", "3", "2"))
    owner, task, operation, pipeline = await material_setup(monkeypatch, [("rubric", rubric_text), ("reference_answer", answer_text)])
    original_tokens = {source[0].role: source[0].source_token for source in pipeline.sources}

    async def read(source, **kwargs):
        draft, text, payload = source
        if draft.role == broken_role:
            return draft, text, {**payload, "source_error": "recognition_response_invalid",
                "recognition": {"coverage": {"processed_pages": [1, 3], "failed_pages": [2]}}}
        return source

    model_calls = []
    async def parse_material(*args, **kwargs):
        prompt = kwargs["user_prompt"]
        values = rubrics if "Rule A:" in prompt else answers
        model_calls.append("rubric" if values is rubrics else "reference_answer")
        return inc._MaterialItems(items=[inc._MaterialItem(number=n, text_value=value)
            for n, value in values.items()]), None

    pipeline.read_source = read
    extract = AsyncMock(return_value=inc._rows([row(n, stem=f"Synthetic stem {n}.") for n in ("3", "1", "2")]))
    generate = AsyncMock(side_effect=AssertionError("Never replace supplied incomplete teacher material with generated text"))
    monkeypatch.setattr(inc, "_extract_problems_call", extract)
    monkeypatch.setattr(inc, "structured_llm_call", parse_material)
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    with pytest.raises(RecognitionError) as failed:
        await pipeline.run()
    assert failed.value.code == "recognition_response_invalid"
    saved, before = persisted(owner, operation)
    assert len(before) == 3
    assert [item["number"] for item in before.values()] == ["3", "1", "2"]
    for item in before.values():
        assert item["stem"] == f"Synthetic stem {item['number']}."
        assert item["criterion"] == rubrics[item["number"]]
        assert item["reference_answer"] == answers[item["number"]]
        for role, field in (("rubric", "criterion"), ("reference_answer", "reference_answer")):
            assert item["material_provenance"][field]["source_token"] == original_tokens[role]
        assert any(issue["code"] == "recognition_partial" and issue["severity"] == "blocking"
            for issue in item["preparation_issues"])
    before_issues = {item["number"]: item["preparation_issues"] for item in before.values()}
    before_by_number = {item["number"]: item for item in before.values()}
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=operation.operation_id, expected_revision=workflow.workflow_revision)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    before_calls = list(model_calls)
    forbidden = AsyncMock(side_effect=AssertionError("Manual handoff cannot invoke a model"))
    monkeypatch.setattr(inc, "_extract_problems_call", forbidden)
    monkeypatch.setattr(inc, "structured_llm_call", forbidden)
    response = await api.manually_complete_question_preparation(task, operation.operation_id,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision), current=SimpleNamespace(id=owner))
    assert response["status"] == "ready_for_review"
    for _ in range(2):
        published = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
        assert len(published) == 3
        for item in published.values():
            original = before_by_number[item["number"]]
            for field in ("q_id", "number", "type", "stem", "max_score", "material_provenance"):
                assert item[field] == original[field], field
            assert item["criterion"] == rubrics[item["number"]]
            assert item["reference_answer"] == answers[item["number"]]
            for prior in before_issues[item["number"]]:
                matching = [issue for issue in item["preparation_issues"] if issue["issue_id"] == prior["issue_id"]]
                assert len(matching) == 1
                assert {key: value for key, value in matching[0].items() if key != "severity"} == {
                    key: value for key, value in prior.items() if key != "severity"}
        blockers = task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
        # Current product contract makes optional reference-answer issues advisory.
        # Its original issue must survive, but only an incomplete rubric blocks.
        assert ("question_review_blocked" in blockers) is (broken_role == "rubric")
        expected_severity = "blocking" if broken_role == "rubric" else "warning"
        for item in published.values():
            partial = [issue for issue in item["preparation_issues"] if issue["code"] == "recognition_partial"]
            assert partial and all(issue["severity"] == expected_severity for issue in partial)
    assert model_calls == before_calls
    assert set(model_calls) == {"rubric", "reference_answer"}
    generate.assert_not_awaited()
    forbidden.assert_not_awaited()
    print("U3_CUSTOM " + json.dumps({"case": "three_material_gap_pause_manual", "broken_role": broken_role,
        "question_numbers": [v["number"] for v in published.values()], "retained_fields": 9,
        "unresolved_issues_retained": True, "blocks_question_review": broken_role == "rubric", "manual_new_calls": 0}))


@pytest.mark.asyncio
async def test_duplicate_numbers_two_retries_preserve_success_and_manual_unresolved(monkeypatch):
    """Three stems, including chapter-local number 1; two failed retries then stop."""
    owner, task = _seed_task()
    _, queued, _ = await _queue_question_preparation(owner, task,
        source_text="Chapter B: 1. beta.\nChapter A: 7. gamma.\nChapter A: 1. alpha.")
    registry = _RecoveryRegistry()
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: registry)
    stems = ["Chapter B: beta.", "Chapter A: gamma.", "Chapter A: alpha."]
    numbers = ["1", "7", "1"]
    extracts = AsyncMock(return_value=inc._rows([row(number, stem=stem) for number, stem in zip(numbers, stems)]))
    calls = []
    async def generate(data, targets, *args, **kwargs):
        stem = data["q1"]["stem"]
        calls.append(stem)
        if stem == stems[2]:
            raise ValidationError("Synthetic unresolved material", code="provider_response_invalid")
        return [generated(t["target"], f"Saved {t['target']} from {stem}") for t in targets]
    monkeypatch.setattr(inc, "_extract_problems_call", extracts)
    monkeypatch.setattr(inc, "generate_missing_question_materials", generate)
    job = queued["job_id"]
    for attempt in range(3):
        await api.run_durable_question_preparation(_claim(owner, job, f"u3-retry-{attempt}"))
        saved = workflow_repository.get_operation(job, owner_id=owner)
        assert saved.status == "error"
        assert saved.checkpoint["preparation_counts"] == dict(questions=3, stems=3, reference_answers=2, rubrics=2)
        if attempt < 2:
            workflow = workflow_repository.get_workflow(task, owner_id=owner)
            response = await api.retry_question_preparation(task, job,
                api.RetryQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
                background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=registry)
            assert isinstance(response, dict), getattr(response, "body", response)
            job = response["job_id"]
    extracts.assert_awaited_once()
    assert calls.count(stems[0]) == calls.count(stems[1]) == 1
    assert calls.count(stems[2]) == 3
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    before_calls = list(calls)
    response = await api.manually_complete_question_preparation(task, job,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision), current=SimpleNamespace(id=owner))
    assert response["status"] == "ready_for_review"
    rows = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
    assert [v["number"] for v in rows.values()] == numbers
    assert [v["stem"] for v in rows.values()] == stems
    for q_id, stem in zip(("q1", "q2"), stems[:2]):
        assert rows[q_id]["q_id"] == q_id
        assert rows[q_id]["criterion"] == f"Saved criterion from {stem}"
        assert rows[q_id]["reference_answer"] == f"Saved reference_answer from {stem}"
    assert not rows["q3"]["criterion"] and not rows["q3"]["reference_answer"]
    assert "question_review_blocked" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
    assert calls == before_calls
    print("U3_CUSTOM " + json.dumps({"case": "repeat_number_two_retries_manual", "question_numbers": numbers,
        "extract_calls": extracts.await_count, "generation_calls_by_stem": {s:calls.count(s) for s in stems},
        "manual_new_calls": 0, "unresolved_blockers_retained": True}))


@pytest.mark.asyncio
@pytest.mark.parametrize('arrival', ['cancel', 'late_after_stop', 'late_after_edit'])
async def test_inflight_cancel_or_late_response_cannot_replace_manual_saved_content(monkeypatch, arrival):
    """Real pipeline/fences and SQLite; event-controlled model boundary, no GUI."""
    owner, task, operation, pipeline = await setup(monkeypatch, 'Synthetic first and second chunks.')
    release, second_started, first_saved = (asyncio.Event() for _ in range(3))
    monkeypatch.setattr(inc, '_chunk_problem_text', lambda *_: ['first', 'second', 'queued-third'])
    pipeline.gate = asyncio.Semaphore(1)
    source_token = pipeline.sources[0][0].source_token
    calls = []
    async def extract(text, *args, **kwargs):
        calls.append(text)
        if text == 'second':
            second_started.set()
            await release.wait()
        item = row('1' if text == 'first' else '2',
            stem='Saved first stem.' if text == 'first' else 'Late second stem.',
            criterion='Saved original rubric.', reference_answer='Saved original answer.')
        if text == 'first':
            # Deliberate provider-side source ambiguity, independent of the
            # completed first chunk. A closed chunk is not itself incomplete.
            item['preparation_issues'] = [dict(issue_id='synthetic-open-source', q_id='q1',
                code='ambiguous_question_match', field='source', severity='warning',
                status='open', source_ids=[source_token], details={})]
        return inc._rows([item])
    original_finish = pipeline.finish
    async def finish(key, rows=None, error=None):
        await original_finish(key, rows, error)
        if key.startswith('extract:') and rows and any(item.get('stem') == 'Saved first stem.' for item in rows.values()):
            first_saved.set()
    monkeypatch.setattr(pipeline, 'finish', finish)
    monkeypatch.setattr(inc, '_extract_problems_call', extract)
    forbidden = AsyncMock(side_effect=AssertionError('Supplied and manually edited fields must not regenerate'))
    monkeypatch.setattr(inc, 'generate_missing_question_materials', forbidden)
    runner = asyncio.create_task(pipeline.run())
    try:
        await asyncio.wait_for(asyncio.gather(second_started.wait(), first_saved.wait()), 5)
        _, saved = persisted(owner, operation)
        assert len(saved) == 1 and saved['q1']['stem'] == 'Saved first stem.'
        saved_source_issues = [issue for issue in saved['q1']['preparation_issues']
                               if issue['code'] == 'ambiguous_question_match' and issue['field'] == 'source']
        assert len(saved_source_issues) == 1 and saved_source_issues[0]['status'] == 'open'
        assert source_token in saved_source_issues[0]['source_ids']
        if arrival == 'cancel':
            runner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await runner
        workflow = workflow_repository.get_workflow(task, owner_id=owner)
        stopped = stop_task_run(task_id=task, owner_id=owner, job_id=operation.operation_id,
            expected_revision=workflow.workflow_revision)
        assert stopped['status'] == 'stopped'
        if arrival == 'late_after_stop':
            release.set()
            with pytest.raises((LeaseLost, asyncio.CancelledError)):
                await asyncio.wait_for(runner, 5)
        workflow = workflow_repository.get_workflow(task, owner_id=owner)
        response = await api.manually_complete_question_preparation(task, operation.operation_id,
            api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision), current=SimpleNamespace(id=owner))
        assert response['status'] == 'ready_for_review'
        before_edit = task_facade.get_task(task_id=task, owner_id=owner)['problem_data']
        assert len(before_edit) == 1
        original = before_edit['q1']
        for field in ('q_id', 'number', 'type', 'max_score', 'stem', 'criterion', 'reference_answer'):
            assert original[field] == saved['q1'][field]
        assert original.get('material_provenance', {}) == saved['q1'].get('material_provenance', {})
        assert saved_source_issues[0] in original['preparation_issues'], 'Existing source ambiguity must remain unchanged through manual recovery'
        original_issues = deepcopy(original['preparation_issues'])
        if arrival == 'late_after_edit':
            workflow = workflow_repository.get_workflow(task, owner_id=owner)
            task_facade.update_problem(task_id=task, owner_id=owner, q_id='q1',
                patch={'criterion': 'Teacher saved replacement rubric.', 'reference_answer': 'Teacher saved replacement answer.'},
                expected_revision=workflow.workflow_revision)
        expected = deepcopy(task_facade.get_task(task_id=task, owner_id=owner)['problem_data'])
        revision = workflow_repository.get_workflow(task, owner_id=owner).workflow_revision
        refs = deepcopy(workflow_repository.get_operation(operation.operation_id, owner_id=owner).checkpoint['incremental_unit_artifacts'])
        if arrival == 'late_after_edit':
            assert expected['q1']['criterion'] == 'Teacher saved replacement rubric.'
            assert expected['q1']['reference_answer'] == 'Teacher saved replacement answer.'
            release.set()
            with pytest.raises((LeaseLost, asyncio.CancelledError)):
                await asyncio.wait_for(runner, 5)
        for _ in range(2):
            refreshed = task_facade.get_task(task_id=task, owner_id=owner)
            assert refreshed['problem_data'] == expected
            assert refreshed['workflow_revision'] == revision
            async_refreshed = await task_facade.async_task_state(task_id=task, owner_id=owner)
            assert async_refreshed['status'] == refreshed['status'] == 'problems_ready'
            assert async_refreshed['workflow_revision'] == revision
        for issue in original_issues:
            retained = [item for item in expected['q1']['preparation_issues'] if item['issue_id'] == issue['issue_id']]
            assert len(retained) == 1, 'Existing issue identity disappeared'
            if arrival == 'late_after_edit' and issue['code'] == 'manual_completion_required':
                assert retained[0]['status'] == 'resolved'
                assert {k: v for k, v in retained[0].items() if k != 'status'} == {k: v for k, v in issue.items() if k != 'status'}
            else:
                assert retained[0] == issue, 'Unedited unresolved issue changed'
        assert saved_source_issues[0] in expected['q1']['preparation_issues']
        # Replaying the obsolete job is rejected; no new AI or stale publication.
        before_calls = list(calls)
        retry = await api.retry_question_preparation(task, operation.operation_id,
            api.RetryQuestionPreparationRequest(expected_workflow_revision=revision),
            background_tasks=_BackgroundTasks(), current=SimpleNamespace(id=owner), registry=_RecoveryRegistry())
        assert retry.status_code == 409
        assert task_facade.get_task(task_id=task, owner_id=owner)['problem_data'] == expected
        assert workflow_repository.get_operation(operation.operation_id, owner_id=owner).checkpoint['incremental_unit_artifacts'] == refs
        assert calls == before_calls == ['first', 'second']
        forbidden.assert_not_awaited()
        print('U3_CUSTOM ' + json.dumps({'case': 'inflight_stop_manual', 'arrival': arrival,
            'provider_calls': calls, 'manual_generation_calls': 0, 'stale_retry_status': retry.status_code,
            'published_questions': len(expected), 'edited_content_retained': arrival == 'late_after_edit',
            'unresolved_issues_retained': True}))
    finally:
        release.set()
        if not runner.done():
            runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
