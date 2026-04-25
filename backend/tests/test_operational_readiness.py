from app import create_app
from app.models import Food, User, db
from manage import seed_database


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
