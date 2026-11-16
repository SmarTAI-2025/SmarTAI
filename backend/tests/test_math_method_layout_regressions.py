"""Source layout variants must preserve both scope and usable method evidence."""
import json

import pytest

from backend.services.math_grading_context import math_plans_context
from backend.services.math_source_evidence import source_catalog
from backend.skills import math_grading as grading
from backend.skills.math_method_review import _has_method_evidence, _supported_method_quote, _labelled_ranges
from backend.skills.part_scoring import _student_span
from backend.tests.test_math_method_review import case, choice, multipart_case, provider, revision, run
from backend.tests.test_math_method_boundary_regressions import linked_case
from backend.tests.test_math_method_policy_regressions import follow_through_case


LAYOUTS = [('2）', '\n'), ('２）', '\n'), ('(2)', '\r'), ('(2)', '\u2028'),
           ('(2)', '\u2029'), ('2）', '\r\n')]


def layout_case(label, separator):
    p, a, plan, experts, right, other = multipart_case()
    a = a.model_copy(update={'content': a.content.replace('(2) ', label).replace('\n', separator)})
    return p, a, plan, experts, right, other


@pytest.mark.parametrize('separator', ['\n', '\r\n', '\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'])
def test_source_lines_and_part_boundaries_share_all_line_separators(separator):
    p, a, plan, _, _, _ = layout_case('2）', separator)
    ranges = _labelled_ranges(a.content, plan.part_rubric.parts)
    assert ranges['p'][1] == ranges['p2'][0] == a.content.index('2）')
    for key, (text, start, end) in source_catalog(a.content, '', '').items():
        span = _student_span(a.content, key)
        assert (span.text, span.start, span.end) == (text, start, end)
        assert not text.endswith(separator)
        part = 'p' if int(key.split(':')[1]) < 3 else 'p2'
        assert ranges[part][0] <= start < end <= ranges[part][1]


@pytest.mark.asyncio
@pytest.mark.parametrize('label,separator', LAYOUTS)
@pytest.mark.parametrize('stage', ['arbitration', 'reassessment', 'ordinary', 'final'])
async def test_layout_does_not_authorize_foreign_evidence(label, separator, stage):
    p, a, plan, experts, right, _ = layout_case(label, separator)
    bad = right.model_copy(update={'student_quote': 'student:3', 'errors': tuple(
        e.model_copy(update={'evidence': 'student:4', 'origin_quote': 'student:4'}) for e in right.errors)})
    selected = choice(student_source_ids=['student:3']) if stage == 'arbitration' else choice()
    final = {'revisions': [revision(bad)]} if stage in {'ordinary', 'final'} else {'revisions': []}
    main, calls = provider('primary', *([] if stage == 'ordinary' else
                          [selected, selected] if stage == 'arbitration' else [selected]), final, final)
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
@pytest.mark.parametrize('label,separator', LAYOUTS)
async def test_correct_initial_judgment_is_accepted_without_repair(label, separator):
    p, a, plan, experts, _, _ = layout_case(label, separator)
    initial = grading.MathAssessment.model_validate(grading.read_math_trace(experts[1])['assessment'])
    output = grading.FrozenPartAssessment(part_decisions=initial.part_decisions, comment='Checked').model_dump(mode='json')
    model, calls = provider('initial', output)
    skill = grading.MathGradingSkill(model)
    result, _ = await skill._assess(plan, system_prompt=grading.PART_SCORING_INSTRUCTION,
        user_prompt=skill._prompt(p, a, plan, ''), validator=grading._assessment_validator(plan, a, p))
    assert len(calls) == 1
    assert grading.score_assessment(result, plan, a.content, problem_text=p.stem)[0] == 16


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['配方解法无效；改用因式分解', '配方解法属于错误选项；改用因式分解',
                                  '配方求解毫无意义；改用因式分解'])
