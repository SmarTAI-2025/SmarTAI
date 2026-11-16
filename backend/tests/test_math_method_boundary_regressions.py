"""Exercise source boundaries through arbitration, reassessment and final review."""
import json
import re
from types import SimpleNamespace

import pytest

from backend.llm.providers import LLMResponse
from backend.models import ProblemInfo
from backend.services.part_rubric import AnswerSegment
from backend.services.rubric_drafts import RubricDefinition, bind_draft, generated_rubric
from backend.skills import math_grading as grading
from backend.tests.test_alternative_route import ANSWER
from backend.tests.test_frozen_part_rubric import question_row
from backend.tests.test_math_method_review import case, choice, multipart_case, provider, revision, run


def linked_case(separator='，', fragment=False):
    p, a, plan, experts, right, _ = multipart_case()
    texts = ['用配方法解得 1 和 2', '用因式分解解得 1 和 2']
    reference = f'(1) {texts[0]}{separator}(2) {texts[1]}。'
    refs = ['配方', '因式分解'] if fragment else texts
    parts = tuple(part.model_copy(update={'answer_refs': (f'a{i}',), 'steps': tuple(
        step.model_copy(update={'criterion': '正确作答', 'source_quote': '', 'answer_refs': ()})
        for step in part.steps)}) for i, part in enumerate(plan.part_rubric.parts))
    rules = plan.part_rubric.model_copy(update={'parts': parts, 'answer_segments': tuple(
        AnswerSegment(id=f'a{i}', text=text, origin='teacher') for i, text in enumerate(refs))})
    row = question_row(rules.model_dump(mode='json'))
    row.update(reference_answer=reference, criterion='每小问正确作答得10分。')
    row['criterion'], draft = generated_rubric(row, RubricDefinition(part_rubric=rules))
    p = ProblemInfo(**row); plan = bind_draft(draft, p)
    for expert in experts:
        trace = grading.read_math_trace(expert); trace['plan_id'] = plan.plan_id
        expert.raw_output = json.dumps(trace)
    return p, a, plan, experts, right


@pytest.mark.asyncio
@pytest.mark.parametrize('separator', ['，', ', ', '；', '\n'])
@pytest.mark.parametrize('fragment', [False, True])
@pytest.mark.parametrize('foreign', [False, True])
async def test_frozen_citation_boundary_survives_context_lookup(separator, fragment, foreign):
    p, a, plan, experts, right = linked_case(separator, fragment)
    selected = choice(reference_quote='因式分解' if foreign else '配方')
    main, calls = provider('primary', selected, *([selected] if foreign else []), {'revisions': []})
    regrader, regrades = provider('a', revision(right))
    result = await run(p, a, plan, experts, main, {'a': regrader})
    assert len(regrades) == int(not foreign)
    assert [e.score for e in result] == ([8, 16] if foreign else [16, 16])
    assert ('selected_methods' in json.loads(calls[-1][1].content)) is not foreign
    assert not any('因式分解' in text for text in json.loads(calls[0][1].content)['method_reference_passages']['p'])


@pytest.mark.asyncio
@pytest.mark.parametrize('reference,quote', [
    ('避免使用配方，改用因式分解', '配方'),
    ('Never use substitution', 'substitution'),
    ('Refrain from using substitution; use factorization.', 'substitution'),
    ('配方法应避免使用，改用因式分解。', '配方'),
    ('Rule out substitution; use factorization.', 'substitution'),
    ('Exclude substitution; use factorization.', 'substitution'),
])
async def test_prohibited_method_cannot_lock_a_route(reference, quote):
    p, a, plan, experts, _ = case(reference=reference, teacher='正确作答得10分。')
    calls = []
    async def invoke(messages):
        calls.append(messages)
        data = json.loads(messages[1].content)
        output = choice(reference_quote=quote) if 'disputed_part_ids' in data else {'revisions': []}
        return LLMResponse(content=json.dumps(output), provider='offline', model='fixed', duration_ms=1)
    regrader, regrades = provider('a')
    result = await run(p, a, plan, experts, SimpleNamespace(provider_id='primary', ainvoke=invoke), {'a': regrader})
    assert not regrades and [e.score for e in result] == [0, 8]
    assert 'selected_methods' not in json.loads(calls[-1][1].content)
    assert grading.read_math_trace(result[0])['method_review']['fallback'] == 'joint_review'


@pytest.mark.asyncio
@pytest.mark.parametrize('student', ['以上是我的答案。', 'x = 1 y = 2', 'x=1，y=2'])
async def test_student_method_requires_more_than_a_line_address(student):
    p, a, plan, experts, _ = case()
    a = a.model_copy(update={'content': student + '\n没有写推导。'})
    calls = []
    async def invoke(messages):
        calls.append(messages)
        data = json.loads(messages[1].content)
        output = choice() if 'disputed_part_ids' in data else {'revisions': []}
        return LLMResponse(content=json.dumps(output), provider='offline', model='fixed', duration_ms=1)
    regrader, regrades = provider('a')
    result = await run(p, a, plan, experts, SimpleNamespace(provider_id='primary', ainvoke=invoke), {'a': regrader})
    assert not regrades and [e.score for e in result] == [0, 8]
    assert 'selected_methods' not in json.loads(calls[-1][1].content)


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['student_quote', 'evidence', 'origin_quote'])
@pytest.mark.parametrize('repair', [False, True])
async def test_reassessment_cannot_reuse_another_parts_student_evidence(field, repair):
    p, a, plan, experts, right, _ = multipart_case()
    bad = right.model_copy(update={'student_quote': 'student:3'}) if field == 'student_quote' else right.model_copy(update={
        'errors': tuple(e.model_copy(update={field: 'student:4'}) for e in right.errors)})
    main, calls = provider('primary', choice(), {'revisions': []})
    regrader, regrades = provider('a', revision(bad), revision(right if repair else bad))
    result = await run(p, a, plan, experts, main, {'a': regrader})
    assert len(regrades) == 2
    assert [e.score for e in result] == ([16, 16] if repair else [8, 16])
    trace = grading.read_math_trace(result[0])
    assert trace['method_review']['status'] == ('reassessed' if repair else 'reassessment_failed')


