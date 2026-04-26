"""reconcile legacy enterprise schema

Revision ID: 4a287724d2e0
Revises: 5e076f0f1815
Create Date: 2026-04-26 19:18:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "4a287724d2e0"
down_revision = "5e076f0f1815"
branch_labels = None
depends_on = None


TIMESTAMP_DEFAULT = sa.text("CURRENT_TIMESTAMP")


def _add_missing_columns(table_name, column_factories):
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
    missing_column_names = [column_name for column_name, _ in column_factories if column_name not in existing_columns]
    if not missing_column_names:
        return

    with op.batch_alter_table(table_name, schema=None) as batch_op:
        for column_name, factory in column_factories:
            if column_name in missing_column_names:
                batch_op.add_column(factory())


def _create_index_if_missing(table_name, index_name, ddl):
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return

    existing_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
    if index_name in existing_indexes:
        return

    op.execute(sa.text(ddl))


def upgrade():
    _add_missing_columns(
        "users",
        [
            ("email", lambda: sa.Column("email", sa.String(length=255), nullable=True)),
            ("full_name", lambda: sa.Column("full_name", sa.String(length=120), nullable=True)),
            ("school_id", lambda: sa.Column("school_id", sa.Integer(), nullable=True)),
            ("primary_student_id", lambda: sa.Column("primary_student_id", sa.Integer(), nullable=True)),
            ("last_login_at", lambda: sa.Column("last_login_at", sa.DateTime(), nullable=True)),
            ("last_password_change_at", lambda: sa.Column("last_password_change_at", sa.DateTime(), nullable=True)),
            ("session_version", lambda: sa.Column("session_version", sa.Integer(), nullable=False, server_default="1")),
            ("can_manage_students", lambda: sa.Column("can_manage_students", sa.Boolean(), nullable=False, server_default=sa.false())),
            ("can_manage_meals", lambda: sa.Column("can_manage_meals", sa.Boolean(), nullable=False, server_default=sa.false())),
            ("can_manage_attendance", lambda: sa.Column("can_manage_attendance", sa.Boolean(), nullable=False, server_default=sa.false())),
            ("can_view_reports", lambda: sa.Column("can_view_reports", sa.Boolean(), nullable=False, server_default=sa.false())),
            ("can_manage_staff", lambda: sa.Column("can_manage_staff", sa.Boolean(), nullable=False, server_default=sa.false())),
            ("can_approve_workflows", lambda: sa.Column("can_approve_workflows", sa.Boolean(), nullable=False, server_default=sa.false())),
            ("is_deleted", lambda: sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false())),
            ("deleted_at", lambda: sa.Column("deleted_at", sa.DateTime(), nullable=True)),
            ("created_at", lambda: sa.Column("created_at", sa.DateTime(), nullable=False, server_default=TIMESTAMP_DEFAULT)),
            ("updated_at", lambda: sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=TIMESTAMP_DEFAULT)),
        ],
    )
    _add_missing_columns(
        "student_details",
        [
            ("guardian_name", lambda: sa.Column("guardian_name", sa.String(length=120), nullable=True)),
            ("guardian_email", lambda: sa.Column("guardian_email", sa.String(length=255), nullable=True)),
            ("guardian_phone", lambda: sa.Column("guardian_phone", sa.String(length=32), nullable=True)),
            ("status", lambda: sa.Column("status", sa.String(length=32), nullable=False, server_default="active")),
            ("is_deleted", lambda: sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false())),
            ("deleted_at", lambda: sa.Column("deleted_at", sa.DateTime(), nullable=True)),
            ("created_at", lambda: sa.Column("created_at", sa.DateTime(), nullable=False, server_default=TIMESTAMP_DEFAULT)),
            ("updated_at", lambda: sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=TIMESTAMP_DEFAULT)),
        ],
    )
    _add_missing_columns(
        "health_metrics",
        [
            ("created_at", lambda: sa.Column("created_at", sa.DateTime(), nullable=False, server_default=TIMESTAMP_DEFAULT)),
            ("updated_at", lambda: sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=TIMESTAMP_DEFAULT)),
        ],
    )
    _add_missing_columns(
        "attendance",
        [
            ("recorded_by_user_id", lambda: sa.Column("recorded_by_user_id", sa.Integer(), nullable=True)),
            ("approval_status", lambda: sa.Column("approval_status", sa.String(length=32), nullable=False, server_default="approved")),
            ("created_at", lambda: sa.Column("created_at", sa.DateTime(), nullable=False, server_default=TIMESTAMP_DEFAULT)),
            ("updated_at", lambda: sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=TIMESTAMP_DEFAULT)),
        ],
    )
    _add_missing_columns(
        "food",
        [
            ("school_id", lambda: sa.Column("school_id", sa.Integer(), nullable=True)),
            ("created_by_user_id", lambda: sa.Column("created_by_user_id", sa.Integer(), nullable=True)),
            ("created_at", lambda: sa.Column("created_at", sa.DateTime(), nullable=False, server_default=TIMESTAMP_DEFAULT)),
            ("updated_at", lambda: sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=TIMESTAMP_DEFAULT)),
        ],
    )
    _add_missing_columns(
        "meal_plan",
        [
            ("title", lambda: sa.Column("title", sa.String(length=120), nullable=True)),
            ("notes", lambda: sa.Column("notes", sa.Text(), nullable=True)),
            ("status", lambda: sa.Column("status", sa.String(length=32), nullable=False, server_default="approved")),
            ("recurrence_label", lambda: sa.Column("recurrence_label", sa.String(length=64), nullable=True)),
            ("created_by_user_id", lambda: sa.Column("created_by_user_id", sa.Integer(), nullable=True)),
            ("approved_by_user_id", lambda: sa.Column("approved_by_user_id", sa.Integer(), nullable=True)),
            ("approved_at", lambda: sa.Column("approved_at", sa.DateTime(), nullable=True)),
            ("template_id", lambda: sa.Column("template_id", sa.Integer(), nullable=True)),
            ("is_deleted", lambda: sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false())),
            ("deleted_at", lambda: sa.Column("deleted_at", sa.DateTime(), nullable=True)),
            ("created_at", lambda: sa.Column("created_at", sa.DateTime(), nullable=False, server_default=TIMESTAMP_DEFAULT)),
            ("updated_at", lambda: sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=TIMESTAMP_DEFAULT)),
        ],
    )
    _add_missing_columns(
        "meal_plan_item",
        [
            ("created_at", lambda: sa.Column("created_at", sa.DateTime(), nullable=False, server_default=TIMESTAMP_DEFAULT)),
            ("updated_at", lambda: sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=TIMESTAMP_DEFAULT)),
        ],
    )

    _create_index_if_missing("users", "ix_users_email", "CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)")
    _create_index_if_missing("users", "ix_users_is_deleted", "CREATE INDEX IF NOT EXISTS ix_users_is_deleted ON users (is_deleted)")
    _create_index_if_missing("users", "ix_users_primary_student_id", "CREATE INDEX IF NOT EXISTS ix_users_primary_student_id ON users (primary_student_id)")
    _create_index_if_missing("users", "ix_users_school_id", "CREATE INDEX IF NOT EXISTS ix_users_school_id ON users (school_id)")
    _create_index_if_missing("student_details", "ix_student_details_is_deleted", "CREATE INDEX IF NOT EXISTS ix_student_details_is_deleted ON student_details (is_deleted)")
    _create_index_if_missing("student_details", "ix_student_details_status", "CREATE INDEX IF NOT EXISTS ix_student_details_status ON student_details (status)")
    _create_index_if_missing("attendance", "ix_attendance_approval_status", "CREATE INDEX IF NOT EXISTS ix_attendance_approval_status ON attendance (approval_status)")
    _create_index_if_missing("attendance", "ix_attendance_recorded_by_user_id", "CREATE INDEX IF NOT EXISTS ix_attendance_recorded_by_user_id ON attendance (recorded_by_user_id)")
    _create_index_if_missing("food", "ix_food_created_by_user_id", "CREATE INDEX IF NOT EXISTS ix_food_created_by_user_id ON food (created_by_user_id)")
    _create_index_if_missing("food", "ix_food_school_id", "CREATE INDEX IF NOT EXISTS ix_food_school_id ON food (school_id)")
    _create_index_if_missing("meal_plan", "ix_meal_plan_approved_by_user_id", "CREATE INDEX IF NOT EXISTS ix_meal_plan_approved_by_user_id ON meal_plan (approved_by_user_id)")
    _create_index_if_missing("meal_plan", "ix_meal_plan_created_by_user_id", "CREATE INDEX IF NOT EXISTS ix_meal_plan_created_by_user_id ON meal_plan (created_by_user_id)")
    _create_index_if_missing("meal_plan", "ix_meal_plan_is_deleted", "CREATE INDEX IF NOT EXISTS ix_meal_plan_is_deleted ON meal_plan (is_deleted)")
    _create_index_if_missing("meal_plan", "ix_meal_plan_status", "CREATE INDEX IF NOT EXISTS ix_meal_plan_status ON meal_plan (status)")
    _create_index_if_missing("meal_plan", "ix_meal_plan_template_id", "CREATE INDEX IF NOT EXISTS ix_meal_plan_template_id ON meal_plan (template_id)")


def downgrade():
    pass
