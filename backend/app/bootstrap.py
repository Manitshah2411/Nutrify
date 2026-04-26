import logging
import os

import click
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from .models import (
    AIUsageLog,
    ApprovalRequest,
    AuditLog,
    Food,
    MealTemplate,
    Notification,
    PasswordResetToken,
    PlatformJob,
    User,
    UserFeedback,
    db,
)


logger = logging.getLogger(__name__)

CURRENT_SCHEMA_REVISION = "4a287724d2e0"
SQLITE_TIMESTAMP_SENTINEL = "1970-01-01 00:00:00"


DEFAULT_FOOD_ITEMS = (
    {"name": "Apple", "calories": 95, "protein": 0.5, "carbs": 25, "fats": 0.3},
    {"name": "Paneer (100g)", "calories": 265, "protein": 20, "carbs": 3.5, "fats": 20},
    {"name": "Moong Dal (1 cup cooked)", "calories": 212, "protein": 14, "carbs": 38, "fats": 0.8},
    {"name": "Rice (1 cup cooked)", "calories": 205, "protein": 4, "carbs": 45, "fats": 0.4},
    {"name": "Tomato (1 medium)", "calories": 22, "protein": 1, "carbs": 5, "fats": 0.2},
    {"name": "Whole Wheat Roti (1)", "calories": 104, "protein": 3, "carbs": 22, "fats": 0.5},
    {"name": "Chickpeas (1 cup cooked)", "calories": 269, "protein": 15, "carbs": 45, "fats": 4},
    {"name": "Banana", "calories": 105, "protein": 1.3, "carbs": 27, "fats": 0.4},
)

REQUIRED_TABLES = (
    "users",
    "student_details",
    "health_metrics",
    "attendance",
    "food",
    "meal_plan",
    "meal_plan_item",
    "meal_templates",
    "meal_template_items",
    "audit_logs",
    "password_reset_tokens",
    "notifications",
    "approval_requests",
    "ai_usage_logs",
    "platform_jobs",
    "user_feedback",
)

