from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from backend.config import settings


_lock = RLock()
_database_url = ""
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def prepare_sqlite_parent(database_url: str) -> None:
    """Create the parent directory for a file-backed SQLite database."""
    url = make_url(database_url)
    is_named_memory = (
        str(url.query.get("uri", "")).lower() == "true"
        and str(url.query.get("mode", "")).lower() == "memory"
    )
    if (
        url.get_backend_name() != "sqlite"
        or not url.database
        or url.database == ":memory:"
        or is_named_memory
    ):
        return
    Path(url.database).parent.mkdir(parents=True, exist_ok=True)


def validate_database_mode(database_url: str) -> None:
    is_sqlite = database_url.startswith("sqlite")
    is_postgres = database_url.startswith(("postgresql://", "postgresql+"))
    if settings.database_heavy and not is_postgres:
        raise RuntimeError(
            "SMARTAI_DATABASE_HEAVY=ON requires a PostgreSQL SMARTAI_DATABASE_URL."
        )
    if not settings.database_heavy and not is_sqlite:
        raise RuntimeError(
            "SMARTAI_DATABASE_HEAVY=OFF requires a SQLite SMARTAI_DATABASE_URL."
        )


def configure_database(database_url: str | None = None) -> Engine:
    """Configure the process-wide engine, disposing any previous engine."""
    global _database_url, _engine, _session_factory
    url = database_url or settings.database_url
    with _lock:
        if _engine is not None and _database_url == url:
            return _engine
        if _engine is not None:
            _engine.dispose()
        validate_database_mode(url)
        prepare_sqlite_parent(url)
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        if url.startswith("sqlite"):
            event.listen(_engine, "connect", _enable_sqlite_foreign_keys)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
        _database_url = url
        return _engine


def get_engine() -> Engine:
    return configure_database()


def get_session() -> Session:
    configure_database()
    assert _session_factory is not None
    return _session_factory()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_schema() -> None:
    from backend.db.base import Base
    Base.metadata.create_all(get_engine())


def database_ready() -> bool:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
