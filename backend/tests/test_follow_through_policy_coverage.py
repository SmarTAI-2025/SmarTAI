"""Unparsed teacher follow-through requirements cannot silently become zero credit."""
import json
from pathlib import Path

import pytest

from backend.domain.errors import ValidationError
from backend.services.rubric_drafts import (
    RubricDefinition, compile_parts_draft, generated_rubric, saved_draft,
)
from backend.services.teacher_point_allocations import TeacherPolicyUnresolved
from backend.skills.part_scoring import PartDecision, settle_parts
from backend.tests.test_question_material_format_repair import Provider
from backend.tests.test_teacher_frozen_allocations import make_row, proposed
from backend.tests.test_teacher_policy_audit import PolicyProvider

BEFORE = json.loads((Path(__file__).parent / 'fixtures/part_rubrics/'
                     'follow-through-omitted-20261112.json').read_text())


def example(clause='计算结果沿用错值仍得一半分', *, award=None):
    row = make_row('列式2分，计算结果8分。' + clause + '。')
    raw = proposed(points=(2, 8))
    if award is not None:
        raw['parts'][0]['steps'][1]['follow_through_pts'] = award
    return row, raw


@pytest.mark.parametrize('clause', [
    '计算结果沿用错值仍得一半分',
    '计算结果沿用错值给一半分',
    '计算结果沿错可得该步分值的一半',
    '后续的计算结果即使用错值也按该步配分的一半计',
    '若前一步计算错误，计算结果沿用该值时仍应得一半分',
    '计算结果沿用错值不得给一半分',
    '计算结果: follow-through earns half credit',
    '计算结果: error carried forward earns half credit',
])
def test_unparsed_award_and_negation_require_full_source_verification(clause):
    row, raw = example(clause)
    with pytest.raises(TeacherPolicyUnresolved) as caught:
        generated_rubric(row, RubricDefinition(part_rubric=raw))
    issue, = caught.value.issues
    assert issue['type'] == 'follow_through'
    assert issue['source_quote'] == clause
    assert [(s['criterion'], s['total_pts'], s['follow_through_pts']) for s in issue['candidates']] == [
        ('列式', 2, 0), ('计算结果', 8, 0)]


@pytest.mark.parametrize('case', BEFORE['cases'])
def test_preexisting_omission_is_not_reused_but_history_remains_readable(case):
    from backend.services.math_grading_context import MATH_GRADING_ENGINE_VERSION, plans_from_snapshots
    assert case['reloaded'] and case['score'] == 0 and case['expected_score'] == 4
    assert saved_draft({**case['question'], 'rubric_draft': case['draft']}) is None
    manifest = {'math_grading_engine_version': MATH_GRADING_ENGINE_VERSION,
                'math_rubric_plans': {'q1': case['draft']['plan']}}
    assert not plans_from_snapshots(manifest, [], for_grading=True)
    assert 'q1' in plans_from_snapshots(manifest, [])


@pytest.mark.asyncio
@pytest.mark.parametrize('clause', ['计算结果沿用错值仍得一半分', '计算结果沿用错值给一半分'])
@pytest.mark.parametrize('verdict', ['inconsistent', 'uncertain'])
async def test_omitted_model_output_cannot_be_saved_when_audit_rejects(clause, verdict):
    row, raw = example(clause)
    provider = PolicyProvider(raw, verdict=verdict)
    with pytest.raises(ValidationError, match='could not be verified'):
        await compile_parts_draft(row, provider)
    assert len(provider.calls) == 2
    assert saved_draft(row) is None


