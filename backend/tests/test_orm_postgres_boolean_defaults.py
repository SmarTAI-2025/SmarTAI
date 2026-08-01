"""Regression test for ORM-generated PostgreSQL boolean defaults.

The PostgreSQL integration job (``backend/tests/test_postgres_integration.py``)
and the auto-create path (``backend.db.session.create_schema``) both rely on
``Base.metadata.create_all()``. When the ORM columns in
``backend/db/models.py`` declared boolean columns with
``server_default=text("1")`` / ``text("0")``, ``create_all()`` rendered
``BOOLEAN DEFAULT 1`` / ``BOOLEAN DEFAULT 0``. PostgreSQL rejects that with
``column "..." is of type boolean but default expression is of type integer``
before the integration tests can run.

The baseline Alembic migration was already fixed to emit boolean literals
(``true`` / ``false``); this test pins the *ORM metadata* to the same portable
default so ``create_all()`` agrees with the migration on PostgreSQL while
remaining valid on SQLite.
"""
from __future__ import annotations

import re

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable


def _render_postgres_ddl() -> str:
    """Render ``Base.metadata.create_all()`` DDL for the PostgreSQL dialect.

    This is exactly what ``create_schema()`` would send to a PostgreSQL
    engine, without needing a live database. We import ``backend.db.models``
    so every mapped table is registered on ``Base.metadata``.
    """
    from backend.db.base import Base
    from backend.db import models  # noqa: F401 (registers tables on metadata)

    dialect = postgresql.dialect()
    return "\n".join(
        str(CreateTable(table).compile(dialect=dialect))
        for table in Base.metadata.sorted_tables
    )


def test_orm_boolean_columns_emit_portable_postgres_defaults() -> None:
    """ORM boolean ``server_default`` must render as ``true``/``false`` on PostgreSQL.

    PostgreSQL rejects integer literals as the default for a BOOLEAN column, so
    the ORM metadata that backs ``Base.metadata.create_all()`` must emit a
    boolean literal. SQLite accepts both ``1``/``0`` and ``true``/``false`` so
    switching to boolean literals keeps SQLite working too. This guards the
    three boolean columns: ``users.is_active``, ``provider_configs.enabled``
    and ``grade_results.requires_review``.
    """
    ddl = _render_postgres_ddl()

    expected = {
        "is_active": "true",
        "enabled": "true",
        "requires_review": "false",
    }
    for column, expected_literal in expected.items():
        # PostgreSQL DDL quotes identifiers, so match an optional quote around
        # the column name. The default expression follows DEFAULT and may end
        # with a comma when the column is not the last in the table.
        pattern = re.compile(
            rf'"?{column}"?\s+BOOLEAN\s+DEFAULT\s+(\S+)',
            re.IGNORECASE,
        )
        match = pattern.search(ddl)
        assert match is not None, (
            f"expected a BOOLEAN DEFAULT for {column!r} in the ORM PostgreSQL DDL"
        )
        default = match.group(1).rstrip(",").rstrip(")").lower()
        assert default == expected_literal, (
            f"{column!r} ORM server_default must render as the boolean literal "
            f"{expected_literal!r} on PostgreSQL, got {default!r}"
        )
