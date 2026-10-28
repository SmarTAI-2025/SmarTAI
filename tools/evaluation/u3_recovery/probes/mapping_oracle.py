"""Audit assertions for the existing question-review DTO, not a product mapper."""

STEMS = (
    'For matrix A = diag(1,2), compute its determinant.',
    'For matrix B = diag(3,4), compute its determinant.',
)
LITERAL = 'Use the supplied blue formula; award 6 points for the stated condition and 4 for the conclusion.'


def assert_original_questions_unchanged(rows, material_token):
    questions = [item for item in rows.values() if item.get('stem')]
    assert len(questions) == 2, 'Original question set changed'
    for index, stem in enumerate(STEMS):
        matches = [(key, item) for key, item in rows.items() if item.get('stem') == stem]
        assert len(matches) == 1, 'Original question identity changed'
        key, item = matches[0]
        assert key == item.get('q_id') == f'q{index + 1}', 'Original storage identity changed'
        assert item['number'] == '7', 'Original displayed number changed'
        suffix = 'AB'[index]
        assert item['criterion'] == f'Existing rubric {suffix}.', 'Original rubric changed or ambiguous material was assigned'
        assert item['reference_answer'] == f'Answer {suffix}.', 'Original answer changed or swapped'
        for field in ('criterion', 'reference_answer'):
            assert item.get('material_provenance', {}).get(field, {}).get('source_token') != material_token, 'Ambiguous source falsely attributed to an original question'


def assert_unresolved_material_visible(rows, material_token):
    # The existing editable matrix can display an unassigned material row. An
    # opaque diagnostic/extra JSON property is not such a review surface. This
    # fixture has no unique target, so assigning it to either stem is invalid.
    unassigned = [item for item in rows.values()
                  if not item.get('stem') and item.get('criterion') == LITERAL]
    assert len(unassigned) == 1, 'Unresolved material must remain once in the editable review projection'
    sourced = [item for item in rows.values()
               if item.get('material_provenance', {}).get('criterion', {}).get('source_token') == material_token]
    assert len(sourced) == 1, 'Unassigned source duplicated or lost'
    assert sum(str(item.get('criterion') or '').count(LITERAL) for item in rows.values()) == 1, 'Unassigned literal duplicated'
    item = unassigned[0]
    assert item.get('review_status') == 'needs_review', 'Unassigned material was silently confirmed'
    provenance = item.get('material_provenance', {}).get('criterion', {})
    assert provenance.get('source_token') == material_token, 'Unassigned material lost its source identity'
    assert provenance.get('authority') == 'uploaded_material', 'Uploaded source authority changed'
    assert provenance.get('scope') == 'question', 'Question-specific ambiguity became an overall rubric'
    assert provenance.get('review_status') == 'pending', 'Unassigned source was silently confirmed'
    assert any(issue.get('status') == 'open'
               and issue.get('code') in ('ambiguous_question_match', 'source_conflict')
               and issue.get('field') in ('source', 'rubric')
               and material_token in issue.get('source_ids', [])
               for issue in item.get('preparation_issues', [])), 'Unresolved assignment needs an open, source-linked review issue'


def assert_ambiguous_projection(rows, material_token):
    # Keep these separate: a known omission must never hide an incorrect binding.
    assert_original_questions_unchanged(rows, material_token)
    assert_unresolved_material_visible(rows, material_token)
