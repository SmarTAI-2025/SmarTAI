"""Alias matching is global, order independent, and adds no matching model pass."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents import ingest_agent
from backend.services.question_matching import (
    copied_question_candidates, question_number_aliases,
    question_number_candidates, resolve_question_match,
)
from backend.services.submission_incremental import split_submission_question_units
from backend.services.submission_source_pipeline import PreparedSubmissionSource


def problems(numbers=('Section 1.2 / 1', 'Section 1.2 / 2')):
    stems = ['Find the eigenvalues of the real matrix diag(1, 2, 3).',
             'Find the eigenvalues of the real matrix diag(1, 2, 4).']
    return {f'q{i}': dict(q_id=f'q{i}', number=number, type='计算题', stem=stems[i - 1])
            for i, number in enumerate(numbers, 1)}


@pytest.mark.parametrize('label', ['Section 1.2 / 1', 'section 1.2 1', '1.2.1',
                                   'Question 1.2.1', '１．２．１', '第1章第2节第1题', '1', '1.'])
def test_unique_number_aliases(label):
    assert question_number_candidates(label, problems()) == ['q1']
    assert resolve_question_match(label, 'Wrong answer: eigenvalues are zero.', problems()) == 'q1'


def test_short_alias_is_ambiguous_across_the_whole_assignment():
    known = problems(('Section 1.1 / 1', 'Section 1.2 / 1'))
    assert question_number_candidates('1', known) == ['q1', 'q2']
    assert resolve_question_match('1', 'The answer is zero.', known) is None
    assert all('1' not in aliases for aliases in question_number_aliases(known).values())
    assert question_number_candidates('1.2.1', known) == ['q2']
    assert resolve_question_match('1', known['q2']['stem'] + '\nMy calculation.', known) == 'q2'


@pytest.mark.parametrize('labels', [('1', 'Section 1 / 1'), ('1.2.1', 'Section 1.2 / 1'),
                                  ('1', '1'), ('Section 1 / 1', 'Chapter 1 / 1')])
def test_canonical_collisions_never_overwrite_a_candidate(labels):
    known = problems(labels)
    query = '1.2.1' if labels[0] == '1.2.1' else '1'
    assert question_number_candidates(query, known) == ['q1', 'q2']
    assert resolve_question_match(query, 'An answer without matching context.', known) is None


def test_copied_stem_matches_without_number_but_near_identical_math_does_not():
    known = problems()
    assert resolve_question_match('', known['q2']['stem'] + '\nAnswer: zero.', known) == 'q2'
    assert resolve_question_match('', 'Find the eigenvalues of the real matrix diag(1, 2, 5).', known) is None
    assert copied_question_candidates(known['q1']['stem'], known) == ['q1']
    assert resolve_question_match('1', known['q2']['stem'], known) is None


def source(text):
    return PreparedSubmissionSource('source', 'file', 'S1_Alice.txt', 'text/plain', text)


def test_reversed_explicit_aliases_split_by_identity_not_position():
    known = problems()
    original = source('Student ID: S1\nQuestion 1.2.2: Second answer.\nQuestion 1.2.1: First answer.')
    units = split_submission_question_units(original, known)
    assert [ids for _, ids in units] == [{'q2'}, {'q1'}]
    assert 'Second answer' in units[0][0].text and 'First answer' not in units[0][0].text


def test_ambiguous_short_number_and_conflicting_copied_stem_keep_all_candidates():
    known = problems(('Section 1.1 / 1', 'Section 1.2 / 1'))
    original = source('Question 1: An answer.\nQuestion 2: Another answer.')
    assert split_submission_question_units(original, known, {'q2'}) == [(original, {'q2'})]
    known = problems()
    conflict = source('Question 1: ' + known['q2']['stem'] + '\nQuestion 2: Some work.')
    assert split_submission_question_units(conflict, known) == [(conflict, set(known))]


@pytest.mark.asyncio
@pytest.mark.parametrize('label', ['1.2.1', '1'])
async def test_ocr_only_aliases_need_no_model(label):
    result = (await ingest_agent.parse_student_answer_sources_from_ocr_markdown(
        [source(f'Student ID: S1\nName: Alice\n{label}: My unchanged answer.')], problems()))[0]
    assert result.student['stu_ans'][0]['q_id'] == 'q1'
    assert result.student['stu_ans'][0]['content'] == 'My unchanged answer.'


@pytest.mark.asyncio
async def test_ocr_duplicate_numbers_do_not_silently_choose_last_question():
    known = problems(('1', '1'))
    result = (await ingest_agent.parse_student_answer_sources_from_ocr_markdown(
        [source('1: An answer without distinguishing context.')], known))[0]
    assert result.status == 'no_matching_answer' and result.student is None
    assert result.unknown_question_ids == ('number:1',)


@pytest.mark.asyncio
async def test_ocr_partial_match_keeps_unresolved_identity_for_existing_review_gate():
    known = problems(('Section 1.1 / 1', 'Section 1.2 / 1'))
    known['q3'] = dict(q_id='q3', number='2', type='short', stem='A third distinct question.')
    result = (await ingest_agent.parse_student_answer_sources_from_ocr_markdown(
        [source('1: Ambiguous first answer.\n2: Clear second answer.')], known))[0]
    assert [a['q_id'] for a in result.student['stu_ans']] == ['q3']
    assert result.unknown_question_ids == ('number:1',)


@pytest.mark.asyncio
async def test_ocr_unlabelled_work_is_not_synthetic_question_one():
    known = problems(('1', '2'))
    result = (await ingest_agent.parse_student_answer_sources_from_ocr_markdown(
        [source('An answer without any question label or copied question.')], known))[0]
    assert result.status == 'no_matching_answer' and result.student is None
    copied = (await ingest_agent.parse_student_answer_sources_from_ocr_markdown(
        [source(known['q2']['stem'] + '\nMy answer.')], known))[0]
    assert [a['q_id'] for a in copied.student['stu_ans']] == ['q2']


@pytest.mark.asyncio
@pytest.mark.parametrize('catalog_number, accepted', [('', True), ('2', False)])
async def test_ocr_single_question_without_teacher_number_retains_existing_review_fallback(catalog_number, accepted):
    known = {'q1': dict(q_id='q1', number=catalog_number, type='short', stem='One exercise.')}
    result = (await ingest_agent.parse_student_answer_sources_from_ocr_markdown(
        [source('1. Student work.')], known))[0]
    if accepted:
        assert result.student['stu_ans'][0]['content'] == 'Student work.'
        assert 'ocr_only_structure_requires_teacher_review' in result.student['stu_ans'][0]['flag']
    else:
        assert result.status == 'no_matching_answer' and result.student is None


@pytest.mark.asyncio
async def test_ocr_retry_subset_does_not_make_ambiguous_alias_unique():
    known = problems(('Section 1.1 / 1', 'Section 1.2 / 1'))
    result = (await ingest_agent.parse_student_answer_sources_from_ocr_markdown(
        [source('1: An answer.')], known, source_question_ids={'source': ['q2']}))[0]
    assert result.status == 'no_matching_answer'


@pytest.mark.asyncio
@pytest.mark.parametrize('conflict', ['number', 'copied_stem', None])
async def test_existing_single_model_call_has_aliases_and_conflict_review(monkeypatch, conflict):
    known = problems()
    answer = dict(q_id='q2', number=known['q2']['number'], type='计算题', content='Wrong but readable answer.', flag=[])
    if conflict == 'number':
        answer['number'] = '1.2.1'
    elif conflict == 'copied_stem':
        answer['content'] = known['q1']['stem'] + '\nMy work.'
    response = SimpleNamespace(content=json.dumps(dict(stu_id='S1', stu_name='Alice', stu_ans=[answer])))
    invoke = AsyncMock(return_value=response)
    monkeypatch.setattr(ingest_agent, 'ainvoke_with_retry', invoke)
    result = (await ingest_agent.parse_student_answer_sources([source('Some work in reverse order.')], known,
        SimpleNamespace(provider_id='fixture'), source_question_ids={'source': ['q2']}, independent_answers=True))[0]
    invoke.assert_awaited_once()
    messages = invoke.await_args.args[1]
    assert 'sequence is only a search hint' in messages[0].content
    assert '1.2.2' in messages[1].content and 'number_aliases' in messages[1].content
    assert 'Question Data' in messages[1].content
    saved = result.student['stu_ans'][0]
    assert saved['content'] == answer['content']
    assert ('recognition_manual_pending' in saved['flag']) is bool(conflict)


@pytest.mark.asyncio
async def test_real_saved_out_of_order_answers_keep_canonical_identity(monkeypatch):
    from backend.db import assignment_repository
    from backend.services import task_facade
    from backend.tests.test_submission_incremental import _claim, _questions, _registry
    from backend.tests.test_task_background_workflows import _seed_task
    owner, task = _seed_task()
    known = problems()
    for index, (q_id, row) in enumerate(known.items()):
        assignment_repository.add_question(task, teacher_id=owner, q_id=q_id, order_index=index,
            number=row['number'], type=row['type'], stem=row['stem'], criterion='Teacher rubric.', max_score=10)
    calls = []
    async def invoke(messages):
        requested = _questions(messages)
        calls.append([q['q_id'] for q in requested])
        return SimpleNamespace(content=json.dumps(dict(stu_id='S1', stu_name='Alice', stu_ans=[
            dict(q_id=q['q_id'], number=q['number'], type=q['type'], content='Answer for ' + q['q_id'], flag=[])
            for q in requested])))
    registry = _registry(monkeypatch, invoke)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename='S1_Alice.txt', content=b'Question 1.2.2: Second answer.\nQuestion 1.2.1: First answer.',
        content_type='text/plain', registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued['job_id']))
    after = task_facade.get_task(task_id=task, owner_id=owner)
    assert after['status'] == 'submissions_ready'
    saved = {a['q_id']: a for a in after['student_data']['S1']['stu_ans']}
    assert saved['q1']['content'] == 'Answer for q1' and saved['q2']['content'] == 'Answer for q2'
    assert sorted(calls) == [['q1'], ['q2']]


@pytest.mark.asyncio
async def test_ocr_running_aliases_preserve_both_continuation_fragments():
    result = (await ingest_agent.parse_student_answer_sources_from_ocr_markdown(
        [source('1.2.1: First part.\n1: Continued part.')], problems()))[0]
    assert len(result.student['stu_ans']) == 1
    assert result.student['stu_ans'][0]['content'] == 'First part.\n\nContinued part.'


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog_number", ["Section 1.2 / 1", "1.2.1"])
@pytest.mark.parametrize("page_order", [(1, 2), (2, 1)])
async def test_pdf_running_labels_with_different_aliases_keep_full_answer(monkeypatch, catalog_number, page_order):
    from backend.db import assignment_repository
    from backend.services import task_facade
    from backend.tests.test_recognition_agent import pdf
    from backend.tests.test_submission_incremental import _claim, _registry
    from backend.tests.test_task_background_workflows import _seed_task
    owner, task = _seed_task()
    assignment_repository.add_question(task, teacher_id=owner, q_id='q1', order_index=0,
        number=catalog_number, type='short', stem='A mathematical question.', criterion='Teacher rubric.', max_score=10)
    pages = ['Question 1.2.1: First part.', 'Question 1: Continued part.']
    calls = []
    async def invoke(messages):
        text = messages[-1].content.split('**[Student Submission Content]**:', 1)[1]
        calls.append(text)
        assert all(part in text for part in ('First part.', 'Continued part.'))
        return SimpleNamespace(content=json.dumps(dict(stu_id='S1', stu_name='Alice', stu_ans=[
            dict(q_id='q1', number=catalog_number, type='short', content='First part. Continued part.', flag=[])])))
    registry = _registry(monkeypatch, invoke)
    async def read(**kwargs):
        for number in page_order:
            await kwargs['on_pages']([dict(page_number=number, text=pages[number - 1])])
        return SimpleNamespace(text='\n\n'.join(pages), recognition=None)
    monkeypatch.setattr(task_facade, 'read_question_source', read)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename='S1.pdf', content=pdf(pages), content_type='application/pdf', registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued['job_id']))
    after = task_facade.get_task(task_id=task, owner_id=owner)
    assert after['status'] == 'submissions_ready' and len(calls) == 1
    assert after['student_data']['S1']['stu_ans'][0]['content'] == 'First part. Continued part.'


@pytest.mark.parametrize('identical', [False, True])
@pytest.mark.asyncio
async def test_explicit_number_survives_shared_copied_stem(identical):
    known = problems(('1', '2'))
    known['q1']['stem'] = 'Compute the determinant of the given matrix A.'
    known['q2']['stem'] = known['q1']['stem'] + ('' if identical else ' Also find its inverse.')
    content = known['q2']['stem'] + '\nMy calculation is shown below.'
    assert copied_question_candidates(content, known) == ['q1', 'q2']
    assert resolve_question_match('2', content, known) == 'q2'
    assert resolve_question_match('', content, known) is None
    result = (await ingest_agent.parse_student_answer_sources_from_ocr_markdown(
        [source('2: ' + content)], known))[0]
    assert result.student['stu_ans'][0]['q_id'] == 'q2'
    assert result.student['stu_ans'][0]['content'] == content


@pytest.mark.parametrize('symbol', ['x₂', 'x2'])
@pytest.mark.asyncio
async def test_copied_math_does_not_fold_superscripts_into_subscripts_or_digits(symbol):
    known = problems(('1', '2'))
    known['q1']['stem'] = 'Find the value of x² under the stated conditions.'
    content = known['q1']['stem'].replace('x²', symbol) + '\nMy work.'
    assert copied_question_candidates(content, known) == []
    assert resolve_question_match('', content, known) is None
    assert resolve_question_match('２', content, known) == 'q2'
    result = (await ingest_agent.parse_student_answer_sources_from_ocr_markdown(
        [source('2: ' + content)], known))[0]
    assert result.student['stu_ans'][0]['q_id'] == 'q2'


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_case", ["retained_conflict", "discarded_conflict", "repaired_number_conflict"])
async def test_format_repair_rechecks_retained_answer_matching_before_real_save(monkeypatch, repair_case):
    from backend.db import assignment_repository
    from backend.services import task_facade
    from backend.tests.test_submission_incremental import _claim, _questions, _registry
    from backend.tests.test_task_background_workflows import _seed_task
    owner, task = _seed_task()
    known = problems(('1', '2'))
    for index, (q_id, row) in enumerate(known.items()):
        assignment_repository.add_question(task, teacher_id=owner, q_id=q_id, order_index=index,
            number=row['number'], type=row['type'], stem=row['stem'], criterion='Teacher rubric.', max_score=10)
    retained_content = known['q2' if repair_case == 'retained_conflict' else 'q1']['stem'] + '\nStudent work.'
    calls = []
    async def invoke(messages):
        requested = _questions(messages)
        calls.append([q['q_id'] for q in requested])
        answers = [dict(q_id=q['q_id'], number=q['number'], type=q['type'],
                        content='Verified answer for ' + q['q_id'], flag=[]) for q in requested]
        if len(calls) == 1:
            answers[0].update(content=retained_content)
            answers[0]['number' if repair_case == 'repaired_number_conflict' else 'type'] = {'invalid': 'format'}
        elif repair_case == 'discarded_conflict':
            answers[0]['content'] = known['q2']['stem'] + '\nUnadopted rewritten content.'
        elif repair_case == 'repaired_number_conflict':
            answers[0]['number'] = '2'
        return SimpleNamespace(content=json.dumps(dict(stu_id='S1', stu_name='Alice', stu_ans=answers)))
    registry = _registry(monkeypatch, invoke)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename='S1_Alice.txt', content=b'Answers without explicit section boundaries.',
        content_type='text/plain', registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued['job_id']))
    after = task_facade.get_task(task_id=task, owner_id=owner)
    answers = {a['q_id']: a for a in after['student_data']['S1']['stu_ans']}
    assert calls == [['q1', 'q2'], ['q1']]
    assert answers['q1']['type'] == known['q1']['type']
    assert answers['q1']['content'] == retained_content
    assert ('recognition_manual_pending' in answers['q1']['flag']) is (repair_case != 'discarded_conflict')
    assert not any(flag.startswith('recognition_invalid_fields:') for flag in answers['q1']['flag'])
    assert answers['q2']['content'] == 'Verified answer for q2' and not answers['q2']['flag']
    blocked = task_facade.grading_readiness(task_id=task, owner_id=owner)['blocking_issues']
    assert ('submission_recognition_blocked' in blocked) is (repair_case != 'discarded_conflict')


def test_page_boundaries_use_unique_identity_and_keep_original_alias_text():
    from backend.services.source_question_units import source_page_groups, source_question_units
    known = problems(('1.2.1', '1.2.2'))
    aliases = {alias: q_id for q_id, labels in question_number_aliases(known).items() for alias in labels}
    pages = {1: 'Question 1.2.1: First half.', 2: 'Question 1: Second half.', 3: 'Question 1.2.2: Next answer.'}
    groups = source_page_groups(pages, numbered=False, number_key=aliases.get)
    assert [(g.pages, g.complete) for g in groups] == [((1, 2), True), ((3,), False)]
    assert groups[0].text == pages[1] + '\n\n' + pages[2]
    pages[3] += '\nQuestion 1: Late continuation.'
    repeated = source_question_units(pages, known_numbers=set(aliases), number_key=aliases.get)
    assert all(not unit.complete for unit in repeated if aliases[unit.number] == 'q1')


def test_ambiguous_running_number_cannot_close_previous_answer():
    from backend.services.source_question_units import source_page_groups
    known = problems(('1.2.1', '1.3.1'))
    aliases = {alias: q_id for q_id, labels in question_number_aliases(known).items() for alias in labels}
    groups = source_page_groups({1: 'Question 1.2.1: First half.', 2: 'Question 1: Possible continuation.'},
                                numbered=False, number_key=aliases.get)
    assert not any(group.complete for group in groups)


@pytest.mark.asyncio
@pytest.mark.parametrize('first_review, repair_review', [(True, True), (True, False), (False, True), (False, False)])
async def test_model_matching_uncertainty_survives_type_repair_and_saved_grading_gate(monkeypatch, first_review, repair_review):
    from backend.db import assignment_repository, file_repository
    from backend.services import task_facade
    from backend.tests.test_submission_incremental import _claim, _questions, _registry
    from backend.tests.test_task_background_workflows import _seed_task
    owner, task = _seed_task()
    known = problems(('1.2.1', '1.3.1'))
    for index, (q_id, row) in enumerate(known.items()):
        assignment_repository.add_question(task, teacher_id=owner, q_id=q_id, order_index=index,
            number=row['number'], type=row['type'], stem=row['stem'], criterion='Teacher rubric.', max_score=10)
    calls = []
    async def invoke(messages):
        requested = _questions(messages)
        calls.append([q['q_id'] for q in requested])
        if len(calls) == 2:
            artifacts = [file for file in file_repository.list_files(owner_id=owner, assignment_id=task)
                         if file.kind == 'submission_source_parse_v1']
            assert len(artifacts) == 1
            saved = task_facade._load_submission_results(artifacts[0])[0]
            assert saved.student['recognition']['model_review_question_ids'] == (['q1'] if first_review else [])
        answers = []
        for q in requested:
            answer = dict(q_id=q['q_id'], number=q['number'], type=q['type'], content='Verified sibling.', flag=[])
            if q['q_id'] == 'q1':
                answer.update(number='1', content='The student work has no distinguishing copied question.')
                if (first_review if len(calls) == 1 else repair_review):
                    answer['flag'] = ['recognition_manual_pending']
                if len(calls) == 1:
                    answer['type'] = {'invalid': 'format'}
            answers.append(answer)
        return SimpleNamespace(content=json.dumps(dict(stu_id='S1', stu_name='Alice', stu_ans=answers)))
    registry = _registry(monkeypatch, invoke)
    queued = task_facade.queue_task_submission_parsing(task_id=task, owner_id=owner,
        filename='S1_Alice.txt', content=b'Question 1: The student work has no distinguishing copied question.',
        content_type='text/plain', registry=registry)
    await task_facade.run_durable_submission_recognition(_claim(owner, queued['job_id']))
    after = task_facade.get_task(task_id=task, owner_id=owner)
    answers = {a['q_id']: a for a in after['student_data']['S1']['stu_ans']}
    assert calls == [['q1', 'q2'], ['q1']]
    assert answers['q1']['type'] == known['q1']['type']
    assert answers['q1']['number'] == '1'
    assert answers['q1']['content'] == 'The student work has no distinguishing copied question.'
    assert not any(flag.startswith('recognition_invalid_fields:') for flag in answers['q1']['flag'])
    review = first_review or repair_review
    assert ('recognition_manual_pending' in answers['q1']['flag']) is review
    blocked = task_facade.grading_readiness(task_id=task, owner_id=owner)['blocking_issues']
    assert ('submission_recognition_blocked' in blocked) is review
    assert answers['q2']['content'] == 'Verified sibling.' and not answers['q2']['flag']
    if review:
        # The real model/format-repair/save path must have an unchanged-text
        # teacher exit, while retaining the source evidence in storage.
        task_facade.update_student_answer(task_id=task, owner_id=owner, display_student_id='S1', q_id='q1',
            patch={'answer_id': answers['q1']['answer_id'], 'content': answers['q1']['content'], 'review_status': 'confirmed'},
            expected_revision=after['workflow_revision'])
        reopened = task_facade.get_task(task_id=task, owner_id=owner)
        confirmed = next(a for a in reopened['student_data']['S1']['stu_ans'] if a['q_id'] == 'q1')
        assert confirmed['content'] == answers['q1']['content'] and confirmed['review_status'] == 'confirmed'
        assert 'submission_recognition_blocked' not in task_facade.grading_readiness(task_id=task, owner_id=owner)['blocking_issues']
        assert calls == [['q1', 'q2'], ['q1']]
