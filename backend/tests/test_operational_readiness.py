import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import ProgrammingError

import app as app_module
from app import create_app
from app.bootstrap import CURRENT_SCHEMA_REVISION, bootstrap_database
from app.models import Food, User, db
from manage import INITIAL_SCHEMA_REVISION, ensure_migration_state, seed_database


class ProductionLikeConfig:
    APP_ENV = "production"
    TESTING = True
    SECRET_KEY = "prod-secret"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    DEFAULT_SCHOOL_USERNAME = "BestSchool"
    DEFAULT_SCHOOL_NAME = "The Best School"
    DEFAULT_SCHOOL_PASSWORD = "generated-password"
    SHOW_DEMO_CREDENTIALS = False


class InvalidRuntimeConfig:
    APP_ENV = "development"
    TESTING = True
    SECRET_KEY = ""
    SQLALCHEMY_DATABASE_URI = "not-a-valid-database-url"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False


class ProductionNoBootstrapConfig:
    APP_ENV = "production"
    SECRET_KEY = "prod-secret"
    SQLALCHEMY_DATABASE_URI = "postgresql://nutrify:nutrify@localhost:5432/nutrify"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False


def test_seed_database_is_idempotent(app):
    with app.app_context():
        seed_database(app)
        seed_database(app)

        assert User.query.filter_by(username="BestSchool").count() == 1
        assert Food.query.count() == 8


def test_login_page_hides_demo_credentials_in_production():
    app = create_app(ProductionLikeConfig)
    with app.app_context():
        db.create_all()
        seed_database(app)
        client = app.test_client()

        response = client.get("/login")

    assert response.status_code == 200
    assert b"Demo Account" not in response.data
    assert b"school123" not in response.data


def test_create_app_rejects_invalid_runtime_configuration():
    with pytest.raises(RuntimeError):
        create_app(InvalidRuntimeConfig)


def test_create_app_skips_auto_bootstrap_in_production(monkeypatch):
    def fail_bootstrap():
        raise AssertionError("bootstrap should not run during production worker startup")

    monkeypatch.setattr(app_module, "bootstrap_database", fail_bootstrap)
    app = create_app(ProductionNoBootstrapConfig)

    assert app.config["APP_ENV"] == "production"


def test_bootstrap_repairs_legacy_sqlite_schema(tmp_path):
    database_path = tmp_path / "legacy-bootstrap.db"

    class LegacySQLiteConfig:
        APP_ENV = "testing"
        TESTING = True
        SECRET_KEY = "legacy-secret"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        WTF_CSRF_ENABLED = False
        RATELIMIT_ENABLED = False

    app = create_app(LegacySQLiteConfig)

    with app.app_context():
        with db.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE users (
                    id INTEGER NOT NULL PRIMARY KEY,
                    username VARCHAR(80) NOT NULL UNIQUE,
                    password_hash VARCHAR(256) NOT NULL,
                    role VARCHAR(20) NOT NULL,
                    school_name VARCHAR(120)
                )
            """))
            connection.execute(text("""
                CREATE TABLE student_details (
                    id INTEGER NOT NULL PRIMARY KEY,
                    user_id INTEGER NOT NULL UNIQUE,
                    school_id INTEGER NOT NULL,
                    full_name VARCHAR(120) NOT NULL,
                    roll_no INTEGER NOT NULL,
                    dob DATE NOT NULL,
                    sex VARCHAR(10) NOT NULL,
                    grade INTEGER NOT NULL,
                    section VARCHAR(10) NOT NULL,
                    activity_level VARCHAR(50),
                    allergies TEXT
                )
            """))
            connection.execute(text("""
                CREATE TABLE health_metrics (
                    id INTEGER NOT NULL PRIMARY KEY,
                    student_id INTEGER NOT NULL,
                    record_date DATE NOT NULL,
                    height_cm FLOAT NOT NULL,
                    weight_kg FLOAT NOT NULL
                )
            """))
            connection.execute(text("""
                CREATE TABLE attendance (
                    id INTEGER NOT NULL PRIMARY KEY,
                    student_id INTEGER NOT NULL,
                    attendance_date DATE NOT NULL,
                    ate_breakfast BOOLEAN NOT NULL,
                    ate_lunch BOOLEAN NOT NULL,
                    ate_dinner BOOLEAN NOT NULL
                )
            """))
            connection.execute(text("""
                CREATE TABLE food (
                    id INTEGER NOT NULL PRIMARY KEY,
                    name VARCHAR(100) NOT NULL UNIQUE,
                    calories FLOAT NOT NULL,
                    protein FLOAT NOT NULL,
                    carbs FLOAT NOT NULL,
                    fats FLOAT NOT NULL
                )
            """))
            connection.execute(text("""
                CREATE TABLE meal_plan (
                    id INTEGER NOT NULL PRIMARY KEY,
                    school_id INTEGER NOT NULL,
                    plan_date DATE NOT NULL
                )
            """))
            connection.execute(text("""
                CREATE TABLE meal_plan_item (
                    id INTEGER NOT NULL PRIMARY KEY,
                    meal_plan_id INTEGER NOT NULL,
                    food_id INTEGER NOT NULL,
                    meal_type VARCHAR(20) NOT NULL
                )
            """))

        result = bootstrap_database()
        inspector = inspect(db.engine)

        assert result["legacy_schema_repaired"] is True
        assert result["school_created"] is True
        assert User.query.filter_by(username="BestSchool").one_or_none() is not None
        assert Food.query.count() == 8
        assert "email" in {column["name"] for column in inspector.get_columns("users")}
        assert "is_deleted" in {column["name"] for column in inspector.get_columns("users")}
        assert inspector.has_table("notifications")
        assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar() == CURRENT_SCHEMA_REVISION


def test_ensure_migration_state_stamps_legacy_database(tmp_path):
    database_path = tmp_path / "legacy-stamp.db"

    class LegacySQLiteConfig:
        APP_ENV = "testing"
        TESTING = True
        SECRET_KEY = "legacy-secret"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        WTF_CSRF_ENABLED = False
        RATELIMIT_ENABLED = False

    app = create_app(LegacySQLiteConfig)

    with app.app_context():
        with db.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE users (
                    id INTEGER NOT NULL PRIMARY KEY,
                    username VARCHAR(80) NOT NULL UNIQUE,
                    password_hash VARCHAR(256) NOT NULL,
                    role VARCHAR(20) NOT NULL,
                    school_name VARCHAR(120)
                )
            """))

        result = ensure_migration_state(app)

        assert result == {
            "stamped": True,
            "revision": INITIAL_SCHEMA_REVISION,
            "reason": "legacy_database",
        }
        assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar() == INITIAL_SCHEMA_REVISION


