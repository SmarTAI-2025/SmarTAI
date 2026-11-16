"""Review only changed judgments, preserving frozen cross-part follow-through."""
import json

import pytest

from backend.models import ProblemInfo, StudentAnswerInfo
from backend.services.math_grading_context import math_plans_context
from backend.services.rubric_drafts import bind_draft, retarget_draft, generated_rubric, RubricDefinition
from backend.skills import math_grading as grading
from backend.skills.math_method_review import _has_method_evidence, _supported_method_quote
from backend.tests.test_frozen_part_rubric import question_row
from backend.tests.test_math_grading import expert_for
from backend.tests.test_math_method_review import case, choice, multipart_case, provider, revision, run


@pytest.mark.parametrize('source,quote', [
    ('配方法：千万别用。改用因式分解。', '配方'),
    ('配方为错误选项。改用因式分解。', '配方'),
    ('不要使用\n配方法。改用因式分解。', '配方'),
    ('Never use\nsubstitution. Use factorization.', 'substitution'),
    ('不要使用\r\n配方法。改用因式分解。', '配方'),
    ('Never use\r\nsubstitution. Use factorization.', 'substitution'),
])
def test_soft_wrap_does_not_erase_a_method_prohibition(source, quote):
    assert not _supported_method_quote(source, quote)
    assert _supported_method_quote(source, '因式分解' if quote == '配方' else 'factorization')


@pytest.mark.asyncio
@pytest.mark.parametrize('source,quote', [
    ('配方法：千万别用。改用因式分解。', '配方'),
    ('配方为错误选项。改用因式分解。', '配方'),
    ('不要使用\n配方法。改用因式分解。', '配方'),
    ('Never use\nsubstitution. Use factorization.', 'substitution'),
])
async def test_invalid_wrapped_or_mentioned_method_never_reaches_reassessment(source, quote):
    p, a, plan, experts, _ = case(reference=source, teacher='正确作答得10分。')
    invalid = choice(reference_quote=quote)
    main, calls = provider('primary', invalid, invalid, {'revisions': []})
    regrader, regrades = provider('a')
    result = await run(p, a, plan, experts, main, {'a': regrader})
    assert not regrades and [e.score for e in result] == [0, 8]
    assert len(calls) == 3
    assert 'selected_methods' not in json.loads(calls[-1][1].content)


@pytest.mark.parametrize('text', ['x²−3x+2=0⇒(x−1)(x−2)=0', '2x=4⇒x=2',
                                  '2*x=4⇒x=2', 'x²−3x+2=0\n(x−1)(x−2)=0'])
def test_one_and_two_line_derivations_use_the_same_expression_parser(text):
    assert _has_method_evidence(text)


@pytest.mark.parametrize('label', ['(100)', '（１００）'])
def test_numeric_heading_width_and_length_do_not_change_offsets(label):
    from backend.skills.math_method_review import _labelled_ranges
    _, _, plan, _, _, _ = multipart_case()
    parts = (plan.part_rubric.parts[0], plan.part_rubric.parts[1].model_copy(update={'label': '(100)'}))
    answer = '(1)本问作答\n' + label + '后问作答'
    ranges = _labelled_ranges(answer, parts)
    assert ranges['p'][1] == answer.index(label)
    assert answer[ranges['p2'][0]:ranges['p2'][1]] == label + '后问作答'


@pytest.mark.parametrize('text', ['$2x=4⇒x=2$', r'\(2x=4⇒x=2\)'])
def test_math_delimiters_do_not_change_transformation_detection(text):
    assert _has_method_evidence(text)


@pytest.mark.parametrize('source,quote', [('用\n配方法解得1和2。', '配方'), ('Use\nsubstitution.', 'substitution')])
def test_wrapped_affirmative_use_is_still_supported(source, quote):
    assert _supported_method_quote(source, quote)


@pytest.mark.asyncio
@pytest.mark.parametrize('separator', ['⇒', '\n'])
async def test_same_quadratic_derivation_can_align_on_one_or_two_lines(separator):
    p, a, plan, experts, right = case(same_total=True)
    a = a.model_copy(update={'content': 'x²−3x+2=0' + separator + '(x−1)(x−2)=0'})
    ids = ['student:1'] if separator == '⇒' else ['student:1', 'student:2']
    main, calls = provider('primary', choice(student_source_ids=ids), {'revisions': []})
    regrader, regrades = provider('a', revision(right))
    result = await run(p, a, plan, experts, main, {'a': regrader})
    assert len(regrades) == 1 and len(calls) == 2
    assert [e.score for e in result] == [10, 10]