LEGACY_SQLITE_COLUMN_PATCHES = {
    "users": {
        "email": "ALTER TABLE users ADD COLUMN email VARCHAR(255)",
        "full_name": "ALTER TABLE users ADD COLUMN full_name VARCHAR(120)",
        "school_id": "ALTER TABLE users ADD COLUMN school_id INTEGER",
        "primary_student_id": "ALTER TABLE users ADD COLUMN primary_student_id INTEGER",
        "last_login_at": "ALTER TABLE users ADD COLUMN last_login_at DATETIME",
        "last_password_change_at": "ALTER TABLE users ADD COLUMN last_password_change_at DATETIME",
        "session_version": "ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1",
        "can_manage_students": "ALTER TABLE users ADD COLUMN can_manage_students BOOLEAN NOT NULL DEFAULT 0",
        "can_manage_meals": "ALTER TABLE users ADD COLUMN can_manage_meals BOOLEAN NOT NULL DEFAULT 0",
        "can_manage_attendance": "ALTER TABLE users ADD COLUMN can_manage_attendance BOOLEAN NOT NULL DEFAULT 0",
        "can_view_reports": "ALTER TABLE users ADD COLUMN can_view_reports BOOLEAN NOT NULL DEFAULT 0",
        "can_manage_staff": "ALTER TABLE users ADD COLUMN can_manage_staff BOOLEAN NOT NULL DEFAULT 0",
        "can_approve_workflows": "ALTER TABLE users ADD COLUMN can_approve_workflows BOOLEAN NOT NULL DEFAULT 0",
        "is_deleted": "ALTER TABLE users ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT 0",
        "deleted_at": "ALTER TABLE users ADD COLUMN deleted_at DATETIME",
        "created_at": f"ALTER TABLE users ADD COLUMN created_at DATETIME NOT NULL DEFAULT '{SQLITE_TIMESTAMP_SENTINEL}'",
        "updated_at": f"ALTER TABLE users ADD COLUMN updated_at DATETIME NOT NULL DEFAULT '{SQLITE_TIMESTAMP_SENTINEL}'",
    },
    "student_details": {
        "guardian_name": "ALTER TABLE student_details ADD COLUMN guardian_name VARCHAR(120)",
        "guardian_email": "ALTER TABLE student_details ADD COLUMN guardian_email VARCHAR(255)",
        "guardian_phone": "ALTER TABLE student_details ADD COLUMN guardian_phone VARCHAR(32)",
        "status": "ALTER TABLE student_details ADD COLUMN status VARCHAR(32) NOT NULL DEFAULT 'active'",
        "is_deleted": "ALTER TABLE student_details ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT 0",
        "deleted_at": "ALTER TABLE student_details ADD COLUMN deleted_at DATETIME",
        "created_at": f"ALTER TABLE student_details ADD COLUMN created_at DATETIME NOT NULL DEFAULT '{SQLITE_TIMESTAMP_SENTINEL}'",
        "updated_at": f"ALTER TABLE student_details ADD COLUMN updated_at DATETIME NOT NULL DEFAULT '{SQLITE_TIMESTAMP_SENTINEL}'",
    },
    "health_metrics": {
        "created_at": f"ALTER TABLE health_metrics ADD COLUMN created_at DATETIME NOT NULL DEFAULT '{SQLITE_TIMESTAMP_SENTINEL}'",
        "updated_at": f"ALTER TABLE health_metrics ADD COLUMN updated_at DATETIME NOT NULL DEFAULT '{SQLITE_TIMESTAMP_SENTINEL}'",
    },
    "attendance": {
        "recorded_by_user_id": "ALTER TABLE attendance ADD COLUMN recorded_by_user_id INTEGER",
        "approval_status": "ALTER TABLE attendance ADD COLUMN approval_status VARCHAR(32) NOT NULL DEFAULT 'approved'",
        "created_at": f"ALTER TABLE attendance ADD COLUMN created_at DATETIME NOT NULL DEFAULT '{SQLITE_TIMESTAMP_SENTINEL}'",
        "updated_at": f"ALTER TABLE attendance ADD COLUMN updated_at DATETIME NOT NULL DEFAULT '{SQLITE_TIMESTAMP_SENTINEL}'",
    },
    "food": {
        "school_id": "ALTER TABLE food ADD COLUMN school_id INTEGER",
        "created_by_user_id": "ALTER TABLE food ADD COLUMN created_by_user_id INTEGER",
        "created_at": f"ALTER TABLE food ADD COLUMN created_at DATETIME NOT NULL DEFAULT '{SQLITE_TIMESTAMP_SENTINEL}'",
        "updated_at": f"ALTER TABLE food ADD COLUMN updated_at DATETIME NOT NULL DEFAULT '{SQLITE_TIMESTAMP_SENTINEL}'",
    },
    "meal_plan": {
        "title": "ALTER TABLE meal_plan ADD COLUMN title VARCHAR(120)",
        "notes": "ALTER TABLE meal_plan ADD COLUMN notes TEXT",
        "status": "ALTER TABLE meal_plan ADD COLUMN status VARCHAR(32) NOT NULL DEFAULT 'approved'",
        "recurrence_label": "ALTER TABLE meal_plan ADD COLUMN recurrence_label VARCHAR(64)",
        "created_by_user_id": "ALTER TABLE meal_plan ADD COLUMN created_by_user_id INTEGER",
        "approved_by_user_id": "ALTER TABLE meal_plan ADD COLUMN approved_by_user_id INTEGER",
        "approved_at": "ALTER TABLE meal_plan ADD COLUMN approved_at DATETIME",
        "template_id": "ALTER TABLE meal_plan ADD COLUMN template_id INTEGER",
        "is_deleted": "ALTER TABLE meal_plan ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT 0",
        "deleted_at": "ALTER TABLE meal_plan ADD COLUMN deleted_at DATETIME",
        "created_at": f"ALTER TABLE meal_plan ADD COLUMN created_at DATETIME NOT NULL DEFAULT '{SQLITE_TIMESTAMP_SENTINEL}'",
        "updated_at": f"ALTER TABLE meal_plan ADD COLUMN updated_at DATETIME NOT NULL DEFAULT '{SQLITE_TIMESTAMP_SENTINEL}'",
    },
    "meal_plan_item": {
        "created_at": f"ALTER TABLE meal_plan_item ADD COLUMN created_at DATETIME NOT NULL DEFAULT '{SQLITE_TIMESTAMP_SENTINEL}'",
        "updated_at": f"ALTER TABLE meal_plan_item ADD COLUMN updated_at DATETIME NOT NULL DEFAULT '{SQLITE_TIMESTAMP_SENTINEL}'",
    },
}