async def test_negative_predicate_after_method_cannot_regrade(source):
    assert not _supported_method_quote(source, '配方')
    assert _supported_method_quote(source, '因式分解')
    p, a, plan, experts, right = case(reference=source, teacher='正确作答得10分。')
    invalid = choice(reference_quote='配方')
    main, calls = provider('primary', invalid, invalid, {'revisions': []})
    regrader, regrades = provider('a', revision(right))
    result = await run(p, a, plan, experts, main, {'a': regrader})
    assert not regrades and [e.score for e in result] == [0, 8]
    assert 'selected_methods' not in json.loads(calls[-1][1].content)


@pytest.mark.parametrize('separator', ['\n', '\r', '\u2028', '\u2029'])
def test_explicit_part_heading_ends_context_but_soft_wrap_keeps_negation(separator):
    source = '(1) 用配方法解得1和2' + separator + '(2) 用因式分解解得1和2'
    assert _supported_method_quote(source, '配方')
    assert _supported_method_quote(source, '因式分解')
    assert not _supported_method_quote('不要使用' + separator + '配方法', '配方')
    assert not _supported_method_quote('Never use' + separator + 'substitution', 'substitution')


@pytest.mark.parametrize('source', ['因式分解：先把二次式写成两个一次因式', '配方法：解方程',
                                  '用配方法解得1和2', '因式分解得两根'])
def test_affirmative_instruction_is_supported(source):
    assert _has_method_evidence(source)


@pytest.mark.asyncio
async def test_second_frozen_reference_without_punctuation_can_align():
    p, a, plan, experts, right = linked_case('\n')
    # Only the second part disagrees. Its frozen reference must retain its own
    # context despite the preceding part having no sentence-final punctuation.
    first = grading.read_math_trace(experts[1])['assessment']['part_decisions'][0]
    for expert in experts:
        trace = grading.read_math_trace(expert)
        trace['assessment']['part_decisions'][0] = first
        trace['assessment']['part_decisions'][1]['method'] = 'reference' if expert.provider_id == 'a' else 'alternative'
        expert.raw_output = json.dumps(trace)
    second = grading.MathAssessment.model_validate(grading.read_math_trace(experts[1])['assessment']).part_decisions[1]
    selected = choice(part_id='p2', student_source_ids=['student:3'], reference_quote='因式分解')
    main, calls = provider('primary', selected, {'revisions': []})
    regrader, regrades = provider('a', revision(second))
    result = await run(p, a, plan, experts, main, {'a': regrader})
    assert len(regrades) == 1 and len(calls) == 2
    assert grading.read_math_trace(result[0])['method_review']['status'] == 'reassessed'


FORMULAS = [r'2x=4\Rightarrow x=2', r'2x=4\Rightarrow x=\frac{4}{2}',
            r'2x=4⇒x=\frac{4}{2}', r'\(2x=4\Rightarrow x=\frac{4}{2}\)',
            r'$2x=4\Rightarrow x=\frac{4}{2}$', r'\frac{x}{2}=1\Rightarrow x=2',
            r'x^{2}-3x+2=0\Rightarrow (x-1)(x-2)=0']


@pytest.mark.asyncio
@pytest.mark.parametrize('formula', FORMULAS)
async def test_original_latex_derivation_aligns_without_repair(formula):
    assert _has_method_evidence(formula)
    p, a, plan, experts, right = case(same_total=True)
    a = a.model_copy(update={'content': formula})
    main, calls = provider('primary', choice(), {'revisions': []})
    regrader, regrades = provider('a', revision(right))
    result = await run(p, a, plan, experts, main, {'a': regrader})
    assert len(calls) == 2 and len(regrades) == 1
    assert [e.score for e in result] == [10, 10]


@pytest.mark.parametrize('formula', [r'2x=4 x=\frac{4}{2}', r'2x=4\Rightarrow x=\frac{4}',
                                     r'2x=4\Rightarrow x=\unknown{2}', r'x=\frac{4}{2}',
                                     r'不要使用2x=4\Rightarrow x=2'])
def test_tex_does_not_erase_unsupported_syntax_or_negation(formula):
    assert not _has_method_evidence(formula)


