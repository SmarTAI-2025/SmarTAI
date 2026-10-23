"""Whole source operands, not substrings or semantic proof certification."""
from types import SimpleNamespace

import pytest

from backend.services.math_tool_planning import automatic_checks, binding_error
from backend.skills.math_grading import Claim
from backend.tools.math_verification import MathVerificationRequest, verify_math


def context(reference='', stem='核查所提交的算式。', criterion=''):
    return (SimpleNamespace(stem=stem, reference_answer=reference),
            SimpleNamespace(original_criterion=criterion, effective_criterion=criterion))


def check(quote, student, reference, *, reference_text=None, stem=None, criterion='', **kwargs):
    p, plan = context(reference if reference_text is None else reference_text,
                      stem or '核查所提交的算式。', criterion)
    req = MathVerificationRequest(student_expression=student, reference_expression=reference, **kwargs)
    source = p.reference_answer or p.stem
    return binding_error(req, quote, source, problem=p, plan=plan), req


@pytest.mark.parametrize('quote,student,reference,reference_text,kwargs,status', [
    ('pi/4 ≈ 0.7853982。', '0.7853982', 'pi/4', 'pi/4',
     dict(comparison='numeric', allow_approximation=True, absolute_tolerance=1e-6), 'supported'),
    ('exp(I*pi) = 1。', 'exp(I*pi)', '1', '', dict(comparison='numeric'), 'refuted'),
    ("逐项求导：f'(x) = 3*x^2 + 6*x + 3。", '3*x^2+6*x+3', '3*(x+1)^2', '3*(x+1)^2', {}, 'supported'),
    ('F = ma = 2*3 = 5 N。', '5', '6', 'F = 6 N；W = 30 J。',
     dict(comparison='numeric', student_unit='N', reference_unit='N'), 'refuted'),
    ('化简结果：2*(3+4) = 14。', '2*(3+4)', '14', '', {}, 'supported'),
    ('Therefore 7-2=6.', '7-2', '6', '', {}, 'refuted'),
    ("求导结果：g'(y)=2*y。", '2*y', 'y+y', 'y+y', {}, 'supported'),
])
@pytest.mark.asyncio
async def test_complete_source_relation_reaches_worker(quote, student, reference, reference_text, kwargs, status):
    issue, req = check(quote, student, reference, reference_text=reference_text,
                       criterion='允许近似，绝对误差不超过1e-6。', **kwargs)
    assert issue is None
    assert (await verify_math(req)).status == status


@pytest.mark.parametrize('quote,student,reference,kwargs', [
    ('1+2=1+3', '1', '1', {}),
    ('$3 s$；$4 m$', '3', '3', dict(student_unit='m', reference_unit='m')),
    ('3 s', '3', '3', dict(reference_unit='m')),
    ('sin(x+1)=0', 'x+1', '0', {}),
    ('x^20', 'x^2', 'x^2', {}),
    ('1e-3', '1', '1', {}),
    ('2*(3+4)=14', '3+4', '7', {}),
    ('1..', '1', '1', {}),
    ('2+=3=3', '3', '3', {}),
    ('sin(x=0)=1', '0', '1', {}),
    ('5 N=5 N/m', '5', '5', dict(student_unit='m', reference_unit='m')),
])
def test_invented_or_truncated_operands_and_cross_units_remain_rejected(quote, student, reference, kwargs):
    issue, _ = check(quote, student, reference, reference_text=reference+' m' if kwargs else reference, **kwargs)
    assert issue is not None


@pytest.mark.parametrize('quote', [
    'exp(I*pi)=1 不成立。', '$exp(I*pi)$ = $1$ 不成立。',
    '不能得到：2+3=5。', '假设：2+3=5。', '如果2+3=5，则结论成立。',
    '错误结论：2+3=5。', '不正确的化简结果：2+3=5。',
    '欧拉公式给出 exp(I*pi)=1。',  # Unknown prose is not stripped to fit a fixture.
])
def test_negations_hypotheses_and_unknown_prefixes_are_not_discarded(quote):
    p, plan = context()
    c = Claim(id='c', quote=quote, verdict='unknown', reason='Scope control')
    assert automatic_checks([c], p, plan, student_answer=quote) == []
    issue, _ = check(quote, 'exp(I*pi)' if 'exp(' in quote else '2+3',
                       '1' if 'exp(' in quote else '5', reference_text='')
    assert issue == 'student_expression_not_bound'