LEGACY_SQLITE_INDEX_PATCHES = (
    ("users", "ix_users_email", "CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)"),
    ("users", "ix_users_is_deleted", "CREATE INDEX IF NOT EXISTS ix_users_is_deleted ON users (is_deleted)"),
    ("users", "ix_users_primary_student_id", "CREATE INDEX IF NOT EXISTS ix_users_primary_student_id ON users (primary_student_id)"),
    ("users", "ix_users_school_id", "CREATE INDEX IF NOT EXISTS ix_users_school_id ON users (school_id)"),
    ("student_details", "ix_student_details_is_deleted", "CREATE INDEX IF NOT EXISTS ix_student_details_is_deleted ON student_details (is_deleted)"),
    ("student_details", "ix_student_details_status", "CREATE INDEX IF NOT EXISTS ix_student_details_status ON student_details (status)"),
    ("attendance", "ix_attendance_approval_status", "CREATE INDEX IF NOT EXISTS ix_attendance_approval_status ON attendance (approval_status)"),
    ("attendance", "ix_attendance_recorded_by_user_id", "CREATE INDEX IF NOT EXISTS ix_attendance_recorded_by_user_id ON attendance (recorded_by_user_id)"),
    ("food", "ix_food_created_by_user_id", "CREATE INDEX IF NOT EXISTS ix_food_created_by_user_id ON food (created_by_user_id)"),
    ("food", "ix_food_school_id", "CREATE INDEX IF NOT EXISTS ix_food_school_id ON food (school_id)"),
    ("meal_plan", "ix_meal_plan_approved_by_user_id", "CREATE INDEX IF NOT EXISTS ix_meal_plan_approved_by_user_id ON meal_plan (approved_by_user_id)"),
    ("meal_plan", "ix_meal_plan_created_by_user_id", "CREATE INDEX IF NOT EXISTS ix_meal_plan_created_by_user_id ON meal_plan (created_by_user_id)"),
    ("meal_plan", "ix_meal_plan_is_deleted", "CREATE INDEX IF NOT EXISTS ix_meal_plan_is_deleted ON meal_plan (is_deleted)"),
    ("meal_plan", "ix_meal_plan_status", "CREATE INDEX IF NOT EXISTS ix_meal_plan_status ON meal_plan (status)"),
    ("meal_plan", "ix_meal_plan_template_id", "CREATE INDEX IF NOT EXISTS ix_meal_plan_template_id ON meal_plan (template_id)"),
)


def _env_flag(name, default="1"):
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def default_school_config():
    return {
        "username": (os.environ.get("DEFAULT_SCHOOL_USERNAME") or "BestSchool").strip(),
        "password": os.environ.get("DEFAULT_SCHOOL_PASSWORD") or "school123",
        "school_name": (os.environ.get("DEFAULT_SCHOOL_NAME") or "The Best School").strip(),
        "email": (os.environ.get("DEFAULT_SCHOOL_EMAIL") or "").strip() or None,
    }


def default_master_admin_config():
    return {
        "username": (os.environ.get("DEFAULT_MASTER_ADMIN_USERNAME") or "platform-admin").strip(),
        "password": (os.environ.get("DEFAULT_MASTER_ADMIN_PASSWORD") or "").strip(),
        "email": (os.environ.get("DEFAULT_MASTER_ADMIN_EMAIL") or "").strip() or None,
    }


def _missing_tables():
    inspector = inspect(db.engine)
    return [table_name for table_name in REQUIRED_TABLES if not inspector.has_table(table_name)]


def _uses_sqlite():
    return db.engine.url.get_backend_name() == "sqlite"


def _sqlite_missing_columns():
    if not _uses_sqlite():
        return {}

    inspector = inspect(db.engine)
    missing_columns = {}
    for table_name, patch_map in LEGACY_SQLITE_COLUMN_PATCHES.items():
        if not inspector.has_table(table_name):
            continue
        existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
        missing_for_table = [
            column_name for column_name in patch_map
            if column_name not in existing_columns
        ]
        if missing_for_table:
            missing_columns[table_name] = missing_for_table
    return missing_columns


def repair_legacy_sqlite_schema():
    missing_columns = _sqlite_missing_columns()
    if not missing_columns:
        return False

    repaired_columns = [
        f"{table_name}.{column_name}"
        for table_name, column_names in missing_columns.items()
        for column_name in column_names
    ]
    logger.warning(
        "Repairing legacy SQLite schema by adding missing columns: %s",
        ", ".join(repaired_columns),
    )

    with db.engine.begin() as connection:
        inspector = inspect(connection)
        for table_name, column_names in missing_columns.items():
            for column_name in column_names:
                connection.execute(text(LEGACY_SQLITE_COLUMN_PATCHES[table_name][column_name]))

        for table_name, _, ddl in LEGACY_SQLITE_INDEX_PATCHES:
            if not inspector.has_table(table_name):
                continue
            connection.execute(text(ddl))

    return True