@pytest.mark.asyncio
async def test_final_review_cannot_reintroduce_foreign_error_evidence():
    p, a, plan, experts, right, _ = multipart_case()
    bad = right.model_copy(update={'errors': tuple(e.model_copy(update={
        'evidence': 'student:4', 'origin_quote': 'student:4'}) for e in right.errors)})
    final = {'revisions': [revision(bad)]}
    main, _ = provider('primary', choice(), final, final)
    regrader, _ = provider('a', revision(right))
    result = await run(p, a, plan, experts, main, {'a': regrader})
    assert grading.read_math_trace(result[0])['assessment']['part_decisions'][0]['errors'][0]['origin_quote'] == 'student:2'


@pytest.mark.asyncio
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
async def test_standalone_subquestion_labels_keep_valid_method_alignment(newline):
    p, a, plan, experts, right, _ = multipart_case()
    a = a.model_copy(update={'content': ('(1)\n' + ANSWER + '\n(2)\n' + ANSWER).replace('\n', newline)})
    def remap(value):
        return json.loads(re.sub(r'student:([1-4])', lambda m: 'student:' + {'1':'2','2':'3','3':'5','4':'6'}[m[1]], json.dumps(value)))
    for expert in experts:
        expert.raw_output = json.dumps(remap(grading.read_math_trace(expert)))
    right = type(right).model_validate(remap(right.model_dump(mode='json')))
    main, calls = provider('primary', choice(student_source_ids=['student:2']), {'revisions': []})
    regrader, regrades = provider('a', revision(right))
    result = await run(p, a, plan, experts, main, {'a': regrader})
    assert len(regrades) == 1 and [e.score for e in result] == [16, 16]
    assert 'selected_methods' in json.loads(calls[-1][1].content)


@pytest.mark.parametrize('source,quote,allowed', [
    ('避免使用配方，改用因式分解', '因式分解', True),
    ('Never use substitution; use factorization.', 'factorization', True),
    ('Use substitution.', 'substitution', True),
    ('配方法应避免使用，改用因式分解。', '因式分解', True),
    ('Refrain from using substitution; use factorization.', 'factorization', True),
    ('Circumvent using substitution.', 'substitution', False),
    ('Use substitution, which is forbidden.', 'substitution', False),
    ('Do not use, substitution.', 'substitution', False),
])
def test_positive_and_uncertain_method_language(source, quote, allowed):
    from backend.skills.math_method_review import _supported_method_quote
    assert _supported_method_quote(source, quote) is allowed


@pytest.mark.asyncio
async def test_citing_a_closing_line_cannot_borrow_a_method_from_an_uncited_line():
    p, a, plan, experts, _ = case()
    a = a.model_copy(update={'content': a.content + '\n以上是我的答案。'})
    bad = choice(student_source_ids=['student:3'])
    main, calls = provider('primary', bad, bad, {'revisions': []})
    regrader, regrades = provider('a')
    result = await run(p, a, plan, experts, main, {'a': regrader})
    assert not regrades and [e.score for e in result] == [0, 8]
    assert 'selected_methods' not in json.loads(calls[-1][1].content)


@pytest.mark.parametrize('foreign', [False, True])
@pytest.mark.parametrize('field', ['step', 'follow_through', 'goal', 'error'])
def test_all_reassessment_evidence_fields_use_the_same_scope(field, foreign):
    from backend.skills.math_method_review import validate_method_part_sources
    p, a, plan, _, right, _ = multipart_case()
    ref = 'student:4' if foreign else 'student:2'
    data = right.model_dump(mode='json')
    if field in {'step', 'follow_through'}:
        data['steps'] = [dict(id='complete_square', outcome='missed', evidence=ref if field == 'step' else 'student:1',
                              error_source=ref if field == 'follow_through' else '', reason='具体错误')]
    elif field == 'goal':
        data['goal_findings'] = [dict(group='g', evidence=ref, implication='具体推导', repair='none')]
    else:
        data['errors'][0]['origin_quote'] = ref
    decision = type(right).model_validate(data)
    if foreign:
        with pytest.raises(ValueError, match='reviewed part'):
            validate_method_part_sources([decision], a.content, plan.part_rubric)
    else:
        validate_method_part_sources([decision], a.content, plan.part_rubric)


def test_truncated_frozen_excerpt_keeps_negative_context_without_expanding_permission():
    from backend.skills.math_method_review import _method_evidence, _supported_reference_quote
    p, _, plan, _, _ = linked_case(fragment=True)
    p = p.model_copy(update={'reference_answer': '(1) 避免使用配方，(2) 用因式分解解得 1 和 2。'})
    evidence = _method_evidence(p, plan.part_rubric)
    assert not _supported_reference_quote(evidence['p'], '配方')
    assert not _supported_reference_quote(evidence['p'], '因式分解')
    assert _supported_reference_quote(evidence['p2'], '因式分解')
