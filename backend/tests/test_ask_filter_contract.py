from __future__ import annotations

import json

import pytest

from backend.api import analytics
from backend.db.models import SubmissionRecord
from backend.db.session import session_scope
from backend.db.workflow_repository import AssignmentStudentPresentationRecord, GradingRunSetupRecord
from backend.tests.test_normalized_analytics import (
    _Provider, _Registry, _client, _id, _seed_graded_assignment, _seed_ungraded_assignment, _user,
)


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    with analytics._query_rate_lock:
        analytics._query_last_at.clear()
    yield


@pytest.mark.parametrize("surface, fields", [
    ("question_preparation", {"sort": "max_score_asc"}),
    ("question_preparation", {"low_confidence": True, "preparation_status": "source_conflict", "sort": None}),
    ("question_preparation", {"min_max_score": 0, "material_field": "answer", "material_status": "missing", "sort": None}),
    ("submission_review", {"sort": "coverage_desc", "submission_status": "reviewed"}),
    ("submission_review", {"sort": "id_desc", "question_types": ["proof"]}),
    ("student_answer_review", {"sort": "question_desc", "submission_status": "missing"}),
    ("question_analysis", {"sort": "confidence_desc", "max_average_confidence": 0.8}),
    ("student_analysis", {"sort": "id_desc"}),
    ("review_overview", {"sort": "review_asc"}),
])
def test_instruction_contract_does_not_require_grading_or_load_task_facts(monkeypatch, surface, fields):
    owner = _user("teacher", "ask-ungraded")
    task_id = _seed_ungraded_assignment(owner)
    provider = _Provider()
    provider.outputs["intent"].update(fields)
    monkeypatch.setattr(analytics, "_load_facts", lambda *_: pytest.fail("Instruction routing must not load grading facts"))
    response = _client(owner, _Registry(provider)).post(
        f"/analytics/{task_id}/filter-intent", json={"question": "请按我的要求筛选和排序", "surface": surface},
    )
    assert response.status_code == 200, response.text
    assert response.json()["recognized"] is True
    for field, value in fields.items():
        assert response.json()[field] == value
    payload = json.loads(str(provider.calls[0][1][-1].content))
    assert payload == {"surface": surface, "teacher_query": "请按我的要求筛选和排序"}


@pytest.mark.parametrize("fields", [
    {"sort": "max_score_asc", "min_score_percent": 0},
    {"sort": "max_score_asc", "unknown_condition": True},
    {"sort": "name_asc"},
    {"sort": "max_score_asc", "question_types": ["invented"]},
    {"sort": "max_score_asc", "material_field": "answer"},
    {"sort": "max_score_asc", "min_max_score": 10, "max_max_score": 5},
    {"sort": "max_score_asc", "min_max_score": float("nan")},
    {"sort": "max_score_asc", "recognized": False},
])
def test_invalid_or_partially_supported_intent_discards_every_control(fields):
    owner = _user("teacher", "ask-invalid")
    task_id = _seed_ungraded_assignment(owner)
    provider = _Provider()
    provider.outputs["intent"].update(fields)
    response = _client(owner, _Registry(provider)).post(
        f"/analytics/{task_id}/filter-intent", json={"question": "不能只执行一半", "surface": "question_preparation"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == analytics.analytics_agent.FilterIntentOutput(recognized=False, explanation=body["explanation"]).model_dump()


def test_owner_is_checked_before_provider_availability_and_invocation():
    owner = _user("teacher", "ask-owner")
    other = _user("teacher", "ask-other")
    task_id = _seed_ungraded_assignment(owner)
    response = _client(other, _Registry(None)).post(
        f"/analytics/{task_id}/filter-intent", json={"question": "按满分升序", "surface": "question_preparation"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "analytics_task_not_found"}


def test_pregrading_display_identities_are_masked_and_restored_without_answers():
    owner = _user("teacher", "ask-private")
    student = _user("student", "private-internal")
    task_id = _seed_ungraded_assignment(owner)
    with session_scope() as session:
        session.add(SubmissionRecord(id=_id("submission"), assignment_id=task_id, student_id=student.id))
        session.add(AssignmentStudentPresentationRecord(
            id=_id("presentation"), assignment_id=task_id, student_id=student.id,
            display_student_id="202699998888", display_name="测试学生甲",
        ))
    provider = _Provider()
    provider.outputs["intent"].update(sort="coverage_desc", text_terms=["<student_1>"])
    response = _client(owner, _Registry(provider)).post(
        f"/analytics/{task_id}/filter-intent", json={"question": "看看测试学生甲，按覆盖率降序", "surface": "submission_review"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["text_terms"] == ["测试学生甲"]
    payload = json.loads(str(provider.calls[0][1][-1].content))
    assert payload == {"surface": "submission_review", "teacher_query": "看看<student_1>，按覆盖率降序"}
    assert "202699998888" not in json.dumps(payload, ensure_ascii=False)


def test_frozen_identity_is_masked_without_sending_the_input_manifest():
    owner = _user("teacher", "ask-frozen")
    seeded = _seed_graded_assignment(owner)
    with session_scope() as session:
        session.add(GradingRunSetupRecord(
            grading_run_id=seeded["run_id"], assignment_id=seeded["task_id"], owner_id=owner.id,
            setup={}, fingerprint="fixture", input_manifest={
                "student_presentations": [{"student_id": seeded["students"][0].id, "display_name": "旧版学生姓名", "display_student_id": "202688887777"}],
                "answers": "PRIVATE-ANSWER-NOT-FOR-MODEL",
            },
        ))
    provider = _Provider()
    provider.outputs["intent"].update(text_terms=["<student_1>"])
    response = _client(owner, _Registry(provider)).post(
        f"/analytics/{seeded['task_id']}/filter-intent", json={"question": "旧版学生姓名按得分率降序", "surface": "student_analysis"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["text_terms"] == ["旧版学生姓名"]
    sent = str(provider.calls[0][1][-1].content)
    assert "旧版学生姓名" not in sent and "PRIVATE-ANSWER" not in sent and "202688887777" not in sent


def test_unknown_identity_placeholder_cannot_turn_into_a_partial_sort():
    owner = _user("teacher", "ask-placeholder")
    task_id = _seed_ungraded_assignment(owner)
    provider = _Provider()
    provider.outputs["intent"].update(text_terms=["<student_999>"])
    response = _client(owner, _Registry(provider)).post(
        f"/analytics/{task_id}/filter-intent", json={"question": "按得分率降序", "surface": "student_analysis"},
    )
    assert response.json()["recognized"] is False
    assert response.json()["sort"] is None
    assert response.json()["text_terms"] == []


def test_short_student_ids_do_not_consume_question_numbers_or_score_bounds():
    text, identities = analytics._redact_filter_question("90同学、学生90、学号1，Q1 低于90分", {"90", "1"})
    assert text == "<student_1>同学、学生<student_1>、学号<student_2>，Q1 低于90分"
    assert identities == {"<student_1>": "90", "<student_2>": "1"}
