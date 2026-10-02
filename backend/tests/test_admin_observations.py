from __future__ import annotations

from datetime import datetime
import json
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from backend.analytics.admin_adoption import AnalyticsRangeTooLarge, query_adoption
from backend.api.admin_monitoring import router
from backend.auth import create_token
from backend.config import settings
from backend.db.models import AdminUsageEventRecord as Event, KnowledgeStorageRecord, StoredFileRecord, UserRecord
from backend.db.session import session_scope
from backend.services import admin_monitoring as monitoring

DAY = 86400
BASE = datetime.fromisoformat("2026-01-01T00:00:00+00:00").timestamp()


def event(name, user="teacher", at=BASE, role="teacher", success=True, dimensions=None):
    with session_scope() as session:
        session.add(Event(id=uuid.uuid4().hex, event_name=name, user_id=user, occurred_at=at,
                          role=role, success=success, dimensions=dimensions))


def user(user_id="operator", role="admin", active=True):
    with session_scope() as session:
        session.add(UserRecord(id=user_id, username=user_id, password_hash="unused-synthetic-hash", role=role, is_active=active))
    return {"Authorization": "Bearer " + create_token(user_id, role, auth_version=0)}


def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_growth_activity_frequency_and_privacy():
    event("account_created", at=BASE)
    event("login_success", at=BASE + 1)
    event("task_created", at=BASE + 2, dimensions={"secret": "must-not-project", "student_answer": "private"})
    event("task_created", at=BASE + 3)
    event("grading_run_started", at=BASE + DAY)
    event("login_success", user="login-only", at=BASE + 5)
    event("task_created", user="failed", at=BASE + 6, success=False)
    event("task_created", user="admin", role="admin", at=BASE + 7)
    event("task_created", user="student", role="student", at=BASE + 8)
    event("task_created", user=None, at=BASE + 9)
    result = query_adoption(start=BASE - DAY, end=BASE + 3 * DAY, now=BASE + 3 * DAY, timezone="UTC")
    assert result["summary"] == {"current_teacher_accounts": 0, "new_users": 1, "login_users": 2,
                                 "business_users": 1, "business_actions": 3, "actions_per_business_user": 3}
    assert result["series"][0]["new_users"] is None
    assert result["series"][-1]["business_users"] == 0
    assert result["frequency"][1]["users"] == 1
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["first_retained_event_at"] == BASE
    assert "must-not-project" not in json.dumps(result)
    assert "login-only" not in json.dumps(result)


def test_no_history_is_unknown_not_zero():
    result = query_adoption(start=BASE, end=BASE + DAY, now=BASE + DAY)
    assert result["coverage"]["status"] == "unavailable"
    assert result["summary"]["business_users"] is None
    assert all(row["new_users"] is None for row in result["series"])
    assert result["retention"] == []


def test_retention_maturity_relative_windows_and_deleted_disabled_users():
    user("disabled", "teacher", active=False)
    # No UserRecord for deleted user: retained event denominator still counts.
    for uid in ("deleted", "disabled"):
        event("task_created", uid, BASE)
    event("task_created", "deleted", BASE + 7 * DAY)  # Inclusive lower bound.
    event("task_created", "disabled", BASE + 14 * DAY)  # Exclusive upper bound.
    event("task_created", "deleted", BASE + 28 * DAY)
    event("login_success", "disabled", BASE + 8 * DAY)  # Login is not return activity.
    event("task_created", "young", BASE + 34 * DAY)
    result = query_adoption(start=BASE, end=BASE + 35 * DAY, now=BASE + 35 * DAY, timezone="UTC")
    first, young = result["retention"]
    assert first["w1"] == {"numerator": 1, "denominator": 2, "pending": 0, "rate": .5}
    assert first["w4"] == {"numerator": 1, "denominator": 2, "pending": 0, "rate": .5}
    assert young["w1"]["rate"] is None and young["w1"]["pending"] == 1
    assert result["summary"]["current_teacher_accounts"] == 1


def test_first_business_action_before_range_does_not_become_new_cohort():
    event("task_created", at=BASE)
    event("task_created", at=BASE + 8 * DAY)
    result = query_adoption(start=BASE + 7 * DAY, end=BASE + 30 * DAY, now=BASE + 30 * DAY)
    assert result["summary"]["business_users"] == 1
    assert result["retention"] == []


def test_timezone_boundaries_and_end_exclusion():
    event("account_created", at=BASE + 16 * 3600)  # Next Singapore date.
    event("account_created", user="excluded", at=BASE + DAY)
    result = query_adoption(start=BASE, end=BASE + DAY, now=BASE + DAY)
    assert result["summary"]["new_users"] == 1
    assert result["series"][-1]["date"] == "2026-01-02"
    assert result["series"][-1]["new_users"] == 1


def test_calendar_days_follow_dst():
    start = datetime.fromisoformat("2026-03-08T00:00:00-05:00").timestamp()
    end = datetime.fromisoformat("2026-03-10T00:00:00-04:00").timestamp()
    result = query_adoption(start=start, end=end, now=end, timezone="America/New_York")
    assert len(result["series"]) == 2
    assert result["series"][0]["end"] - result["series"][0]["start"] == 23 * 3600