@pytest.mark.asyncio
@pytest.mark.parametrize('full_first', [False, True])
@pytest.mark.parametrize('stage', ['arbitration', 'ordinary', 'reassessment', 'final'])
async def test_full_width_numbers_preserve_scope_through_all_review_entries(full_first, stage):
    p, a, plan, experts, right, _ = multipart_case()
    text = a.content.replace('(2) ', '（２）')
    if full_first:
        text = text.replace('(1) ', '（１）')
    a = a.model_copy(update={'content': text})
    bad = right.model_copy(update={'student_quote': 'student:3', 'errors': tuple(
        e.model_copy(update={'evidence': 'student:4', 'origin_quote': 'student:4'}) for e in right.errors)})
    selected = choice(student_source_ids=['student:3']) if stage == 'arbitration' else choice()
    final = {'revisions': [revision(bad)]} if stage in {'final', 'ordinary'} else {'revisions': []}
    main, calls = provider('primary', *([selected, selected] if stage == 'arbitration' else
                                     [] if stage == 'ordinary' else [selected]), final, final)
    regrader, regrades = provider('a', revision(bad if stage == 'reassessment' else right), revision(bad))
    if stage == 'ordinary':
        with math_plans_context({p.q_id: plan}), grading.math_review_scope():
            result = await grading.review_math_experts(p, a, experts, main)
    else:
        result = await run(p, a, plan, experts, main, {'a': regrader})
    assert [e.score for e in result] == ([16, 16] if stage == 'final' else [8, 16])
    if stage in {'arbitration', 'ordinary'}:
        assert not regrades


@pytest.mark.asyncio
@pytest.mark.parametrize('full_first', [False, True])
async def test_full_width_numbered_parts_keep_valid_alignment(full_first):
    p, a, plan, experts, right, _ = multipart_case()
    text = a.content.replace('(2) ', '（２）')
    if full_first:
        text = text.replace('(1) ', '（１）')
    a = a.model_copy(update={'content': text})
    main, calls = provider('primary', choice(), {'revisions': []})
    regrader, regrades = provider('a', revision(right))
    result = await run(p, a, plan, experts, main, {'a': regrader})
    assert len(regrades) == 1 and [e.score for e in result] == [16, 16]


def same_line_case():
    p, a, plan, experts, right, _ = multipart_case()
    text = '(1)因式分解得两根，第二个根误写为负二；(2)因式分解得两根，第二个根误写为负二。'
    a = a.model_copy(update={'content': text})
    for expert in experts:
        trace = grading.read_math_trace(expert)
        for d in trace['assessment']['part_decisions']:
            d['student_quote'] = 'student:1'
            for event in d['errors']:
                event.update(evidence='student:1', origin_quote='student:1')
        expert.raw_output = json.dumps(trace)
    first = f'student:1:1-{text.index("(2)")}'
    right = right.model_copy(update={'student_quote': first, 'errors': tuple(
        e.model_copy(update={'evidence': first, 'origin_quote': first}) for e in right.errors)})
    return p, a, plan, experts, right, first


@pytest.mark.asyncio
async def test_untouched_legacy_whole_line_part_does_not_block_a_valid_changed_part():
    p, a, plan, experts, right, first = same_line_case()
    complete = right.model_copy(update={'relevance': 'complete', 'errors': ()})
    main, calls = provider('primary', choice(student_source_ids=[first]), {'revisions': [revision(complete)]})
    regrader, regrades = provider('a', revision(right))
    result = await run(p, a, plan, experts, main, {'a': regrader})
    assert [e.score for e in result] == [18, 16]
    before = grading.read_math_trace(experts[0])['assessment']['part_decisions'][1]
    assert grading.read_math_trace(result[0])['assessment']['part_decisions'][1] == before
    assert len(calls) == 2 and len(regrades) == 1


def test_initial_assessment_and_prompt_use_bounded_evidence_for_same_line_parts():
    p, a, plan, experts, _, first = same_line_case()
    initial = grading.MathAssessment.model_validate(grading.read_math_trace(experts[1])['assessment'])
    with pytest.raises(ValueError):
        grading._assessment_validator(plan, a, p)(initial)
    text = grading.MathGradingSkill(None)._prompt(p, a, plan, '')
    assert first in text


@pytest.mark.asyncio
async def test_initial_assessment_repairs_broad_lines_using_the_supplied_bounded_ids():
    p, a, plan, experts, _, first = same_line_case()
    original = grading.MathAssessment.model_validate(grading.read_math_trace(experts[1])['assessment'])
    output = grading.FrozenPartAssessment(part_decisions=original.part_decisions, comment='Checked').model_dump(mode='json')
    corrected = json.loads(json.dumps(output))
    second = f'student:1:{a.content.index("(2)") + 1}-{len(a.content)}'
    for decision, address in zip(corrected['part_decisions'], [first, second]):
        decision['student_quote'] = address
        for event in decision['errors']:
            event.update(evidence=address, origin_quote=address)
    model, calls = provider('initial', output, corrected)
    skill = grading.MathGradingSkill(model)
    result, _ = await skill._assess(plan, system_prompt=grading.PART_SCORING_INSTRUCTION,
                    user_prompt=skill._prompt(p, a, plan, ''), validator=grading._assessment_validator(plan, a, p))
    assert len(calls) == 2
    assert grading.score_assessment(result, plan, a.content, problem_text=p.stem)[0] == 16


