import logging
import os

import click
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from .models import Food, User, db


logger = logging.getLogger(__name__)


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
)


def _env_flag(name, default="1"):
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def default_school_config():
    return {
        "username": (os.environ.get("DEFAULT_SCHOOL_USERNAME") or "BestSchool").strip(),
        "password": os.environ.get("DEFAULT_SCHOOL_PASSWORD") or "school123",
        "school_name": (os.environ.get("DEFAULT_SCHOOL_NAME") or "The Best School").strip(),
    }


def _missing_tables():
    inspector = inspect(db.engine)
    return [table_name for table_name in REQUIRED_TABLES if not inspector.has_table(table_name)]


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
        return {"school_created": False, "foods_seeded": False}

    school_config = default_school_config()
    school_created = False
    foods_seeded = False

    if school_config["username"] and school_config["password"]:
        school_user = User.query.filter_by(username=school_config["username"]).first()
        if school_user is None:
            school_user = User(
                username=school_config["username"],
                role="school",
                school_name=school_config["school_name"] or "The Best School",
            )
            school_user.set_password(school_config["password"])
            db.session.add(school_user)
            school_created = True

    if not Food.query.first():
        db.session.bulk_insert_mappings(Food, list(DEFAULT_FOOD_ITEMS))
        foods_seeded = True

    if not school_created and not foods_seeded:
        return {"school_created": False, "foods_seeded": False}

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        logger.warning("Default data seeding raced with another process. Continuing with existing records.")
        school_created = User.query.filter_by(username=school_config["username"]).first() is not None
        foods_seeded = Food.query.first() is not None

    return {"school_created": school_created, "foods_seeded": foods_seeded}


def bootstrap_database():
    schema_created = ensure_database_schema()
    seed_result = seed_default_data()

    result = {
        "schema_created": schema_created,
        "school_created": seed_result["school_created"],
        "foods_seeded": seed_result["foods_seeded"],
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