@pytest.mark.asyncio
@pytest.mark.parametrize('same_total', [False, True])
@pytest.mark.parametrize('final_change', [False, True])
async def test_method_rationale_is_visible_even_when_final_review_abstains(same_total, final_change):
    p, a, plan, experts, right = case(same_total=same_total)
    selected = choice()
    final = {'revisions': [revision(right)] if final_change else []}
    main, _ = provider('primary', selected, final)
    regrader, _ = provider('a', revision(right))
    result = await run(p, a, plan, experts, main, {'a': regrader})
    reason = selected['choices'][0]['reason']
    assert reason in result[0].comment
    assert reason not in result[1].comment  # This expert's route did not change.
    assert len(result[0].steps) == 1
    assert reason not in result[0].steps[0].desc
    assert result[0].score == (10 if same_total else 8)
    assert grading._with_pending_advice(result[0], 'zh').comment.count(reason) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('body', ['用等式 (2) 代入。', '系数为 (2)x。', '讨论情形 (1) 正数与 (2) 负数。'])
async def test_inline_references_do_not_displace_actual_part_headings(body):
    p, a, plan, experts, _, _ = multipart_case()
    lines = a.content.splitlines()
    lines[0] += body
    a = a.model_copy(update={'content': '\n'.join(lines)})
    ranges = _labelled_ranges(a.content, plan.part_rubric.parts)
    assert ranges['p'][1] == ranges['p2'][0] == a.content.index('\n(2)') + 1
    initial = grading.MathAssessment.model_validate(grading.read_math_trace(experts[1])['assessment'])
    output = grading.FrozenPartAssessment(part_decisions=initial.part_decisions, comment='Checked').model_dump(mode='json')
    model, calls = provider('initial', output)
    skill = grading.MathGradingSkill(model)
    result, _ = await skill._assess(plan, system_prompt=grading.PART_SCORING_INSTRUCTION,
        user_prompt=skill._prompt(p, a, plan, ''), validator=grading._assessment_validator(plan, a, p))
    assert len(calls) == 1 and grading.score_assessment(result, plan, a.content, problem_text=p.stem)[0] == 16


@pytest.mark.asyncio
@pytest.mark.parametrize('alias', ['line', 'columns', 'quote'])
async def test_same_error_alias_cannot_add_a_second_deduction(alias):
    p, a, plan, experts, right = case()
    a = a.model_copy(update={'content': a.content + '  \t'})
    line = a.content.splitlines()[1].rstrip()
    origin = {'line': 'student:2', 'columns': f'student:2:1-{len(line)}', 'quote': line}[alias]
    duplicate = right.errors[0].model_copy(update={'id': 'duplicate', 'origin_quote': origin})
    bad = right.model_copy(update={'errors': (*right.errors, duplicate)})
    experts[0] = experts[1].model_copy(deep=True, update={'provider_id': 'a'})
    trace = grading.read_math_trace(experts[0])
    trace['assessment']['needs_review'] = True
    experts[0].raw_output = json.dumps(trace)
    assessment = grading.MathAssessment.model_validate(trace['assessment'])
    assessment.part_decisions[0] = bad
    final = {'revisions': [dict(expert_index=0, reason='Two claimed errors.', source_quote=line,
                                assessment=assessment.model_dump(mode='json'))]}
    main, calls = provider('primary', final, final)
    with math_plans_context({p.q_id: plan}), grading.math_review_scope():
        result = await grading.review_math_experts(p, a, experts, main)
    assert result[0].score == 8
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('alias', ['line', 'columns', 'quote'])
@pytest.mark.parametrize('reverse', [False, True])
async def test_verified_follow_through_uses_the_same_source_identity(alias, reverse):
    p, a, plan, experts, second = follow_through_case()
    a = a.model_copy(update={'content': '(1)x=3  \t\n(2)2x=6'})
    origin = {'line': 'student:1', 'columns': 'student:1:1-6', 'quote': '(1)x=3'}[alias]
    if reverse:
        for expert in experts:
            trace = grading.read_math_trace(expert)
            trace['assessment']['part_decisions'][0]['steps'][0]['evidence'] = origin
            expert.raw_output = json.dumps(trace)
    else:
        second = second.model_copy(update={'steps': (second.steps[0].model_copy(update={'error_source': origin}),)})
    main, calls = provider('primary', {'revisions': [revision(second)]})
    with math_plans_context({p.q_id: plan}), grading.math_review_scope():
        result = await grading.review_math_experts(p, a, experts, main)
    assert result[0].score == 5 and len(calls) == 1


