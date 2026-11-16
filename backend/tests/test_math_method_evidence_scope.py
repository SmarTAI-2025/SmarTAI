"""Adversarial and valid evidence must be tested through the same review entry."""
import json
from types import SimpleNamespace
import pytest
from backend.services.part_rubric import AnswerSegment
from backend.skills import math_grading as grading
from backend.skills.math_method_review import _method_passages
from backend.tests.test_math_method_review import case, choice, multipart_case, provider, revision, run


@pytest.mark.asyncio
@pytest.mark.parametrize('teacher', ['正确作答得10分。','参考解配方得10分。'])
@pytest.mark.parametrize('reference,quote', [
    ('无需配方。用因式分解解得 1 和 2。', '配方'),
    ('无需配方。', '配方'),
    ('Do not use substitution. Use factorization.', 'substitution'),
    ('x = 1 y = 2', 'x = 1 y = 2'),
])
async def test_invalid_method_evidence_never_reaches_original_expert(reference, quote, teacher):
    from backend.llm.providers import LLMResponse
    p,a,plan,experts,_=case(reference=reference,teacher=teacher)
    calls=[]
    async def invoke(messages):
        calls.append(messages)
        data=json.loads(messages[1].content)
        response=choice(reference_quote=quote) if 'disputed_part_ids' in data else {'revisions': []}
        return LLMResponse(content=json.dumps(response),provider='offline',model='fixed',duration_ms=1)
    regrader, regrades=provider('a')
    result=await run(p,a,plan,experts,SimpleNamespace(provider_id='primary',ainvoke=invoke),{'a':regrader})
    assert not regrades
    assert [e.score for e in result]==[0,8]
    assert 'disputed_part_ids' not in json.loads(calls[-1][1].content)
    assert 'selected_methods' not in json.loads(calls[-1][1].content)
    assert all(grading.read_math_trace(e)['method_review']['fallback']=='joint_review' for e in result)


@pytest.mark.asyncio
@pytest.mark.parametrize('source_ids,allowed', [(['student:1'],True),(['student:3'],False),(['student:1','student:3'],False)])
async def test_student_sources_must_belong_to_the_disputed_part(source_ids,allowed):
    p,a,plan,experts,right,_=multipart_case()
    selected=choice(student_source_ids=source_ids)
    main,calls=provider('primary',selected,*([{'revisions': []}] if allowed else [selected,{'revisions': []}]))
    regrader,regrades=provider('a',revision(right))
    result=await run(p,a,plan,experts,main,{'a':regrader})
    assert len(regrades)==int(allowed)
    assert [e.score for e in result]==([16,16] if allowed else [8,16])
    assert ('selected_methods' in json.loads(calls[-1][1].content)) is allowed


def test_frozen_answer_links_precede_equation_numbers():
    p,_,plan,_,_,_=multipart_case()
    text='对第一问用配方法解得 1 和 2。\nx=1 (1) 是第一个结果\ny=2 (2) 是第二个结果\n对第二问用因式分解解得 1 和 2。'
    refs=['对第一问用配方法解得 1 和 2。','对第二问用因式分解解得 1 和 2。']
    parts=tuple(part.model_copy(update={'label':f'({chr(97+i)})','answer_refs':(f'a{i}',), 'steps':tuple(
        step.model_copy(update={'criterion':'正确作答','source_quote':'','answer_refs':()}) for step in part.steps)})
        for i,part in enumerate(plan.part_rubric.parts))
    rules=plan.part_rubric.model_copy(update={'parts':parts,'answer_segments':tuple(
        AnswerSegment(id=f'a{i}',text=t,origin='teacher') for i,t in enumerate(refs))})
    assert _method_passages(p.model_copy(update={'reference_answer':text}),rules)=={'p':['正确作答',refs[0]],'p2':['正确作答',refs[1]]}


