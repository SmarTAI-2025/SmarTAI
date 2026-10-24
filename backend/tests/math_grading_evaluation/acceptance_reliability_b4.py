"""Independent B4 increment: raw policy JSON and whole-relation source scope.

Expectations are authored before execution. No production helpers are replaced;
worker checks execute locally, with disposable settings and network denied.
"""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from backend.llm.providers import LLMResponse
from backend.services.math_rubric_plan import prepare_math_rubric_plan
from backend.services.math_tool_planning import automatic_checks, binding_error
from backend.skills.math_grading import Claim
from backend.tools.math_verification import MathVerificationRequest, verify_math
from tools.evaluation.math_grading.reliability import no_network


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    monkeypatch.setattr('socket.socket.connect', no_network)
    monkeypatch.setattr('socket.create_connection', no_network)


def context(reference='', criterion=''):
    return (SimpleNamespace(stem='独立核查给出的关系。', reference_answer=reference),
            SimpleNamespace(original_criterion=criterion, effective_criterion=criterion))


@pytest.mark.parametrize('quote,student,reference,units,status', [
    ('所以：$7+8$=$14$。', '7+8', '14', {}, 'refuted'),
    (r'计算结果：\(9-4\)=\(5\)。', '9-4', '5', {}, 'supported'),
    ('结果为 8 cm=0.08 m。', '8', '0.08', {'student_unit':'cm','reference_unit':'m'}, 'supported'),
    ('因此：2 s=2 m。', '2', '2', {'student_unit':'s','reference_unit':'m'}, 'refuted'),
    ('化简结果：2*(5+1)=12=3*4。', '12', '3*4', {}, 'supported'),
    ('f(2)=3。', 'f(2)', '3', {}, 'unknown'),
])
def test_complete_bound_relation_reaches_real_worker(quote, student, reference, units, status):
    problem, plan = context()
    request = MathVerificationRequest(student_expression=student, reference_expression=reference, **units)
    assert binding_error(request, quote, problem.stem, problem=problem, plan=plan) is None
    assert asyncio.run(verify_math(request)).status == status


@pytest.mark.parametrize('quote,student,reference,units', [
    ('2*(5+1)=12', '5+1', '12', {}),
    ('cos(t+2)=0', 't+2', '0', {}),
    ('x^21=1', 'x^2', '1', {}),
    ('0.123456789012345678901=1', '0.123456789012345678902', '1', {}),
    ('8 cm=0.08 m', '8', '0.08', {}),
    ('3+4=7；8+1=9', '7', '8+1', {}),
    ('1+2=3=4+5=9', '1+2', '9', {}),
    ('2+=3=3', '3', '3', {}),
    ('$7+8=16', '7+8', '16', {}),
    ('7+8=16 不成立。', '7+8', '16', {}),
    ('某定理声称7+8=16。', '7+8', '16', {}),
    ('假设7+8=16。', '7+8', '16', {}),
])
def test_truncated_or_unasserted_source_is_rejected(quote, student, reference, units):
    problem, plan = context()
    request = MathVerificationRequest(student_expression=student, reference_expression=reference, **units)
    assert binding_error(request, quote, problem.stem, problem=problem, plan=plan) is not None


@pytest.mark.parametrize('quote', [
    '不能推出以下等式：\n7+8=16。',
    '假设如下等式成立：\n7+8=16。',
    '7+8=16\n不成立。',
    'Assume\n7+8=16.',
])
def test_line_break_does_not_remove_negation_or_hypothesis_scope(quote):
    # The same complete claim includes its scope; a newline does not authorize
    # treating the embedded relation as an unqualified student assertion.
    problem, plan = context()
    claim = Claim(id='scoped', quote=quote, verdict='unknown', reason='保留整个原文范围')
    checks = automatic_checks([claim], problem, plan, student_answer=quote)
    request = MathVerificationRequest(student_expression='7+8', reference_expression='16')
    issue = binding_error(request, quote, problem.stem, problem=problem, plan=plan)
    assert checks == [] and issue is not None, {'automatic_checks':checks, 'binding_error':issue}


@pytest.mark.parametrize('criterion,expected_count', [('', 0), ('允许近似，绝对误差不超过1e-4。', 1)])
def test_approximate_relation_uses_only_sourced_precision(criterion, expected_count):
    quote = '计算结果：pi≈3.1416。'
    problem, plan = context(criterion=criterion)
    claim = Claim(id='approx', quote=quote, verdict='unknown', reason='保留近似关系')
    checks = automatic_checks([claim], problem, plan, student_answer=quote)
    assert len(checks) == expected_count
    if checks:
        request = MathVerificationRequest(**checks[0]['request'])
        assert request.absolute_tolerance == 1e-4 and request.allow_approximation
        assert binding_error(request, quote, problem.stem, problem=problem, plan=plan) is None
        assert asyncio.run(verify_math(request)).status == 'supported'


