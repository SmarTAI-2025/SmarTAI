from __future__ import annotations
import json
import pytest
from sqlalchemy import select
from backend.api import analytics
from backend.db.models import GradeResultRecord, UserRecord
from backend.db.session import session_scope
from backend.db.workflow_repository import AssignmentStudentPresentationRecord
from backend.tests.test_grounded_ask import Provider
from backend.tests.test_normalized_analytics import _client, _Registry, _seed_graded_assignment, _user, _id

@pytest.fixture(autouse=True)
def reset_limits():
    analytics._ask_bursts.clear()
    yield
    analytics._ask_bursts.clear()


def test_authorization_precedes_model_and_no_other_owner_data_is_loaded():
    owner, other = _user("teacher","ask-owner"), _user("teacher","ask-other")
    seeded = _seed_graded_assignment(owner)
    provider = Provider()
    response = _client(other,_Registry(provider)).post(f"/analytics/{seeded['task_id']}/ask",json={"question":"所有学生","surface":"student_analysis"})
    assert response.status_code == 404 and provider.calls == []
    assert seeded["students"][0].username not in response.text


def test_public_presentation_identity_not_internal_username_drives_chart():
    owner=_user("teacher","ask-names")
    seeded=_seed_graded_assignment(owner)
    with session_scope() as session:
        for i,(name,wrong_name) in enumerate([("Alex Chen","Maya internal"),("Maya Lin","Alex internal")]):
            sid=seeded["students"][i].id
            session.get(UserRecord,sid).username=wrong_name
            session.add(AssignmentStudentPresentationRecord(id=_id("presentation"),assignment_id=seeded["task_id"],student_id=sid,display_student_id=f"DEMO-00{i+1}",display_name=name))
    provider=Provider({"references":[{"kind":"student","text":"Alex","field":"student_name","operator":"contains"}]},
        {"result_kind":"chart","sql":"SELECT student_id,q_id,score FROM grades","chart":{"type":"bar","x":"q_id","y":["score"]}})
    response=_client(owner,_Registry(provider)).post(f"/analytics/{seeded['task_id']}/ask",json={"question":"Alex各题得分柱状图","surface":"chart"})
    assert response.status_code == 200, response.text
    body=response.json()
    assert body["recognized"] and body["chart"]["title"]=="Alex Chen (DEMO-001)"
    assert body["chart"]["traces"][0]["y"]==[8.5]  # teacher-adjusted grade, not Maya's 4
    assert body["data"]["rows"]==[["DEMO-001","Q1",8.5]]
    assert "Maya internal" not in json.dumps(provider.calls[1][1].content)


def test_history_is_authorized_by_owner_even_when_query_requests_every_task():
    owner, other=_user("teacher","history-owner"),_user("teacher","history-other")
    own=_seed_graded_assignment(owner); hidden=_seed_graded_assignment(other,"hidden")
    provider=Provider({"scope":"teacher_tasks"},{"result_kind":"tasks","sql":"SELECT task_id FROM tasks"})
    response=_client(owner,_Registry(provider)).post("/analytics/ask",json={"question":"列出所有任务","surface":"history"})
    assert response.status_code==200,response.text
    assert response.json()["selection"]["ids"]==[own["task_id"]]
    assert hidden["task_id"] not in response.text


def test_invalid_detail_student_cannot_invoke_model():
    owner=_user("teacher","detail-owner"); seeded=_seed_graded_assignment(owner)
    provider=Provider()
    response=_client(owner,_Registry(provider)).post(f"/analytics/{seeded['task_id']}/ask",json={"question":"错题","surface":"question_analysis","context_student_id":"NOT_IN_TASK"})
    assert response.status_code==404 and provider.calls==[]


def test_rejected_write_does_not_touch_production_grade_rows():
    owner=_user("teacher","write-owner"); seeded=_seed_graded_assignment(owner)
    bad={"sql":"UPDATE grades SET score=999","result_kind":"table"}
    provider=Provider({},bad,bad,bad)
    response=_client(owner,_Registry(provider)).post(f"/analytics/{seeded['task_id']}/ask",json={"question":"查看成绩","surface":"student_analysis"})
    assert response.status_code==200 and not response.json()["recognized"]
    with session_scope() as session:
        assert session.get(GradeResultRecord,seeded["result_ids"][0]).ai_score==7


def test_oversized_history_is_rejected_before_model():
    owner=_user("teacher","history-length"); seeded=_seed_graded_assignment(owner)
    provider=Provider()
    response=_client(owner,_Registry(provider)).post(f"/analytics/{seeded['task_id']}/ask",json={"question":"查询","history":["x"*2001]})
    assert response.status_code==422 and provider.calls==[]