def follow_through_case():
    rules = dict(id='q1', label='1', stem='Find x, then double x.', total_pts=20, rubric_origin='teacher', parts=[
        dict(id='p', label='(1)', stem='Find x.', total_pts=10, alternative={'deduct_per_error': 1},
             steps=[dict(id='x', criterion='x=2', total_pts=10)]),
        dict(id='p2', label='(2)', stem='Double x.', total_pts=10, alternative={'deduct_per_error': 1},
             steps=[dict(id='double', criterion='Double the submitted x', total_pts=10, follow_through_pts=5)])])
    row = question_row(rules)
    row.update(criterion='第(2)问沿错保留5分。')
    row['criterion'], draft = generated_rubric(row, RubricDefinition(part_rubric=rules))
    p = ProblemInfo(**row)
    plan = bind_draft(retarget_draft(draft, p), p)
    a = StudentAnswerInfo(q_id=p.q_id, number=p.number, type=p.type, content='(1)x=3\n(2)2x=6')
    first = grading.PartDecision.model_validate(dict(part_id='p', target_quote='part:p', student_quote='student:1',
        method='reference', relevance='incorrect', progress='x should be 2',
        steps=[dict(id='x', outcome='missed', evidence='student:1', reason='x was incorrectly computed as 3')]))
    second = grading.PartDecision.model_validate(dict(part_id='p2', target_quote='part:p2', student_quote='student:2',
        method='reference', relevance='partial', progress='No process credit yet',
        steps=[dict(id='double', outcome='missed', evidence='student:2', reason='Based on the wrong x')]))
    assessment = grading.FrozenPartAssessment(part_decisions=[first, second], comment='Original').assessment()
    experts = [expert_for(assessment, plan, a.content).model_copy(update={'provider_id': name}) for name in ['a', 'b']]
    second = second.model_copy(update={'steps': (second.steps[0].model_copy(update={
        'outcome': 'follow_through', 'error_source': 'student:1', 'reason': 'Correctly doubled the submitted 3'}),)})
    correct = assessment.model_copy(update={'part_decisions': [first, second]})
    experts[1] = expert_for(correct, plan, a.content).model_copy(update={'provider_id': 'b'})
    return p, a, plan, experts, second


@pytest.mark.asyncio
@pytest.mark.parametrize('full_ledger', [False, True])
@pytest.mark.parametrize('invalid', ['', 'foreign_positive', 'unverified_origin'])
async def test_teacher_cross_part_follow_through_preserves_only_verified_origins(invalid, full_ledger):
    p, a, plan, experts, second = follow_through_case()
    if invalid == 'foreign_positive':
        second = second.model_copy(update={'steps': (second.steps[0].model_copy(update={'evidence': 'student:1'}),)})
    elif invalid == 'unverified_origin':
        a = a.model_copy(update={'content': '(1)x=3\n无依据的草稿值\n(2)2x=6'})
        second = second.model_copy(update={'student_quote': 'student:3', 'steps': (second.steps[0].model_copy(update={
            'evidence': 'student:3', 'error_source': 'student:2'}),)})
        for expert in experts:
            trace = grading.read_math_trace(expert)
            d = trace['assessment']['part_decisions'][1]
            d['student_quote'] = 'student:3'; d['steps'][0]['evidence'] = 'student:3'
            expert.raw_output = json.dumps(trace)
    if full_ledger:
        trace = grading.read_math_trace(experts[0]); trace['assessment']['needs_review'] = True
        experts[0].raw_output = json.dumps(trace)
        assessment = grading.MathAssessment.model_validate(trace['assessment'])
        assessment.part_decisions[1] = second
        final = {'revisions': [dict(expert_index=0, reason='Apply the frozen process-credit rule.',
                    source_quote=a.content.splitlines()[-1], assessment=assessment.model_dump(mode='json'))]}
    else:
        final = {'revisions': [revision(second)]}
    main, calls = provider('primary', final, final)
    with math_plans_context({p.q_id: plan}), grading.math_review_scope():
        result = await grading.review_math_experts(p, a, experts, main)
    assert result[0].score == (0 if invalid else 5)


@pytest.mark.asyncio
async def test_direct_repair_for_shared_lines_accepts_the_same_bounded_ids():
    p, a, plan, experts, _, first = same_line_case()
    data = grading.read_math_trace(experts[1])['assessment']
    second = f'student:1:{a.content.index("(2)") + 1}-{len(a.content)}'
    for decision, address in zip(data['part_decisions'], [first, second]):
        decision['student_quote'] = address
        for event in decision['errors']:
            event.update(evidence=address, origin_quote=address)
    output = {key: data[key] for key in ['part_decisions', 'comment', 'confidence', 'needs_review']}
    model, calls = provider('a', output)
    skill = grading.MathGradingSkill(model, language='zh')
    result = await skill._direct(p, a, plan, skill._prompt(p, a, plan, ''), None, [])
    assert result.score == 16 and not result.error_kind and len(calls) == 1
