from datetime import date

from app.bootstrap import bootstrap_database
from app.models import AIAccessPolicy, Notification, PlatformSetting, StudentDetail, User, db
from conftest import create_school, login


def create_master_admin(username="platform-admin", password="masteradmin123"):
    admin = User(username=username, role=User.ROLE_MASTER_ADMIN, full_name="Platform Administrator")
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()
    return admin


def create_school_staff(school, username="staff-user", password="secret123"):
    staff = User(
        username=username,
        role=User.ROLE_SCHOOL_ADMIN,
        school_id=school.id,
        full_name="School Staff",
        can_manage_students=True,
    )
    staff.set_password(password)
    db.session.add(staff)
    db.session.commit()
    return staff


def test_bootstrap_database_creates_master_admin_in_testing(app):
    with app.app_context():
        result = bootstrap_database()
        admin = User.query.filter_by(username="platform-admin").one_or_none()

    assert result["master_admin_created"] is True
    assert admin is not None
    assert admin.check_password("masteradmin123")


def test_master_admin_dashboard_renders(client, app):
    with app.app_context():
        create_master_admin()
        school = create_school()
        student_user = User(username="student-1", role=User.ROLE_USER)
        student_user.set_password("secret123")
        student_user.student_detail = StudentDetail(
            full_name="Student One",
            roll_no=11,
            dob=date(2012, 1, 1),
            sex="Female",
            grade=7,
            section="A",
            school_id=school.id,
        )
        db.session.add(student_user)
        db.session.commit()

    login(client, username="platform-admin", password="masteradmin123")
    response = client.get("/platform")

    assert response.status_code == 200
    assert b"Master admin control plane" in response.data
    assert b"School footprint" in response.data


def test_master_admin_can_create_school(client, app):
    with app.app_context():
        create_master_admin()

    login(client, username="platform-admin", password="masteradmin123")
    response = client.post(
        "/platform/schools",
        data={
            "school_name": "North Ridge School",
            "username": "north-ridge",
            "email": "north@example.com",
            "password": "schoolpass123",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"School created successfully." in response.data
    with app.app_context():
        school = User.query.filter_by(username="north-ridge").one_or_none()
        assert school is not None
        assert school.is_school_root is True
        assert school.check_password("schoolpass123")


def test_master_admin_cannot_downgrade_last_master_admin(client, app):
    with app.app_context():
        admin = create_master_admin()
        admin_id = admin.id
        admin_username = admin.username
        admin_full_name = admin.full_name

    login(client, username="platform-admin", password="masteradmin123")
    response = client.post(
        f"/platform/users/{admin_id}/update",
        data={
            "username": admin_username,
            "role": User.ROLE_USER,
            "full_name": admin_full_name,
            "email": "",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"last active master admin cannot be downgraded" in response.data.lower()
    with app.app_context():
        refreshed = db.session.get(User, admin_id)
        assert refreshed.normalized_role == User.ROLE_MASTER_ADMIN


def test_master_admin_password_reset_forces_change_on_next_sign_in(client, app):
    with app.app_context():
        create_master_admin()
        school = create_school()
        staff = create_school_staff(school)
        staff_id = staff.id

    login(client, username="platform-admin", password="masteradmin123")
    response = client.post(
        f"/platform/users/{staff_id}/reset-password",
        data={"confirm_value": "staff-user", "new_password": "resetpass123"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Password reset successfully." in response.data

    client.post("/logout", follow_redirects=True)
    login_response = client.post("/login", data={"username": "staff-user", "password": "resetpass123"}, follow_redirects=True)

    assert b"Change password" in login_response.data
    with app.app_context():
        refreshed = db.session.get(User, staff_id)
        assert refreshed.force_password_reset is True
        assert refreshed.check_password("resetpass123")


def test_master_admin_can_broadcast_notifications(client, app):
    with app.app_context():
        create_master_admin()
        school = create_school()
        create_school_staff(school)
        school_id = school.id

    login(client, username="platform-admin", password="masteradmin123")
    response = client.post(
        "/platform/notifications/broadcast",
        data={
            "title": "Platform maintenance",
            "message": "We are rolling out updates tonight.",
            "category": "warning",
            "scope": "school",
            "school_id": school_id,
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Broadcast delivered" in response.data
    with app.app_context():
        assert Notification.query.filter_by(school_id=school_id).count() >= 2


def test_master_admin_can_save_ai_policy_and_global_limits(client, app):
    with app.app_context():
        create_master_admin()
        school = create_school()
        staff = create_school_staff(school)
        staff_id = staff.id

    login(client, username="platform-admin", password="masteradmin123")
    client.post(
        "/platform/ai-controls",
        data={
            "form_type": "global-limits",
            "limit_nutrition_lookup": 77,
            "limit_recipe_lookup": 55,
            "limit_meal_generator": 22,
            "limit_health_insights": 15,
        },
        follow_redirects=True,
    )
    client.post(
        "/platform/ai-controls",
        data={
            "form_type": "policy",
            "scope_type": "user",
            "user_id": staff_id,
            "feature": "meal_generator",
            "daily_limit": 4,
            "is_enabled": "1",
            "notes": "Tighter limit",
        },
        follow_redirects=True,
    )

    with app.app_context():
        global_limit = PlatformSetting.query.filter_by(key="ai.daily_limit.nutrition_lookup").one_or_none()
        policy = AIAccessPolicy.query.filter_by(user_id=staff_id, feature="meal_generator").one_or_none()
        assert global_limit is not None
        assert global_limit.value == 77
        assert policy is not None
        assert policy.daily_limit == 4


def test_master_admin_can_export_users_csv(client, app):
    with app.app_context():
        create_master_admin()
        school = create_school()
        create_school_staff(school)

    login(client, username="platform-admin", password="masteradmin123")
    response = client.get("/platform/exports/users.csv")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert b"platform-admin" in response.data
