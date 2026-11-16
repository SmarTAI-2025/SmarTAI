"""New spellings of method evidence must preserve scope and valid alignment."""
import json
from types import SimpleNamespace

import pytest

from backend.llm.providers import LLMResponse
from backend.skills import math_grading as grading
from backend.skills.math_method_review import _has_method_evidence, _supported_method_quote
from backend.tests.test_math_method_review import case, choice, multipart_case, provider, revision, run


@pytest.mark.parametrize('source,bad,good', [
    ('Substitution fails here. Use factorization.', 'Substitution', 'factorization'),
    ('配方仅用于展示错误，改用因式分解', '配方', '因式分解'),
    ('Use substitution as an example of a failed attempt; use factorization.', 'substitution', 'factorization'),
    ('配方是错误示范；用因式分解', '配方', '因式分解'),
    ('Substitution only illustrates an error. Use factorization.', 'Substitution', 'factorization'),
    ('配方只是为了展示常见错误，改用因式分解', '配方', '因式分解'),
])
def test_failed_or_demonstration_method_is_not_positive_evidence(source, bad, good):
    assert not _supported_method_quote(source, bad)
    assert _supported_method_quote(source, good)


@pytest.mark.parametrize('text,expected', [
    ('x²−3x+2=0\n(x−1)(x−2)=0', True),
    ('x^2-3*x+2=0\r\n(x-1)*(x-2)=0', True),
    ('$x²−3x+2=0$\n$(x−1)(x−2)=0$', True),
    ('x=1\ny=2', False),
    ('x²−3x+2=0\ny²−3y+2=0', False),
    ('x²−3x+2=0\n以上是答案。\n(x−1)(x−2)=0', False),
    ('x²−3x+2=0 (x−1)(x−2)=0', False),
    ('x+1=0\nx+1=0', False),
    ('构造反例说明该错误命题', True),
])
def test_adjacent_symbolic_derivation_without_a_method_name(text, expected):
    assert _has_method_evidence(text) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['arbitration', 'reassessment', 'final'])
@pytest.mark.parametrize('label', ['(2)', '（2）'])
async def test_no_space_heading_stops_foreign_student_evidence_at_every_stage(stage, label):
    p, a, plan, experts, right, _ = multipart_case()
    a = a.model_copy(update={'content': a.content.replace('(2) ', label)})
    bad = right.model_copy(update={'student_quote': 'student:3', 'errors': tuple(
        e.model_copy(update={'evidence': 'student:4', 'origin_quote': 'student:4'}) for e in right.errors)})
    selected = choice(student_source_ids=['student:3']) if stage == 'arbitration' else choice()
    final = {'revisions': [revision(bad)]} if stage == 'final' else {'revisions': []}
    main, calls = provider('primary', selected, *([selected] if stage == 'arbitration' else []), final, final)
    regrader, regrades = provider('a', revision(bad if stage == 'reassessment' else right), revision(bad))
    result = await run(p, a, plan, experts, main, {'a': regrader})
    assert [e.score for e in result] == ([16, 16] if stage == 'final' else [8, 16])
    if stage == 'arbitration':
        assert not regrades and 'selected_methods' not in json.loads(calls[-1][1].content)
    elif stage == 'reassessment':
        assert len(regrades) == 2
    else:
        assert grading.read_math_trace(result[0])['assessment']['part_decisions'][0]['student_quote'] == right.student_quote


@pytest.mark.asyncio
@pytest.mark.parametrize('reference,quote', [
    ('Substitution fails here. Use factorization.', 'Substitution'),
    ('配方仅用于展示错误，改用因式分解', '配方'),
])
async def test_failed_method_does_not_trigger_original_expert_reassessment(reference, quote):
    p, a, plan, experts, right = case(reference=reference, teacher='正确作答得10分。')
    calls = []
    async def invoke(messages):
        calls.append(messages)
        data = json.loads(messages[1].content)
        output = choice(reference_quote=quote) if 'disputed_part_ids' in data else {'revisions': []}
        return LLMResponse(content=json.dumps(output), provider='offline', model='fixed', duration_ms=1)
    regrader, regrades = provider('a', revision(right))
    result = await run(p, a, plan, experts, SimpleNamespace(provider_id='primary', ainvoke=invoke), {'a': regrader})
    assert not regrades and [e.score for e in result] == [0, 8]
    assert 'selected_methods' not in json.loads(calls[-1][1].content)