def test_source_identity_preserves_occurrence_and_subexpression_boundaries():
    from backend.services.math_source_evidence import source_identity
    text = '  $x=3$  \n  $x=3$  '
    first = _student_span(text, 'student:1')
    bare = _student_span(text, 'student:1:4-6')
    assert source_identity(first) == source_identity(bare)
    assert source_identity(first) != source_identity(_student_span(text, 'student:2'))
    assert source_identity(first) != source_identity(_student_span(text, 'student:1:6-6'))


@pytest.mark.asyncio
@pytest.mark.parametrize('evidence', ['student:1, student:2', '(1)x=2\n(2)2x=4', 'student:1'])
@pytest.mark.parametrize('verified', [False, True])
async def test_correct_earlier_premise_still_requires_this_parts_work(evidence, verified):
    p, a, plan, experts, second = follow_through_case()
    a = a.model_copy(update={'content': '(1)x=2\n(2)2x=4'})
    for expert in experts:
        trace = grading.read_math_trace(expert)
        first = trace['assessment']['part_decisions'][0]
        first['relevance'] = 'complete' if verified else 'partial'
        first['steps'][0]['outcome'] = 'earned' if verified else 'missed'
        expert.raw_output = json.dumps(trace)
        expert.score = 10 if verified else 0
    second = second.model_copy(update={'relevance': 'complete', 'student_quote': evidence, 'steps': (
        second.steps[0].model_copy(update={'outcome': 'earned', 'evidence': evidence, 'error_source': ''}),)})
    final = {'revisions': [revision(second)]}
    main, calls = provider('primary', final, final)
    with math_plans_context({p.q_id: plan}), grading.math_review_scope():
        result = await grading.review_math_experts(p, a, experts, main)
    assert result[0].score == (20 if verified and evidence != 'student:1' else 10 if verified else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('precise', [False, True])
async def test_proof_audit_uses_the_catalog_supplied_to_initial_grading(precise, monkeypatch):
    from unittest.mock import AsyncMock
    from backend.skills.proof import ProofSkill
    from backend.tests.test_math_method_policy_regressions import same_line_case
    from backend.tests.test_math_proof_audit import notes
    from backend.models import ProblemInfo
    from backend.services.rubric_drafts import generated_rubric, RubricDefinition, bind_draft, retarget_draft
    monkeypatch.setattr(grading.kb_tool, 'retrieve_for_grading', AsyncMock(return_value=[]))
    p, a, plan, experts, right, address = same_line_case()
    p = p.model_copy(update={'type': '推理题'})
    row = p.model_dump()
    row['criterion'], frozen = generated_rubric(row, RubricDefinition(part_rubric=plan.part_rubric))
    p = ProblemInfo(**row)
    plan = bind_draft(retarget_draft(frozen, p), p)
    draft = grading.MathAssessment.model_validate(grading.read_math_trace(experts[1])['assessment'])
    second = f'student:1:{a.content.index("(2)") + 1}-{len(a.content)}'
    for index, key in enumerate([address, second]):
        draft.part_decisions[index] = draft.part_decisions[index].model_copy(update={
            'student_quote': key, 'errors': tuple(e.model_copy(update={'evidence': key, 'origin_quote': key})
                                                 for e in draft.part_decisions[index].errors)})
    claim = grading.Claim(id='s1', source_id=address, verdict='supported', reason='Submitted derivation', item_ids=[])
    output = grading.FrozenPartAssessment(part_decisions=draft.part_decisions, claims=[claim], comment='Initial')
    updated = output.model_copy(deep=True)
    updated.part_decisions[0] = right.model_copy(update={'relevance': 'complete', 'errors': ()})
    audit = notes(effect='unresolved', inference='invalid', concern='核对本问已经写出的等价变换')
    audit['steps'][0]['premises'][0]['source_ids'] = [address if precise else 'student:1']
    wire = output.model_dump_json() + '\nPROOF_AUDIT\n' + json.dumps(audit)
    model, calls = provider('a', wire, updated.model_dump(mode='json'))
    with math_plans_context({p.q_id: plan}), grading.math_review_scope():
        result = await ProofSkill(model).grade(p, a)
    assert len(calls) == 2 and result.score == 18 and not result.error_kind
    assert address in json.loads(calls[0][1].content.split('INPUT DATA:\n')[1])['source_catalog']
    assert grading.read_math_trace(result)['proof_audit']['review_concerns']


@pytest.mark.asyncio
@pytest.mark.parametrize('address_only', [False, True])
async def test_part_conversion_keeps_resolved_source_for_arithmetic_verification(address_only, monkeypatch):
    from unittest.mock import AsyncMock
    from backend.skills.calculation import CalculationSkill
    monkeypatch.setattr(grading.kb_tool, 'retrieve_for_grading', AsyncMock(return_value=[]))
    p, a, plan, experts, right = case(same_total=True)
    a = a.model_copy(update={'content': '2+2=5'})
    claim = grading.Claim(id='s1', source_id='student:1' if address_only else '',
                          quote='' if address_only else a.content, verdict='supported', reason='Submitted equality', item_ids=[])
    output = grading.FrozenPartAssessment(part_decisions=[right], claims=[claim], comment='Initial')
    model, calls = provider('a', output.model_dump(mode='json'))
    with math_plans_context({p.q_id: plan}), grading.math_review_scope():
        grading.defer_local_review(True)
        result = await CalculationSkill(model).grade(p, a)
    trace = grading.read_math_trace(result)
    assert len(calls) == 1 and not result.error_kind
    assert trace['assessment']['claims'][0]['quote'] == a.content
    assert trace['evidence'] and trace['evidence'][0]['status'] == 'refuted'
    assert trace['review_pending']


@pytest.mark.asyncio
@pytest.mark.parametrize('origin', ['ai', 'teacher'])
@pytest.mark.parametrize('foreign_quote', [False, True])
async def test_missing_ai_supplement_does_not_hide_this_parts_teacher_answer(origin, foreign_quote):
    from backend.models import ProblemInfo
    from backend.services.rubric_drafts import generated_rubric, RubricDefinition, bind_draft, retarget_draft
    p, a, plan, experts, right = linked_case('\n')
    rules = plan.part_rubric.model_copy(update={'answer_segments': tuple(
        segment.model_copy(update={'text': 'AI补充的辅助说明，不是教师标答原文。' if origin == 'ai' else '正确作答', 'origin': origin})
        if segment.id == 'a0' else segment for segment in plan.part_rubric.answer_segments)})
    row = p.model_dump()
    if origin == 'teacher':
        row['reference_answer'] = row['reference_answer'].replace('(1) ', '(1) 正确作答。')
    row['criterion'], frozen = generated_rubric(row, RubricDefinition(part_rubric=rules))
    p = ProblemInfo(**row)
    plan = bind_draft(retarget_draft(frozen, p), p)
    for expert in experts:
        trace = grading.read_math_trace(expert)
        trace['plan_id'] = plan.plan_id
        expert.raw_output = json.dumps(trace)
    selected = choice(reference_quote='因式分解' if foreign_quote else '配方')
    main, calls = provider('primary', *([selected, selected] if foreign_quote else [selected]) if origin == 'ai' else [],
                           {'revisions': []})
    regrader, regrades = provider('a', revision(right))
    result = await run(p, a, plan, experts, main, {'a': regrader})
    allowed = origin == 'ai' and not foreign_quote
    assert len(regrades) == int(allowed)
    assert [e.score for e in result] == ([16, 16] if allowed else [8, 16])
