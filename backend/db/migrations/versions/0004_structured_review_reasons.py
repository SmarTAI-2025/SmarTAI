"""store grading review reasons as ordered JSON arrays

Revision ID: 0004_structured_review_reasons
Revises: 0003_assignment_workflow_facade
Create Date: 2026-08-02

The previous singular columns stored multiple stable IDs as one comma-joined
string.  That representation was lossy at API boundaries and made exact reason
matching unreliable.  This migration backfills canonical ordered arrays,
preserves the immutable initial reason set, and normalizes legacy hard failures
so a missing score is never represented as a real zero.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "0004_structured_review_reasons"
down_revision: Union[str, None] = "0003_assignment_workflow_facade"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _normalize_reason_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_values = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw_values = value
    else:
        raw_values = [value]

    normalized: list[str] = []
    for raw in raw_values:
        for part in str(raw).split(","):
            reason = part.strip()
            if reason and reason not in normalized:
                normalized.append(reason)
    return normalized


def _backfill_arrays_online() -> None:
    bind = op.get_bind()
    table = sa.table(
        "grade_results",
        sa.column("id", sa.String()),
        sa.column("review_reason", sa.String()),
        sa.column("initial_review_reason", sa.String()),
        sa.column("review_reasons", sa.JSON()),
        sa.column("initial_review_reasons", sa.JSON()),
    )
    rows = bind.execute(sa.select(
        table.c.id,
        table.c.review_reason,
        table.c.initial_review_reason,
    )).mappings()
    for row in rows:
        current = _normalize_reason_ids(row["review_reason"])
        initial = _normalize_reason_ids(row["initial_review_reason"])
        if not initial:
            initial = list(current)
        bind.execute(
            table.update().where(table.c.id == row["id"]).values(
                review_reasons=current,
                initial_review_reasons=initial,
            )
        )


def _backfill_singular_online() -> None:
    bind = op.get_bind()
    table = sa.table(
        "grade_results",
        sa.column("id", sa.String()),
        sa.column("review_reason", sa.String()),
        sa.column("initial_review_reason", sa.String()),
        sa.column("review_reasons", sa.JSON()),
        sa.column("initial_review_reasons", sa.JSON()),
    )
    rows = bind.execute(sa.select(
        table.c.id,
        table.c.review_reasons,
        table.c.initial_review_reasons,
    )).mappings()
    for row in rows:
        current = _normalize_reason_ids(row["review_reasons"])
        initial = _normalize_reason_ids(row["initial_review_reasons"])
        bind.execute(
            table.update().where(table.c.id == row["id"]).values(
                review_reason=",".join(current) or None,
                initial_review_reason=",".join(initial) or None,
            )
        )


def upgrade() -> None:
    op.add_column(
        "grade_results",
        sa.Column(
            "review_reasons",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "grade_results",
        sa.Column(
            "initial_review_reasons",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )

    if context.is_offline_mode():
        # Offline SQL generation is PostgreSQL-only in this repository.  Keep
        # the generated migration self-contained even though no rows can be
        # fetched through Alembic's mock connection.
        op.execute(sa.text(
            "UPDATE grade_results SET review_reasons = "
            "CASE WHEN review_reason IS NULL OR btrim(review_reason) = '' "
            "THEN '[]'::json ELSE "
            "to_json(regexp_split_to_array(review_reason, '\\s*,\\s*')) END"
        ))
        op.execute(sa.text(
            "UPDATE grade_results SET initial_review_reasons = "
            "CASE WHEN initial_review_reason IS NULL OR btrim(initial_review_reason) = '' "
            "THEN review_reasons ELSE "
            "to_json(regexp_split_to_array(initial_review_reason, '\\s*,\\s*')) END"
        ))
    else:
        _backfill_arrays_online()

    # Before the soft/hard split, an unresolved missing result was represented
    # as ``needs_review`` with no AI score. It has no usable default and must
    # remain release-blocking under the new semantics. A row with a persisted
    # teacher review is different: the review supplies its effective score, so
    # preserve that valid saved-decision state even when it was not explicitly
    # confirmed by the old client.
    op.execute(sa.text(
        "UPDATE grade_results SET result_status = 'failed' "
        "WHERE result_status = 'needs_review' AND ai_score IS NULL "
        "AND NOT EXISTS ("
        "SELECT 1 FROM teacher_reviews "
        "WHERE teacher_reviews.grade_result_id = grade_results.id)"
    ))

    # Legacy failed rows sometimes stored zero as a failure sentinel.  Only the
    # explicit hard-failure state is normalized; a genuine zero in graded or
    # scored soft-review rows remains untouched.
    op.execute(sa.text(
        "UPDATE grade_results SET ai_score = NULL WHERE result_status = 'failed'"
    ))

    with op.batch_alter_table("grade_results") as batch_op:
        batch_op.drop_column("review_reason")
        batch_op.drop_column("initial_review_reason")


def downgrade() -> None:
    # Schema fields are reversible, but the semantic cleanup above is not:
    # after upgrade there is no reliable way to distinguish a legacy failure
    # sentinel from an original NULL/zero value. Operators who may need exact
    # pre-upgrade row restoration must retain a database backup.
    op.add_column(
        "grade_results",
        sa.Column("review_reason", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "grade_results",
        sa.Column("initial_review_reason", sa.String(length=128), nullable=True),
    )

    if context.is_offline_mode():
        op.execute(sa.text(
            "UPDATE grade_results SET review_reason = ("
            "SELECT string_agg(value, ',' ORDER BY ord) "
            "FROM json_array_elements_text(review_reasons) WITH ORDINALITY "
            "AS reasons(value, ord))"
        ))
        op.execute(sa.text(
            "UPDATE grade_results SET initial_review_reason = ("
            "SELECT string_agg(value, ',' ORDER BY ord) "
            "FROM json_array_elements_text(initial_review_reasons) WITH ORDINALITY "
            "AS reasons(value, ord))"
        ))
    else:
        _backfill_singular_online()

    with op.batch_alter_table("grade_results") as batch_op:
        batch_op.drop_column("review_reasons")
        batch_op.drop_column("initial_review_reasons")
