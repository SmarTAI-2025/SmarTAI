"""Bounded, content-free operational observations for the private admin app.

No shell, provider call, object listing, or file-content reads. Disk samples use
the existing PR117 event ledger; no migration or background scheduler is needed.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import socket
import time

from sqlalchemy import delete, func, select, text, union_all
from sqlalchemy.exc import SQLAlchemyError

from backend.config import settings
from backend.db.models import AdminUsageEventRecord as Event, KnowledgeStorageRecord, StoredFileRecord
from backend.db.session import session_scope

SAMPLE_EVENT = "admin_capacity_sample_v1"
SAMPLE_SECONDS = 300
HISTORY_DAYS = 7
RETENTION_DAYS = 90
DAY = 86400
GIB = 1024 ** 3


def _database_probe() -> dict:
    started = time.monotonic()
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
        return {"status": "ok", "latency_ms": round((time.monotonic() - started) * 1000, 1), "check": "select_1"}
    except Exception:
        # Connection errors can contain credentials, hostnames or full URLs.
        return {"status": "error", "latency_ms": None, "check": "select_1"}


def _storage_probe() -> dict:
    started = time.monotonic()
    try:
        if settings.storage_backend == "local":
            root = Path(settings.storage_root)
            ok = root.is_dir() and os.access(root, os.R_OK | os.W_OK | os.X_OK)
            check = "directory_access_only"
        else:
            import boto3
            from botocore.config import Config

            check = "head_bucket_only"
            # Explicit credentials prevent the SDK from probing instance metadata.
            if not (settings.storage_s3_bucket and settings.storage_s3_access_key and settings.storage_s3_secret_key):
                return {"status": "unavailable", "backend": "object", "check": check, "latency_ms": None}
            client = boto3.client("s3", endpoint_url=settings.storage_s3_endpoint or None,
                region_name=settings.storage_s3_region, aws_access_key_id=settings.storage_s3_access_key,
                aws_secret_access_key=settings.storage_s3_secret_key,
                config=Config(connect_timeout=2, read_timeout=2, retries={"total_max_attempts": 1}))
            try:
                client.head_bucket(Bucket=settings.storage_s3_bucket)
                ok = True
            finally:
                client.close()
        return {"status": "ok" if ok else "error", "backend": settings.storage_backend,
                "check": check, "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    except Exception:
        return {"status": "error", "backend": settings.storage_backend, "check": "directory_access_only" if settings.storage_backend == "local" else "head_bucket_only", "latency_ms": None}


def _application_storage() -> dict:
    # Knowledge rows may reference a StoredFile row for the same object. Group
    # by backend + key before summing to avoid counting it twice. The response
    # never includes keys, filenames, owners or directory paths.
    files = select(StoredFileRecord.storage_backend.label("backend"), StoredFileRecord.storage_key.label("key"), StoredFileRecord.size_bytes.label("size")).where(StoredFileRecord.availability_status != "unavailable")
    knowledge = select(KnowledgeStorageRecord.storage_backend.label("backend"), KnowledgeStorageRecord.storage_key.label("key"), KnowledgeStorageRecord.size_bytes.label("size")).where(KnowledgeStorageRecord.state.in_(["available", "cleanup_pending"]))
    objects = union_all(files, knowledge).subquery()
    unique = select(objects.c.backend, objects.c.key, func.max(objects.c.size).label("size")).group_by(objects.c.backend, objects.c.key).subquery()
    try:
        with session_scope() as session:
            rows = session.execute(select(unique.c.backend, func.sum(unique.c.size), func.count()).group_by(unique.c.backend)).all()
            reserved = int(session.scalar(select(func.coalesce(func.sum(KnowledgeStorageRecord.size_bytes), 0)).where(KnowledgeStorageRecord.state == "reserved")) or 0)
        return {"status": "available", "bytes": sum(int(row[1]) for row in rows), "objects": sum(int(row[2]) for row in rows),
                "by_backend": [{"backend": row[0], "bytes": int(row[1]), "objects": int(row[2])} for row in rows], "knowledge_reserved_bytes": reserved}
    except SQLAlchemyError:
        return {"status": "unavailable", "bytes": None, "objects": None, "by_backend": [], "knowledge_reserved_bytes": None}


def _disk_probe() -> tuple[dict, str | None]:
    # A container's '/' is not proof of host capacity. A verified host mount
    # must be explicitly supplied by the operator; no request controls paths.
    host_path = os.getenv("SMARTAI_MONITOR_HOST_DISK_PATH", "").strip()
    path = Path(host_path or (settings.storage_root if settings.storage_backend == "local" else ".")).expanduser()
    scope = "configured_host_mount" if host_path else "application_mount"
    try:
        usage = shutil.disk_usage(path)
        stat = path.stat()
        if usage.total <= 0:
            raise OSError("invalid_disk_size")
        fingerprint = hashlib.sha256(f"{socket.gethostname()}:{path.resolve()}:{stat.st_dev}:{scope}:{settings.storage_backend}".encode()).hexdigest()[:24]
        return {"status": "available", "scope": scope, "total_bytes": usage.total, "used_bytes": usage.used,
                "free_bytes": usage.free, "used_ratio": usage.used / usage.total,
                "host_status": "operator_configured" if host_path else "unavailable"}, fingerprint
    except (OSError, ValueError):
        return {"status": "unavailable", "scope": scope, "total_bytes": None, "used_bytes": None, "free_bytes": None, "used_ratio": None, "host_status": "unavailable"}, None


def _sample_and_history(*, now: float, disk: dict, storage: dict, fingerprint: str) -> dict:
    values = {"id": "capacity-" + hashlib.sha256(f"{fingerprint}:{int(now // SAMPLE_SECONDS)}".encode()).hexdigest()[:48],
              "event_name": SAMPLE_EVENT, "role": "admin", "user_id": None, "occurred_at": now, "success": True,
              "dimensions": {"scope_id": fingerprint, "scope": disk["scope"], "total_bytes": disk["total_bytes"],
                             "used_bytes": disk["used_bytes"], "free_bytes": disk["free_bytes"], "application_bytes": storage["bytes"]}}
    try:
        with session_scope() as session:
            dialect = session.get_bind().dialect.name
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert
            elif dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                return _empty_history("unsupported_database")
            session.execute(insert(Event).values(**values).on_conflict_do_nothing(index_elements=["id"]))
            # Retention applies ONLY to these technical samples, never product
            # usage or audit events. Across instances each distinct scope stays
            # separate and only this scope is returned below.
            session.execute(delete(Event).where(Event.event_name == SAMPLE_EVENT, Event.occurred_at < now - RETENTION_DAYS * DAY))
            rows = session.execute(select(Event.occurred_at, Event.dimensions).where(Event.event_name == SAMPLE_EVENT,
                Event.occurred_at >= now - HISTORY_DAYS * DAY, Event.occurred_at <= now,
                Event.dimensions["scope_id"].as_string() == fingerprint).order_by(Event.occurred_at)
                .limit(HISTORY_DAYS * DAY // SAMPLE_SECONDS + 2)).all()
        samples = [{"at": row[0], **{key: row[1].get(key) for key in ("total_bytes", "used_bytes", "free_bytes", "application_bytes")}} for row in rows]
        # Capacity changes invalidate the older slope, while its observations
        # remain on the graph. Projection only consumes the latest size segment.
        segment: list[dict] = []
        for sample in samples:
            if sample["total_bytes"] != disk["total_bytes"]:
                segment = []
            else:
                segment.append(sample)
        projection = _projection(segment, now)
        return {"status": "available", "coverage_start": samples[0]["at"] if samples else None,
                "sample_interval_seconds": SAMPLE_SECONDS, "retention_days": RETENTION_DAYS, "samples": samples, "projection": projection}
    except SQLAlchemyError:
        return _empty_history("history_unavailable")


def _empty_history(reason: str) -> dict:
    return {"status": "unavailable", "coverage_start": None, "sample_interval_seconds": SAMPLE_SECONDS,
            "retention_days": RETENTION_DAYS, "samples": [], "projection": {"status": "unavailable", "reason": reason, "bytes_per_day": None, "days_remaining": None}}


def _projection(samples: list[dict], now: float) -> dict:
    unavailable = {"status": "unavailable", "reason": "insufficient_samples", "bytes_per_day": None, "days_remaining": None}
    if len(samples) < 3 or samples[-1]["at"] - samples[0]["at"] < DAY:
        return unavailable
    if now - samples[-1]["at"] > 2 * SAMPLE_SECONDS or any(b["at"] - a["at"] > 6 * 3600 for a, b in zip(samples, samples[1:])):
        return {**unavailable, "reason": "sampling_gaps"}
    growth = (samples[0]["free_bytes"] - samples[-1]["free_bytes"]) * DAY / (samples[-1]["at"] - samples[0]["at"])
    return {"status": "available", "reason": "positive_growth" if growth > 0 else "no_positive_growth",
            "bytes_per_day": round(growth), "days_remaining": round(samples[-1]["free_bytes"] / growth, 1) if growth > 0 else None}


def _recommendation(disk: dict, history: dict) -> dict:
    if disk["status"] != "available":
        return {"level": "unavailable", "message": "无法读取磁盘容量，请检查采样目录或挂载权限。"}
    days = history["projection"]["days_remaining"]
    if disk["used_ratio"] >= .9 or disk["free_bytes"] < GIB:
        return {"level": "critical", "message": "磁盘空间紧张：已用达到 90% 或可用不足 1 GiB。请核查日志、备份和待清理文件，安排扩容。"}
    if disk["used_ratio"] >= .8 or disk["free_bytes"] < 2 * GIB or (days is not None and days < 14):
        return {"level": "warning", "message": "建议规划容量：已用达到 80%、可用不足 2 GiB，或按近期净增长估计不足 14 天。先核实趋势与清理积压。"}
    return {"level": "normal", "message": "当前容量未触发提示阈值；扩容前仍需核对宿主监控、备份和实际增长。"}


def collect_monitoring(*, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    database = _database_probe()
    storage_health = _storage_probe()
    storage = _application_storage() if database["status"] == "ok" else {"status": "unavailable", "bytes": None, "objects": None, "by_backend": [], "knowledge_reserved_bytes": None}
    disk, fingerprint = _disk_probe()
    history = _sample_and_history(now=now, disk=disk, storage=storage, fingerprint=fingerprint) if fingerprint and database["status"] == "ok" else _empty_history("history_unavailable")
    return {"metric_version": "capacity-observations-v1", "as_of": now,
            "health": {"application": {"status": "ok", "check": "private_admin_request"}, "database": database, "storage": storage_health,
                       "public_api": {"status": "unavailable", "check": "not_probed"}, "providers": {"status": "unavailable", "check": "not_probed"}},
            "application_storage": storage, "disk": disk, "history": history, "recommendation": _recommendation(disk, history),
            "notes": ["应用占用来自持久文件元数据并按对象去重，包含待清理对象；不等于实测磁盘或对象存储账单。预留知识字节另列。",
                      "未跟踪的孤儿对象、上传中数据、数据库、日志、备份与对象旧版本不包含在应用占用中。",
                      "默认仅观测当前进程可见的分区；容器卷不代表宿主整盘。宿主分区需运维显式配置并核验挂载。",
                      "健康检查只证明当前管理员请求、数据库 SELECT 1、目录访问权限或对象桶 HEAD；不证明读写完整性、公开应用或模型可用。",
                      "打开或刷新监控时采样，每分区每 5 分钟最多一条，保留 90 天、展示近 7 天；未采样时段没有数据，未安装后台监控。",
                      "预测需至少 24 小时样本且间隔不超过 6 小时；按可用空间净减少线性估计，容量变化后重新积累，不保证耗尽日期。"]}