@pytest.mark.asyncio
async def test_real_derivation_completes_method_alignment_without_named_method():
    p, a, plan, experts, right = case()
    a = a.model_copy(update={'content': 'x²−3x+2=0\n(x−1)(x−2)=0'})
    right = right.model_copy(update={'relevance': 'complete', 'errors': ()})
    main, calls = provider('primary', choice(student_source_ids=['student:1', 'student:2']), {'revisions': []})
    regrader, regrades = provider('a', revision(right))
    result = await run(p, a, plan, experts, main, {'a': regrader})
    assert len(regrades) == 1 and [e.score for e in result] == [10, 8]
    assert 'selected_methods' in json.loads(calls[-1][1].content)


@pytest.mark.asyncio
async def test_uncited_intervening_line_does_not_create_a_derivation():
    p, a, plan, experts, right = case()
    a = a.model_copy(update={'content': 'x²−3x+2=0\n另一个独立计算\n(x−1)(x−2)=0'})
    selected = choice(student_source_ids=['student:1', 'student:3'])
    main, calls = provider('primary', selected, selected, {'revisions': []})
    regrader, regrades = provider('a', revision(right))
    result = await run(p, a, plan, experts, main, {'a': regrader})
    assert not regrades and [e.score for e in result] == [0, 8]


@pytest.mark.asyncio
@pytest.mark.parametrize('separator', ['；', ' ', '\t', '，'])
async def test_same_line_parts_have_citable_bounded_fragments_through_final_review(separator):
    p, a, plan, experts, right, _ = multipart_case()
    a = a.model_copy(update={'content': '(1)因式分解为 (x−1)(x−2)=0。第二个根误写为负二' + separator + '(2)因式分解为 (x−3)(x−4)=0。'})
    boundary = a.content.index('(2)')
    addresses = {'p': f'student:1:1-{boundary}', 'p2': f'student:1:{boundary + 1}-{len(a.content)}'}
    # The original votes must still refer to the same immutable one-line answer.
    for expert in experts:
        trace = grading.read_math_trace(expert)
        for decision in trace['assessment']['part_decisions']:
            address = addresses[decision['part_id']]
            decision['student_quote'] = address
            for event in decision['errors']:
                event.update(evidence=address, origin_quote=address)
        expert.raw_output = json.dumps(trace)
    calls = []
    selected_ids = []
    async def invoke(messages):
        calls.append(messages)
        data = json.loads(messages[1].content)
        if 'disputed_part_ids' in data:
            selected_ids[:] = data['method_student_source_ids']['p']
            assert selected_ids
            assert all('(2)' not in data['source_catalog'][key] for key in selected_ids)
            output = choice(student_source_ids=selected_ids)
        else:
            assert all(key in data['source_catalog'] for key in selected_ids)
            output = {'revisions': []}
        return LLMResponse(content=json.dumps(output), provider='offline', model='fixed', duration_ms=1)
    regrades = []
    async def regrade(messages):
        regrades.append(messages)
        fragment = selected_ids[0]
        decision = right.model_copy(update={'student_quote': fragment, 'errors': tuple(
            e.model_copy(update={'evidence': fragment, 'origin_quote': fragment}) for e in right.errors)})
        return LLMResponse(content=json.dumps(revision(decision)), provider='offline', model='fixed', duration_ms=1)
    result = await run(p, a, plan, experts, SimpleNamespace(provider_id='primary', ainvoke=invoke),
                       {'a': SimpleNamespace(provider_id='a', ainvoke=regrade)})
    assert len(regrades) == 1 and [e.score for e in result] == [16, 16]
    assert 'selected_methods' in json.loads(calls[-1][1].content)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['exhausted_arbitration', 'no_alignment'])
