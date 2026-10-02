from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
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
    engine = get_engine()
    Base.metadata.create_all(engine)
    # Development databases created before Alembic was introduced do not
    # have an ``alembic_version`` row.  ``create_all`` creates new tables but
    # deliberately does not alter existing tables, which used to leave local
    # accounts missing additive columns such as ``users.auth_version``.
    # Apply only safe additive column changes here. Production deployments
    # keep ``SMARTAI_DATABASE_AUTO_CREATE=false`` and use Alembic instead.
    if engine.dialect.name == "sqlite" and settings.database_auto_create:
        _repair_sqlite_additive_columns(engine, Base.metadata)
        _stamp_legacy_sqlite_database(engine)


def _repair_sqlite_additive_columns(engine: Engine, metadata) -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table in metadata.tables.values():
            if table.name not in existing_tables:
                continue
            existing_columns = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                column_type = column.type.compile(dialect=engine.dialect)
                nullable = "" if column.nullable else " NOT NULL"
                default = ""
                if column.server_default is not None:
                    default_value = getattr(column.server_default.arg, "text", column.server_default.arg)
                    default = f" DEFAULT {default_value}"
                elif not column.nullable:
                    raise RuntimeError(
                        f"Cannot add non-nullable local column without a server default: "
                        f"{table.name}.{column.name}"
                    )
                connection.execute(text(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" '
                    f"{column_type}{nullable}{default}"
                ))


def _stamp_legacy_sqlite_database(engine: Engine) -> None:
    """Stamp a schema built by ``create_all`` so Alembic can inspect it.

    Older local databases predate Alembic and may have no version table, or
    may have an empty table left by a failed first migration attempt.  The
    compatibility repair has already checked the current ORM columns, so an
    empty/absent version table represents the complete local schema and can be
    safely marked at the current development head. A non-empty revision is
    never overwritten; it belongs to the migration workflow.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repository_root = Path(__file__).resolve().parents[2]
    alembic_config = Config(str(repository_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(repository_root / "backend" / "db" / "migrations")
    )
    heads = ScriptDirectory.from_config(alembic_config).get_heads()
    if len(heads) != 1:
        raise RuntimeError(
            "Cannot stamp a local SQLite schema while Alembic has multiple heads: "
            + ", ".join(heads)
        )
    schema_head = heads[0]
    inspector = inspect(engine)
    with engine.begin() as connection:
        if "alembic_version" not in inspector.get_table_names():
            connection.execute(text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
            ))
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": schema_head},
            )
            return
        has_revision = connection.execute(
            text("SELECT 1 FROM alembic_version LIMIT 1")
        ).scalar()
        if has_revision is None:
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": schema_head},
            )


def database_ready() -> bool:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
