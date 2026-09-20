"""Regression cases for the September 20 integration, using synthetic inputs."""
from __future__ import annotations

import asyncio

import pytest

from backend.services.question_structure import (
    build_major_question_structure, summarize_rubric_points,
)


@pytest.mark.parametrize("stem", [
    "Let f(a)=a and f(b)=b. Prove continuity.",
    "Let f (a) = a and f (b) = b. Prove continuity.",
    "Compute g(1) + g(2).",
])
def test_function_arguments_are_not_subpart_labels(stem):
    structure = build_major_question_structure({"number": "1", "stem": stem}, major_order=0)
    assert structure.subparts == []
    assert structure.shared_stem == stem


@pytest.mark.parametrize("criterion", [
    "(a) 4分，漏步骤扣1分；(b) 6分",
    "(a) 4 points; deduct 1 point for a missing step. (b) 6 points",
    "(a) 方法2分、结果2分，错误减去1分；(b) 证明6分",
    "(a) 4 points, subtract 0.5 points for a missing unit; (b) 6 points",
])
def test_penalties_do_not_increase_subpart_maximum(criterion):
    structure = build_major_question_structure(
        {"number": "1", "stem": "(a) Calculate. (b) Prove."}, major_order=0)
    summary = summarize_rubric_points(criterion, 10, structure)
    assert summary.is_valid
    assert summary.total_points == "10"
    assert [item.points for item in summary.items] == ["4", "6"]


def test_repository_edit_rebuilds_stale_structure_without_discarding_other_source_metadata():
    from backend.db.assignment_repository import _normalise_question_source
    original = build_major_question_structure(
        {"number": "1", "stem": "(a) Calculate. (b) Prove."}, major_order=0)
    source = _normalise_question_source(
        {"origin": "uploaded", "presentation": {
            "question_structure": original.model_dump(),
            "recognition_evidence": {"artifact_file_id": "synthetic-artifact"},
        }},
        number="2", order_index=3, stem="One new question without subparts.",
        criterion="Correct reasoning earns credit.", max_score=10)
    metadata = source["presentation"]["question_structure"]
    assert metadata["major_number"] == "2"
    assert metadata["major_order"] == 3
    assert metadata["subparts"] == []
    assert metadata["shared_stem"] == "One new question without subparts."
    assert metadata["review_status"] == "needs_review"
    assert source["origin"] == "uploaded"
    assert source["presentation"]["recognition_evidence"]["artifact_file_id"] == "synthetic-artifact"


def test_failed_duplicate_lease_claim_does_not_remove_active_reporter(monkeypatch):
    from backend.domain.errors import LeaseLost
    from backend.progress import tracker
    from backend.services import grading_runs
    run_id = "synthetic-live-run"
    reporter = tracker.get_or_create_reporter(run_id, 1, 1)
    def reject_claim(**_kwargs):
        raise LeaseLost("run_not_claimable")
    monkeypatch.setattr(grading_runs.grading_repository, "claim_lease", reject_claim)
    try:
        asyncio.run(grading_runs.process_run(run_id=run_id, worker_id="duplicate-worker"))
        assert tracker.get_reporter(run_id) is reporter
    finally:
        tracker.remove_reporter(run_id)


def test_stale_worker_cannot_remove_replacement_reporter():
    from backend.progress import tracker
    run_id = "synthetic-reclaimed-run"
    old = tracker.get_or_create_reporter(run_id, 1, 1)
    tracker.remove_reporter(run_id)
    current = tracker.get_or_create_reporter(run_id, 1, 1)
    try:
        tracker.remove_reporter(run_id, expected=old)
        assert tracker.get_reporter(run_id) is current
        tracker.remove_reporter(run_id, expected=current)
        assert tracker.get_reporter(run_id) is None
    finally:
        tracker.remove_reporter(run_id)


@pytest.mark.parametrize("numbers", [("1(a)", "1(b)"), ("(a)", "(b)")])
def test_collapsing_subparts_preserves_first_uploaded_material_label(numbers):
    from backend.services.question_structure import annotate_major_question_structures
    rows = {
        "q1": {"number": numbers[0], "stem": "Calculate.", "type": "计算题",
               "criterion": "4分", "reference_answer": "first solution"},
        "q2": {"number": numbers[1], "stem": "Prove.", "type": "证明题",
               "criterion": "6分", "reference_answer": "second solution"},
        "q3": {"number": "2", "stem": "Explain.", "type": "概念题"},
    }
    result = annotate_major_question_structures(rows)
    assert list(result) == ["q1", "q2"]
    first = result["q1"]
    assert first["criterion"].startswith("(a)")
    assert first["reference_answer"].startswith("(a)")
    summary = summarize_rubric_points(first["criterion"], 10, first["question_structure"])
    assert summary.is_valid and summary.total_points == "10"
