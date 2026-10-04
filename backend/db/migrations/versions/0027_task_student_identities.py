"""Keep imported student identities inside tasks, without login accounts.

IDs, answers, revisions, grading results and teacher presentation are unchanged.
Only exact disabled import placeholders with no other user-owned data are removed.
The dormant real-student users/enrollment schema remains available.
"""
from alembic import op
import sqlalchemy as sa

revision = "0027_task_student_identities"
down_revision = "0026_image_capability"
branch_labels = None
depends_on = None

TASK_TABLES = (
    "submissions", "grading_run_submissions", "grade_results",
    "assignment_student_presentations",
)
# Snapshot of every remaining users FK at revision 0026, except enrollments,
# which are checked against the imported task's course separately below.
USER_REFERENCES = (
    ("assignment_workflows", "owner_id"), ("assignments", "teacher_id"),
    ("course_material_groups", "owner_id"), ("course_materials", "owner_id"),
    ("courses", "teacher_id"), ("grading_requests", "owner_id"),
    ("grading_run_setups", "owner_id"), ("grading_runs", "teacher_id"),
    ("invite_codes", "invited_by"), ("invite_codes", "used_by"),
    ("knowledge_documents", "owner_id"), ("knowledge_ingestions", "owner_id"),
    ("knowledge_storage_records", "owner_id"), ("model_daily_usage", "owner_id"),
    ("ocr_provider_credentials", "owner_id"), ("password_reset_requests", "user_id"),
    ("provider_configs", "owner_id"), ("provider_preferences", "owner_id"),
    ("refresh_sessions", "user_id"), ("result_artifact_manifests", "owner_id"),
    ("shared_provider_image_evidence", "owner_id"),
    ("source_storage_reservations", "file_owner_id"),
    ("source_storage_reservations", "quota_owner_id"),
    ("stored_files", "owner_id"), ("stored_files", "source_quota_owner_id"),
    ("tags", "owner_id"), ("task_create_idempotency", "owner_id"),
    ("teacher_reviews", "teacher_id"), ("user_storage_configuration", "owner_id"),
    ("workflow_operations", "owner_id"), ("workflow_source_items", "owner_id"),
)


def _placeholder_candidates():
    return """
        SELECT u.id FROM users u
        WHERE substr(u.id, 1, 9) = 'imported_'
          AND u.username LIKE ('imported-' || substr(u.id, 10) || '-%')
          AND u.role = 'student' AND u.is_active = false AND u.email IS NULL
          AND u.password_hash = '!disabled-imported-account'
          AND NOT EXISTS (
            SELECT 1 FROM submissions s WHERE s.student_id = u.id
              AND NOT EXISTS (
                SELECT 1 FROM assignment_student_presentations p
                WHERE p.assignment_id = s.assignment_id AND p.student_id = u.id
              )
          )
          AND NOT EXISTS (
            SELECT 1 FROM submission_revisions r
            JOIN submissions s ON s.id = r.submission_id
            WHERE s.student_id = u.id AND r.source <> 'teacher_import'
          )
          AND NOT EXISTS (
            SELECT 1 FROM course_enrollments e WHERE e.student_id = u.id
              AND NOT EXISTS (
                SELECT 1 FROM submissions s
                JOIN assignments a ON a.id = s.assignment_id
                WHERE s.student_id = u.id AND a.course_id = e.course_id
              )
          )
    """ + "".join(
        f" AND NOT EXISTS (SELECT 1 FROM {table} r WHERE r.{column} = u.id)"
        for table, column in USER_REFERENCES
    )


def upgrade():
    # The assignment/revision ownership FKs and unique student-per-task keys
    # remain intact. Only the authentication-user dependency is removed.
    for table in TASK_TABLES:
        with op.batch_alter_table(
            table, naming_convention={"fk": "%(table_name)s_%(column_0_name)s_fkey"},
        ) as batch:
            batch.drop_constraint(f"{table}_student_id_fkey", type_="foreignkey")
    candidates = _placeholder_candidates()
    op.execute(sa.text(f"DELETE FROM course_enrollments WHERE student_id IN ({candidates})"))
    op.execute(sa.text(f"DELETE FROM users WHERE id IN ({candidates})"))


def downgrade():
    # An explicit rollback to the old code needs its disabled placeholder
    # accounts again; never create passwords, usable sessions or active users.
    identities = " UNION ".join(f"SELECT student_id FROM {table}" for table in TASK_TABLES)
    op.execute(sa.text(f"""
        INSERT INTO users
          (id, username, email, role, password_hash, is_active, created_at, updated_at)
        SELECT i.student_id, 'imported-' || substr(i.student_id, 10) || '-restored',
               NULL, 'student', '!disabled-imported-account', false, 0, 0
        FROM ({identities}) i
        WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = i.student_id)
    """))
    op.execute(sa.text("""
        INSERT INTO course_enrollments (course_id, student_id, enrolled_at)
        SELECT DISTINCT a.course_id, s.student_id, 0
        FROM submissions s JOIN assignments a ON a.id = s.assignment_id
        JOIN users u ON u.id = s.student_id
        WHERE u.password_hash = '!disabled-imported-account'
          AND NOT EXISTS (
            SELECT 1 FROM course_enrollments e
            WHERE e.course_id = a.course_id AND e.student_id = s.student_id
          )
    """))
    for table in TASK_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.create_foreign_key(
                f"{table}_student_id_fkey", "users", ["student_id"], ["id"],
                ondelete="CASCADE",
            )