@pytest.mark.parametrize("kwargs", [{"start": float("nan")}, {"end": float("inf")}, {"start": BASE + DAY}, {"end": BASE + 95 * DAY}, {"timezone": "Invalid/Zone"}])
def test_rejects_invalid_or_unbounded_queries(kwargs):
    with pytest.raises(ValueError):
        query_adoption(**{"start": BASE, "end": BASE + DAY, "now": BASE + 100 * DAY, **kwargs})


def test_too_many_events_fails_explicitly_without_truncated_metrics(monkeypatch):
    import backend.analytics.admin_adoption as adoption
    monkeypatch.setattr(adoption, "MAX_ROWS", 1)
    event("task_created", at=BASE)
    event("task_created", at=BASE + 1)
    with pytest.raises(AnalyticsRangeTooLarge):
        query_adoption(start=BASE, end=BASE + DAY, now=BASE + DAY)


def test_metadata_storage_deduplicates_and_keeps_pending_cleanup():
    user("owner", "teacher")
    with session_scope() as session:
        for key, size, state in (("same", 100, "available"), ("pending", 50, "cleanup_pending"), ("deleted", 500, "unavailable")):
            session.add(StoredFileRecord(id=key, owner_id="owner", kind="problem", original_name="private-name", storage_key=key, storage_backend="local", size_bytes=size, sha256="a" * 64, availability_status=state))
        session.add(KnowledgeStorageRecord(id="knowledge", owner_id="owner", original_name="private", storage_backend="local", storage_key="same", size_bytes=100, sha256="b" * 64, state="available"))
        session.add(KnowledgeStorageRecord(id="reserved", owner_id="owner", original_name="private", storage_backend="object", storage_key="reserved", size_bytes=200, sha256="c" * 64, state="reserved"))
    result = monitoring._application_storage()
    assert result["bytes"] == 150 and result["objects"] == 2
    assert result["knowledge_reserved_bytes"] == 200
    assert "private" not in json.dumps(result)


def disk():
    return {"scope": "application_mount", "status": "available", "total_bytes": 100 * monitoring.GIB,
            "used_bytes": 30 * monitoring.GIB, "free_bytes": 70 * monitoring.GIB, "used_ratio": .3}


def test_samples_idempotent_per_bucket_isolated_per_scope_and_persistent():
    one = monitoring._sample_and_history(now=BASE, disk=disk(), storage={"bytes": 10}, fingerprint="node-one")
    replay = monitoring._sample_and_history(now=BASE + 1, disk=disk(), storage={"bytes": 20}, fingerprint="node-one")
    other = monitoring._sample_and_history(now=BASE + 1, disk=disk(), storage={"bytes": 50}, fingerprint="node-two")
    assert one["samples"] == replay["samples"]
    assert len(other["samples"]) == 1 and other["samples"][0]["application_bytes"] == 50
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 2


def test_sample_retention_never_deletes_usage_or_audit_events():
    event("task_created", at=BASE - 100 * DAY)
    event(monitoring.SAMPLE_EVENT, at=BASE - 100 * DAY, role="admin")
    monitoring._sample_and_history(now=BASE, disk=disk(), storage={"bytes": None}, fingerprint="node")
    with session_scope() as session:
        names = session.scalars(select(Event.event_name)).all()
    assert sorted(names) == sorted(["task_created", monitoring.SAMPLE_EVENT])


def test_projection_requires_coverage_and_resets_after_resize():
    samples = [{"at": BASE + i * 6 * 3600, "free_bytes": (70 - i) * monitoring.GIB} for i in range(5)]
    projection = monitoring._projection(samples, BASE + DAY)
    assert projection["bytes_per_day"] == 4 * monitoring.GIB
    assert projection["days_remaining"] == 16.5
    assert monitoring._projection(samples[:2], BASE + DAY)["status"] == "unavailable"
    assert monitoring._projection([samples[0], *samples[2:]], BASE + DAY)["reason"] == "sampling_gaps"
    for sample in samples:
        monitoring._sample_and_history(now=sample["at"], disk={**disk(), "free_bytes": sample["free_bytes"]}, storage={"bytes": 1}, fingerprint="node")
    resized = monitoring._sample_and_history(now=BASE + DAY + 300, disk={**disk(), "total_bytes": 200 * monitoring.GIB}, storage={"bytes": 1}, fingerprint="node")
    assert resized["projection"]["status"] == "unavailable"


def test_no_positive_growth_has_no_exhaustion_date():
    samples = [{"at": BASE + i * 6 * 3600, "free_bytes": 70} for i in range(5)]
    result = monitoring._projection(samples, BASE + DAY)
    assert result["status"] == "available" and result["days_remaining"] is None
    assert result["reason"] == "no_positive_growth"