@pytest.mark.asyncio
async def test_verified_award_survives_save_reload_and_settles_four_points():
    row, raw = example(award=4)
    provider = PolicyProvider(raw)
    draft = await compile_parts_draft(row, provider)
    loaded = saved_draft({**row, 'rubric_draft': json.loads(draft.model_dump_json())})
    assert loaded is not None and len(provider.calls) == 2
    rules = loaded.plan.part_rubric
    part = rules.parts[0]
    assert [step.follow_through_award() for step in part.steps] == [0, 4]
    decision = PartDecision(part_id=part.id, target_quote=f'part:{part.id}', student_quote='student:1, student:2',
        method='reference', relevance='partial', progress='列式错误，后续计算正确沿用错值', steps=[
            dict(id='eq', outcome='missed', evidence='student:1', reason='列式错误'),
            dict(id='val', outcome='follow_through', evidence='student:2', error_source='student:1', reason='沿错计算正确'),
        ])
    score, rows = settle_parts(rules, [decision], 'x=4\ny=2x=8', row['stem'])
    assert score == 4 and [r.score for r in rows] == [0, 4]
    # The audit is bound to the actual values and targets, not just the wording.
    changed = json.loads(draft.model_dump_json())
    steps = changed['plan']['part_rubric']['parts'][0]['steps']
    steps[0]['follow_through_pts'] = 1
    steps[1]['follow_through_pts'] = 0
    assert saved_draft({**row, 'rubric_draft': changed}) is None
    assert len(provider.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('clause,award', [
    ('计算结果沿用错值保留一半分', 4),
    ('若沿用错值，计算结果保留一半分', 4),
    ('后续计算沿用题干中的变量定义', None),
    ('列式中说明误差，计算结果保留三位小数', None),
])
async def test_existing_numeric_and_non_policy_paths_still_need_one_generation(clause, award):
    row, raw = example(clause, award=award)
    provider = Provider([json.dumps({'part_rubric': raw}, ensure_ascii=False)])
    draft = await compile_parts_draft(row, provider)
    assert saved_draft({**row, 'rubric_draft': draft.model_dump(mode='json')}) is not None
    assert len(provider.calls) == 1 and not draft.semantic_policy_checks


@pytest.mark.parametrize('second', ['列式沿用错值保留一半分', '列式保留一半分'])
def test_one_parsed_policy_cannot_hide_another_unparsed_policy_in_the_sentence(second):
    clause = '计算结果沿用错值给一半分，' + second
    row, raw = example(clause)
    raw['parts'][0]['steps'][0]['follow_through_pts'] = 1
    with pytest.raises(TeacherPolicyUnresolved) as caught:
        generated_rubric(row, RubricDefinition(part_rubric=raw))
    issue, = caught.value.issues
    assert issue['source_quote'] == clause
    assert [s['follow_through_pts'] for s in issue['candidates']] == [1, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize('allow_partial', [False, True])
async def test_initial_material_candidate_also_requires_the_same_policy_audit(allow_partial):
    from backend.agents.ingest_agent import generate_missing_question_materials
    from backend.llm.providers import LLMResponse
    row, raw = example(award=4)

    class InitialProvider(PolicyProvider):
        async def ainvoke(self, messages, **kwargs):
            if not self.calls:
                self.calls.append(messages)
                value = {'candidates': [{'target_id': 'q1:criterion', 'q_id': 'q1', 'target': 'criterion',
                    'text_value': row['criterion'], 'rubric_definition': {'part_rubric': self.candidate}}]}
                return LLMResponse(content=json.dumps(value), model='fixture', provider='synthetic', duration_ms=1)
            return await super().ainvoke(messages, **kwargs)

    provider = InitialProvider(raw)
    candidates = await generate_missing_question_materials({'q1': row},
        [{'target_id': 'q1:criterion', 'q_id': 'q1', 'target': 'criterion'}], 3, provider, allow_partial=allow_partial)
    assert len(provider.calls) == 2 and len(candidates) == 1
    assert saved_draft({**row, 'rubric_draft': candidates[0].rubric_draft}) is not None


LIVE = json.loads((Path(__file__).parent / 'fixtures/part_rubrics/'
                   'gemini-follow-through-audit-20261112.json').read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize('case', LIVE['cases'], ids=lambda case: case['case'])
async def test_real_gemini_audit_replays_through_the_exact_request_and_settlement(case):
    class AuditReplay(Provider):
        async def ainvoke(self, messages, **kwargs):
            if self.calls:
                assert [{'role': 'system' if type(m).__name__ == 'SystemMessage' else 'user',
                         'content': m.content} for m in messages] == case['audit_request']
            return await super().ainvoke(messages, **kwargs)

    provider = AuditReplay([json.dumps({'part_rubric': case['constructed_candidate']}, ensure_ascii=False),
                            case['audit_response']])
    row = case['question']
    if case['case'] == 'omitted_follow_through':
        with pytest.raises(ValidationError, match='could not be verified'):
            await compile_parts_draft(row, provider)
        assert saved_draft(row) is None
    else:
        draft = await compile_parts_draft(row, provider)
        loaded = saved_draft({**row, 'rubric_draft': json.loads(draft.model_dump_json())})
        assert loaded is not None
        rules = loaded.plan.part_rubric
        part = rules.parts[0]
        decision = PartDecision(part_id=part.id, target_quote=f'part:{part.id}', student_quote='student:1, student:2',
            method='reference', relevance='partial', progress='列式错，沿错计算正确', steps=[
                dict(id='eq', outcome='missed', evidence='student:1', reason='列式错误'),
                dict(id='val', outcome='follow_through', evidence='student:2', error_source='student:1', reason='沿错计算正确'),
            ])
        score, rows = settle_parts(rules, [decision], 'x=4\ny=2x=8', row['stem'])
        assert score == 4 and [r.score for r in rows] == [0, 4]
    assert len(provider.calls) == 2