@pytest.mark.parametrize('foreign,repair', [(True, False), (True, True), (False, False)])
@pytest.mark.parametrize('literal', [False, True])
async def test_ordinary_review_respects_known_student_part_boundaries(mode, foreign, repair, literal):
    p, a, plan, experts, right, _ = multipart_case()
    evidence = a.content.splitlines()[2] if literal else 'student:3'
    bad = right.model_copy(update={'student_quote': evidence, 'errors': tuple(
        e.model_copy(update={'evidence': evidence, 'origin_quote': evidence}) for e in right.errors)})
    attempted = bad if foreign else right
    final = {'revisions': [revision(attempted)]}
    repaired = {'revisions': [revision(right if repair else attempted)]}
    invalid = choice(reference_quote='不存在的标答方法')
    main, calls = provider('primary', *([invalid, invalid] if mode == 'exhausted_arbitration' else []), final, repaired)
    regrader, regrades = provider('a')
    if mode == 'exhausted_arbitration':
        result = await run(p, a, plan, experts, main, {'a': regrader})
    else:
        from backend.services.math_grading_context import math_plans_context
        with math_plans_context({p.q_id: plan}), grading.math_review_scope():
            result = await grading.review_math_experts(p, a, experts, main)
    assert not regrades
    assert [e.score for e in result] == ([8, 16] if foreign and not repair else [16, 16])
    assert all('selected_methods' not in json.loads(call[1].content) for call in calls[-2:])


def test_known_foreign_scope_is_checked_even_if_this_parts_heading_is_missing():
    from backend.skills.math_method_review import validate_method_part_sources
    _, a, plan, _, right, _ = multipart_case()
    answer = a.content.replace('(1) ', '')
    bad = right.model_copy(update={'student_quote': 'student:3'})
    with pytest.raises(ValueError, match='reviewed part'):
        validate_method_part_sources([bad], answer, plan.part_rubric, require_scope=False)
    validate_method_part_sources([right], answer, plan.part_rubric, require_scope=False)


@pytest.mark.parametrize('address', ['student:1:1-3', 'student:1:0-2', 'student:1:1-99', 'student:1:3-2'])
def test_column_addresses_cannot_truncate_tokens_or_escape_the_line(address):
    from backend.skills.part_scoring import _student_span
    assert _student_span('x=10', address) is None


@pytest.mark.parametrize('source', ['Use factorization to solve the equation.', '因式分解得两根',
                                    'Use proof by induction.', '配方法：解方程', 'Use factorization method.'])
def test_affirmative_method_context_remains_available(source):
    assert _has_method_evidence(source)


def test_duplicate_or_unknown_following_heading_does_not_expand_a_known_part():
    from backend.skills.math_method_review import validate_method_part_sources
    _, a, plan, _, right, _ = multipart_case()
    answer = a.content + '\n(2)又一段作答'
    bad = right.model_copy(update={'student_quote': 'student:3'})
    with pytest.raises(ValueError, match='reviewed part'):
        validate_method_part_sources([bad], answer, plan.part_rubric, require_scope=False)


def test_ordinary_review_can_still_cite_shared_work_before_the_first_heading():
    from backend.skills.math_method_review import validate_method_part_sources
    _, a, plan, _, right, _ = multipart_case()
    answer = '先证明两个小问共用的结论。\n' + a.content
    decision = right.model_copy(update={'student_quote': 'student:1', 'errors': ()})
    validate_method_part_sources([decision], answer, plan.part_rubric, require_scope=False)
    with pytest.raises(ValueError, match='reviewed part'):
        validate_method_part_sources([decision], answer, plan.part_rubric)
