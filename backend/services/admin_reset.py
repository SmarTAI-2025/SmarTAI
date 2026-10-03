"""Offline reset of explicitly enrolled, exclusive SmarTAI business resources.

Only the private offline maintenance application may adapt this executor. Preview is read-only. The executor
requires offline services, an exact preview confirmation, and exclusive locks.
Only safe error codes/counts leave this module; no row or object content is read.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Iterator
import uuid

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from backend.db.base import Base

SENTINEL = ".smartai-disposable-storage"
SENTINEL_CONTENT = {"schema_version": 1, "purpose": "smartai-disposable-business-storage"}
CONFIRMATION = "ERASE ALL DISPOSABLE BUSINESS DATA"
_ADVISORY_LOCK = 734720260102
_MAX_ITEMS = 100_000
_REPO = Path(__file__).resolve().parents[2]
_BLOCKED_NAMES = {".env", ".git", "alembic.ini", "pyproject.toml", "config.json", "config.toml", "docker-compose.yml"}


class ResetError(RuntimeError):
    """Public-safe, stable maintenance failure code."""


@dataclass(frozen=True)
class ResetScope:
    engine: Engine = field(repr=False)
    runtime_environment: str
    maintenance_dir: Path
    local_roots: tuple[Path, ...] = ()
    object_store: S3ResetStore | None = field(default=None, repr=False)
    password_hash: str = field(default="", repr=False)
    enabled: bool = False
    object_client_required: bool = False
    temporary_root: Path | None = None
    require_temporary_root: bool = False


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _safe_path(value: Path) -> Path:
    path = Path(os.path.abspath(Path(value).expanduser()))
    # lstat each existing component: resolve() alone would conceal symlinks.
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ResetError("reset_symlink_forbidden")
    return path


def _under(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _validate_scope(scope: ResetScope) -> dict[str, Any]:
    if not scope.enabled:
        raise ResetError("reset_disabled")
    if scope.object_client_required and scope.object_store is None:
        raise ResetError("reset_object_client_required")
    dialect = scope.engine.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise ResetError("reset_database_unsupported")
    url = scope.engine.url
    if dialect == "sqlite":
        if not url.database or url.database == ":memory:" or url.query:
            raise ResetError("reset_requires_file_database")
        database_path = _safe_path(Path(url.database))
        if not database_path.is_file():
            raise ResetError("reset_database_missing")
        database_identity = ["sqlite", str(database_path)]
    else:
        database_path = None
        # Credentials and query parameters are deliberately excluded.
        database_identity = ["postgresql", url.host, url.port, url.database]
    control = _safe_path(scope.maintenance_dir)
    if control.exists() and (control.stat().st_uid != os.getuid() or control.stat().st_mode & 0o022):
        raise ResetError("reset_maintenance_directory_permissions_unsafe")
    roots = tuple(_safe_path(root) for root in scope.local_roots)
    if scope.require_temporary_root and (scope.temporary_root is None or _safe_path(scope.temporary_root) not in roots):
        raise ResetError("reset_dedicated_temporary_root_required")
    if not roots and scope.object_store is None:
        raise ResetError("reset_storage_scope_required")
    protected = [Path("/"), Path.home(), _REPO, _REPO / "data", Path("/tmp"), Path("/private/tmp")]
    for path in (control, *roots):
        if len(path.parts) < 3 or any(_under(p, path) for p in protected):
            raise ResetError("reset_unsafe_directory")
    for root in roots:
        if not root.is_dir() or root.is_mount():
            raise ResetError("reset_requires_dedicated_storage_directory")
        if _under(control, root) or _under(root, control) or database_path and _under(database_path, root):
            raise ResetError("reset_storage_overlaps_database_or_control")
        if any(root != other and (_under(root, other) or _under(other, root)) for other in roots):
            raise ResetError("reset_storage_roots_overlap")
        marker = root / SENTINEL
        try:
            if marker.is_symlink() or marker.stat().st_nlink != 1 or marker.stat().st_size > 256:
                raise ResetError("reset_disposable_storage_marker_required")
            if json.loads(marker.read_text()) not in (SENTINEL_CONTENT, {"schema_version": 1, "purpose": "smartai-exclusive-business-storage"}):
                raise ResetError("reset_disposable_storage_marker_required")
        except (OSError, ValueError):
            raise ResetError("reset_disposable_storage_marker_required") from None
    if database_path and _under(database_path, control):
        raise ResetError("reset_database_inside_control")
    if len(set(roots)) != len(roots):
        raise ResetError("reset_storage_roots_overlap")
    return {
        "database": database_identity,
        "maintenance": str(control),
        "local_storage": [[str(root), root.stat().st_dev, root.stat().st_ino] for root in roots],
        "object_storage": scope.object_store.identity if scope.object_store else None,
        "environment": scope.runtime_environment,
    }


def _local_inventory(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    result = []
    for parent, directories, names in os.walk(root, followlinks=False):
        for name in directories + names:
            path = Path(parent) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ResetError("reset_symlink_forbidden")
            if info.st_dev != root.stat().st_dev:
                raise ResetError("reset_mount_boundary_forbidden")
            if name in _BLOCKED_NAMES or name.startswith(".env.") or name == "config":
                raise ResetError("reset_configuration_in_storage")
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ResetError("reset_special_or_shared_file_forbidden")
            if path == root / SENTINEL:
                continue
            if name in _BLOCKED_NAMES or name.startswith(".env.") or name.endswith((".db", ".sqlite", ".sqlite3", ".pem", ".key", "-wal", "-shm")):
                raise ResetError("reset_configuration_in_storage")
            item = {"identity": _digest([str(path.relative_to(root)), info.st_ino, info.st_mtime_ns, info.st_size]), "bytes": info.st_size}
            result.append((path, item))
            if len(result) > _MAX_ITEMS:
                raise ResetError("reset_inventory_too_large")
    return sorted(result, key=lambda pair: str(pair[0]))


class S3ResetStore:
    """Injected S3 client; tests use fakes, construction never contacts a cloud.

    Full bucket only. Require an operator-provisioned disposable tag, version
    listing, multipart listing and no replication/object-lock configuration.
    Unsupported or ambiguous S3-compatible APIs fail closed.
    """

    def __init__(self, client: Any, *, bucket: str, endpoint_identity: str):
        if not bucket or not endpoint_identity:
            raise ResetError("reset_object_scope_required")
        self.client, self.bucket = client, bucket
        self.identity = _digest([bucket, endpoint_identity])

    def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        result = getattr(self.client, method)(Bucket=self.bucket, **kwargs)
        if not isinstance(result, dict):
            raise ResetError("reset_object_listing_invalid")
        return result

    def validate(self) -> None:
        try:
            tags = self._call("get_bucket_tagging").get("TagSet", [])
            if not any({"Key": "smartai-reset-scope", "Value": value} in tags for value in ("disposable", "smartai-exclusive-business")):
                raise ResetError("reset_disposable_bucket_tag_required")
            versioning = self._call("get_bucket_versioning")
            if versioning.get("MFADelete") == "Enabled":
                raise ResetError("reset_object_mfa_delete_unsupported")
            for method, missing in (("get_bucket_replication", "ReplicationConfigurationNotFoundError"), ("get_object_lock_configuration", "ObjectLockConfigurationNotFoundError")):
                try:
                    self._call(method)
                except Exception as exc:
                    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                    if code != missing:
                        raise ResetError("reset_object_safety_unverified") from None
                else:
                    raise ResetError("reset_replication_or_object_lock_unsupported")
        except ResetError:
            raise
        except Exception:
            raise ResetError("reset_object_safety_unverified") from None

    def _pages(self, method: str, next_fields: dict[str, str]) -> Iterator[dict[str, Any]]:
        request: dict[str, str] = {}
        seen = set()
        while True:
            page = self._call(method, **request)
            if type(page.get("IsTruncated")) is not bool:
                raise ResetError("reset_object_listing_invalid")
            yield page
            if not page["IsTruncated"]:
                break
            marker = {target: page[source] for source, target in next_fields.items() if page.get(source) is not None}
            if not marker or any(not isinstance(value, str) or not value for value in marker.values()):
                raise ResetError("reset_object_listing_invalid")
            signature = _digest(marker)
            if signature in seen:
                raise ResetError("reset_object_listing_invalid")
            seen.add(signature)
            request = marker

    def inventory(self) -> list[dict[str, Any]]:
        self.validate()
        result = []
        try:
            for page in self._pages("list_object_versions", {"NextKeyMarker": "KeyMarker", "NextVersionIdMarker": "VersionIdMarker"}):
                for kind in ("Versions", "DeleteMarkers"):
                    for item in page.get(kind, []):
                        if not isinstance(item.get("Key"), str) or not isinstance(item.get("VersionId"), str):
                            raise ResetError("reset_object_listing_invalid")
                        result.append({"kind": "version", "key": item["Key"], "version": item["VersionId"], "bytes": int(item.get("Size", 0))})
            known_keys = {item["key"] for item in result}
            for page in self._pages("list_objects_v2", {"NextContinuationToken": "ContinuationToken"}):
                for item in page.get("Contents", []):
                    if not isinstance(item.get("Key"), str):
                        raise ResetError("reset_object_listing_invalid")
                    if item["Key"] not in known_keys:
                        # If versions are not enumerable, do not trust a backend
                        # to delete them via a plain delete marker operation.
                        raise ResetError("reset_object_versions_incomplete")
            for page in self._pages("list_multipart_uploads", {"NextKeyMarker": "KeyMarker", "NextUploadIdMarker": "UploadIdMarker"}):
                for item in page.get("Uploads", []):
                    if not isinstance(item.get("Key"), str) or not isinstance(item.get("UploadId"), str):
                        raise ResetError("reset_object_listing_invalid")
                    result.append({"kind": "multipart", "key": item["Key"], "upload": item["UploadId"], "bytes": 0})
            if len(result) > _MAX_ITEMS:
                raise ResetError("reset_inventory_too_large")
            return sorted(result, key=lambda item: json.dumps(item, sort_keys=True))
        except ResetError:
            raise
        except Exception:
            raise ResetError("reset_object_inventory_unavailable") from None

    def purge(self, items: list[dict[str, Any]]) -> None:
        try:
            for item in items:
                if item["kind"] == "multipart":
                    self._call("abort_multipart_upload", Key=item["key"], UploadId=item["upload"])
                else:
                    self._call("delete_object", Key=item["key"], VersionId=item["version"])
        except Exception:
            raise ResetError("reset_object_delete_failed") from None
        if self.inventory():
            raise ResetError("reset_object_delete_not_verified")


def _database_inventory(connection: Connection) -> dict[str, Any]:
    inspector = inspect(connection)
    actual = set(inspector.get_table_names()) - {"alembic_version"}
    expected = {table.name for table in Base.metadata.tables.values()}
    if actual != expected:
        raise ResetError("reset_database_schema_mismatch")
    # Count every table, including tables with no FK and newly added account/
    # block records. Never sample rows, tokens, provider keys or identities.
    quote = connection.dialect.identifier_preparer.quote
    counts = {name: int(connection.execute(text(f"SELECT count(*) FROM {quote(name)}")).scalar_one()) for name in sorted(actual)}
    schema = {name: sorted(column["name"] for column in inspector.get_columns(name)) for name in sorted(actual)}
    revisions = []
    if "alembic_version" in inspector.get_table_names():
        revisions = sorted(connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all())
    backends = set()
    for table in Base.metadata.tables.values():
        if "storage_backend" in table.c:
            backends.update(connection.execute(text(f"SELECT DISTINCT storage_backend FROM {quote(table.name)}")).scalars())
    return {"counts": counts, "schema": _digest(schema), "revisions": revisions, "storage_backends": sorted(backends)}


def _inventory(scope: ResetScope, connection: Connection) -> tuple[dict[str, Any], list[tuple[Path, dict[str, Any]]], list[dict[str, Any]]]:
    identity = _validate_scope(scope)
    database = _database_inventory(connection)
    included = ({"local"} if scope.local_roots else set()) | ({"object"} if scope.object_store else set())
    if not set(database["storage_backends"]) <= included:
        raise ResetError("reset_persisted_storage_backend_not_in_scope")
    local = [entry for root in scope.local_roots for entry in _local_inventory(_safe_path(root))]
    objects = scope.object_store.inventory() if scope.object_store else []
    plan = {
        "scope_fingerprint": _digest(identity), "database": database,
        "files": sorted(_digest([str(path.parent), item]) for path, item in local),
        "objects": sorted(_digest(item) for item in objects),
        "counts": {"files": len(local), "versions_and_markers": sum(item["kind"] == "version" for item in objects), "multipart_uploads": sum(item["kind"] == "multipart" for item in objects), "bytes": sum(item["bytes"] for _, item in local) + sum(item["bytes"] for item in objects)},
    }
    plan["fingerprint"] = _digest(plan)
    return plan, local, objects


def preview_reset(scope: ResetScope) -> dict[str, Any]:
    """Read-only, admin-authorized adapter may expose this safe summary."""
    _validate_scope(scope)
    with scope.engine.connect() as connection:
        plan, _, _ = _inventory(scope, connection)
    fingerprint = plan["fingerprint"] + "-" + uuid.uuid4().hex
    return {
        "schema_version": 1, "fingerprint": fingerprint,
        "scope_fingerprint": plan["scope_fingerprint"],
        "confirmation": f"{CONFIRMATION} {fingerprint[:12]}",
        "tables": plan["database"]["counts"], "storage": plan["counts"],
        "environment": scope.runtime_environment,
        "scope": "all_users_including_administrators_and_all_business_data",
        "execution": "protected_offline_maintenance",
        "target": {"database": f"{scope.engine.dialect.name}:{_digest(_validate_scope(scope)["database"])[:16]}", "local_roots": [str(root) for root in scope.local_roots], "object_scope": scope.object_store.identity[:16] if scope.object_store else None}, "bootstrap_required": True,
        "preserved": ["schema", "alembic_version", "infrastructure", "deployment_configuration", "deployment_secrets", "disposable_storage_marker"],
        "limitations": ["stop_all_public_private_worker_and_scheduler_processes", "restart_all_processes_to_drop_in_memory_caches", "external_backups_and_provider_copies_are_outside_scope", "multipart_bytes_are_not_in_byte_total", "partial_file_deletion_cannot_be_rolled_back"],
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    if path.is_symlink():
        raise ResetError("reset_symlink_forbidden")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with open(temporary, "x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_json(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise ResetError("reset_symlink_forbidden")
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise ResetError("reset_maintenance_record_invalid") from None


def assert_reset_not_in_progress(maintenance_dir: Path) -> None:
    """Startup check; use the lifetime guard too, to close the start/reset race."""
    control = _safe_path(maintenance_dir)
    if (control / "active-reset.json").exists() or (control / "active-reset.json").is_symlink():
        raise ResetError("reset_incomplete_services_must_stay_stopped")


@contextmanager
def _process_lock(scope: ResetScope, *, shared: bool = False) -> Iterator[None]:
    try:
        import fcntl
    except ImportError:
        raise ResetError("reset_requires_posix_maintenance_host") from None
    control = _safe_path(scope.maintenance_dir)
    control.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = control / "service-reset.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    pg_connection = None
    try:
        if os.fstat(descriptor).st_nlink != 1:
            raise ResetError("reset_maintenance_lock_invalid")
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ResetError("reset_services_or_executor_running") from None
        if scope.engine.dialect.name == "postgresql":
            pg_connection = scope.engine.connect()
            function = "pg_try_advisory_lock_shared" if shared else "pg_try_advisory_lock"
            if not pg_connection.execute(text(f"SELECT {function}(:key)"), {"key": _ADVISORY_LOCK}).scalar_one():
                raise ResetError("reset_services_or_executor_running")
        yield
    finally:
        try:
            if pg_connection is not None:
                try:
                    function = "pg_advisory_unlock_shared" if shared else "pg_advisory_unlock"
                    pg_connection.execute(text(f"SELECT {function}(:key)"), {"key": _ADVISORY_LOCK})
                finally:
                    pg_connection.close()
        finally:
            os.close(descriptor)


@contextmanager
def maintenance_service_guard(scope: ResetScope) -> Iterator[None]:
    """Hold for the ENTIRE public/private/worker lifetime on reset-enabled dev.

    Does not require an empty/dedicated upload root: ordinary service startup
    remains possible before the operator explicitly marks storage disposable.
    """
    with _process_lock(scope, shared=True):
        assert_reset_not_in_progress(scope.maintenance_dir)
        yield


@contextmanager
def _offline_database(scope: ResetScope) -> Iterator[Connection]:
    with scope.engine.connect() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("PRAGMA secure_delete=ON")
            connection.exec_driver_sql("PRAGMA busy_timeout=1000")
            connection.exec_driver_sql("BEGIN EXCLUSIVE")
            connection.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
        else:
            connection.begin()
            names = sorted(table.name for table in Base.metadata.tables.values())
            quote = connection.dialect.identifier_preparer.quote
            connection.execute(text("LOCK TABLE " + ", ".join(quote(name) for name in names) + " IN ACCESS EXCLUSIVE MODE NOWAIT"))
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def _purge_database(connection: Connection) -> None:
    names = sorted(table.name for table in Base.metadata.tables.values())
    quote = connection.dialect.identifier_preparer.quote
    if connection.dialect.name == "postgresql":
        connection.execute(text("TRUNCATE " + ", ".join(quote(name) for name in names) + " RESTART IDENTITY"))
    else:
        for name in names:
            connection.execute(text(f"DELETE FROM {quote(name)}"))
        if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise ResetError("reset_foreign_key_validation_failed")
    if any(_database_inventory(connection)["counts"].values()):
        raise ResetError("reset_database_empty_verification_failed")


def _compact_sqlite(scope: ResetScope) -> None:
    if scope.engine.dialect.name != "sqlite":
        return
    # Release freelist pages and truncate WAL after logical erasure. This is
    # application-level cleanup, not a claim of forensic/backup sanitization.
    with scope.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        result = connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").one()
        if result[0] != 0:
            raise ResetError("reset_sqlite_checkpoint_busy")
        connection.exec_driver_sql("VACUUM")
        if connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").one()[0] != 0:
            raise ResetError("reset_sqlite_checkpoint_busy")


def execute_reset(scope: ResetScope, *, fingerprint: str, confirmation: str, services_stopped: bool, maintenance_password: str = "") -> dict[str, Any]:
    """Resume the same plan on failure; repeat after success is a no-op receipt.

    Storage can partially disappear before an error; the DB transaction stays
    intact and a durable marker blocks startup. The isolated maintenance app runs this in one background thread after authorization.
    """
    _validate_scope(scope)
    from backend.services.maintenance_auth import verify_maintenance_password
    if not services_stopped:
        raise ResetError("reset_stop_all_services_required")
    if not re.fullmatch(r"[0-9a-f]{64}-[0-9a-f]{32}", fingerprint) or confirmation != f"{CONFIRMATION} {fingerprint[:12]}":
        raise ResetError("reset_confirmation_mismatch")
    control = _safe_path(scope.maintenance_dir)
    active_path = control / "active-reset.json"
    with _process_lock(scope):
        verify_maintenance_password(scope, maintenance_password)
        receipt_path = control / f"receipt-{fingerprint}.json"
        receipt = _read_json(receipt_path)
        if receipt:
            if receipt.get("scope_fingerprint") != _digest(_validate_scope(scope)):
                raise ResetError("reset_receipt_scope_mismatch")
            leftover = _read_json(active_path)
            if leftover and leftover.get("fingerprint") == fingerprint:
                active_path.unlink()
            return {**receipt, "replayed": True}
        active = _read_json(active_path)
        if active and active.get("fingerprint") != fingerprint:
            raise ResetError("reset_another_plan_incomplete")
        try:
            with _offline_database(scope) as connection:
                current, local, objects = _inventory(scope, connection)
                if active:
                    original = active["plan"]
                    if current["scope_fingerprint"] != original["scope_fingerprint"] or current["database"] != original["database"]:
                        # A crash after COMMIT but before receipt persistence is
                        # recoverable only if every table and byte is now empty.
                        empty = not any(current["database"]["counts"].values()) and not local and not objects
                        if not (empty and current["scope_fingerprint"] == original["scope_fingerprint"] and current["database"]["schema"] == original["database"]["schema"] and current["database"]["revisions"] == original["database"]["revisions"]):
                            raise ResetError("reset_scope_changed_during_recovery")
                    if not set(current["files"]) <= set(original["files"]) or not set(current["objects"]) <= set(original["objects"]):
                        raise ResetError("reset_new_storage_during_recovery")
                else:
                    if current["fingerprint"] != fingerprint.split("-", 1)[0]:
                        raise ResetError("reset_preview_stale")
                    active = {"schema_version": 1, "fingerprint": fingerprint, "operation_id": uuid.uuid4().hex, "started_at": time.time(), "plan": current, "phase": "storage"}
                    _write_json(active_path, active)
                active["files_deleted"] = 0
                active["files_remaining"] = len(local)
                for path, _ in local:
                    # Recheck containment and links immediately before unlink.
                    _safe_path(path)
                    if path.lstat().st_nlink != 1:
                        raise ResetError("reset_special_or_shared_file_forbidden")
                    path.unlink()
                    active["files_deleted"] += 1
                    active["files_remaining"] -= 1
                    if active["files_deleted"] % 100 == 0 or not active["files_remaining"]:
                        _write_json(active_path, active)
                if scope.object_store:
                    scope.object_store.purge(objects)
                for root in scope.local_roots:
                    if _local_inventory(_safe_path(root)):
                        raise ResetError("reset_local_delete_not_verified")
                    # Leave only the dedicated root and its config sentinel.
                    directories = [p for p in root.rglob("*") if p.is_dir()]
                    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
                        directory.rmdir()
                active["phase"] = "database"
                _write_json(active_path, active)
                _purge_database(connection)
            _compact_sqlite(scope)
            receipt = {"schema_version": 1, "operation_id": active["operation_id"], "fingerprint": fingerprint, "scope_fingerprint": active["plan"]["scope_fingerprint"], "started_at": active["started_at"], "completed_at": time.time(), "status": "completed", "rows_deleted": sum(active["plan"]["database"]["counts"].values()), "storage": active["plan"]["counts"], "bootstrap_required": True}
            _write_json(receipt_path, receipt)
            active_path.unlink()
            return receipt
        except BaseException as exc:
            if active is not None:
                active["phase"] = "failed"
                active["error_code"] = str(exc) if isinstance(exc, ResetError) else "reset_failed_keep_services_stopped"
                _write_json(active_path, active)
            if isinstance(exc, (ResetError, KeyboardInterrupt, SystemExit)):
                raise
            raise ResetError("reset_failed_keep_services_stopped") from None


def scope_from_settings(*, object_client: Any = None) -> ResetScope:
    """Shared settings adapter; no implicit S3 client or network construction."""
    from backend.config import settings
    from backend.db.session import get_engine
    maintenance = os.environ.get("SMARTAI_ADMIN_MAINTENANCE_DIR", "").strip()
    if not maintenance:
        raise ResetError("reset_maintenance_directory_required")
    store = None
    try:
        extra = json.loads(os.environ.get("SMARTAI_ADMIN_RESET_EXTRA_STORAGE_ROOTS", "[]"))
        if not isinstance(extra, list) or any(not isinstance(path, str) or not path.strip() for path in extra):
            raise ValueError
    except ValueError:
        raise ResetError("reset_extra_storage_roots_invalid") from None
    roots: tuple[Path, ...] = tuple(Path(path) for path in extra)
    if settings.storage_backend == "local":
        roots = (Path(settings.storage_root), *roots)
    elif object_client is not None:
        store = S3ResetStore(object_client, bucket=settings.storage_s3_bucket or "", endpoint_identity=settings.storage_s3_endpoint or f"aws:{settings.storage_s3_region}")
    return ResetScope(engine=get_engine(), runtime_environment=settings.runtime_environment, maintenance_dir=Path(maintenance), local_roots=roots, object_store=store, password_hash=os.environ.get("SMARTAI_ADMIN_RESET_PASSWORD_HASH", ""), enabled=os.environ.get("SMARTAI_ADMIN_RESET_ENABLED", "false").lower() == "true", object_client_required=settings.storage_backend == "object", temporary_root=Path(os.environ["TMPDIR"]) if os.environ.get("TMPDIR") else None, require_temporary_root=True)
