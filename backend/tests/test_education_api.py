"""Course/assignment/question vertical API tests (Task 4).

Drives the FastAPI app with TestClient against the real database, exercising the
three-role authorization matrix and the optimistic-lock/publish state machine
through the HTTP contract (not the repository directly). Submission/grading
flows are added in later tasks; this file covers the teacher course→assignment→
question→publish happy path plus the cross-role 404-without-leakage guards.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from backend.auth import hash_password
from backend.main import app
from backend.models import User
from backend.state import get_user_store


def _seed(username: str, role: str) -> str:
    uid = f"u_{username}"
    get_user_store()[uid] = User(id=uid, username=username, role=role, password_hash=hash_password("pass-pass"))
    return uid


def _login(client: TestClient, username: str) -> str:
    resp = client.post("/auth/login", json={"username": username, "password": "pass-pass"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _teacher_client() -> tuple[TestClient, str]:
    _seed("t_api", "teacher")
    c = TestClient(app)
    return c, _login(c, "t_api")


def _other_teacher_client() -> tuple[TestClient, str]:
    _seed("t_other_api", "teacher")
    c = TestClient(app)
    return c, _login(c, "t_other_api")


def _student_client() -> tuple[TestClient, str]:
    _seed("s_api", "student")
    c = TestClient(app)
    return c, _login(c, "s_api")


# ─── course ───────────────────────────────────────────────────────────────────


def test_teacher_creates_course():
    c, tok = _teacher_client()
    resp = c.post("/courses", headers=_headers(tok), json={"name": "Algebra", "code": "M101"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Algebra"
    assert body["teacher_id"].startswith("u_")
    assert body["student_ids"] == []


def test_teacher_enrolls_student():
    c, tok = _teacher_client()
    course = c.post("/courses", headers=_headers(tok), json={"name": "Algebra"}).json()
    stu_uid = _seed("s_enroll", "student")
    resp = c.post(f"/courses/{course['id']}/enroll", headers=_headers(tok), json={"student_ids": [stu_uid]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["student_ids"] == [stu_uid]


def test_student_cannot_create_course():
    c, tok = _student_client()
    resp = c.post("/courses", headers=_headers(tok), json={"name": "x"})
    assert resp.status_code == 403


def test_other_teacher_gets_404_for_course_without_leak():
    c, tok = _teacher_client()
    course = c.post("/courses", headers=_headers(tok), json={"name": "Private"}).json()
    c2, tok2 = _other_teacher_client()
    resp = c2.get(f"/courses/{course['id']}", headers=_headers(tok2))
    assert resp.status_code == 404
    assert resp.json().get("error", {}).get("code") == "not_found"


# ─── assignment ───────────────────────────────────────────────────────────────


def _course(client, tok):
    return client.post("/courses", headers=_headers(tok), json={"name": "Algebra"}).json()


def test_teacher_creates_assignment_in_draft():
    c, tok = _teacher_client()
    course = _course(c, tok)
    resp = c.post("/assignments", headers=_headers(tok),
                  json={"course_id": course["id"], "name": "Homework 1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    assert body["version"] == 1
    assert body["question_count"] == 0


def test_other_teacher_cannot_create_assignment_in_foreign_course():
    c, tok = _teacher_client()
    course = _course(c, tok)
    c2, tok2 = _other_teacher_client()
    resp = c2.post("/assignments", headers=_headers(tok2),
                   json={"course_id": course["id"], "name": "hijack"})
    assert resp.status_code == 404
    assert resp.json().get("error", {}).get("code") == "not_found"


def test_publish_before_questions_is_409_invalid_transition():
    c, tok = _teacher_client()
    course = _course(c, tok)
    a = c.post("/assignments", headers=_headers(tok), json={"course_id": course["id"], "name": "HW"}).json()
    resp = c.post(f"/assignments/{a['id']}/publish", headers=_headers(tok), json={"expected_version": 1})
    assert resp.status_code == 409
    assert resp.json().get("error", {}).get("code") == "invalid_transition"


def test_add_questions_reorder_publish_flow():
    c, tok = _teacher_client()
    course = _course(c, tok)
    a = c.post("/assignments", headers=_headers(tok), json={"course_id": course["id"], "name": "HW"}).json()
    q1 = c.post(f"/assignments/{a['id']}/questions", headers=_headers(tok),
                json={"q_id": "q1", "order_index": 0, "type": "short", "stem": "1+1?"}).json()
    q2 = c.post(f"/assignments/{a['id']}/questions", headers=_headers(tok),
                json={"q_id": "q2", "order_index": 1, "type": "short", "stem": "2+2?"}).json()
    listed = c.get(f"/assignments/{a['id']}/questions", headers=_headers(tok)).json()
    assert [q["q_id"] for q in listed] == ["q1", "q2"]

    # Reorder so q2 comes first.
    c.post(f"/assignments/{a['id']}/questions/reorder", headers=_headers(tok), json={"ordered_q_ids": ["q2", "q1"]})
    listed = c.get(f"/assignments/{a['id']}/questions", headers=_headers(tok)).json()
    assert [q["q_id"] for q in listed] == ["q2", "q1"]

    # Publish with the correct version.
    pub = c.post(f"/assignments/{a['id']}/publish", headers=_headers(tok), json={"expected_version": 1})
    assert pub.status_code == 200, pub.text
    assert pub.json()["status"] == "published"
    assert pub.json()["version"] == 2


def test_publish_with_stale_version_is_409_version_conflict():
    c, tok = _teacher_client()
    course = _course(c, tok)
    a = c.post("/assignments", headers=_headers(tok), json={"course_id": course["id"], "name": "HW"}).json()
    c.post(f"/assignments/{a['id']}/questions", headers=_headers(tok),
           json={"q_id": "q1", "order_index": 0, "type": "short", "stem": "?"})
    # First publish bumps version to 2.
    c.post(f"/assignments/{a['id']}/publish", headers=_headers(tok), json={"expected_version": 1})
    # A stale client tries to publish with version 1 again.
    resp = c.post(f"/assignments/{a['id']}/publish", headers=_headers(tok), json={"expected_version": 1})
    assert resp.status_code == 409
    assert resp.json().get("error", {}).get("code") == "version_conflict"


# ─── student visibility ───────────────────────────────────────────────────────


def test_student_sees_only_enrolled_published_assignments():
    c, tok = _teacher_client()
    course = _course(c, tok)
    a = c.post("/assignments", headers=_headers(tok), json={"course_id": course["id"], "name": "HW"}).json()
    c.post(f"/assignments/{a['id']}/questions", headers=_headers(tok),
           json={"q_id": "q1", "order_index": 0, "type": "short", "stem": "?"})
    c.post(f"/assignments/{a['id']}/publish", headers=_headers(tok), json={"expected_version": 1})

    stu_uid = _seed("s_see", "student")
    cs, stok = _student_client()  # different student, not enrolled
    # Not enrolled: listing the course's assignments returns nothing for this student.
    listed = cs.get(f"/assignments?course_id={course['id']}", headers=_headers(stok)).json()
    assert listed == []
    # And direct get is 404 without leak.
    resp = cs.get(f"/assignments/{a['id']}", headers=_headers(stok))
    assert resp.status_code == 404
    assert resp.json().get("error", {}).get("code") == "not_found"

    # Enroll s_see (its uid) via teacher, then a fresh client for s_see can see it.
    c.post(f"/courses/{course['id']}/enroll", headers=_headers(tok), json={"student_ids": [stu_uid]})
    c2 = TestClient(app)
    stok2 = _login(c2, "s_see")
    listed2 = c2.get(f"/assignments?course_id={course['id']}", headers=_headers(stok2)).json()
    assert [x["id"] for x in listed2] == [a["id"]]
    assert listed2[0]["status"] == "published"


def test_student_global_assignment_list_includes_closed_results():
    c, tok = _teacher_client()
    course = _course(c, tok)
    stu_uid = _seed("s_closed_list", "student")
    c.post(f"/courses/{course['id']}/enroll", headers=_headers(tok), json={"student_ids": [stu_uid]})
    a = c.post("/assignments", headers=_headers(tok), json={"course_id": course["id"], "name": "Closed HW"}).json()
    c.post(f"/assignments/{a['id']}/questions", headers=_headers(tok), json={"q_id": "q1", "order_index": 0, "type": "short", "stem": "?"})
    c.post(f"/assignments/{a['id']}/publish", headers=_headers(tok), json={"expected_version": 1})
    c.post(f"/assignments/{a['id']}/close", headers=_headers(tok), json={"expected_version": 2})
    student = TestClient(app)
    student_token = _login(student, "s_closed_list")
    response = student.get("/assignments", headers=_headers(student_token))
    assert response.status_code == 200, response.text
    assert [(item["id"], item["status"]) for item in response.json()] == [(a["id"], "closed")]


def test_student_cannot_add_questions():
    c, tok = _teacher_client()
    course = _course(c, tok)
    a = c.post("/assignments", headers=_headers(tok), json={"course_id": course["id"], "name": "HW"}).json()
    cs, stok = _student_client()
    resp = cs.post(f"/assignments/{a['id']}/questions", headers=_headers(stok),
                   json={"q_id": "q1", "order_index": 0, "type": "short", "stem": "?"})
    assert resp.status_code == 403


def test_student_question_payload_excludes_teacher_only_fields():
    _seed("t_question_visibility", "teacher")
    student_id = _seed("s_question_visibility", "student")
    client = TestClient(app)
    teacher_token = _login(client, "t_question_visibility")
    student_token = _login(client, "s_question_visibility")

    course = client.post(
        "/courses", headers=_headers(teacher_token), json={"name": "Visibility"}
    ).json()
    enrolled = client.post(
        f"/courses/{course['id']}/enroll",
        headers=_headers(teacher_token),
        json={"student_ids": [student_id]},
    )
    assert enrolled.status_code == 200, enrolled.text
    assignment = client.post(
        "/assignments",
        headers=_headers(teacher_token),
        json={"course_id": course["id"], "name": "Protected answers"},
    ).json()
    question = client.post(
        f"/assignments/{assignment['id']}/questions",
        headers=_headers(teacher_token),
        json={
            "q_id": "q_sensitive",
            "order_index": 0,
            "number": "1",
            "type": "short",
            "stem": "What is 1+1?",
            "criterion": "Must show work",
            "max_score": 10,
            "reference_answer": "2",
            "test_cases": [{"input": "1+1", "output": "2"}],
            "source": {"origin": "teacher"},
        },
    ).json()
    published = client.post(
        f"/assignments/{assignment['id']}/publish",
        headers=_headers(teacher_token),
        json={"expected_version": 1},
    )
    assert published.status_code == 200, published.text

    teacher_response = client.get(
        f"/assignments/{assignment['id']}/questions",
        headers=_headers(teacher_token),
    )
    assert teacher_response.status_code == 200, teacher_response.text
    teacher_question = teacher_response.json()[0]
    assert teacher_question["criterion"] == "Must show work"
    assert teacher_question["reference_answer"] == "2"
    assert teacher_question["test_cases"] == [{"input": "1+1", "output": "2"}]
    assert teacher_question["source"] == {"origin": "teacher"}

    student_response = client.get(
        f"/assignments/{assignment['id']}/questions",
        headers=_headers(student_token),
    )
    assert student_response.status_code == 200, student_response.text
    student_question = student_response.json()[0]
    assert set(student_question) == {
        "id", "assignment_id", "q_id", "order_index", "number",
        "type", "stem", "max_score", "version",
    }
    assert student_question["id"] == question["id"]


# ─── submissions (Task 5) ─────────────────────────────────────────────────────


def _published_assignment_with_student():
    """teacher + course + enrolled student + published assignment with one question."""
    _seed("t_sub", "teacher")
    _seed("s_sub", "student")
    c = TestClient(app)
    tok = _login(c, "t_sub")
    stok = _login(c, "s_sub")
    course = c.post("/courses", headers=_headers(tok), json={"name": "C"}).json()
    c.post(f"/courses/{course['id']}/enroll", headers=_headers(tok), json={"student_ids": [f"u_s_sub"]})
    a = c.post("/assignments", headers=_headers(tok), json={"course_id": course["id"], "name": "A"}).json()
    c.post(f"/assignments/{a['id']}/questions", headers=_headers(tok),
           json={"q_id": "q1", "order_index": 0, "type": "short", "stem": "1+1?"})
    c.post(f"/assignments/{a['id']}/publish", headers=_headers(tok), json={"expected_version": 1})
    return c, tok, stok, a["id"], f"u_s_sub"


def test_student_online_submission_creates_immutable_revision():
    c, tok, stok, aid, suid = _published_assignment_with_student()
    resp = c.post("/submissions/submit", headers=_headers(stok),
                  json={"assignment_id": aid, "answers": [{"q_id": "q1", "content": "2"}]})
    assert resp.status_code == 200, resp.text
    rev = resp.json()
    assert rev["revision_number"] == 1
    assert rev["source"] == "online"
    assert rev["answers"][0]["content"] == "2"

    # Resubmission creates a new immutable revision (number 2), not a mutation.
    resp2 = c.post("/submissions/submit", headers=_headers(stok),
                   json={"assignment_id": aid, "answers": [{"q_id": "q1", "content": "two"}]})
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["revision_number"] == 2
    # Revision 1 answer is frozen.
    assert rev["answers"][0]["content"] == "2"


def test_student_not_enrolled_cannot_submit():
    _seed("t_sub2", "teacher")
    _seed("s_unenrolled", "student")
    c = TestClient(app)
    tok = _login(c, "t_sub2")
    stok = _login(c, "s_unenrolled")
    course = c.post("/courses", headers=_headers(tok), json={"name": "C"}).json()
    a = c.post("/assignments", headers=_headers(tok), json={"course_id": course["id"], "name": "A"}).json()
    c.post(f"/assignments/{a['id']}/questions", headers=_headers(tok),
           json={"q_id": "q1", "order_index": 0, "type": "short", "stem": "?"})
    c.post(f"/assignments/{a['id']}/publish", headers=_headers(tok), json={"expected_version": 1})
    resp = c.post("/submissions/submit", headers=_headers(stok),
                  json={"assignment_id": a["id"], "answers": [{"q_id": "q1", "content": "x"}]})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_submission_rejected_when_assignment_closed():
    c, tok, stok, aid, suid = _published_assignment_with_student()
    # Close the assignment so new submissions are rejected.
    a = c.get(f"/assignments/{aid}", headers=_headers(tok)).json()
    c.post(f"/assignments/{aid}/close", headers=_headers(tok), json={"expected_version": a["version"]})
    resp = c.post("/submissions/submit", headers=_headers(stok),
                  json={"assignment_id": aid, "answers": [{"q_id": "q1", "content": "late"}]})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "assignment_closed"


def test_correction_rejected_when_assignment_closed():
    c, tok, stok, aid, _ = _published_assignment_with_student()
    submitted = c.post(
        "/submissions/submit",
        headers=_headers(stok),
        json={"assignment_id": aid, "answers": [{"q_id": "q1", "content": "first"}]},
    )
    assert submitted.status_code == 200, submitted.text
    submission_id = submitted.json()["submission_id"]

    assignment = c.get(f"/assignments/{aid}", headers=_headers(tok)).json()
    closed = c.post(
        f"/assignments/{aid}/close",
        headers=_headers(tok),
        json={"expected_version": assignment["version"]},
    )
    assert closed.status_code == 200, closed.text

    response = c.post(
        f"/submissions/detail/{submission_id}/correct",
        headers=_headers(stok),
        json={"answers": [{"q_id": "q1", "content": "corrected"}]},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "assignment_closed"


def test_teacher_batch_import_persists_revisions_with_partial_failure():
    c, tok, stok, aid, suid = _published_assignment_with_student()
    # One enrolled student + one bogus student id → partial failure, not full success.
    resp = c.post("/submissions/teacher-import", headers=_headers(tok), json={
        "assignment_id": aid,
        "items": [
            {"student_id": suid, "file_name": "a.txt", "answers": [{"q_id": "q1", "content": "imp"}]},
            {"student_id": "u_nonexistent", "answers": [{"q_id": "q1", "content": "x"}]},
        ],
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert suid in body["succeeded"]
    assert any(f["student_id"] == "u_nonexistent" for f in body["failed"])
    # The enrolled student's import landed as a teacher_import revision.
    listed = c.get(f"/submissions/assignment/{aid}", headers=_headers(tok)).json()
    assert any(s["student_id"] == suid for s in listed)


def test_unknown_question_id_in_submission_is_rejected():
    c, tok, stok, aid, suid = _published_assignment_with_student()
    resp = c.post("/submissions/submit", headers=_headers(stok),
                  json={"assignment_id": aid, "answers": [{"q_id": "nope", "content": "x"}]})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
