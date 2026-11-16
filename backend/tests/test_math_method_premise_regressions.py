"""Part labels are layout, and verified premises do not depend on solution route."""
import pytest

from backend.services.math_grading_context import math_plans_context
from backend.services.math_source_evidence import _labelled_ranges, source_catalog
from backend.skills import math_grading as grading
from backend.tests.test_math_grading import expert_for
from backend.tests.test_math_method_policy_regressions import follow_through_case
from backend.tests.test_math_method_review import multipart_case, provider, revision


def premise_case(body='2x=4', method='reference', *, content=None, evidence=None):
    p, a, plan, experts, second = follow_through_case()
    a = a.model_copy(update={'content': content or '(1)x=2\n(2)' + body})
    original = grading.MathAssessment.model_validate(grading.read_math_trace(experts[0])['assessment'])
    first = original.part_decisions[0].model_copy(update={
        'method': method, 'relevance': 'complete', 'progress': 'Correctly obtained x=2',
        'reference_method': 'solve by substitution', 'student_method': 'solve by elimination',
        'steps': () if method == 'alternative' else tuple(
            s.model_copy(update={'outcome': 'earned', 'reason': 'Correct x=2'}) for s in original.part_decisions[0].steps)})
    original.part_decisions[0] = first
    original.part_decisions[1] = original.part_decisions[1].model_copy(update={
        'relevance': 'unanswered', 'steps': (), 'progress': 'No credit yet'})
    evidence = evidence or 'student:1, student:2'
    second = second.model_copy(update={'relevance': 'complete', 'student_quote': evidence, 'steps': (
        second.steps[0].model_copy(update={'outcome': 'earned', 'evidence': evidence,
                                          'error_source': '', 'reason': 'Doubled the established value'}),)})
    complete = original.model_copy(update={'part_decisions': [first, second]})
    experts = [expert_for(value, plan, a.content).model_copy(update={'provider_id': name})
               for name, value in [('a', original), ('b', complete)]]
    return p, a, plan, experts, original, second


async def assess(p, a, plan, *outputs):
    if len(outputs) == 1:
        outputs = outputs * 2  # An invalid reply may use its one repair attempt.
    model, calls = provider('initial', *(grading.FrozenPartAssessment(
        part_decisions=o.part_decisions, comment='Checked').model_dump(mode='json') for o in outputs))
    skill = grading.MathGradingSkill(model)
    result, _ = await skill._assess(plan, system_prompt=grading.PART_SCORING_INSTRUCTION,
        user_prompt=skill._prompt(p, a, plan, ''), validator=grading._assessment_validator(plan, a, p))
    return grading.score_assessment(result, plan, a.content, problem_text=p.stem)[0], calls


@pytest.mark.asyncio
@pytest.mark.parametrize('body', ['', '  ', '：', '；', '\n  '])
@pytest.mark.parametrize('quote', [False, True])
@pytest.mark.parametrize('stage', ['initial', 'joint'])
async def test_heading_without_work_cannot_turn_a_premise_into_credit(body, quote, stage):
    p, a, plan, experts, original, second = premise_case(body)
    if quote:
        second = second.model_copy(update={'student_quote': a.content, 'steps': (
            second.steps[0].model_copy(update={'evidence': a.content}),)})
    if stage == 'initial':
        candidate = original.model_copy(update={'part_decisions': [original.part_decisions[0], second]})
        score, calls = await assess(p, a, plan, candidate, original)
        assert score == 10 and len(calls) == 2
    else:
        final = {'revisions': [revision(second)]}
        main, calls = provider('primary', final, final)
        with math_plans_context({p.q_id: plan}), grading.math_review_scope():
            result = await grading.review_math_experts(p, a, experts, main)
        assert result[0].score == 10 and len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['reference', 'minor_variant', 'alternative'])
