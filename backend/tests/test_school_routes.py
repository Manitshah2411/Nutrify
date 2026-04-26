import json
from datetime import date, timedelta

from werkzeug.security import generate_password_hash as generate_legacy_password_hash

from app import routes
from app.models import ApprovalRequest, Attendance, MealPlan, MealPlanItem, MealTemplate, MealTemplateItem, Notification, StudentDetail, User, db
from conftest import create_food, create_school, create_student, login


def test_dashboard_removes_backdated_meal_plans(client, app):
    with app.app_context():
        school = create_school()
        old_plan = MealPlan(school_id=school.id, plan_date=date.today() - timedelta(days=1))
        current_plan = MealPlan(school_id=school.id, plan_date=date.today())
        db.session.add_all([old_plan, current_plan])
        db.session.commit()
        old_plan_id = old_plan.id
        current_plan_id = current_plan.id

    login(client)
    response = client.get("/dashboard")

    assert response.status_code == 200
    with app.app_context():
        assert db.session.get(MealPlan, old_plan_id) is None
        assert db.session.get(MealPlan, current_plan_id) is not None


def test_school_dashboard_renders_for_schools(client, app):
    with app.app_context():
        create_school()

    login(client)
    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"School Dashboard" in response.data
    assert b"Mark Attendance" in response.data


