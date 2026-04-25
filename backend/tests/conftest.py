import os
import sys
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("APP_ENV", "testing")

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app import create_app
from app.models import db, Food, StudentDetail, User


class TestConfig:
    APP_ENV = "testing"
    TESTING = True
    SECRET_KEY = "test-secret"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def create_school(username="school", password="secret123", school_name="Test School"):
    school = User(username=username, role="school", school_name=school_name)
    school.set_password(password)
    db.session.add(school)
    db.session.commit()
    return school


def create_food(name="Apple"):
    food = Food(name=name, calories=95, protein=0.5, carbs=25, fats=0.3)
    db.session.add(food)
    db.session.commit()
    return food


def create_student(school, username="student", password="secret123"):
    student_user = User(username=username, role="student")
    student_user.set_password(password)
    student_user.student_detail = StudentDetail(
        full_name="Test Student",
        roll_no=1,
        dob=date(2012, 1, 1),
        sex="Female",
        grade=7,
        section="A",
        school_id=school.id,
    )
    db.session.add(student_user)
    db.session.commit()
    return student_user


def login(client, username="school", password="secret123"):
    return client.post("/login", data={"username": username, "password": password})