@pytest.mark.parametrize('stage', ['initial', 'joint'])
@pytest.mark.parametrize('evidence', ['student:1, student:2', '(1)x=2\n(2)2x=4'])
async def test_verified_correct_premise_is_independent_of_route(method, stage, evidence):
    p, a, plan, experts, original, second = premise_case(method=method, evidence=evidence)
    if stage == 'initial':
        candidate = original.model_copy(update={'part_decisions': [original.part_decisions[0], second]})
        score, calls = await assess(p, a, plan, candidate)
        assert score == 20 and len(calls) == 1
    else:
        main, calls = provider('primary', {'revisions': [revision(second)]})
        with math_plans_context({p.q_id: plan}), grading.math_review_scope():
            result = await grading.review_math_experts(p, a, experts, main)
        assert result[0].score == 20 and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('formula', ['x=2', 'x^2=4', 'x+1=3'])
@pytest.mark.parametrize('separator', ['；', '; ', '\n', '\r', '\u2028'])
async def test_delimited_formula_heading_keeps_correct_initial_judgment(formula, separator):
    text = '(1)y=1' + separator + '(2)' + formula
    p, a, plan, _, original, second = premise_case(content=text, evidence='student:1')
    from backend.models import ProblemInfo
    from backend.services.rubric_drafts import RubricDefinition, bind_draft, generated_rubric, retarget_draft
    from backend.tests.test_frozen_part_rubric import question_row
    parts = tuple(part.model_copy(update={'stem': f'Establish {goal}', 'steps': (
        part.steps[0].model_copy(update={'criterion': goal}),)})
        for part, goal in zip(plan.part_rubric.parts, ['y=1', formula]))
    row = question_row(plan.part_rubric.model_copy(update={'parts': parts}).model_dump(mode='json'))
    row['criterion'], draft = generated_rubric(row, RubricDefinition(part_rubric={**plan.part_rubric.model_dump(), 'parts': parts}))
    p = ProblemInfo(**row)
    plan = bind_draft(retarget_draft(draft, p), p)
    split = text.index('(2)')
    addresses = ([f'student:1:1-{split}', f'student:1:{split + 1}-{len(text)}']
                 if separator in {'；', '; '} else ['student:1', 'student:2'])
    decisions = [original.part_decisions[0], second]
    for i, address in enumerate(addresses):
        decisions[i] = decisions[i].model_copy(update={'student_quote': address, 'steps': tuple(
            step.model_copy(update={'evidence': address}) for step in decisions[i].steps)})
    candidate = original.model_copy(update={'part_decisions': decisions})
    score, calls = await assess(p, a, plan, candidate)
    assert score == 20 and len(calls) == 1
    catalog = source_catalog(text, '', '', parts=plan.part_rubric.parts)
    assert all(address in catalog for address in addresses)


@pytest.mark.asyncio
@pytest.mark.parametrize('coefficient', ['(2)x=4', '（２）x=4', '(2)x^2=4', '(2)x+1=3'])
@pytest.mark.parametrize('heading', ['(2) 因式分解得两根', '(2)因式分解得两根', '2）因式分解得两根'])
async def test_line_start_coefficient_does_not_displace_clear_later_heading(coefficient, heading):
    p, a, plan, experts, _, _ = multipart_case()
    lines = a.content.splitlines()
    lines.insert(1, coefficient)
    lines[3] = heading
    a = a.model_copy(update={'content': '\n'.join(lines)})
    initial = grading.MathAssessment.model_validate(grading.read_math_trace(experts[1])['assessment'])
    for i, (quote, error) in enumerate([('student:1', 'student:3'), ('student:4', 'student:5')]):
        decision = initial.part_decisions[i]
        initial.part_decisions[i] = decision.model_copy(update={'student_quote': quote, 'errors': tuple(
            event.model_copy(update={'evidence': error, 'origin_quote': error}) for event in decision.errors)})
    score, calls = await assess(p, a, plan, initial)
    assert score == 16 and len(calls) == 1
    ranges = _labelled_ranges(a.content, plan.part_rubric.parts)
    assert ranges['p'][1] == ranges['p2'][0] == a.content.index(heading)


