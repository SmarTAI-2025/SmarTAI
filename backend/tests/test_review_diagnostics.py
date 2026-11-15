import json
from types import SimpleNamespace

from backend.services.review_diagnostics import public_answer_diagnostics, public_grading_diagnostics


def test_answer_diagnostics_keep_field_and_confidence_without_response_text():
    rows = public_answer_diagnostics([dict(q_id="q1", number="1.10", content="PRIVATE_STUDENT_ANSWER",
        flag=["recognition_failed", "recognition_invalid_fields:number,content,PRIVATE_SECRET", "PRIVATE_ERROR"],
        recognition_confidence=.4)])
    assert rows == [dict(q_id="q1", number="1.10", has_content=True,
        flag=["recognition_failed", "recognition_invalid_fields:number,content"], recognition_confidence=.4)]
    assert public_answer_diagnostics(rows) == rows
    assert "PRIVATE" not in json.dumps(rows)


def test_failed_grading_projection_keeps_pair_identity_and_safe_cause_only():
    source = SimpleNamespace(student_id="s1", q_id="q1", effective_score=None,
        requires_review=True, ai_max_score=10, ai_confidence=0,
        ai_expert_results=[dict(provider="Gemini", score=0, max_score=10, confidence=0,
            error_kind="parse_failed", raw_output="PRIVATE_MODEL_RESPONSE", comment="PRIVATE_STUDENT_TEXT")],
        ai_synthesis_method="all_failed", review_reasons=["parse_failed"],
        initial_review_reasons=["parse_failed"], teacher_review=None)
    rows, summary = public_grading_diagnostics([source])
    assert summary == {"affected": 1, "missing_scores": 1}
    assert rows[0]["student_id"] == "s1" and rows[0]["q_id"] == "q1"
    assert rows[0]["expert_results"][0]["error_kind"] == "parse_failed"
    assert "PRIVATE" not in json.dumps(rows)
    many, summary = public_grading_diagnostics([source] * 40)
    assert len(many) == 20 and summary["missing_scores"] == 40


def test_source_page_failure_does_not_become_confirmable_metadata_only():
    rows = public_answer_diagnostics([dict(q_id="q1", content="present", flag=[
        "recognition_metadata_review", "recognition_failed", "recognition_invalid_fields:type",
        "recognition_failed_pages:1,3", "recognition_unprocessed_pages:PRIVATE", {}])])
    assert "recognition_failed_pages:1,3" in rows[0]["flag"]
    assert "PRIVATE" not in json.dumps(rows)


def test_retired_qualitative_warnings_and_confirmed_blanks_do_not_inflate_counts():
    assert public_answer_diagnostics([
        dict(q_id="q1", content="answer", flag=["low_confidence", "recognition_needs_review"]),
        dict(q_id="q2", content="", flag=["manual_blank_confirmed"]),
        dict(q_id="q3", content="answer", flag=["recognition_failed_pages:2"]),
    ]) == []
    assert len(public_answer_diagnostics([dict(q_id="q1", content="answer", flag=["low_confidence"], recognition_confidence=.2)])) == 1
