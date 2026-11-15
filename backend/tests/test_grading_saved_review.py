"""Paid initial votes survive pause, public details and resume without repeat calls."""
import pytest
import asyncio
import json
from types import SimpleNamespace

from backend.tests.test_task_grading_retry import case, start
from backend.tests.test_grading_checkpoints import prepare, retry, stop
from backend.db import provider_repository, workflow_repository as workflows, grading_repository as grades
from backend.models import ProviderConfig, ExpertResult, TaskGradingSetup, StudentAnswerInfo
from backend.services import task_facade, math_grading_context as context, math_rubric_plan as rubric
from backend.services.task_execution import continue_manual_grading
from backend.skills.math_grading import MathAssessment, TRACE_VERSION


@pytest.mark.parametrize("confidence", [.95, None, 0])
def test_two_saved_votes_pause_manual_details_and_resume_only_review(case, monkeypatch, confidence):
    task_id, owner = case
    flow = workflows.get_live_workflow(task_id, owner_id=owner)
    first = flow.grading_setup['selected_provider_ids'][0]
    second = provider_repository.upsert_provider_config(owner, ProviderConfig(
        provider_type='openai', api_key='synthetic-not-a-key', model='synthetic-second',
        base_url='https://api.openai.com/v1'),
        master_key='test-suite-provider-master-key-0123456789abcdef').id
    config={**flow.grading_setup,'selected_provider_ids':[first,second],
            'primary_provider_id':first,'aggregation_method':'weighted_average',
            'expert_weights':{first:1,second:1}}
    workflows.update_workflow(task_id,owner_id=owner,grading_setup=config)
    run_id=start(case,'two-initial-votes')['job_id']
    checkpoints, questions, revisions, _ = prepare(case,run_id)
    question=questions[0]; student=revisions[0][1]
    problem=context.problem_from_question(question)
    plan=rubric.fallback_math_rubric_plan(problem)
    checkpoints.save_math_plan(problem,plan)
    for pid,score in [(first,8),(second,6)]:
        assessment=MathAssessment(route_id='holistic',route_reason='Original rubric.',
            score=score,confidence=confidence,comment='Synthetic saved assessment.',claims=[])
        trace={'version':TRACE_VERSION,'plan_id':plan.plan_id,
               'assessment':assessment.model_dump(mode='json'),
               'review_pending':True,'relevance_review_pending':True,'review_advisory':True,'evidence':[]}
        checkpoints.save_expert(student,question.q_id,pid,0,ExpertResult(
            provider=pid,provider_id=pid,score=score,max_score=10,confidence=confidence,
            comment='Synthetic saved assessment.',steps=[],raw_output=json.dumps(trace)))
    assert grades.list_results_for_run(run_id)==[]
    stop(case,run_id)
    row=next(x for x in grades.list_results_for_run(run_id) if x.q_id==question.q_id)
    assert row.ai_score==7
    assert len(row.ai_expert_results)==2
    assert 'grading_paused' in row.initial_review_reasons
    assert all(not x.get('error_kind') for x in row.ai_expert_results)
    revision=workflows.get_live_workflow(task_id,owner_id=owner).workflow_revision
    continue_manual_grading(task_id=task_id,owner_id=owner,job_id=run_id,expected_revision=revision)
    public=task_facade.task_results(task_id=task_id,owner_id=owner)
    correction=next(x for x in public['results'][0]['corrections'] if x['q_id']==question.q_id)
    assert correction['score']==7
    assert [x['score'] for x in correction['expert_results']]==[8,6]
    assert all(x['comment']=='Synthetic saved assessment.' for x in correction['expert_results'])
    assert all(x['raw_output'] is None for x in correction['expert_results'])
    resumed=retry(case,'resume-after-two-votes')
    restored,_,_,_=prepare(case,resumed,'resumed-worker')
    assert len(restored.saved)==2
    from backend.agents import multi_expert
    from backend.skills import math_grading
    providers={pid:SimpleNamespace(provider_id=pid) for pid in (first,second)}
    registry=SimpleNamespace(list_available=lambda:list(providers.values()),get=lambda key:providers.get(key))
    class NoInitialCall:
        name='synthetic'; uses_math_evidence=True
        def __init__(self,*args,**kwargs):pass
        async def grade(self,*args,**kwargs):
            raise AssertionError('Saved initial vote must not be repeated')
    review_calls=[]
    async def fake_review(problem,answer,experts,provider,**kwargs):
        review_calls.append(len(experts)); return experts
    monkeypatch.setattr(multi_expert,'get_skill_for_type',lambda _:NoInitialCall)
    monkeypatch.setattr(math_grading,'review_math_experts',fake_review)
    result=asyncio.run(restored.run(multi_expert.run_multi_expert(
        problem=problem,answer=StudentAnswerInfo(q_id=question.q_id,number='1',type='concept',content='Synthetic answer.'),
        student_id=student,registry=registry,grading_setup=TaskGradingSetup.model_validate(config),
        aggregation_method='weighted_average')))
    assert result.score==7
    assert review_calls==[2]