def test_chain_schedules_only_adjacent_complete_equalities():
    quote = '化简结果：2*(5+1)=12=3*4。'
    problem, plan = context()
    claim = Claim(id='chain', quote=quote, verdict='unknown', reason='每条相邻关系分别检查')
    checks = automatic_checks([claim], problem, plan, student_answer=quote)
    assert len(checks) == 2
    observed = set()
    for check in checks:
        request = MathVerificationRequest(**check['request'])
        observed.add((request.student_expression, request.reference_expression))
        assert binding_error(request, quote, problem.stem, problem=problem, plan=plan) is None
        assert asyncio.run(verify_math(request)).status == 'supported'
    assert observed == {('12', '2*(5+1)'), ('3*4', '12')}


RULE = '每一处独立的重要理解错误、推导错误或未完成的明确主要证明要求扣1分'
POLICIES = [
    '仅遗漏特殊情况、局部符号笔误或端点问题等小错误，只指出，不扣分。',
    '不同的正确思路、简洁等价证明均接受，不按参考答案逐步对照扣分。',
]


def policy_payload(quotes):
    return dict(effective_criterion=RULE+'；'+''.join(POLICIES), routes=[
        dict(id='blank', description='空白', applies_when='空白作答', mode='holistic', items=[],
             minimum_score=0, minimum_condition='always'),
        dict(id='answered', description='非空作答', applies_when='非空作答', mode='deductions',
             items=[dict(id='major', description='独立主要错误', source='provided', source_quote=RULE,
                         points=1, max_occurrences=None)], minimum_score=0, minimum_condition='always',
             exemptions=[dict(id=f'e{i}', description=q, source_quote=q) for i,q in enumerate(quotes)])],
        follow_through='按教师规则处理独立错误', numerical_policy='不增加容差', advisories=[])


class FixedJSON:
    provider_id = 'independent-b4-no-network'
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = 0
    async def ainvoke(self, messages, **kwargs):
        value = self.payloads[min(self.calls, len(self.payloads)-1)]
        self.calls += 1
        return LLMResponse(content=json.dumps(value, ensure_ascii=False), provider='test', input_tokens=0,
                           output_tokens=0, duration_ms=0, model='fixed-json')


def compile_policy(provider):
    return asyncio.run(prepare_math_rubric_plan(dict(q_id='independent-b4', number='1', type='证明题',
        stem='证明题独立合成样例。', reference_answer='完整证明。', max_score=10,
        criterion=RULE+'；'+''.join(POLICIES)), provider))


@pytest.mark.parametrize('repair', [False, True])
def test_full_punctuated_policies_preserve_two_routes_and_one_bounded_repair(repair):
    fixed = policy_payload(POLICIES)
    first = deepcopy(fixed)
    if repair:
        first['routes'][1]['items'] = [{**first['routes'][1]['items'][0], 'id':f'r{i}'} for i in range(3)]
    provider = FixedJSON(first, fixed)
    plan = compile_policy(provider)
    assert provider.calls == (2 if repair else 1)
    assert plan.compilation_status == ('repaired' if repair else 'compiled')
    assert [r.mode for r in plan.routes] == ['holistic', 'deductions']
    route = plan.routes[1]
    assert len(route.items) == 1 and route.items[0].points == 1 and route.items[0].max_occurrences is None
    assert [e.source_quote for e in route.exemptions] == [p[:-1] for p in POLICIES]
    source = RULE+'；'+''.join(POLICIES)
    locations = [loc for loc in plan.source_locations if loc.kind == 'exemption']
    assert len(locations) == 2
    assert {source[loc.start:loc.end] for loc in locations} == {p[:-1] for p in POLICIES}


@pytest.mark.parametrize('bad_quote', ['只指出，不扣分。', '不按参考答案逐步对照扣分。',
                                     POLICIES[0]+POLICIES[1]])
def test_policy_alignment_never_accepts_a_removed_condition_or_joined_policies(bad_quote):
    provider = FixedJSON(policy_payload([bad_quote]))
    plan = compile_policy(provider)
    assert plan.compilation_status != 'compiled'
    assert all(route.mode == 'holistic' for route in plan.routes)
    assert plan.compilation_reason == 'exemption_not_in_source'
    assert provider.calls == 2


def test_layout_break_preserves_independent_complete_formula_lines():
    quote = '7+8=15\n6*3=18。'
    problem, plan = context()
    claim = Claim(id='independent_lines', quote=quote, verdict='unknown', reason='两个独立完整算式')
    checks = automatic_checks([claim], problem, plan, student_answer=quote)
    assert len(checks) == 2
    observed = set()
    for check in checks:
        request = MathVerificationRequest(**check['request'])
        observed.add((request.student_expression, request.reference_expression))
        assert binding_error(request, quote, problem.stem, problem=problem, plan=plan) is None
        assert asyncio.run(verify_math(request)).status == 'supported'
    assert observed == {('15', '7+8'), ('18', '6*3')}