def sync_sqlite_alembic_revision():
    if not _uses_sqlite():
        return False

    if _missing_tables():
        return False

    if _sqlite_missing_columns():
        return False

    with db.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version ("
                "version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
        )
        current_revision = connection.execute(
            text("SELECT version_num FROM alembic_version LIMIT 1")
        ).scalar()
        if current_revision == CURRENT_SCHEMA_REVISION:
            return False

        connection.execute(text("DELETE FROM alembic_version"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:version_num)"),
            {"version_num": CURRENT_SCHEMA_REVISION},
        )

    logger.info("Synchronized local SQLite schema to Alembic revision %s.", CURRENT_SCHEMA_REVISION)
    return True


def ensure_database_schema():
    missing_tables = _missing_tables()
    if not missing_tables:
        return False

    logger.warning(
        "Missing database tables detected (%s). Creating schema automatically.",
        ", ".join(missing_tables),
    )
    db.create_all()
    return True


def seed_default_data():
    if not _env_flag("SEED_DEFAULT_DATA", "1"):
        return {"school_created": False, "foods_seeded": False, "master_admin_created": False}

    school_config = default_school_config()
    master_admin_config = default_master_admin_config()
    school_created = False
    master_admin_created = False
    foods_seeded = False

    if school_config["username"] and school_config["password"]:
        school_user = User.query.filter_by(username=school_config["username"]).first()
        if school_user is None:
            school_user = User(
                username=school_config["username"],
                role="school",
                school_name=school_config["school_name"] or "The Best School",
                email=school_config["email"],
                full_name=school_config["school_name"] or "The Best School",
                can_manage_students=True,
                can_manage_meals=True,
                can_manage_attendance=True,
                can_view_reports=True,
                can_manage_staff=True,
                can_approve_workflows=True,
            )
            school_user.set_password(school_config["password"])
            db.session.add(school_user)
            school_created = True
        else:
            school_user.school_name = school_user.school_name or school_config["school_name"] or "The Best School"
            school_user.full_name = school_user.full_name or school_user.school_name
            school_user.email = school_user.email or school_config["email"]

    if master_admin_config["username"] and master_admin_config["password"]:
        master_admin = User.query.filter_by(username=master_admin_config["username"]).first()
        if master_admin is None:
            master_admin = User(
                username=master_admin_config["username"],
                role=User.ROLE_MASTER_ADMIN,
                email=master_admin_config["email"],
                full_name="Platform Administrator",
                can_manage_students=True,
                can_manage_meals=True,
                can_manage_attendance=True,
                can_view_reports=True,
                can_manage_staff=True,
                can_approve_workflows=True,
            )
            master_admin.set_password(master_admin_config["password"])
            db.session.add(master_admin)
            master_admin_created = True

    if not Food.query.first():
        db.session.bulk_insert_mappings(Food, list(DEFAULT_FOOD_ITEMS))
        foods_seeded = True

    if not school_created and not foods_seeded and not master_admin_created:
        return {"school_created": False, "foods_seeded": False, "master_admin_created": False}

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        logger.warning("Default data seeding raced with another process. Continuing with existing records.")
        school_created = User.query.filter_by(username=school_config["username"]).first() is not None
        foods_seeded = Food.query.first() is not None
        master_admin_created = bool(master_admin_config["password"]) and User.query.filter_by(username=master_admin_config["username"]).first() is not None

    return {"school_created": school_created, "foods_seeded": foods_seeded, "master_admin_created": master_admin_created}


def bootstrap_database():
    legacy_schema_repaired = repair_legacy_sqlite_schema()
    schema_created = ensure_database_schema()
    alembic_revision_synced = sync_sqlite_alembic_revision()
    seed_result = seed_default_data()

    result = {
        "schema_created": schema_created,
        "legacy_schema_repaired": legacy_schema_repaired,
        "alembic_revision_synced": alembic_revision_synced,
        "school_created": seed_result["school_created"],
        "foods_seeded": seed_result["foods_seeded"],
        "master_admin_created": seed_result["master_admin_created"],
    }
    logger.info("Database bootstrap result: %s", result)
    return result


def register_bootstrap_commands(app):
    @app.cli.command("bootstrap-db")
    def bootstrap_db_command():
        """Create core tables and seed the minimum live data if missing."""
        with app.app_context():
            result = bootstrap_database()

        click.echo(
            "Bootstrapped database "
            f"(schema_created={result['schema_created']}, "
            f"school_created={result['school_created']}, "
            f"foods_seeded={result['foods_seeded']})"
        )
