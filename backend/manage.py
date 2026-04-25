import argparse
import logging

from app import create_app, db
from app.models import Food, User


logger = logging.getLogger(__name__)

DEFAULT_FOOD_ITEMS = [
    {"name": "Apple", "calories": 95, "protein": 0.5, "carbs": 25, "fats": 0.3},
    {"name": "Paneer (100g)", "calories": 265, "protein": 20, "carbs": 3.5, "fats": 20},
    {"name": "Moong Dal (1 cup cooked)", "calories": 212, "protein": 14, "carbs": 38, "fats": 0.8},
    {"name": "Rice (1 cup cooked)", "calories": 205, "protein": 4, "carbs": 45, "fats": 0.4},
    {"name": "Tomato (1 medium)", "calories": 22, "protein": 1, "carbs": 5, "fats": 0.2},
    {"name": "Whole Wheat Roti (1)", "calories": 104, "protein": 3, "carbs": 22, "fats": 0.5},
    {"name": "Chickpeas (1 cup cooked)", "calories": 269, "protein": 15, "carbs": 45, "fats": 4},
    {"name": "Banana", "calories": 105, "protein": 1.3, "carbs": 27, "fats": 0.4},
]


def _production_mode(app):
    return app.config.get("APP_ENV") == "production"


def init_database(app):
    """Drops all tables and re-creates them."""
    with app.app_context():
        logger.warning("Resetting database: dropping all tables.")
        db.drop_all()
        logger.info("Creating new tables.")
        db.create_all()
        logger.info("Database initialized successfully.")


def _bootstrap_school_credentials(app):
    username = app.config.get("DEFAULT_SCHOOL_USERNAME", "BestSchool")
    school_name = app.config.get("DEFAULT_SCHOOL_NAME", "The Best School")
    password = app.config.get("DEFAULT_SCHOOL_PASSWORD", "")

    if not password and not _production_mode(app):
        password = "school123"

    if not password:
        raise RuntimeError(
            "DEFAULT_SCHOOL_PASSWORD must be set before seeding the initial school account."
        )

    return username, password, school_name


def seed_database(app):
    """Seeds the database with idempotent reference data."""
    with app.app_context():
        summary = {
            "school_created": False,
            "foods_added": 0,
        }

        school_username, school_password, school_name = _bootstrap_school_credentials(app)
        if not User.query.filter_by(username=school_username).first():
            logger.info("Creating the bootstrap school account '%s'.", school_username)
            school_user = User(username=school_username, role="school", school_name=school_name)
            school_user.set_password(school_password)
            db.session.add(school_user)
            summary["school_created"] = True
        else:
            logger.info("Bootstrap school account '%s' already exists.", school_username)

        existing_food_names = {food.name for food in Food.query.with_entities(Food.name).all()}
        for food_payload in DEFAULT_FOOD_ITEMS:
            if food_payload["name"] in existing_food_names:
                continue
            db.session.add(Food(**food_payload))
            summary["foods_added"] += 1

        db.session.commit()
        summary["food_count"] = Food.query.count()
        logger.info(
            "Reference data ready. school_created=%s foods_added=%s total_foods=%s",
            summary["school_created"],
            summary["foods_added"],
            summary["food_count"],
        )

        if summary["school_created"]:
            if _production_mode(app):
                logger.info(
                    "Bootstrap school account created for '%s'. Retrieve DEFAULT_SCHOOL_PASSWORD from your deployment environment variables and rotate it after first login.",
                    school_username,
                )
            else:
                logger.info(
                    "You can log in with the school account: '%s' / '%s'",
                    school_username,
                    school_password,
                )

        return summary


def _parser():
    parser = argparse.ArgumentParser(description="Nutrify database and bootstrap management")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("seed", help="Seed idempotent reference data")
    subparsers.add_parser("prepare-deploy", help="Seed reference data after migrations run")
    subparsers.add_parser("init-dev", help="Create tables locally and seed development data")
    subparsers.add_parser("reset-db", help="Drop all tables, recreate them, and seed development data")

    return parser


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = _parser()
    args = parser.parse_args(argv)
    command = args.command or "init-dev"

    app = create_app()
    is_production = _production_mode(app)

    if command in {"reset-db", "init-dev"} and is_production:
        raise RuntimeError(f"{command} is not allowed in production.")

    if command == "reset-db":
        init_database(app)
        seed_database(app)
        return

    if command == "init-dev":
        with app.app_context():
            logger.info("Creating tables for local development if needed.")
            db.create_all()
        seed_database(app)
        return

    if command in {"seed", "prepare-deploy"}:
        seed_database(app)
        return

    parser.error(f"Unknown command: {command}")


if __name__ == "__main__":
    main()