def test_host_capacity_is_not_inferred_from_application_partition(monkeypatch, tmp_path):
    monkeypatch.delenv("SMARTAI_MONITOR_HOST_DISK_PATH", raising=False)
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    result, fingerprint = monitoring._disk_probe()
    assert result["host_status"] == "unavailable" and result["scope"] == "application_mount"
    assert fingerprint and str(tmp_path) not in json.dumps(result)
    monkeypatch.setenv("SMARTAI_MONITOR_HOST_DISK_PATH", str(tmp_path))
    assert monitoring._disk_probe()[0]["host_status"] == "operator_configured"
    monkeypatch.setenv("SMARTAI_MONITOR_HOST_DISK_PATH", str(tmp_path / "missing"))
    assert monitoring._disk_probe()[0]["status"] == "unavailable"


@pytest.mark.parametrize("ratio,free,days,level", [(.91, 10 * monitoring.GIB, None, "critical"), (.5, .5 * monitoring.GIB, None, "critical"), (.81, 10 * monitoring.GIB, None, "warning"), (.4, 50 * monitoring.GIB, 10, "warning"), (.4, 50 * monitoring.GIB, None, "normal")])
def test_capacity_recommendation_thresholds(ratio, free, days, level):
    assert monitoring._recommendation({**disk(), "used_ratio": ratio, "free_bytes": free}, {"projection": {"days_remaining": days}})["level"] == level


def test_probe_errors_do_not_expose_exception_text(monkeypatch):
    def fail():
        raise RuntimeError("password=private-secret host=internal")
    monkeypatch.setattr(monitoring, "session_scope", fail)
    result = monitoring._database_probe()
    assert result["status"] == "error" and "private-secret" not in json.dumps(result)


def test_object_probe_requires_explicit_credentials(monkeypatch):
    import boto3
    monkeypatch.setattr(settings, "storage_backend", "object")
    monkeypatch.setattr(settings, "storage_s3_access_key", "")
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: pytest.fail("must not contact instance metadata"))
    assert monitoring._storage_probe()["status"] == "unavailable"


def test_object_probe_only_heads_bucket_with_bounded_timeouts(monkeypatch):
    import boto3
    monkeypatch.setattr(settings, "storage_backend", "object")
    for name, value in (("bucket", "synthetic-bucket"), ("access_key", "fake-key"), ("secret_key", "fake-secret")):
        monkeypatch.setattr(settings, "storage_s3_" + name, value)
    calls = []
    class BucketClient:
        def head_bucket(self, **kwargs):
            calls.append(kwargs)
        def close(self):
            calls.append("closed")
    def fake_client(service, **kwargs):
        assert service == "s3"
        assert kwargs["config"].connect_timeout == 2
        assert kwargs["config"].read_timeout == 2
        assert kwargs["config"].retries["total_max_attempts"] == 1
        return BucketClient()
    monkeypatch.setattr(boto3, "client", fake_client)
    result = monitoring._storage_probe()
    assert result["status"] == "ok" and result["check"] == "head_bucket_only"
    assert calls == [{"Bucket": "synthetic-bucket"}, "closed"]
    assert "fake-secret" not in json.dumps(result)


def test_analytics_database_error_is_safe_503(monkeypatch):
    headers = user()
    def fail(**kwargs):
        raise SQLAlchemyError("password=private-secret")
    monkeypatch.setattr("backend.api.admin_monitoring.query_adoption", fail)
    response = client().post("/admin/analytics/query", headers=headers, json={"start": BASE})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "analytics_unavailable"
    assert "private-secret" not in response.text


@pytest.mark.parametrize("role,active,expected", [("teacher", True, 403), ("student", True, 403), ("admin", False, 401), ("admin", True, 200)])
def test_both_endpoints_use_existing_active_admin_auth(monkeypatch, role, active, expected):
    headers = user(role=role, active=active)
    monkeypatch.setattr(monitoring, "_storage_probe", lambda: {"status": "unavailable"})
    c = client()
    for response in (c.get("/admin/monitoring", headers=headers), c.post("/admin/analytics/query", headers=headers, json={"start": BASE, "end": BASE + DAY})):
        assert response.status_code == expected, response.text
        if expected == 200:
            assert response.headers["cache-control"] == "no-store"


def test_anonymous_denied_before_sampling(monkeypatch):
    def never():
        pytest.fail("anonymous request must not collect data")
    monkeypatch.setattr("backend.api.admin_monitoring.collect_monitoring", never)
    c = client()
    assert c.get("/admin/monitoring").status_code == 401
    assert c.post("/admin/analytics/query", json={"start": BASE}).status_code == 401


def test_public_app_does_not_expose_new_routes(monkeypatch):
    from backend.main import create_app
    monkeypatch.setattr(settings, "runtime_environment", "production")
    app = create_app()
    assert "/admin/monitoring" not in app.openapi()["paths"]
    c = TestClient(app)
    assert c.get("/admin/monitoring").status_code == 404
    assert c.post("/admin/analytics/query", json={"start": BASE}).status_code == 404
