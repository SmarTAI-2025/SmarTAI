"""Historical AI precision agrees across reads without rewriting audit rows."""
from __future__ import annotations

import csv
from io import StringIO

import pytest

from backend.tests.test_task_finalization_contract import _prepared_task


@pytest.mark.parametrize("teacher_score", [None, 4.47])
def test_historical_ai_scores_share_precision_across_results_statistics_and_export(
    teacher_score,
):
    from backend.api.analytics import _load_facts
    from backend.db import grading_repository
    from backend.db.models import GradeResultRecord
    from backend.db.session import session_scope
    from backend.services import task_facade

    seeded = _prepared_task()
    task_args = {"task_id": seeded["task_id"], "owner_id": seeded["owner_id"]}
    # Simulate existing SQL rows written before the common one-decimal policy.
    # 2.55 also distinguishes decimal half-up from binary/JavaScript rounding.
    with session_scope() as session:
        session.get(GradeResultRecord, seeded["required_result_id"]).ai_score = 4.44
        session.get(GradeResultRecord, seeded["optional_result_id"]).ai_score = 2.55
    if teacher_score is not None:
        grading_repository.add_teacher_review(
            seeded["required_result_id"],
            teacher_id=seeded["owner_id"],
            new_score=teacher_score,
            new_comment="Deliberate teacher precision",
            confirm=True,
        )

    expected = {
        "q-required": teacher_score if teacher_score is not None else 4.4,
        "q-optional": 2.6,
    }
    expected_total = sum(expected.values())
    rows = {
        row.q_id: row
        for row in grading_repository.list_results_for_run(seeded["run_id"])
    }
    assert {key: row.ai_score for key, row in rows.items()} == {
        "q-required": 4.4, "q-optional": 2.6,
    }
    assert {key: row.effective_score for key, row in rows.items()} == expected

    payload = task_facade.task_results(**task_args)
    corrections = {
        row["q_id"]: row for row in payload["results"][0]["corrections"]
    }
    assert {key: row["score"] for key, row in corrections.items()} == expected
    assert corrections["q-required"]["provisional_score"] == 4.4
    assert corrections["q-required"]["teacher_score"] == teacher_score

    facts = _load_facts(seeded["task_id"], seeded["owner_id"])
    assert {row.q_id: row.score for row in facts.results} == expected
    assert facts.per_student_stats[0]["total"] == pytest.approx(expected_total)
    assert {
        row["q_id"]: row["score"]
        for row in facts.results_payload["results"][0]["corrections"]
    } == expected

    state = task_facade.finalization(**task_args)
    task_facade.confirm_finalization(
        **task_args, expected_revision=state["workflow_revision"],
    )
    snapshot = task_facade.result_snapshot(**task_args, result_version=1)
    assert {
        row["q_id"]: row["score"]
        for row in snapshot["payload"]["results"][0]["corrections"]
    } == expected
    state = task_facade.finalization(**task_args)
    task_facade.generate_artifacts(
        **task_args, expected_revision=state["workflow_revision"],
    )
    body, _, _ = task_facade.artifact_bytes(
        **task_args, version=1, artifact_id="grades_csv",
    )
    exported = next(csv.DictReader(StringIO(body.decode("utf-8-sig"))))
    assert float(exported["total_score"]) == pytest.approx(expected_total)
    assert float(exported["q-required_score"]) == expected["q-required"]
    assert float(exported["q-optional_score"]) == expected["q-optional"]

    # Reading, confirming and exporting cannot rewrite the historical AI evidence.
    with session_scope() as session:
        assert session.get(GradeResultRecord, seeded["required_result_id"]).ai_score == 4.44
        assert session.get(GradeResultRecord, seeded["optional_result_id"]).ai_score == 2.55
