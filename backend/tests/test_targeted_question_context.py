"""Selected-source context, truthful totals and optional chapter labels."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents import incremental_question_preparation as inc
from backend.api import task_preparation as api
from backend.domain.errors import ValidationError
from backend.progress.tracker import ProgressReporter
from backend.services import task_facade
from backend.tests.test_incremental_question_preparation import generated, persisted, row, setup
from backend.tests.test_question_preparation_recovery import _claim, _RecoveryRegistry
from backend.db import workflow_repository
from backend.tools.problem_dedup import dedupe_extracted_problems


TARGETS = ['1.1.5', '1.1.7', '1.1.20', '1.1.29', '1.1.31', '1.2.3', '1.2.16']
PAGES = {
    1: 'Definitions.\n5. Explain the first object.\n7. Explain the second object.\nSec 1.1',
    2: '20. Explain a third object.\n29. Explain a fourth object.\n31. Explain a fifth object.',
    3: '## 1.2 New section\n3. Explain a sixth object.\n16. Explain the last object.\nSec 1.2',
}


async def targeted_setup(monkeypatch):
    original = api.preflight_problem_source
    async def targeted(**kwargs):
        kwargs.update(structure_mode='extract_from_source', extraction_hint='题号: ' + ', '.join(TARGETS),
                      recognition_options=json.dumps({'targets': TARGETS}))
        return await original(**kwargs)
    monkeypatch.setattr(api, 'preflight_problem_source', targeted)
    return await setup(monkeypatch, '\n\n'.join(PAGES.values()))


def page_reader(pages):
    async def read(source, *, on_pages):
        # Exercise both out-of-order arrival and real page callbacks. A callback
        # is not permission to discard the earlier page's section footer.
        for number in reversed(pages):
            await on_pages([dict(page_number=number, text=pages[number])])
        draft, _, payload = source
        return draft, '\n\n'.join(pages.values()), {**payload, 'recognition': {
            'coverage': {'requested_pages': list(pages), 'total_pages': len(pages)}}}
    return read


@pytest.mark.asyncio
@pytest.mark.parametrize('as_pages', [True, False])
async def test_selected_source_keeps_section_footer_and_all_seven_questions(monkeypatch, as_pages):
    owner, _, operation, pipeline = await targeted_setup(monkeypatch)
    if as_pages:
        pipeline.read_source = page_reader(PAGES)
    calls = []
    async def extract(text, *args, **kwargs):
        calls.append(text)
        assert 'Sec 1.1' in text and '20.' in text and '## 1.2' in text
        return inc._rows([row(n, criterion='Teacher rubric', reference_answer='Teacher answer') for n in TARGETS])
    monkeypatch.setattr(inc, '_extract_problems_call', extract)
    result = await pipeline.run()
    assert len(calls) == 1
    assert [r['number'] for r in result.values()] == TARGETS
    _, saved = persisted(owner, operation)
    assert [r['number'] for r in saved.values()] == TARGETS


@pytest.mark.asyncio
async def test_four_of_seven_keeps_total_and_retry_repairs_the_missing_unit(monkeypatch):
    owner, task, operation, pipeline = await targeted_setup(monkeypatch)
    pipeline.read_source = page_reader(PAGES)
    calls = []
    visible = asyncio.Event()
    release = asyncio.Event()
    found = ['1.1.5', '1.1.7', '1.2.3', '1.2.16']
    async def extract(text, *args, **kwargs):
        calls.append(text)
        if len(calls) == 3:
            visible.set()
            await release.wait()
        return inc._rows([row(n) for n in found])
    monkeypatch.setattr(inc, '_extract_problems_call', extract)
    async def generate(data, targets, *args, **kwargs):
        return [generated(t['target'], 'Valid teacher-review material.') for t in targets]
    monkeypatch.setattr(inc, 'generate_missing_question_materials', generate)
    runner = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(visible.wait(), 5)
    snapshot = await pipeline.reporter.snapshot()
    assert snapshot.total_questions == 7
    assert snapshot.stage_metrics['solution_total_questions'] == 7
    release.set()
    with pytest.raises(ValidationError, match='requested questions'):
        await runner
    saved, rows = persisted(owner, operation)
    assert len(calls) == 3 and len(rows) == 7
    assert {r['number'] for r in rows.values() if r['stem']} == set(found)
    assert saved.checkpoint['incremental_incomplete_units']
    assert (await pipeline.reporter.snapshot()).stage_metrics['solution_completed_questions'] == 4

    task_facade._fail_operation(task, owner, operation.operation_id, operation.attempt,
        'question_targets_incomplete', expected_lease_token=operation.lease_token)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    retry = await api.retry_question_preparation(task_id=task, job_id=operation.operation_id,
        request=api.RetryQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision),
        background_tasks=None, current=SimpleNamespace(id=owner), registry=_RecoveryRegistry())
    next_op = _claim(owner, retry['job_id'], 'target-retry')
    inputs = api._rehydrate_question_preparation_inputs(next_op)
    restored = inc.IncrementalQuestionPreparation(operation=next_op, sources=inputs['sources'],
        provider=inputs['route'].provider, provider_id=inputs['recognition_provider_id'],
        reporter=ProgressReporter(next_op.operation_id), score_policy=inputs['score_policy'], read_source=page_reader(PAGES))
    repair = AsyncMock(return_value=inc._rows([row(n) for n in TARGETS]))
    monkeypatch.setattr(inc, '_extract_problems_call', repair)
    result = await restored.run()
    repair.assert_awaited_once()
    assert {r['number'] for r in result.values() if r['stem']} == set(TARGETS)
    assert (await restored.reporter.snapshot()).stage_metrics['solution_total_questions'] == 7
    assert (await restored.reporter.snapshot()).stage_metrics['solution_completed_questions'] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize('label', ['1.', 'Question 1'])
async def test_no_section_keeps_distinct_repeated_numbers_without_failure(monkeypatch, label):
    owner, _, operation, pipeline = await setup(monkeypatch)
    pages = {1: f'{label}: Explain a group.\nQuestion 2: Explain an ideal.',
             2: f'{label}: Compute a matrix.\nQuestion 2: Count the permutations.'}
    pipeline.read_source = page_reader(pages)
    async def extract(text, *a, **kw):
        rows = [row('1', stem='Explain a group.'), row('2', stem='Explain an ideal.')] if 'group' in text else []
        if 'matrix' in text:
            rows += [row('1.', stem='Compute a matrix.'), row('2', stem='Count the permutations.')]
        return inc._rows([{**r, 'criterion': 'Supplied rubric', 'reference_answer': 'Supplied answer'} for r in rows])
    monkeypatch.setattr(inc, '_extract_problems_call', extract)
    result = await pipeline.run()
    assert len(result) == 4
    assert sorted(r['number'] for r in result.values()) == ['1', '1.', '2', '2']
    assert len(persisted(owner, operation)[1]) == 4


@pytest.mark.parametrize('label', ['Section', 'Chapter', 'No', '节'])
def test_same_wording_from_different_explicit_sections_is_not_merged(label):
    stem = 'Explain this long shared definition and justify every condition in it.'
    result = dedupe_extracted_problems(inc._rows([
        row(f'{label} 1 / 1', stem=stem), row(f'{label} 2 / 1', stem=stem)]))
    assert len(result) == 2


@pytest.mark.asyncio
async def test_unscoped_similar_exercises_with_same_number_are_both_persisted(monkeypatch):
    owner, _, operation, pipeline = await setup(monkeypatch)
    stem = 'Calculate every eigenvalue of the following real matrix and justify the multiplicities: '
    pages = {1: 'Question 1\n' + stem + 'diag(1,2,3).',
             2: 'Question 1\n' + stem + 'diag(1,2,4).'}
    pipeline.read_source = page_reader(pages)
    monkeypatch.setattr(inc, '_extract_problems_call', AsyncMock(return_value=inc._rows([
        row('1', stem=stem + 'diag(1,2,3).', criterion='Teacher rubric', reference_answer='Teacher answer'),
        row('1', stem=stem + 'diag(1,2,4).', criterion='Teacher rubric', reference_answer='Teacher answer')
    ])))
    result = await pipeline.run()
    assert len(result) == 2 and len(persisted(owner, operation)[1]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('footer', ['Sec 1.1', 'Chapter 1', '1.1.1', 'No1'])
async def test_unscoped_later_page_receives_earlier_footer_context(monkeypatch, footer):
    _, _, _, pipeline = await setup(monkeypatch)
    pages = {1: 'Question 1\nExplain the first object.\n' + footer,
             2: 'Question 2\nExplain the second object.'}
    pipeline.read_source = page_reader(pages)
    seen = []
    async def extract(text, *args, **kwargs):
        number = '1' if 'Question 1' in text else '2'
        if number == '2':
            assert footer in kwargs['source_context']
            assert 'Source page 1:' in kwargs['source_context']
            seen.append(number)
        return inc._rows([row(f'{footer} / {number}', criterion='Teacher rubric', reference_answer='Teacher answer')])
    monkeypatch.setattr(inc, '_extract_problems_call', extract)
    result = await pipeline.run()
    assert len(result) == 2 and seen == ['2']
    assert [r['number'] for r in result.values()] == [f'{footer} / 1', f'{footer} / 2']


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', [False, True])
async def test_large_selected_source_keeps_prior_section_and_marks_missing_scope_for_retry(monkeypatch, missing):
    owner, _, operation, pipeline = await targeted_setup(monkeypatch)
    pages = {1: 'Sec 1.1\n5. First question.\n7. Second question.',
             2: '20. Third question.\n29. Fourth question.\n31. Fifth question.',
             3: 'Section 1.2\n3. Sixth question.\n16. Last question.'}
    pipeline.read_source = page_reader(pages)
    monkeypatch.setattr(inc, '_chunk_problem_text', lambda *_: [pages[1], pages[2], pages[3]])
    async def extract(text, *a, **kw):
        numbers = TARGETS[:2] if text == pages[1] else TARGETS[2:5] if text == pages[2] else TARGETS[5:]
        if text == pages[2]:
            assert 'Sec 1.1' in kw['source_context']
        return inc._rows([row(n, criterion='Teacher rubric', reference_answer='Teacher answer')
                         for n in numbers if not (missing and n == '1.1.20')])
    monkeypatch.setattr(inc, '_extract_problems_call', extract)
    if missing:
        with pytest.raises(ValidationError, match='requested questions'):
            await pipeline.run()
        saved, rows = persisted(owner, operation)
        assert len(rows) == 7 and sum(bool(r['stem']) for r in rows.values()) == 6
        assert set(saved.checkpoint['incremental_unit_artifacts']) <= set(saved.checkpoint['incremental_incomplete_units'])
    else:
        result = await pipeline.run()
        assert {r['number'] for r in result.values()} == set(TARGETS)


@pytest.mark.asyncio
async def test_same_extraction_request_includes_optional_footer_identity_contract(monkeypatch):
    from backend.agents import ingest_agent
    from langchain_core.messages import AIMessage
    provider = SimpleNamespace(provider_id='test')
    invoke = AsyncMock(return_value=AIMessage(content=json.dumps({'problems': [
        {**row('Section 1 / 1'), 'q_id': 'q1'}]})))
    monkeypatch.setattr(ingest_agent, 'ainvoke_with_retry', invoke)
    result = await ingest_agent._extract_problems_call('1. Explain this.\nSection 1', provider,
                                                     partial_source=True, allow_empty=True)
    invoke.assert_awaited_once()
    prompt = '\n'.join(message.content for message in invoke.await_args.args[1])
    assert 'page footers' in prompt and 'Distinct exercises may share a number' in prompt
    assert 'Missing section information is not missing question content' in prompt
    assert result['q1']['number'] == 'Section 1 / 1'
