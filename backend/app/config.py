import os
import secrets
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

basedir = Path(__file__).resolve().parent.parent
load_dotenv(basedir / ".env", override=False)


def _app_env():
    return os.environ.get("APP_ENV", os.environ.get("FLASK_ENV", "development")).lower()


def _is_production():
    return _app_env() in {"production", "prod"}


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _database_uri():
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url.replace("postgres://", "postgresql://", 1)
    if _is_production():
        raise RuntimeError("DATABASE_URL is required in production.")
    return f"sqlite:///{basedir / 'database.db'}"


def _secret_key():
    secret_key = os.environ.get("SECRET_KEY")
    if secret_key:
        return secret_key
    if _is_production():
        raise RuntimeError("SECRET_KEY is required in production.")
    return secrets.token_urlsafe(32)


def _default_school_username():
    return os.environ.get("DEFAULT_SCHOOL_USERNAME", "BestSchool")


def _default_school_name():
    return os.environ.get("DEFAULT_SCHOOL_NAME", "The Best School")


def _default_school_password():
    password = os.environ.get("DEFAULT_SCHOOL_PASSWORD")
    if password:
        return password
    if _is_production():
        return ""
    return "school123"


def _default_master_admin_password():
    password = os.environ.get("DEFAULT_MASTER_ADMIN_PASSWORD")
    if password:
        return password
    if _is_production():
        return ""
    return "masteradmin123"


def sqlalchemy_engine_options(database_uri=None):
    uri = database_uri or _database_uri()
    options = {
        "pool_pre_ping": True,
    }

    if uri.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
        return options

    options.update(
        {
            "pool_recycle": _env_int("SQLALCHEMY_POOL_RECYCLE", 1800),
            "pool_size": _env_int("DB_POOL_SIZE", 5),
            "max_overflow": _env_int("DB_MAX_OVERFLOW", 10),
            "pool_timeout": _env_int("DB_POOL_TIMEOUT", 30),
            "pool_use_lifo": _env_bool("DB_POOL_USE_LIFO", True),
        }
    )
    return options


class BaseConfig:
    """Shared configuration."""

    APP_ENV = _app_env()
    SECRET_KEY = _secret_key()
    SQLALCHEMY_DATABASE_URI = _database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = sqlalchemy_engine_options(SQLALCHEMY_DATABASE_URI)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SECURE = False
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = False
    SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
    REMEMBER_COOKIE_SAMESITE = os.environ.get("REMEMBER_COOKIE_SAMESITE", "Lax")
    SESSION_REFRESH_EACH_REQUEST = False
    PERMANENT_SESSION_LIFETIME = timedelta(hours=_env_int("SESSION_LIFETIME_HOURS", 12))
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = _env_int("WTF_CSRF_TIME_LIMIT", 3600)
    WTF_CSRF_HEADERS = ["X-CSRFToken", "X-CSRF-Token"]
    MAX_CONTENT_LENGTH = _env_int("MAX_CONTENT_LENGTH", 16 * 1024 * 1024)
    JSON_SORT_KEYS = False
    RATELIMIT_ENABLED = _env_bool("RATELIMIT_ENABLED", True)
    SHOW_DEMO_CREDENTIALS = _env_bool("SHOW_DEMO_CREDENTIALS", not _is_production())
    DEFAULT_SCHOOL_USERNAME = _default_school_username()
    DEFAULT_SCHOOL_NAME = _default_school_name()
    DEFAULT_SCHOOL_PASSWORD = _default_school_password()
    PASSWORD_RESET_TOKEN_TTL_MINUTES = _env_int("PASSWORD_RESET_TOKEN_TTL_MINUTES", 30)
    AI_DAILY_LIMIT_NUTRITION_LOOKUP = _env_int("AI_DAILY_LIMIT_NUTRITION_LOOKUP", 100)
    AI_DAILY_LIMIT_RECIPE_LOOKUP = _env_int("AI_DAILY_LIMIT_RECIPE_LOOKUP", 60)
    AI_DAILY_LIMIT_MEAL_GENERATOR = _env_int("AI_DAILY_LIMIT_MEAL_GENERATOR", 30)
    AI_DAILY_LIMIT_HEALTH_INSIGHTS = _env_int("AI_DAILY_LIMIT_HEALTH_INSIGHTS", 30)
    DEFAULT_MASTER_ADMIN_USERNAME = os.environ.get("DEFAULT_MASTER_ADMIN_USERNAME", "platform-admin")
    DEFAULT_MASTER_ADMIN_PASSWORD = _default_master_admin_password()
    DEFAULT_MASTER_ADMIN_EMAIL = os.environ.get("DEFAULT_MASTER_ADMIN_EMAIL", "")
    ERROR_TRACKING_DSN = os.environ.get("ERROR_TRACKING_DSN", "")


class DevelopmentConfig(BaseConfig):
    DEBUG = True


class ProductionConfig(BaseConfig):
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
    PREFERRED_URL_SCHEME = "https"


class TestingConfig(BaseConfig):
    TESTING = True
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