def test_ensure_migration_state_stamps_current_revision_when_schema_is_already_expanded(tmp_path):
    database_path = tmp_path / "enterprise-stamp.db"

    class ExpandedSQLiteConfig:
        APP_ENV = "testing"
        TESTING = True
        SECRET_KEY = "expanded-secret"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        WTF_CSRF_ENABLED = False
        RATELIMIT_ENABLED = False

    app = create_app(ExpandedSQLiteConfig)

    with app.app_context():
        with db.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE users (
                    id INTEGER NOT NULL PRIMARY KEY,
                    username VARCHAR(80) NOT NULL UNIQUE,
                    password_hash VARCHAR(256) NOT NULL,
                    role VARCHAR(20) NOT NULL,
                    school_name VARCHAR(120),
                    email VARCHAR(255)
                )
            """))

        result = ensure_migration_state(app)

        assert result == {
            "stamped": True,
            "revision": CURRENT_SCHEMA_REVISION,
            "reason": "legacy_database",
        }
        assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar() == CURRENT_SCHEMA_REVISION


def test_ensure_migration_state_realigns_misstamped_revision(tmp_path):
    database_path = tmp_path / "misstamped.db"

    class MisstampedSQLiteConfig:
        APP_ENV = "testing"
        TESTING = True
        SECRET_KEY = "misstamped-secret"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        WTF_CSRF_ENABLED = False
        RATELIMIT_ENABLED = False

    app = create_app(MisstampedSQLiteConfig)

    with app.app_context():
        with db.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE users (
                    id INTEGER NOT NULL PRIMARY KEY,
                    username VARCHAR(80) NOT NULL UNIQUE,
                    password_hash VARCHAR(256) NOT NULL,
                    role VARCHAR(20) NOT NULL,
                    school_name VARCHAR(120)
                )
            """))
            connection.execute(text("""
                CREATE TABLE alembic_version (
                    version_num VARCHAR(32) NOT NULL PRIMARY KEY
                )
            """))
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:version_num)"),
                {"version_num": CURRENT_SCHEMA_REVISION},
            )

        result = ensure_migration_state(app)

        assert result == {
            "stamped": True,
            "revision": INITIAL_SCHEMA_REVISION,
            "reason": "schema_revision_mismatch",
        }
        assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar() == INITIAL_SCHEMA_REVISION


def test_load_user_handles_database_programming_error(monkeypatch):
    class SessionRecoveryConfig:
        APP_ENV = "testing"
        TESTING = True
        SECRET_KEY = "session-secret"
        SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        WTF_CSRF_ENABLED = False
        RATELIMIT_ENABLED = False

    app = create_app(SessionRecoveryConfig)

    class FakeOrigError(Exception):
        pass

    def broken_get(*args, **kwargs):
        raise ProgrammingError("SELECT users.email FROM users", {}, FakeOrigError("missing column"))

    with app.app_context():
        monkeypatch.setattr(app_module.db.session, "get", broken_get)

        assert app_module.load_user("1") is None
