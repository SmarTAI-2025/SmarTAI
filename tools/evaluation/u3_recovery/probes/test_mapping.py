"""Independent synthetic U3 probes. No product changes; fixed extraction replies."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents import incremental_question_preparation as inc
from backend.api import task_preparation as api
from backend.db import workflow_repository
from backend.services import task_facade
from backend.services.task_execution import stop_task_run
from backend.tests.test_incremental_question_preparation import row, persisted, setup
from backend.tests.test_incremental_question_materials import material_setup, parsed_material
from backend.tests.test_cross_page_teacher_sources import pdf_pipeline
from backend.tests.test_question_preparation_recovery import _RecoveryRegistry

SAMPLES = Path(__file__).resolve().parents[1] / 'samples'
EVIDENCE = Path(os.environ['SMARTAI_U3_EVIDENCE'])

def record(name, payload):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / (name + '.json')).write_text(json.dumps(payload, ensure_ascii=False, indent=2))


@pytest.mark.asyncio
async def test_long_selected_source_retains_shared_definition_in_later_request(monkeypatch):
    definition = 'For Questions 1 and 2, the operation star is defined by a star b = a + 2*b.'
    first = 'Calculate 2 star 3 using the common definition.'
    second = 'Calculate 4 star 5 using the common definition.'
    text = (SAMPLES / 'long_shared_definition.txt').read_text()
    original = api.preflight_problem_source
    async def selected(**kwargs):
        kwargs.update(structure_mode='extract_from_source', extraction_hint='题号: 1, 2',
                      recognition_options=json.dumps({'targets': ['1', '2']}))
        return await original(**kwargs)
    monkeypatch.setattr(api, 'preflight_problem_source', selected)
    owner, _, operation, pipeline = await setup(monkeypatch, text)
    calls = []
    async def extract(chunk, *args, **kwargs):
        calls.append({'text': chunk, 'source_context': kwargs.get('source_context', '')})
        return inc._rows([row(number, stem=stem, criterion='Teacher rubric.', reference_answer='Teacher answer.')
                          for number, stem in [('1', first), ('2', second)] if stem in chunk])
    monkeypatch.setattr(inc, '_extract_problems_call', extract)
    forbidden = AsyncMock(side_effect=AssertionError('No real generation is allowed'))
    monkeypatch.setattr(inc, 'generate_missing_question_materials', forbidden)
    await pipeline.run()
    _, saved = persisted(owner, operation)
    later = [call for call in calls if second in call['text']]
    record('long_shared_definition', {
        'definition': definition, 'source_chars': len(text), 'real_chunk_chars': [len(c['text']) for c in calls],
        'call_count': len(calls), 'later_call_context': [c['source_context'] for c in later],
        'definition_in_later_payload': [definition in c['text'] or definition in c['source_context'] for c in later],
        'saved': saved, 'generation_calls': forbidden.await_count,
    })
    assert later
    assert all(definition in c['text'] or definition in c['source_context'] for c in later), 'Shared definition was omitted from the request containing Question 2'


@pytest.mark.asyncio
@pytest.mark.parametrize('role,field', [('rubric', 'criterion'), ('reference_answer', 'reference_answer')])
async def test_three_page_reverse_arrival_retains_all_material_parts(monkeypatch, role, field):
    parts = ['State the assumptions: 2 points.', 'Derive the intermediate relation: 3 points.', 'Check the endpoint: 5 points.']
    pages = {index: 'Question 1\n' + part for index, part in enumerate(parts, 1)}
    owner, _, operation, pipeline = await pdf_pipeline(monkeypatch, role, pages, (3, 1, 2))
    monkeypatch.setattr(inc, '_extract_problems_call', AsyncMock(return_value=inc._rows([
        row(1, criterion='' if field == 'criterion' else 'Teacher rubric.',
            reference_answer='' if field == 'reference_answer' else 'Teacher answer.')
    ])))
    calls = []
    async def material(*args, **kwargs):
        value = kwargs['user_prompt'].split('[Teacher material]\n', 1)[1].split('\n[End material]', 1)[0]
        calls.append(value)
        return parsed_material('1', value)
    monkeypatch.setattr(inc, 'structured_llm_call', material)
    forbidden = AsyncMock(side_effect=AssertionError('Uploaded material must not be regenerated'))
    monkeypatch.setattr(inc, 'generate_missing_question_materials', forbidden)
    await pipeline.run()
    _, saved = persisted(owner, operation)
    record('three_pages_' + role, {'parts': parts, 'calls': calls, 'saved': saved, 'generation_calls': forbidden.await_count})
    assert len(saved) == 1 and all(part in saved['q1'][field] for part in parts)
    assert len(calls) == 1 and all(part in calls[0] for part in parts)
    for text in (saved['q1'][field], calls[0]):
        assert all(text.count(part) == 1 for part in parts)
        assert [text.index(part) for part in parts] == sorted(text.index(part) for part in parts)
    assert not saved['q1']['preparation_issues']
    forbidden.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_number_material_remains_visible_as_unresolved_after_manual_handoff(monkeypatch):
    literal = 'Use the supplied blue formula; award 6 points for the stated condition and 4 for the conclusion.'
    source_text = (SAMPLES / 'duplicate_number_problems.txt').read_text()
    original = api.preflight_problem_source
    async def literal_problem_source(**kwargs):
        if kwargs.get('role') == 'problem':
            kwargs['inline_text'] = source_text
        return await original(**kwargs)
    monkeypatch.setattr(api, 'preflight_problem_source', literal_problem_source)
    owner, task, operation, pipeline = await material_setup(monkeypatch, [
        ('rubric', (SAMPLES / 'duplicate_number_rubric.txt').read_text())])
    monkeypatch.setattr(inc, '_extract_problems_call', AsyncMock(return_value=inc._rows([
        row('7', stem='For matrix A = diag(1,2), compute its determinant.', criterion='Existing rubric A.', reference_answer='Answer A.'),
        row('7', stem='For matrix B = diag(3,4), compute its determinant.', criterion='Existing rubric B.', reference_answer='Answer B.'),
    ])))
    monkeypatch.setattr(inc, 'structured_llm_call', AsyncMock(return_value=parsed_material('7', literal)))
    forbidden = AsyncMock(side_effect=AssertionError('No real generation is allowed'))
    monkeypatch.setattr(inc, 'generate_missing_question_materials', forbidden)
    await pipeline.run()
    saved_op, saved = persisted(owner, operation)
    material_units = {key: data for key, data in pipeline.units.items() if key.startswith('material:')}
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=operation.operation_id, expected_revision=workflow.workflow_revision)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    response = await api.manually_complete_question_preparation(task, operation.operation_id,
        api.ManualQuestionPreparationRequest(expected_workflow_revision=workflow.workflow_revision), current=SimpleNamespace(id=owner))
    published = task_facade.get_task(task_id=task, owner_id=owner)['problem_data']
    readiness = task_facade.grading_readiness(task_id=task, owner_id=owner)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename='SYNTHETIC001_Example.txt', content=b'Question 7: Synthetic student work.',
        content_type='text/plain', registry=_RecoveryRegistry())
    record('duplicate_number_material', {
        'problem_source': source_text, 'literal': literal, 'material_units': material_units, 'saved': saved, 'published': published,
        'manual_status': response.get('status'), 'generation_calls': forbidden.await_count,
        'checkpoint_source_plans': saved_op.checkpoint.get('incremental_source_plans'),
        'grading_readiness_before_student_upload': readiness,
        'submission_upload_accepted_status': queued.get('status'),
    })
    assert len([r for r in published.values() if r.get('stem')]) == 2
    assert literal in json.dumps(material_units, ensure_ascii=False), 'The source unit itself must be retained'
    assert literal in json.dumps(published, ensure_ascii=False), 'Ambiguous uploaded material disappeared from the editable/manual projection'
