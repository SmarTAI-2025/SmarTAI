from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import analytics
from backend.auth import require_teacher
from backend.db.models import (
    AssignmentQuestionRecord,
    AssignmentRecord,
    CourseRecord,
    GradeResultRecord,
    GradingRunRecord,
    SubmissionAnswerRecord,
    SubmissionRecord,
    SubmissionRevisionRecord,
    TeacherReviewRecord,
    UserRecord,
)
from backend.db.session import session_scope
from backend.llm.registry import get_scoped_expert_registry
from backend.models import User


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _user(role: str, label: str) -> User:
    suffix = uuid.uuid4().hex[:10]
    user = User(
        id=f"{role}_{label}_{suffix}",
        username=f"{label}-{suffix}",
        email=f"{label}-{suffix}@example.test",
        role=role,
        password_hash="test",
    )
    now = time.time()
    with session_scope() as session:
        session.add(UserRecord(
            id=user.id,
            username=user.username,
            email=user.email,
            role=user.role,
            password_hash=user.password_hash,
            is_active=True,
            created_at=now,
            updated_at=now,
        ))
    return user


class _Provider:
    provider_id = "provider-test"
    provider_type = "openai"
    model = "test-model"

    def __init__(self) -> None:
        self.outputs: dict[str, object] = {
            "filter": {"student_ids": [], "explanation": "matched"},
            "summary": {"markdown": "summary"},
            "chart": {
                "title": "Scores",
                "rationale": "Compare results",
                "traces": [{
                    "type": "bar",
                    "name": "Scores",
                    "x": ["A", "B"],
                    "y": [8.5, 4],
                    "mode": "markers",
                    "marker": {"color": "red", "unsafe_nested": {"x": 1}},
                }],
                "layout": {"height": 360, "barmode": "group"},
            },
            "mistakes": {"common_mistakes_md": "- Check signs"},
        }
        self.calls: list[tuple[str, list]] = []

    async def ainvoke(self, messages):
        system = str(messages[0].content)
        if "subset of students" in system:
            mode = "filter"
        elif "asks for a chart" in system:
            mode = "chart"
        elif "common mistakes" in system:
            mode = "mistakes"
        else:
            mode = "summary"
        self.calls.append((mode, messages))
        output = self.outputs[mode]
        if isinstance(output, Exception):
            raise output
        return SimpleNamespace(content=json.dumps(output))


class _Registry:
    def __init__(self, provider: _Provider | None) -> None:
        self.provider = provider

    def pick_default(self):
        return self.provider


def _client(owner: User, registry: _Registry) -> TestClient:
    app = FastAPI()
    app.include_router(analytics.router)
    app.dependency_overrides[require_teacher] = lambda: owner
    app.dependency_overrides[get_scoped_expert_registry] = lambda: registry
    return TestClient(app)