@pytest.mark.parametrize('first,second', [('(2)x=4', '(2)y=2'), ('(2) 作答', '(2) 另一作答')])
def test_unresolved_duplicate_headings_remain_barriers(first, second):
    _, _, plan, _, _, _ = multipart_case()
    text = '(1) 本问\n' + first + '\n' + second
    ranges = _labelled_ranges(text, plan.part_rubric.parts)
    assert 'p2' not in ranges
    assert ranges['p'][1] == text.index(first)


@pytest.mark.asyncio
@pytest.mark.parametrize('evidence,accepted', [('student:1, student:2', False),
                                             ('student:2', False), ('student:1, student:3', True)])
async def test_uncited_body_does_not_turn_a_cited_label_into_work(evidence, accepted):
    p, a, plan, experts, _, second = premise_case(body='\n2x=4', evidence=evidence)
    final = {'revisions': [revision(second)]}
    main, calls = provider('primary', final, final)
    with math_plans_context({p.q_id: plan}), grading.math_review_scope():
        result = await grading.review_math_experts(p, a, experts, main)
    assert result[0].score == (20 if accepted else 10)
    assert len(calls) == (1 if accepted else 2)


@pytest.mark.asyncio
async def test_alternative_with_a_known_error_is_not_a_verified_correct_premise():
    p, a, plan, _, original, second = premise_case(method='alternative')
    error = grading.PartDecision.model_validate({**original.part_decisions[0].model_dump(), 'relevance': 'partial',
        'errors': [dict(id='e', rule_id='alternative', evidence='student:1', origin_quote='student:1',
                        reason='The cited result has not been established')]})
    original.part_decisions[0] = error
    complete = original.model_copy(update={'part_decisions': [error, second]})
    experts = [expert_for(value, plan, a.content).model_copy(update={'provider_id': name})
               for name, value in [('a', original), ('b', complete)]]
    final = {'revisions': [revision(second)]}
    main, calls = provider('primary', final, final)
    with math_plans_context({p.q_id: plan}), grading.math_review_scope():
        result = await grading.review_math_experts(p, a, experts, main)
    assert result[0].score == 9 and len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('uncited_work', [False, True])
async def test_follow_through_needs_cited_work_not_just_a_label(uncited_work):
    p, a, plan, experts, second = follow_through_case()
    a = a.model_copy(update={'content': '(1)x=3\n(2)' + ('\n2x=6' if uncited_work else '')})
    if uncited_work:
        second = second.model_copy(update={'student_quote': 'student:3'})
    final = {'revisions': [revision(second)]}
    main, calls = provider('primary', final, final)
    with math_plans_context({p.q_id: plan}), grading.math_review_scope():
        result = await grading.review_math_experts(p, a, experts, main)
    assert result[0].score == 0 and len(calls) == 2


@pytest.mark.parametrize('relevance', ['partial', 'incorrect'])
def test_partial_alternative_still_requires_work_beyond_the_label(relevance):
    from backend.skills.math_method_review import validate_method_part_sources
    _, a, plan, _, original, second = premise_case(body='')
    second = second.model_copy(update={'student_quote': 'student:2', 'steps': (),
        'method': 'alternative', 'relevance': relevance})
    with pytest.raises(ValueError, match='beyond its heading'):
        validate_method_part_sources([second], a.content, plan.part_rubric, known_decisions=original.part_decisions)


@pytest.mark.parametrize('body,has_content', [('√', True), ('×', True), ('✓', True), ('∅', True),
                                            ('∞', True), ('0', True), ('$$', False), (r'\(\)', False)])
def test_mathematical_symbols_are_content_but_empty_delimiters_are_not(body, has_content):
    from backend.skills.math_method_review import validate_method_part_sources
    _, a, plan, _, original, second = premise_case(body=body)
    if has_content:
        validate_method_part_sources([second], a.content, plan.part_rubric, known_decisions=original.part_decisions)
    else:
        with pytest.raises(ValueError, match='beyond its heading'):
            validate_method_part_sources([second], a.content, plan.part_rubric, known_decisions=original.part_decisions)
