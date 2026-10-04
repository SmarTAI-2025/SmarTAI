from types import SimpleNamespace

import pytest

from backend.agents.ingest_agent import HW_SYSTEM_PROMPT, SubmissionSourceParseResult
from backend.services.submission_source_pipeline import attach_submission_recognition


@pytest.mark.parametrize("summary,expected", [
    (None, []),
    ({"confidence": "medium", "requires_review": True}, []),
    ({"confidence": "low"}, ["recognition_needs_review"]),
    ({"confidence": "high", "error_code": "partial"}, ["recognition_needs_review"]),
    ({"confidence": "medium", "coverage": {"missing_targets": ["Q2"]}}, ["recognition_needs_review"]),
    ({"confidence": "medium", "coverage": {"failed_pages": [2]}}, ["recognition_needs_review", "recognition_failed_pages:2"]),
])
def test_only_specific_uncertainty_requires_answer_review(summary, expected):
    student = {"stu_ans": [{"q_id": "q1", "content": "0*x=0 (wrong on purpose)", "flag": []}]}
    result = SubmissionSourceParseResult("source", "file", "work.pdf", "parsed", student,
        "student", 1, (), None, None, False)
    sources = [SimpleNamespace(source_id="source", recognition=summary)]
    attached = attach_submission_recognition([result], sources)
    attached = attach_submission_recognition(attached, sources)[0]
    assert attached.student["stu_ans"][0]["flag"] == expected
    assert attached.student["stu_ans"][0]["content"] == student["stu_ans"][0]["content"]
    assert student["stu_ans"][0]["flag"] == []


def test_structure_prompt_does_not_use_teacher_marks_as_student_reasoning():
    assert "external_annotation_present" in HW_SYSTEM_PROMPT
    assert "authorship_uncertain" in HW_SYSTEM_PROMPT
    assert "not student work" in HW_SYSTEM_PROMPT
