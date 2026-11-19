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


def test_required_answers_survive_advisory_rows_before_the_page_limit():
    yellow = [dict(q_id=f"yellow-{i}", content="answer", recognition_confidence=.3) for i in range(25)]
    red = [dict(q_id=f"red-{i}", content="answer", flag=["recognition_invalid_fields:content"], recognition_confidence=.3) for i in range(25)]
    rows = public_answer_diagnostics([*yellow, *red])
    assert len(rows) == 20 and all(row["q_id"].startswith("red-") for row in rows)
    assert len(public_answer_diagnostics([*yellow, *red], limit=None, required_only=True)) == 25


def test_programming_evidence_remains_a_mandatory_reason_in_safe_diagnostics():
    source = SimpleNamespace(student_id="s1", q_id="q1", effective_score=8, requires_review=True,
        ai_max_score=10, ai_confidence=1, ai_synthesis_method="single", review_reasons=[], initial_review_reasons=[],
        teacher_review=None, ai_expert_results=[dict(provider="test", score=8, programming_evidence={
            "contract": "smartai.programming_evidence", "score_status": "provisional", "execution_status": "unsupported"})])
    rows, _ = public_grading_diagnostics([source])
    assert rows[0]["review_reasons"] == ["programming_code_review_only"]
    assert "programming_evidence" not in rows[0]["expert_results"][0]


def test_current_confirmed_advisories_disappear_but_required_evidence_is_not_suppressed():
    assert public_answer_diagnostics([dict(q_id="q1", content="Verified answer", recognition_confidence=.2,
        flag=[], review_status="confirmed")]) == []
    unresolved = public_answer_diagnostics([dict(q_id="q2", content="", flag=["no_matching_answer"],
        review_status="confirmed")], required_only=True)
    assert unresolved[0]["q_id"] == "q2"


def test_matching_advice_is_visible_but_not_counted_as_required():
    flags = ["recognition_matching_advisory"]
    matching = dict(q_id="q1", content="1.1.20. Student work", flag=flags)
    duplicate = dict(q_id="q2", content="Another answer", flag=[*flags, "answer_assignment_conflict"])
    assert [row["q_id"] for row in public_answer_diagnostics([matching, duplicate])] == ["q2", "q1"]
    assert [row["q_id"] for row in public_answer_diagnostics([matching, duplicate], required_only=True)] == ["q2"]