def _seed_graded_assignment(owner: User, label: str = "owner") -> dict[str, object]:
    students = [_user("student", f"{label}-s{index}") for index in range(1, 4)]
    now = time.time()
    course_id = _id("course")
    assignment_id = _id("assignment")
    question_pk = _id("question")
    run_id = _id("run")
    revision_ids: list[str] = []
    result_ids: list[str] = []
    with session_scope() as session:
        session.add(CourseRecord(
            id=course_id,
            name="Calculus",
            code="MATH-101",
            description="",
            teacher_id=owner.id,
            created_at=now,
            updated_at=now,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=assignment_id,
            course_id=course_id,
            teacher_id=owner.id,
            name="Quiz",
            description="",
            status="closed",
            created_at=now,
            updated_at=now,
            version=1,
        ))
        session.flush()
        session.add(AssignmentQuestionRecord(
            id=question_pk,
            assignment_id=assignment_id,
            q_id="Q1",
            order_index=0,
            number="1",
            type="calculation",
            stem="Differentiate x squared",
            criterion="Correct derivative",
            max_score=10,
            version=1,
            created_at=now,
            updated_at=now,
        ))
        session.flush()

        submissions: list[SubmissionRecord] = []
        for index, student in enumerate(students):
            submission = SubmissionRecord(
                id=_id("submission"),
                assignment_id=assignment_id,
                student_id=student.id,
                current_revision_id=None,
                created_at=now + index,
                updated_at=now + index,
            )
            session.add(submission)
            submissions.append(submission)
        session.flush()
        for index, (student, submission) in enumerate(zip(students, submissions)):
            revision_id = _id("revision")
            revision_ids.append(revision_id)
            session.add(SubmissionRevisionRecord(
                id=revision_id,
                submission_id=submission.id,
                revision_number=1,
                source="teacher_import",
                file_name=f"{student.username}.txt",
                created_at=now + index,
            ))
            session.flush()
            session.add(SubmissionAnswerRecord(
                id=_id("answer"),
                revision_id=revision_id,
                question_id=question_pk,
                q_id="Q1",
                number="1",
                type="calculation",
                content=["2x", "x", "unparsed"][index],
                flag=[],
                created_at=now + index,
            ))
            submission.current_revision_id = revision_id
        session.flush()

        session.add(GradingRunRecord(
            id=run_id,
            assignment_id=assignment_id,
            teacher_id=owner.id,
            status="completed",
            total_submissions=3,
            completed_submissions=3,
            failed_submissions=0,
            created_at=now + 10,
            started_at=now + 11,
            completed_at=now + 12,
        ))
        session.flush()
        values = [
            (7.0, "graded", False, [], "AI comment one", 0.8),
            (
                4.0,
                "needs_review",
                True,
                ["minority_veto", "high_indecisiveness"],
                "AI comment two",
                0.7,
            ),
            (
                None,
                "failed",
                True,
                ["low_confidence"],
                "Unavailable",
                0.2,
            ),
        ]
        for index, (student, revision_id, value) in enumerate(
            zip(students, revision_ids, values)
        ):
            score, result_status, requires_review, reasons, comment, confidence = value
            result_id = _id("result")
            result_ids.append(result_id)
            session.add(GradeResultRecord(
                id=result_id,
                grading_run_id=run_id,
                submission_revision_id=revision_id,
                question_id=question_pk,
                student_id=student.id,
                q_id="Q1",
                ai_score=score,
                ai_max_score=10,
                ai_comment=comment,
                ai_steps=[],
                ai_confidence=confidence,
                ai_expert_results=[],
                ai_synthesis_method="single",
                requires_review=requires_review,
                review_reasons=list(reasons),
                initial_requires_review=requires_review,
                initial_review_reasons=list(reasons),
                result_status=result_status,
                created_at=now + 20 + index,
                updated_at=now + 20 + index,
            ))
        session.flush()
        session.add(TeacherReviewRecord(
            id=_id("review"),
            grade_result_id=result_ids[0],
            teacher_id=owner.id,
            previous_score=7,
            previous_comment="AI comment one",
            new_score=8.5,
            new_comment="Reviewed comment",
            comment="Reviewed comment",
            created_at=now + 30,
        ))
    return {
        "task_id": assignment_id,
        "question_id": question_pk,
        "run_id": run_id,
        "students": students,
        "result_ids": result_ids,
    }


def _seed_ungraded_assignment(owner: User) -> str:
    now = time.time()
    course_id = _id("course")
    assignment_id = _id("assignment")
    with session_scope() as session:
        session.add(CourseRecord(
            id=course_id,
            name="Draft Course",
            code="DRAFT",
            description="",
            teacher_id=owner.id,
            created_at=now,
            updated_at=now,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=assignment_id,
            course_id=course_id,
            teacher_id=owner.id,
            name="Draft",
            description="",
            status="draft",
            created_at=now,
            updated_at=now,
            version=1,
        ))
    return assignment_id


@pytest.fixture(autouse=True)
def _clear_derived_state():
    with analytics._cache_lock:
        analytics._cache.clear()
    with analytics._query_rate_lock:
        analytics._query_last_at.clear()
    yield


def test_per_question_uses_effective_normalized_results_and_owner_cache():
    owner = _user("teacher", "analytics-owner")
    other = _user("teacher", "analytics-other")
    seeded = _seed_graded_assignment(owner)
    other_seeded = _seed_graded_assignment(other, "other")
    provider = _Provider()
    owner_client = _client(owner, _Registry(provider))
    other_provider = _Provider()
    other_client = _client(other, _Registry(other_provider))

    response = owner_client.get(
        f"/analytics/{seeded['task_id']}/per_question/Q1"
    )
    assert response.status_code == 200, response.text
    breakdown = response.json()
    assert breakdown["q_id"] == "Q1"
    assert breakdown["stem"] == "Differentiate x squared"
    assert breakdown["avg_score"] == 6.25
    assert breakdown["stats"]["n"] == 2
    assert breakdown["stats"]["total_results"] == 3
    assert breakdown["stats"]["unavailable"] == 1
    assert [row["score"] for row in breakdown["rows"]] == [8.5, 4.0]
    assert breakdown["rows"][0]["comment"] == "Reviewed comment"
    assert breakdown["rows"][1]["review_reasons"] == [
        "minority_veto",
        "high_indecisiveness",
    ]
    assert breakdown["common_mistakes_md"] == "- Check signs"
    assert [mode for mode, _messages in provider.calls] == ["mistakes"]

    # The derived summary is cached by owner/task/question/result fingerprint.
    assert owner_client.get(
        f"/analytics/{seeded['task_id']}/per_question/{seeded['question_id']}"
    ).status_code == 200
    assert [mode for mode, _messages in provider.calls] == ["mistakes"]

    hidden = other_client.get(
        f"/analytics/{seeded['task_id']}/per_question/Q1"
    )
    assert hidden.status_code == 404
    assert hidden.json()["detail"] == {"code": "analytics_task_not_found"}
    assert other_provider.calls == []

    # Existing Figma per-question clear path and canonical task-level clear both work.
    cleared = owner_client.delete(
        f"/analytics/{seeded['task_id']}/per_question/Q1/cache"
    )
    assert cleared.status_code == 200
    assert owner_client.get(
        f"/analytics/{seeded['task_id']}/per_question/Q1"
    ).status_code == 200
    assert [mode for mode, _messages in provider.calls].count("mistakes") == 2
    assert owner_client.delete(
        f"/analytics/{seeded['task_id']}/cache"
    ).json() == {"status": "cleared"}

    # Clearing or reading one owner never touches the other owner's facts/cache.
    assert other_client.get(
        f"/analytics/{other_seeded['task_id']}/per_question/Q1"
    ).status_code == 200


