"""Mutation tests of the acceptance oracle; these do not prove product fixes."""
from audit_guard import require_probe_run
require_probe_run()

from copy import deepcopy
import json

import pytest

from mapping_oracle import LITERAL, STEMS, assert_ambiguous_projection

TOKEN = 'synthetic-unassigned-rubric'


def safe_projection(severity='warning'):
    rows = {f'q{i + 1}': dict(q_id=f'q{i + 1}', number='7', stem=stem,
             criterion=f'Existing rubric {"AB"[i]}.', reference_answer=f'Answer {"AB"[i]}.')
            for i, stem in enumerate(STEMS)}
    rows['unassigned'] = dict(q_id='unassigned', number='7', stem='', criterion=LITERAL,
        review_status='needs_review', material_provenance={'criterion': {
            'source_token': TOKEN, 'authority': 'uploaded_material', 'scope': 'question', 'review_status': 'pending'}},
        preparation_issues=[dict(issue_id='synthetic-ambiguity', code='ambiguous_question_match',
            field='source', source_ids=[TOKEN], status='open', severity=severity)])
    return rows


@pytest.mark.parametrize('severity', ['warning', 'blocking'])
def test_oracle_accepts_visible_unassigned_source_without_prescribing_severity(severity):
    assert_ambiguous_projection(safe_projection(severity), TOKEN)


@pytest.mark.parametrize('target', ['q1', 'q2'])
@pytest.mark.parametrize('placement', ['replace', 'append'])
def test_oracle_catches_wrong_assignment_that_passes_old_json_check(target, placement):
    rows = deepcopy(safe_projection())
    rows[target]['criterion'] = LITERAL if placement == 'replace' else rows[target]['criterion'] + '\n' + LITERAL
    del rows['unassigned']
    assert len([item for item in rows.values() if item.get('stem')]) == 2
    assert LITERAL in json.dumps(rows)  # Previous acceptance check incorrectly passes.
    with pytest.raises(AssertionError, match='Original rubric changed'):
        assert_ambiguous_projection(rows, TOKEN)


@pytest.mark.parametrize('mutation,reason', [
    ('opaque_json', 'editable review projection'),
    ('wrong_source', 'duplicated or lost'),
    ('drop_issue', 'open, source-linked'),
    ('resolved_issue', 'open, source-linked'),
    ('wrong_issue_source', 'open, source-linked'),
    ('confirmed', 'silently confirmed'),
    ('overall', 'overall rubric'),
    ('swapped_answers', 'Original answer changed'),
    ('forged_question_provenance', 'falsely attributed'),
    ('swapped_identity', 'storage identity changed'),
    ('forged_q_id', 'storage identity changed'),
    ('duplicate_material', 'source duplicated'),
    ('duplicate_literal', 'literal duplicated'),
])
def test_oracle_rejects_loss_or_false_resolution(mutation, reason):
    rows = deepcopy(safe_projection())
    material = rows['unassigned']
    if mutation == 'opaque_json':
        del rows['unassigned']
        rows['q1']['diagnostic_only'] = {'raw': LITERAL}
    elif mutation == 'wrong_source':
        material['material_provenance']['criterion']['source_token'] = 'other-source'
    elif mutation == 'drop_issue':
        material['preparation_issues'] = []
    elif mutation == 'resolved_issue':
        material['preparation_issues'][0]['status'] = 'resolved'
    elif mutation == 'wrong_issue_source':
        material['preparation_issues'][0]['source_ids'] = ['other-source']
    elif mutation == 'confirmed':
        material['review_status'] = 'confirmed'
    elif mutation == 'overall':
        material['material_provenance']['criterion']['scope'] = 'overall'
    elif mutation == 'swapped_answers':
        rows['q1']['reference_answer'], rows['q2']['reference_answer'] = rows['q2']['reference_answer'], rows['q1']['reference_answer']
    elif mutation == 'forged_question_provenance':
        rows['q2']['material_provenance'] = deepcopy(material['material_provenance'])
    elif mutation == 'swapped_identity':
        rows['q1'], rows['q2'] = rows['q2'], rows['q1']
    elif mutation == 'forged_q_id':
        rows['q1']['q_id'] = 'q2'
    elif mutation in ('duplicate_material', 'duplicate_literal'):
        rows['extra'] = deepcopy(material)
        rows['extra']['q_id'] = 'extra'
        rows['extra']['criterion'] += '\nExtra.'
        if mutation == 'duplicate_literal':
            rows['extra']['material_provenance']['criterion']['source_token'] = 'forged-other-token'
    assert LITERAL in json.dumps(rows)
    with pytest.raises(AssertionError, match=reason):
        assert_ambiguous_projection(rows, TOKEN)