def test_create_meal_plan_rejects_past_date(client, app):
    with app.app_context():
        create_school()
        food = create_food()
        food_id = food.id

    login(client)
    response = client.post(
        "/create-meal-plan",
        data={
            "plan_date": (date.today() - timedelta(days=1)).isoformat(),
            "breakfast_foods": [str(food_id)],
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        assert MealPlan.query.count() == 0


def test_create_meal_plan_rejects_invalid_food_ids(client, app):
    with app.app_context():
        create_school()

    login(client)
    response = client.post(
        "/create-meal-plan",
        data={
            "plan_date": date.today().isoformat(),
            "breakfast_foods": ["not-a-number"],
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Food selection must be a whole number." in response.data
    with app.app_context():
        assert MealPlan.query.count() == 0


def test_add_student_rejects_students_younger_than_five(client, app):
    with app.app_context():
        create_school()

    login(client)
    too_recent_dob = date.today() - timedelta(days=365 * 4)
    response = client.post(
        "/add-student",
        data={
            "full_name": "Too Young",
            "roll_no": "10",
            "dob": too_recent_dob.isoformat(),
            "sex": "Female",
            "grade": "1",
            "section": "A",
            "username": "too-young",
            "password": "secret123",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        assert User.query.filter_by(username="too-young").first() is None
        assert StudentDetail.query.count() == 0


def test_delete_student_handles_missing_student_detail_gracefully(client, app):
    with app.app_context():
        create_school()
        orphan_student = User(username="orphan-student", role="student")
        orphan_student.set_password("secret123")
        db.session.add(orphan_student)
        db.session.commit()
        orphan_user_id = orphan_student.id

    login(client)
    response = client.post(f"/delete-student/{orphan_user_id}", follow_redirects=True)

    assert response.status_code == 200
    assert b"Student record is incomplete or missing." in response.data


def test_edit_student_handles_missing_student_detail_gracefully(client, app):
    with app.app_context():
        create_school()
        orphan_student = User(username="orphan-edit", role="student")
        orphan_student.set_password("secret123")
        db.session.add(orphan_student)
        db.session.commit()
        orphan_user_id = orphan_student.id

    login(client)
    response = client.get(f"/edit-student/{orphan_user_id}", follow_redirects=True)

    assert response.status_code == 200
    assert b"Student record is incomplete or missing." in response.data


def test_save_attendance_rejects_future_date(client, app):
    with app.app_context():
        school = create_school()
        student = create_student(school)
        student_id = student.student_detail.id

    login(client)
    response = client.post(
        "/save-attendance",
        data={
            "attendance_date": (date.today() + timedelta(days=1)).isoformat(),
            "attendance_data": json.dumps([
                {"id": student_id, "meals": {"breakfast": True, "lunch": False, "dinner": False}}
            ]),
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        assert Attendance.query.count() == 0


def test_save_attendance_rejects_past_date(client, app):
    with app.app_context():
        school = create_school()
        student = create_student(school)
        student_id = student.student_detail.id

    login(client)
    response = client.post(
        "/save-attendance",
        data={
            "attendance_date": (date.today() - timedelta(days=1)).isoformat(),
            "attendance_data": json.dumps([
                {"id": student_id, "meals": {"breakfast": True, "lunch": False, "dinner": False}}
            ]),
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        assert Attendance.query.count() == 0


def test_save_attendance_requires_today_meal_plan(client, app):
    with app.app_context():
        school = create_school()
        student = create_student(school)
        student_id = student.student_detail.id

    login(client)
    response = client.post(
        "/save-attendance",
        data={
            "attendance_date": date.today().isoformat(),
            "attendance_data": json.dumps([
                {"id": student_id, "meals": {"breakfast": True, "lunch": True, "dinner": False}}
            ]),
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        assert Attendance.query.count() == 0


def test_save_attendance_rejects_non_list_payload(client, app):
    with app.app_context():
        school = create_school()
        create_student(school)

    login(client)
    response = client.post(
        "/save-attendance",
        data={
            "attendance_date": date.today().isoformat(),
            "attendance_data": json.dumps({"bad": "payload"}),
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Attendance data was invalid." in response.data


def test_save_attendance_ignores_students_from_other_schools(client, app):
    with app.app_context():
        school_one = create_school(username="school-one")
        other_school = create_school(username="school-two")
        food = create_food()
        plan = MealPlan(school_id=school_one.id, plan_date=date.today())
        db.session.add(plan)
        db.session.flush()
        db.session.add(MealPlanItem(meal_plan_id=plan.id, food_id=food.id, meal_type="Breakfast"))
        other_student = create_student(other_school, username="other-student")
        other_student_id = other_student.student_detail.id
        db.session.commit()

    login(client, username="school-one")
    response = client.post(
        "/save-attendance",
        data={
            "attendance_date": date.today().isoformat(),
            "attendance_data": json.dumps([
                {"id": other_student_id, "meals": {"breakfast": True, "lunch": True, "dinner": True}}
            ]),
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        assert Attendance.query.count() == 0


def test_get_attendance_returns_editor_state_for_selected_date(client, app):
    with app.app_context():
        school = create_school()
        first_student = create_student(school)
        first_student_id = first_student.student_detail.id
        second_student = create_student(school, username="student-two")
        second_student.student_detail.roll_no = 2
        second_student.student_detail.full_name = "Second Student"
        db.session.add(
            Attendance(
                student_id=first_student_id,
                attendance_date=date.today(),
                ate_breakfast=True,
                ate_lunch=False,
                ate_dinner=True,
            )
        )
        db.session.commit()

    login(client)
    response = client.get(f"/get-attendance/{date.today().isoformat()}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["summary"]["ate_something"] == 1
    assert payload["summary"]["absent"] == 1
    assert len(payload["editor_students"]) == 2
    loaded_student = next(student for student in payload["editor_students"] if student["id"] == first_student_id)
    assert loaded_student["meals"] == {"breakfast": True, "lunch": False, "dinner": True}


def test_student_dashboard_renders_for_students(client, app):
    with app.app_context():
        school = create_school()
        create_student(school)

    login(client, username="student")
    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Student Dashboard" in response.data
    assert b"Welcome back, Test Student." in response.data


def test_student_dashboard_renders_for_students_with_legacy_password_hash(client, app):
    with app.app_context():
        school = create_school(username="school-legacy")
        legacy_student = User(username="legacy-student", role="student")
        legacy_student.password_hash = generate_legacy_password_hash("secret123")
        legacy_student.student_detail = StudentDetail(
            full_name="Legacy Student",
            roll_no=7,
            dob=date(2012, 1, 1),
            sex="Female",
            grade=7,
            section="A",
            school_id=school.id,
        )
        db.session.add(legacy_student)
        db.session.commit()

    login(client, username="legacy-student")
    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Student Dashboard" in response.data
    assert b"Welcome back, Legacy Student." in response.data

    with app.app_context():
        updated_student = User.query.filter_by(username="legacy-student").first()
        assert updated_student is not None
        assert updated_student.password_hash.startswith("$2")


def test_dashboard_handles_unsupported_role_without_500(client, app):
    with app.app_context():
        unsupported_user = User(username="platform-admin", role="admin")
        unsupported_user.set_password("secret123")
        db.session.add(unsupported_user)
        db.session.commit()

    login(client, username="platform-admin")
    response = client.get("/dashboard", follow_redirects=True)

    assert response.status_code == 200
    assert b"Platform Console" in response.data


def test_school_insights_page_renders_for_schools(client, app):
    with app.app_context():
        create_school()

    login(client)
    response = client.get("/insights")

    assert response.status_code == 200
    assert b"School Insights Dashboard" in response.data


def test_health_awareness_page_renders_for_students(client, app):
    with app.app_context():
        school = create_school()
        create_student(school)

    login(client, username="student")
    response = client.get("/awareness")

    assert response.status_code == 200
    assert b"Health Awareness" in response.data
    assert b"All Topics" in response.data


def test_recipe_finder_redirects_students_to_meal_plan(client, app):
    with app.app_context():
        school = create_school()
        create_student(school)

    login(client, username="student")
    response = client.get("/recipe-finder")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/meal-generator")


def test_get_nutrition_data_returns_student_history_payload(client, app):
    with app.app_context():
        school = create_school()
        student = create_student(school)
        food = create_food(name="History Apple")
        plan = MealPlan(school_id=school.id, plan_date=date.today())
        db.session.add(plan)
        db.session.flush()
        db.session.add(MealPlanItem(meal_plan_id=plan.id, food_id=food.id, meal_type="Breakfast"))
        db.session.add(
            Attendance(
                student_id=student.student_detail.id,
                attendance_date=date.today(),
                ate_breakfast=True,
                ate_lunch=False,
                ate_dinner=False,
            )
        )
        db.session.commit()

    login(client, username="student")
    response = client.get(f"/get-nutrition-data/{date.today().isoformat()}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["daily_nutrition"]["calories"] > 0
    assert "History Apple" in payload["meal_plan"]["Breakfast"][0]


def test_meal_generator_page_keeps_recipe_finder_and_removes_nutrition_explorer(client, app):
    with app.app_context():
        school = create_school()
        create_student(school)

    login(client, username="student")
    response = client.get("/meal-generator")

    assert response.status_code == 200
    assert b"Recipe Finder" in response.data
    assert b"Nutrition Explorer" not in response.data


def test_interactive_pages_allow_alpine_runtime_under_csp(client, app):
    with app.app_context():
        school = create_school()
        create_student(school)

    login(client)
    school_response = client.get("/dashboard")

    assert school_response.status_code == 200
    assert "unsafe-eval" in school_response.headers["Content-Security-Policy"]
    assert b"cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js" in school_response.data

    client.post("/logout")
    login(client, username="student")
    meal_response = client.get("/meal-generator")

    assert meal_response.status_code == 200
    assert "unsafe-eval" in meal_response.headers["Content-Security-Policy"]
    assert b"cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js" in meal_response.data


def test_get_ai_nutrition_returns_local_food_without_ai_call(client, app, monkeypatch):
    with app.app_context():
        create_school()
        create_food(name="Paneer")

    login(client)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("AI lookup should not run for existing foods.")

    monkeypatch.setattr(routes, "_cached_ai_nutrition_lookup", fail_if_called)

    response = client.post("/get-ai-nutrition", json={"food_name": "paneer"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["source"] == "local"
    assert payload["data"]["calories"] == 95


def test_search_food_returns_local_matches_without_ai_call(client, app, monkeypatch):
    with app.app_context():
        school = create_school()
        create_student(school)
        create_food(name="Paneer")

    login(client, username="student")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("AI lookup should not run for local search matches.")

    monkeypatch.setattr(routes, "_cached_ai_nutrition_lookup", fail_if_called)

    response = client.get("/search-food?q=pan")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload[0]["name"] == "Paneer"
    assert payload[0]["protein"] == 0.5


def test_get_ai_nutrition_caches_repeat_requests(client, app, monkeypatch):
    with app.app_context():
        create_school()

    login(client)
    routes._cached_ai_nutrition_lookup.cache_clear()
    call_count = {"value": 0}

    def fake_ai_lookup(prompt, *, generation_config, log_label):
        call_count["value"] += 1
        return {"calories": 120, "protein": 4, "carbs": 18, "fats": 2}

    monkeypatch.setattr(routes, "_call_gemini_json", fake_ai_lookup)

    first_response = client.post("/get-ai-nutrition", json={"food_name": "Poha"})
    second_response = client.post("/get-ai-nutrition", json={"food_name": "poha"})

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert call_count["value"] == 1
    assert first_response.get_json()["data"]["calories"] == 120
    assert second_response.get_json()["data"]["protein"] == 4


def test_get_ai_nutrition_uses_fallback_when_ai_fails(client, app, monkeypatch):
    with app.app_context():
        create_school()

    login(client)

    def fail_if_called(*args, **kwargs):
        raise RuntimeError("Gemini unavailable")

    monkeypatch.setattr(routes, "_cached_ai_nutrition_lookup", fail_if_called)

    response = client.post("/get-ai-nutrition", json={"food_name": "Jalebi"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["source"] == "fallback"
    assert payload["data"]["calories"] > 0


def test_get_ai_recipe_uses_fallback_when_ai_fails(client, app, monkeypatch):
    with app.app_context():
        school = create_school()
        create_student(school)

    login(client, username="student")

    def fail_if_called(*args, **kwargs):
        raise RuntimeError("Gemini unavailable")

    monkeypatch.setattr(routes, "_cached_ai_recipe_lookup", fail_if_called)

    response = client.post("/get-ai-recipe", json={"food_name": "Sushi"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["source"] == "fallback"
    assert payload["data"]["recipe_title"]
    assert payload["data"]["ingredients"]


def test_meal_generator_uses_fallback_when_ai_fails(client, app, monkeypatch):
    with app.app_context():
        school = create_school()
        create_student(school)

    login(client, username="student")

    def fail_if_called(*args, **kwargs):
        raise RuntimeError("Gemini unavailable")

    monkeypatch.setattr(routes, "_call_gemini_json", fail_if_called)

    response = client.post(
        "/meal-generator",
        data={
            "diet_type": "Vegetarian",
            "allergies": "",
            "dislikes": "",
            "meal_count": "3 meals/day",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"generated a meal plan locally instead" in response.data
    assert b"Personalized meal plan" in response.data


def test_health_form_uses_fallback_when_ai_fails(client, app, monkeypatch):
    with app.app_context():
        school = create_school()
        create_student(school)

    login(client, username="student")

    def fail_if_called(*args, **kwargs):
        raise RuntimeError("Gemini unavailable")

    monkeypatch.setattr(routes, "_call_gemini_json", fail_if_called)

    response = client.post(
        "/health-form",
        data={
            "age": "20",
            "sex": "Female",
            "height": "160",
            "weight": "58",
            "waist": "72",
            "meals_per_day": "Medium",
            "fruit_veg_intake": "Medium",
            "junk_food_intake": "Low",
            "water_intake": "Medium",
            "sleep_hours": "High",
            "physical_activity": "High",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"generated health insights locally instead" in response.data
    assert b"AI Health Report" in response.data


def test_login_route_authenticates_and_redirects(client, app):
    with app.app_context():
        create_school()

    response = client.post(
        "/login",
        data={"username": "school", "password": "secret123"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"School Dashboard" in response.data


def test_login_route_accepts_and_upgrades_legacy_school_password_hash(client, app):
    with app.app_context():
        school = User(username="legacy-school", role="school", school_name="Legacy School")
        school.password_hash = generate_legacy_password_hash("secret123")
        db.session.add(school)
        db.session.commit()

    response = client.post(
        "/login",
        data={"username": "legacy-school", "password": "secret123"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"School Dashboard" in response.data

    with app.app_context():
        updated_school = User.query.filter_by(username="legacy-school").first()
        assert updated_school is not None
        assert updated_school.password_hash.startswith("$2")


def test_health_route_is_public(client):
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["service"] == "nutrify"


def test_change_password_updates_credentials(client, app):
    with app.app_context():
        create_school()

    login(client)
    response = client.post(
        "/account/change-password",
        data={
            "current_password": "secret123",
            "new_password": "newsecret123",
            "confirm_password": "newsecret123",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    client.post("/logout")
    login_response = client.post(
        "/login",
        data={"username": "school", "password": "newsecret123"},
        follow_redirects=True,
    )
    assert login_response.status_code == 200
    assert b"School Dashboard" in login_response.data


def test_password_reset_request_and_completion_flow(client, app):
    with app.app_context():
        school = create_school()
        school.email = "school@example.com"
        db.session.commit()

    response = client.post(
        "/password-reset/request",
        data={"identifier": "school"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"/password-reset/" in response.data
    reset_link = response.data.decode("utf-8").split("/password-reset/")[1].split('"')[0]

    reset_response = client.post(
        f"/password-reset/{reset_link}",
        data={"new_password": "resetsecret123", "confirm_password": "resetsecret123"},
        follow_redirects=True,
    )

    assert reset_response.status_code == 200
    login_response = client.post(
        "/login",
        data={"username": "school", "password": "resetsecret123"},
        follow_redirects=True,
    )
    assert login_response.status_code == 200
    assert b"School Dashboard" in login_response.data


def test_staff_management_creates_staff_account(client, app):
    with app.app_context():
        school = create_school()
        school_id = school.id

    login(client)
    response = client.post(
        "/staff",
        data={
            "account_type": "staff",
            "full_name": "Operations Lead",
            "email": "ops@example.com",
            "username": "ops-lead",
            "password": "secret1234",
            "can_manage_students": "on",
            "can_manage_meals": "on",
            "can_view_reports": "on",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        staff = User.query.filter_by(username="ops-lead").first()
        assert staff is not None
        assert staff.school_id == school_id
        assert staff.can_manage_students is True
        assert staff.can_manage_meals is True


def test_meal_template_apply_creates_recurring_plans(client, app):
    with app.app_context():
        create_school()
        food = create_food()
        food_id = food.id

    login(client)
    response = client.post(
        "/meal-templates",
        data={
            "name": "Weekday Template",
            "description": "Standard week",
            "breakfast_foods": [str(food_id)],
            "lunch_foods": [str(food_id)],
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    with app.app_context():
        template = MealTemplate.query.filter_by(name="Weekday Template").first()
        assert template is not None
        template_id = template.id

    apply_response = client.post(
        f"/meal-templates/{template_id}/apply",
        data={
            "start_date": date.today().isoformat(),
            "recurrence_count": "2",
            "recurrence": "weekly",
        },
        follow_redirects=True,
    )
    assert apply_response.status_code == 200
    with app.app_context():
        plans = MealPlan.query.order_by(MealPlan.plan_date.asc()).all()
        assert len(plans) == 2
        assert plans[0].status == "approved"
        assert plans[1].status == "approved"


def test_staff_meal_plan_submission_creates_approval_and_can_be_approved(client, app):
    with app.app_context():
        school = create_school()
        food = create_food()
        food_id = food.id
        template = MealTemplate(
            school_id=school.id,
            name="Staff Template",
            description="Pending approval template",
            created_by_user_id=school.id,
        )
        db.session.add(template)
        db.session.flush()
        db.session.add(MealTemplateItem(template_id=template.id, food_id=food_id, meal_type="Breakfast"))
        staff = User(
            username="meal-staff",
            role=User.ROLE_SCHOOL_ADMIN,
            school_id=school.id,
            full_name="Meal Staff",
            can_manage_meals=True,
            can_approve_workflows=False,
        )
        staff.set_password("secret1234")
        db.session.add(staff)
        db.session.commit()
        template_id = template.id

    login(client, username="meal-staff", password="secret1234")
    submit_response = client.post(
        f"/meal-templates/{template_id}/apply",
        data={"start_date": date.today().isoformat(), "recurrence_count": "1", "recurrence": "daily"},
        follow_redirects=True,
    )
    assert submit_response.status_code == 200

    with app.app_context():
        plan = MealPlan.query.first()
        approval = ApprovalRequest.query.first()
        assert plan is not None
        assert plan.status == "pending"
        assert approval is not None
        approval_id = approval.id

    client.post("/logout")
    login(client)
    approve_response = client.post(
        f"/approvals/{approval_id}/approve",
        follow_redirects=True,
    )
    assert approve_response.status_code == 200
    with app.app_context():
        plan = MealPlan.query.first()
        assert plan.status == "approved"


def test_feedback_submission_creates_notification_for_school(client, app):
    with app.app_context():
        school = create_school()
        create_student(school)

    login(client, username="student")
    response = client.post(
        "/feedback",
        data={"subject": "Lunch feedback", "message": "The lunch menu was great today."},
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        school_notifications = Notification.query.filter(Notification.title == "New portal feedback").all()
        assert school_notifications