@pytest.mark.parametrize('source,quote,expected', [
    ('无需配方。用因式分解解得 1 和 2。','配方',False),
    ('无需配方。用因式分解解得 1 和 2。','因式分解',True),
    ('无需配方，用因式分解解得 1 和 2。','因式分解',True),
    ('配方不适用于该参考方法。','配方',False),
    ('Substitution is not the reference method. Use factorization.','Substitution',False),
    ('Use substitution.','substitution',True),
    ('无需推导 x+2=3⇒x=1。','x+2=3⇒x=1',False),
    ('x = 1 y = 2','x = 1 y = 2',False),
    ('x=1, y=2','x=1, y=2',False),
    ('x=1 and y=2','x=1 and y=2',False),
    ('x=1\ny=2','x=1\ny=2',False),
    ('x+2=3⇒x=1。','x+2=3⇒x=1',True),
    ('x+2=3⇒x=1.','x+2=3⇒x=1',True),
    ('x+x=2*x=4','x+x=2*x=4',True),
])
def test_quote_is_validated_at_its_original_context(source,quote,expected):
    from backend.skills.math_method_review import _supported_method_quote
    assert _supported_method_quote(source,quote) is expected


@pytest.mark.asyncio
async def test_equation_numbered_reference_with_frozen_links_completes_reassessment():
    from backend.models import ProblemInfo
    from backend.services.rubric_drafts import RubricDefinition, generated_rubric, bind_draft
    from backend.tests.test_frozen_part_rubric import question_row
    p,a,old_plan,experts,right,_=multipart_case()
    text='用配方法解得 1 和 2。\nx=1 (1) 是第一个结果\ny=2 (2) 是第二个结果\n用因式分解解得 1 和 2。'
    refs=['用配方法解得 1 和 2。','用因式分解解得 1 和 2。']
    parts=tuple(part.model_copy(update={'answer_refs':(f'a{i}',), 'steps':tuple(
        step.model_copy(update={'criterion':'正确作答','source_quote':''}) for step in part.steps)})
        for i,part in enumerate(old_plan.part_rubric.parts))
    rules=old_plan.part_rubric.model_copy(update={'parts':parts,'answer_segments':tuple(
        AnswerSegment(id=f'a{i}',text=t,origin='teacher') for i,t in enumerate(refs))})
    row=question_row(rules.model_dump(mode='json'))
    row.update(reference_answer=text,criterion='每小问正确作答得10分。')
    row['criterion'],draft=generated_rubric(row,RubricDefinition(part_rubric=rules))
    p=ProblemInfo(**row);plan=bind_draft(draft,p)
    for e in experts:
        trace=grading.read_math_trace(e);trace['plan_id']=plan.plan_id;e.raw_output=json.dumps(trace)
    main,calls=provider('primary',choice(),{'revisions':[]})
    regrader,regrades=provider('a',revision(right))
    result=await run(p,a,plan,experts,main,{'a':regrader})
    assert [e.score for e in result]==[16,16]
    assert len(calls)==2 and len(regrades)==1
    passages=json.loads(calls[0][1].content)['method_reference_passages']
    assert passages=={'p':['正确作答',refs[0]]}


@pytest.mark.asyncio
async def test_unknown_student_part_scope_preserves_ordinary_review():
    p,a,plan,experts,_,_=multipart_case()
    a=a.model_copy(update={'content':a.content.replace('(1) ','').replace('(2) ','')})
    main,calls=provider('primary',{'revisions':[]})
    result=await run(p,a,plan,experts,main,{})
    assert len(calls)==1 and [e.score for e in result]==[8,16]
    assert grading.read_math_trace(result[0])['method_review']['status']=='student_scope_unavailable'
    assert 'selected_methods' not in json.loads(calls[0][1].content)


@pytest.mark.parametrize('quote,expected', [
    ('配方',False), ('用因式分解解得 1 和 2。',True),
])
def test_conflicting_context_requires_an_unambiguous_positive_quote(quote,expected):
    from backend.skills.math_method_review import _supported_reference_quote
    assert _supported_reference_quote(['参考解配方得10分。',
        '无需配方。用因式分解解得 1 和 2。'],quote) is expected