def test_nl_query_filters_hallucinated_ids_and_emits_only_safe_chart_fields():
    owner = _user("teacher", "query-owner")
    other = _user("teacher", "query-other")
    seeded = _seed_graded_assignment(owner)
    other_seeded = _seed_graded_assignment(other, "query-other")
    provider = _Provider()
    valid_id = seeded["students"][0].id
    provider.outputs["filter"] = {
        "student_ids": [valid_id, other_seeded["students"][0].id, valid_id],
        "explanation": "Only the requested student",
    }
    client = _client(owner, _Registry(provider))

    filtered = client.post(
        f"/analytics/{seeded['task_id']}/query",
        json={"question": "Who needs support?", "mode": "filter"},
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json() == {
        "mode": "filter",
        "student_ids": [valid_id],
        "explanation": "Only the requested student",
    }
    prompt = str(provider.calls[-1][1][-1].content)
    assert other_seeded["students"][0].username not in prompt

    limited = client.post(
        f"/analytics/{seeded['task_id']}/query",
        json={"question": "Again", "mode": "summary"},
    )
    assert limited.status_code == 429
    assert limited.json()["detail"]["code"] == "analytics_rate_limited"
    assert int(limited.headers["retry-after"]) >= 1

    with analytics._query_rate_lock:
        analytics._query_last_at.clear()
    chart = client.post(
        f"/analytics/{seeded['task_id']}/query",
        json={"question": "Chart the scores", "mode": "chart"},
    )
    assert chart.status_code == 200, chart.text
    body = chart.json()
    assert body["mode"] == "chart"
    assert body["traces"][0] == {
        "type": "bar",
        "name": "Scores",
        "x": ["A", "B"],
        "y": [8.5, 4.0],
    }
    assert "marker" not in body["traces"][0]
    assert "mode" not in body["traces"][0]


def test_analytics_readiness_and_generation_errors_are_stable_and_redacted():
    owner = _user("teacher", "error-owner")
    ungraded_id = _seed_ungraded_assignment(owner)
    provider = _Provider()
    client = _client(owner, _Registry(provider))

    not_ready = client.get(f"/analytics/{ungraded_id}/per_question/Q1")
    assert not_ready.status_code == 409
    assert not_ready.json()["detail"] == {"code": "analytics_not_ready"}
    assert provider.calls == []

    seeded = _seed_graded_assignment(owner, "error")
    provider.outputs["chart"] = {
        "title": "Unsafe",
        "rationale": "Too many points",
        "traces": [{"type": "bar", "x": list(range(51)), "y": list(range(51))}],
        "layout": {"height": 360},
    }
    invalid_chart = client.post(
        f"/analytics/{seeded['task_id']}/query",
        json={"question": "Make a huge chart", "mode": "chart"},
    )
    assert invalid_chart.status_code == 502
    assert invalid_chart.json()["detail"] == {
        "code": "analytics_generation_failed"
    }

    with analytics._query_rate_lock:
        analytics._query_last_at.clear()
    provider.outputs["summary"] = RuntimeError("secret-provider-payload")
    failed = client.post(
        f"/analytics/{seeded['task_id']}/query",
        json={"question": "Summarize", "mode": "summary"},
    )
    assert failed.status_code == 502
    assert failed.json()["detail"] == {
        "code": "analytics_generation_failed"
    }
    assert "secret-provider-payload" not in failed.text

    no_provider = _client(owner, _Registry(None)).post(
        f"/analytics/{seeded['task_id']}/query",
        json={"question": "Summarize", "mode": "summary"},
    )
    assert no_provider.status_code == 503
    assert no_provider.json()["detail"] == {
        "code": "analytics_provider_unavailable"
    }