@pytest.mark.parametrize('quote,student,reference', [
    ('2+3=5；7+2=9', '5', '7+2'),
    ('1+1=2=3+3=6', '1+1', '6'),
    ('3 m=300 cm；4 s=4 s', '3', '4'),
])
def test_only_adjacent_whole_terms_in_student_chain_are_paired(quote, student, reference):
    kwargs = dict(student_unit='m', reference_unit='s') if student == '3' else {}
    issue, _ = check(quote, student, reference, reference_text='', **kwargs)
    assert issue == 'relation_not_bound'


@pytest.mark.parametrize('quote', ['因此 2*3=6。', '计算结果：8/2=4。', '所以 1e-3≈0.001。'])
def test_auto_candidates_use_complete_constant_relations(quote):
    p, plan = context(criterion='允许近似；绝对误差不超过1e-6。')
    claim = Claim(id='c', quote=quote, verdict='unknown', reason='Numeric relation')
    checks = automatic_checks([claim], p, plan, student_answer=quote)
    # A syntactically identical 1e-3/.001 relation is not a useful new check.
    if '1e-3' in quote:
        assert checks == []
    else:
        assert len(checks) == 1
        req = MathVerificationRequest(**checks[0]['request'])
        assert binding_error(req, quote, p.stem, problem=p, plan=plan) is None


def test_approximate_relation_requires_explicit_tolerance_for_auto_check():
    quote = 'pi/4≈0.7853982。'
    p, plan = context()
    claim = Claim(id='c', quote=quote, verdict='unknown', reason='Approximation')
    assert automatic_checks([claim], p, plan, student_answer=quote) == []
    plan.original_criterion = plan.effective_criterion = '允许近似，绝对误差不超过1e-6。'
    checks = automatic_checks([claim], p, plan, student_answer=quote)
    assert len(checks) == 1 and checks[0]['request']['absolute_tolerance'] == 1e-6


@pytest.mark.parametrize('quote', ['x=3', 'F=ma=2*3=5 N。', "f'(x)=3*x^2+6*x+3。"])
def test_variable_labels_and_one_sided_units_are_not_universal_identities(quote):
    p, plan = context()
    claim = Claim(id='c', quote=quote, verdict='unknown', reason='Scope control')
    assert automatic_checks([claim], p, plan, student_answer=quote) == []


@pytest.mark.parametrize('stem', [
    'x>0 is false. Simplify sqrt(x^2).', '如果x>0，求sqrt(x^2)。',
    '已知x>0或y>0。', 'x为实数。', '求sqrt(x^2)，答案中可能要讨论x>0。',
])
def test_missing_or_local_variable_condition_is_never_added(stem):
    issue, _ = check('sqrt(x^2)', 'sqrt(x^2)', 'x', stem=stem, assumptions={'x': {'positive': True}})
    assert issue == 'assumption_not_bound'


@pytest.mark.parametrize('quote', [
    '$2+3$ = $6$。', r'\(2+3\) = \(6\)。', '$$2+3$$ = $$6$$。',
    '计算结果：$2+3$ = $6$。',
])
@pytest.mark.asyncio
async def test_separate_math_delimiters_do_not_masquerade_as_one_outer_fence(quote):
    issue, req = check(quote, '2+3', '6', reference_text='')
    assert issue is None
    assert (await verify_math(req)).status == 'refuted'


def test_precise_reference_formula_quote_keeps_source_binding_despite_unknown_surrounding_prose():
    p, plan = context('该题解析给出的参考表达式为 $2*pi$，可接受其等价形式。')
    request = MathVerificationRequest(student_expression='2*pi', reference_expression='2*pi')
    assert binding_error(request, '$2*pi$', '$2*pi$', problem=p, plan=plan) is None
    p.reference_answer = '结果为 $2*pi$。'
    assert binding_error(request, '$2*pi$', p.reference_answer, problem=p, plan=plan) is None


@pytest.mark.asyncio
async def test_equal_number_with_different_units_is_not_discarded_as_tautology():
    text = '$3 s$ = $3 m$。'
    p, plan = context()
    claim = Claim(id='c', quote=text, verdict='unknown', reason='Unit relation')
    checks = automatic_checks([claim], p, plan, student_answer=text)
    assert len(checks) == 1
    req = MathVerificationRequest(**checks[0]['request'])
    assert binding_error(req, text, p.stem, problem=p, plan=plan) is None
    assert (await verify_math(req)).status == 'refuted'
