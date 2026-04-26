import csv
import io
import logging
import secrets
from datetime import date, timedelta

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from werkzeug.exceptions import abort

from .ai_usage import ALL_FEATURES, daily_limit_for
from .audit import add_audit_log
from .exports import csv_response, json_response
from .jobs import enqueue_job, mark_job_complete, mark_job_failed, mark_job_running
from .models import (
    AIAccessPolicy,
    AIUsageLog,
    ApprovalRequest,
    Attendance,
    AuditLog,
    Food,
    HealthMetric,
    MealPlan,
    MealPlanItem,
    MealTemplate,
    MealTemplateItem,
    Notification,
    PasswordResetToken,
    PlatformSetting,
    PlatformJob,
    StudentDetail,
    User,
    UserFeedback,
    db,
    utcnow,
)
from .notifications import add_notification, broadcast_platform_notification, broadcast_school_notification
from .password_reset import consume_password_reset_token, issue_password_reset_token, resolve_password_reset_token
from .platform_services import (
    ai_global_limits,
    ai_policy_rows,
    ai_usage_by_school,
    ai_usage_by_user,
    analytics_payload,
    count_active_master_admins,
    dashboard_summary,
    get_platform_setting,
    get_school_detail,
    get_user_detail,
    invalidate_platform_cache,
    list_audit_logs,
    list_jobs,
    list_notifications,
    list_schools,
    list_users,
    paginate_query,
    school_dependency_summary,
    school_roots_query,
    set_platform_setting,
    upsert_ai_policy,
)
from .security import roles_required


logger = logging.getLogger(__name__)

platform = Blueprint('platform', __name__)


def _parse_bool(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on', 'y'}


def _school_scope_id():
    return getattr(current_user, 'school_scope_id', None) or getattr(current_user, 'id', None)


def _school_food_query(school_scope_id):
    return Food.query.filter(or_(Food.school_id == school_scope_id, Food.school_id.is_(None)))


def _commit_or_rollback(action_label):
    try:
        db.session.commit()
        logger.info("%s completed successfully", action_label)
        return True
    except Exception:
        db.session.rollback()
        logger.exception("%s failed", action_label)
        return False


def _ensure_school_permission(flag_name, *, redirect_endpoint='main.dashboard'):
    if getattr(current_user, flag_name, False):
        return None
    flash('You do not have permission to perform this action.', 'danger')
    return redirect(url_for(redirect_endpoint))


def _csv_bool(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'y', 'present', 'ate'}


def _parse_csv_upload(upload):
    if upload is None or not upload.filename:
        raise ValueError('Please choose a CSV file first.')
    payload = upload.stream.read()
    if not payload:
        raise ValueError('The uploaded CSV file was empty.')
    try:
        text = payload.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('The CSV file must be UTF-8 encoded.') from exc
    return csv.DictReader(io.StringIO(text))


def _school_roots_query():
    return User.query.filter(User.school_id.is_(None), User.role.in_(['school', User.ROLE_SCHOOL_ADMIN]))


def _school_staff_query(school_scope_id, *, include_deleted=False):
    query = User.query.filter(
        User.school_id == school_scope_id,
        User.role.in_(['school', User.ROLE_SCHOOL_ADMIN]),
    )
    if include_deleted:
        query = query.execution_options(include_deleted=True)
    return query.order_by(User.full_name.asc(), User.username.asc())


def _guardian_accounts_query(school_scope_id, *, include_deleted=False):
    query = User.query.join(StudentDetail, User.primary_student_id == StudentDetail.id).filter(
        StudentDetail.school_id == school_scope_id,
        User.role.in_(['student', User.ROLE_USER]),
    )
    if include_deleted:
        query = query.execution_options(include_deleted=True)
    return query.order_by(User.full_name.asc(), User.username.asc())


def _attendance_percentage(student_id):
    records = Attendance.query.filter_by(student_id=student_id).all()
    if not records:
        return None
    present = sum(1 for record in records if record.was_present)
    return round((present / len(records)) * 100, 1)


def _meal_template_query(school_scope_id):
    return MealTemplate.query.filter_by(school_id=school_scope_id).order_by(MealTemplate.created_at.desc())


def _serialize_notification(notification):
    return {
        'id': notification.id,
        'title': notification.title,
        'message': notification.message,
        'category': notification.category,
        'link': notification.link,
        'is_read': notification.is_read,
        'created_at': notification.created_at,
    }


def _parse_int(value, *, minimum=None, maximum=None, default=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _temporary_password(prefix='Nutrify'):
    return f"{prefix}-{secrets.token_urlsafe(8)}"


def _confirmation_matches(expected_value):
    confirmation = (request.form.get('confirm_value') or '').strip()
    return confirmation == expected_value


def _require_confirmation(expected_value, message):
    if _confirmation_matches(expected_value):
        return True
    flash(message, 'danger')
    return False


def _master_admin_only():
    if not current_user.has_role(User.ROLE_MASTER_ADMIN):
        abort(403)


def _school_root_or_404(school_id):
    school = school_roots_query(include_deleted=True).filter_by(id=school_id).first()
    if school is None:
        abort(404)
    return school


def _user_or_404(user_id):
    user = User.query.execution_options(include_deleted=True).filter_by(id=user_id).first()
    if user is None:
        abort(404)
    return user


def _master_admin_guard_for_user_mutation(target_user, *, allow_self=False, allow_school_root=False):
    if target_user.is_school_root and not allow_school_root:
        flash('Use school management to change the lifecycle of a school root account.', 'danger')
        return False
    if target_user.id == current_user.id and not allow_self:
        flash('Use your own account settings for this action.', 'danger')
        return False
    return True


def _apply_school_status(school, *, active):
    accounts = User.query.execution_options(include_deleted=True).filter(
        or_(User.id == school.id, User.school_id == school.id)
    ).all()
    students = StudentDetail.query.execution_options(include_deleted=True).filter_by(school_id=school.id).all()
    for account in accounts:
        if account.is_deleted:
            continue
        if active:
            account.activate_account()
        else:
            account.deactivate_account()
    for student in students:
        if student.is_deleted:
            continue
        student.status = 'active' if active else 'inactive'


def _soft_delete_school_bundle(school):
    accounts = User.query.execution_options(include_deleted=True).filter(
        or_(User.id == school.id, User.school_id == school.id)
    ).all()
    students = StudentDetail.query.execution_options(include_deleted=True).filter_by(school_id=school.id).all()
    meal_plans = MealPlan.query.execution_options(include_deleted=True).filter_by(school_id=school.id).all()
    templates = MealTemplate.query.execution_options(include_deleted=True).filter_by(school_id=school.id).all()
    for account in accounts:
        if not account.is_deleted:
            account.soft_delete()
            account.deactivate_account()
    for student in students:
        if not student.is_deleted:
            student.soft_delete()
            student.status = 'inactive'
    for plan in meal_plans:
        if not plan.is_deleted:
            plan.soft_delete()
    for template in templates:
        if not template.is_deleted:
            template.soft_delete()


def _restore_school_bundle(school):
    accounts = User.query.execution_options(include_deleted=True).filter(
        or_(User.id == school.id, User.school_id == school.id)
    ).all()
    students = StudentDetail.query.execution_options(include_deleted=True).filter_by(school_id=school.id).all()
    meal_plans = MealPlan.query.execution_options(include_deleted=True).filter_by(school_id=school.id).all()
    templates = MealTemplate.query.execution_options(include_deleted=True).filter_by(school_id=school.id).all()
    for account in accounts:
        if account.is_deleted:
            account.restore()
        account.activate_account()
    for student in students:
        if student.is_deleted:
            student.restore()
        student.status = 'active'
    for plan in meal_plans:
        if plan.is_deleted:
            plan.restore()
    for template in templates:
        if template.is_deleted:
            template.restore()


@platform.route('/platform')
@roles_required(User.ROLE_MASTER_ADMIN)
def platform_dashboard():
    summary = dashboard_summary()
    school_page = list_schools(page=_parse_int(request.args.get('page'), minimum=1, default=1), per_page=6, status='all')
    context = {
        'summary': summary,
        'school_page': school_page,
        'global_ai_limits': ai_global_limits(),
    }
    return render_template('platform_dashboard.html', **context)


@platform.route('/platform/schools', methods=['GET', 'POST'])
@roles_required(User.ROLE_MASTER_ADMIN)
def platform_schools():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        school_name = (request.form.get('school_name') or '').strip()
        email = (request.form.get('email') or '').strip() or None
        password = request.form.get('password') or ''
        if not username or not school_name:
            flash('School name and username are required.', 'danger')
            return redirect(url_for('platform.platform_schools'))
        if User.query.execution_options(include_deleted=True).filter_by(username=username).first() is not None:
            flash('That school username is already in use.', 'danger')
            return redirect(url_for('platform.platform_schools'))
        generated_password = False
        if len(password) < 8:
            password = _temporary_password('School')
            generated_password = True

        school = User(
            username=username,
            role=User.ROLE_SCHOOL_ADMIN,
            school_name=school_name,
            full_name=school_name,
            email=email,
            can_manage_students=True,
            can_manage_meals=True,
            can_manage_attendance=True,
            can_view_reports=True,
            can_manage_staff=True,
            can_approve_workflows=True,
        )
        school.set_password(password)
        db.session.add(school)
        db.session.flush()
        add_audit_log(
            'create_school',
            'school',
            entity_id=school.id,
            details={'username': school.username, 'school_name': school.school_name},
        )
        invalidate_platform_cache()
        if not _commit_or_rollback(f"Create school school_id={school.id}"):
            flash('The school could not be created right now.', 'danger')
            return redirect(url_for('platform.platform_schools'))

        message = 'School created successfully.'
        if generated_password:
            message += f' Temporary password: {password}'
        flash(message, 'success')
        return redirect(url_for('platform.platform_school_detail', school_id=school.id))

    page = _parse_int(request.args.get('page'), minimum=1, default=1)
    status = (request.args.get('status') or 'active').strip().lower()
    search = (request.args.get('q') or '').strip()
    schools_page = list_schools(page=page, per_page=15, search=search, status=status)
    return render_template(
        'platform_schools.html',
        schools_page=schools_page,
        search=search,
        status=status,
    )


@platform.route('/platform/schools/<int:school_id>')
@roles_required(User.ROLE_MASTER_ADMIN)
def platform_school_detail(school_id):
    detail = get_school_detail(school_id)
    if detail is None:
        abort(404)
    return render_template('platform_school_detail.html', **detail)


@platform.route('/platform/schools/<int:school_id>/update', methods=['POST'])
@roles_required(User.ROLE_MASTER_ADMIN)
def update_platform_school(school_id):
    school = _school_root_or_404(school_id)
    username = (request.form.get('username') or '').strip()
    school_name = (request.form.get('school_name') or '').strip()
    email = (request.form.get('email') or '').strip() or None
    if not username or not school_name:
        flash('School name and username are required.', 'danger')
        return redirect(url_for('platform.platform_school_detail', school_id=school_id))

    existing = User.query.execution_options(include_deleted=True).filter(User.username == username, User.id != school_id).first()
    if existing is not None:
        flash('That username is already assigned to another account.', 'danger')
        return redirect(url_for('platform.platform_school_detail', school_id=school_id))

    school.username = username
    school.school_name = school_name
    school.full_name = school_name
    school.email = email
    add_audit_log('update_school', 'school', entity_id=school.id, details={'username': username, 'school_name': school_name})
    invalidate_platform_cache()
    if not _commit_or_rollback(f"Update school school_id={school.id}"):
        flash('The school could not be updated right now.', 'danger')
    else:
        flash('School details updated.', 'success')
    return redirect(url_for('platform.platform_school_detail', school_id=school_id))


@platform.route('/platform/schools/<int:school_id>/status', methods=['POST'])
@roles_required(User.ROLE_MASTER_ADMIN)
def update_platform_school_status(school_id):
    school = _school_root_or_404(school_id)
    action = (request.form.get('action') or '').strip().lower()
    if action not in {'activate', 'deactivate'}:
        flash('Unsupported school status action.', 'danger')
        return redirect(url_for('platform.platform_school_detail', school_id=school_id))
    if not _require_confirmation(school.username, 'Type the school username to confirm this status change.'):
        return redirect(url_for('platform.platform_school_detail', school_id=school_id))

    _apply_school_status(school, active=(action == 'activate'))
    add_audit_log(
        f'{action}_school',
        'school',
        entity_id=school.id,
        details={'username': school.username},
    )
    invalidate_platform_cache()
    if not _commit_or_rollback(f"{action.title()} school school_id={school.id}"):
        flash('The school status could not be updated right now.', 'danger')
    else:
        flash(f'School {action}d successfully.', 'success')
    return redirect(url_for('platform.platform_school_detail', school_id=school_id))


@platform.route('/platform/schools/<int:school_id>/delete', methods=['POST'])
@roles_required(User.ROLE_MASTER_ADMIN)
def delete_platform_school(school_id):
    school = _school_root_or_404(school_id)
    if not _require_confirmation(school.username, 'Type the school username to confirm this delete action.'):
        return redirect(url_for('platform.platform_school_detail', school_id=school_id))

    dependencies = school_dependency_summary(school.id)
    dependency_total = sum(dependencies.values())
    if dependency_total > 1 and not _parse_bool(request.form.get('confirm_dependencies')):
        flash('This school has active dependencies. Tick the dependency confirmation and try again.', 'danger')
        return redirect(url_for('platform.platform_school_detail', school_id=school_id))

    _soft_delete_school_bundle(school)
    add_audit_log(
        'soft_delete_school',
        'school',
        entity_id=school.id,
        details={'dependencies': dependencies, 'username': school.username},
    )
    invalidate_platform_cache()
    if not _commit_or_rollback(f"Soft delete school school_id={school.id}"):
        flash('The school could not be deleted right now.', 'danger')
    else:
        flash('School archived successfully.', 'success')
    return redirect(url_for('platform.platform_school_detail', school_id=school_id))


@platform.route('/platform/schools/<int:school_id>/restore', methods=['POST'])
@roles_required(User.ROLE_MASTER_ADMIN)
def restore_platform_school(school_id):
    school = _school_root_or_404(school_id)
    _restore_school_bundle(school)
    add_audit_log('restore_school', 'school', entity_id=school.id, details={'username': school.username})
    invalidate_platform_cache()
    if not _commit_or_rollback(f"Restore school school_id={school.id}"):
        flash('The school could not be restored right now.', 'danger')
    else:
        flash('School restored successfully.', 'success')
    return redirect(url_for('platform.platform_school_detail', school_id=school_id))


@platform.route('/platform/users', methods=['GET', 'POST'])
@roles_required(User.ROLE_MASTER_ADMIN)
def platform_users():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        full_name = (request.form.get('full_name') or '').strip() or None
        email = (request.form.get('email') or '').strip() or None
        role = (request.form.get('role') or User.ROLE_USER).strip().lower()
        school_id = request.form.get('school_id', type=int)
        password = request.form.get('password') or ''

        if role not in {User.ROLE_MASTER_ADMIN, User.ROLE_SCHOOL_ADMIN, User.ROLE_USER}:
            flash('Choose a valid role for the new user.', 'danger')
            return redirect(url_for('platform.platform_users'))
        if not username:
            flash('Username is required.', 'danger')
            return redirect(url_for('platform.platform_users'))
        if role != User.ROLE_MASTER_ADMIN and not school_id:
            flash('School assignment is required for school staff and portal users.', 'danger')
            return redirect(url_for('platform.platform_users'))
        if User.query.execution_options(include_deleted=True).filter_by(username=username).first() is not None:
            flash('That username is already in use.', 'danger')
            return redirect(url_for('platform.platform_users'))

        generated_password = False
        if len(password) < 8:
            password = _temporary_password('User')
            generated_password = True

        user = User(
            username=username,
            full_name=full_name,
            email=email,
            role=role,
            school_id=school_id if role != User.ROLE_MASTER_ADMIN else None,
            can_manage_students=_parse_bool(request.form.get('can_manage_students')),
            can_manage_meals=_parse_bool(request.form.get('can_manage_meals')),
            can_manage_attendance=_parse_bool(request.form.get('can_manage_attendance')),
            can_view_reports=_parse_bool(request.form.get('can_view_reports')),
            can_manage_staff=_parse_bool(request.form.get('can_manage_staff')),
            can_approve_workflows=_parse_bool(request.form.get('can_approve_workflows')),
        )
        if role == User.ROLE_MASTER_ADMIN:
            user.can_manage_students = True
            user.can_manage_meals = True
            user.can_manage_attendance = True
            user.can_view_reports = True
            user.can_manage_staff = True
            user.can_approve_workflows = True
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        add_audit_log(
            'create_user',
            'user',
            entity_id=user.id,
            details={'role': role, 'school_id': school_id, 'username': user.username},
        )
        invalidate_platform_cache()
        if not _commit_or_rollback(f"Create platform user user_id={user.id}"):
            flash('The user could not be created right now.', 'danger')
            return redirect(url_for('platform.platform_users'))

        message = 'User created successfully.'
        if generated_password:
            message += f' Temporary password: {password}'
        flash(message, 'success')
        return redirect(url_for('platform.platform_user_detail', user_id=user.id))

    page = _parse_int(request.args.get('page'), minimum=1, default=1)
    role = (request.args.get('role') or '').strip().lower() or None
    school_id = request.args.get('school_id', type=int)
    status = (request.args.get('status') or 'active').strip().lower()
    search = (request.args.get('q') or '').strip()
    users_page = list_users(page=page, per_page=20, search=search, role=role, school_id=school_id, status=status)
    schools = school_roots_query().order_by(User.school_name.asc(), User.username.asc()).all()
    return render_template(
        'platform_users.html',
        users_page=users_page,
        schools=schools,
        search=search,
        role=role,
        school_id=school_id,
        status=status,
    )


@platform.route('/platform/users/<int:user_id>')
@roles_required(User.ROLE_MASTER_ADMIN)
def platform_user_detail(user_id):
    user = get_user_detail(user_id)
    if user is None:
        abort(404)
    user_logs = AuditLog.query.filter(
        or_(AuditLog.actor_user_id == user.id, AuditLog.entity_id == str(user.id))
    ).order_by(AuditLog.created_at.desc()).limit(25).all()
    notifications = Notification.query.filter_by(user_id=user.id).order_by(Notification.created_at.desc()).limit(20).all()
    ai_usage = AIUsageLog.query.filter_by(user_id=user.id).order_by(AIUsageLog.created_at.desc()).limit(20).all()
    schools = school_roots_query().order_by(User.school_name.asc(), User.username.asc()).all()
    return render_template(
        'platform_user_detail.html',
        target_user=user,
        user_logs=user_logs,
        notifications=notifications,
        ai_usage=ai_usage,
        schools=schools,
    )


@platform.route('/platform/users/<int:user_id>/update', methods=['POST'])
@roles_required(User.ROLE_MASTER_ADMIN)
def update_platform_user(user_id):
    target_user = _user_or_404(user_id)
    new_role = (request.form.get('role') or target_user.normalized_role).strip().lower()
    new_school_id = request.form.get('school_id', type=int)
    if target_user.is_school_root and new_role != target_user.normalized_role:
        flash('Change school root roles from the school management view instead.', 'danger')
        return redirect(url_for('platform.platform_user_detail', user_id=user_id))
    if target_user.has_role(User.ROLE_MASTER_ADMIN) and new_role != User.ROLE_MASTER_ADMIN and count_active_master_admins() <= 1:
        flash('The last active master admin cannot be downgraded.', 'danger')
        return redirect(url_for('platform.platform_user_detail', user_id=user_id))
    if new_role not in {User.ROLE_MASTER_ADMIN, User.ROLE_SCHOOL_ADMIN, User.ROLE_USER}:
        flash('Choose a valid role.', 'danger')
        return redirect(url_for('platform.platform_user_detail', user_id=user_id))
    if new_role != User.ROLE_MASTER_ADMIN and not (new_school_id or target_user.school_scope_id):
        flash('A school assignment is required for school staff and portal users.', 'danger')
        return redirect(url_for('platform.platform_user_detail', user_id=user_id))

    new_username = (request.form.get('username') or '').strip()
    if not new_username:
        flash('Username is required.', 'danger')
        return redirect(url_for('platform.platform_user_detail', user_id=user_id))
    existing = User.query.execution_options(include_deleted=True).filter(User.username == new_username, User.id != user_id).first()
    if existing is not None:
        flash('That username is already in use.', 'danger')
        return redirect(url_for('platform.platform_user_detail', user_id=user_id))

    target_user.username = new_username
    target_user.full_name = (request.form.get('full_name') or '').strip() or None
    target_user.email = (request.form.get('email') or '').strip() or None
    target_user.role = new_role
    target_user.school_id = None if new_role == User.ROLE_MASTER_ADMIN else (new_school_id or target_user.school_id)
    target_user.can_manage_students = _parse_bool(request.form.get('can_manage_students'))
    target_user.can_manage_meals = _parse_bool(request.form.get('can_manage_meals'))
    target_user.can_manage_attendance = _parse_bool(request.form.get('can_manage_attendance'))
    target_user.can_view_reports = _parse_bool(request.form.get('can_view_reports'))
    target_user.can_manage_staff = _parse_bool(request.form.get('can_manage_staff'))
    target_user.can_approve_workflows = _parse_bool(request.form.get('can_approve_workflows'))
    if new_role == User.ROLE_MASTER_ADMIN:
        target_user.can_manage_students = True
        target_user.can_manage_meals = True
        target_user.can_manage_attendance = True
        target_user.can_view_reports = True
        target_user.can_manage_staff = True
        target_user.can_approve_workflows = True
    add_audit_log(
        'update_user',
        'user',
        entity_id=target_user.id,
        details={'role': target_user.role, 'school_id': target_user.school_id, 'username': target_user.username},
    )
    invalidate_platform_cache()
    if not _commit_or_rollback(f"Update platform user user_id={target_user.id}"):
        flash('The user could not be updated right now.', 'danger')
    else:
        flash('User details updated successfully.', 'success')
    return redirect(url_for('platform.platform_user_detail', user_id=user_id))


@platform.route('/platform/users/<int:user_id>/reset-password', methods=['POST'])
@roles_required(User.ROLE_MASTER_ADMIN)
def reset_platform_user_password(user_id):
    target_user = _user_or_404(user_id)
    if not _master_admin_guard_for_user_mutation(target_user, allow_self=True, allow_school_root=True):
        return redirect(url_for('platform.platform_user_detail', user_id=user_id))
    if not _require_confirmation(target_user.username, 'Type the username to confirm the password reset.'):
        return redirect(url_for('platform.platform_user_detail', user_id=user_id))

    temp_password = request.form.get('new_password') or _temporary_password('Reset')
    target_user.set_password(temp_password)
    target_user.force_password_reset = True
    add_audit_log('reset_password', 'user', entity_id=target_user.id, details={'username': target_user.username})
    add_notification(
        target_user.id,
        'Password reset required',
        'An administrator reset your password. Please change it after your next sign-in.',
        category='warning',
        school_id=target_user.school_scope_id,
    )
    invalidate_platform_cache()
    if not _commit_or_rollback(f"Reset user password user_id={target_user.id}"):
        flash('The password could not be reset right now.', 'danger')
    else:
        flash(f'Password reset successfully. Temporary password: {temp_password}', 'success')
    return redirect(url_for('platform.platform_user_detail', user_id=user_id))


@platform.route('/platform/users/<int:user_id>/action/<action>', methods=['POST'])
@roles_required(User.ROLE_MASTER_ADMIN)
def platform_user_action(user_id, action):
    target_user = _user_or_404(user_id)
    action = action.strip().lower()
    dangerous_actions = {'deactivate', 'lock', 'delete', 'force-reset', 'invalidate-sessions', 'disable-ai'}
    if action in dangerous_actions and not _require_confirmation(target_user.username, 'Type the username to confirm this action.'):
        return redirect(url_for('platform.platform_user_detail', user_id=user_id))
    allow_school_root = action in {'activate', 'reset-password', 'force-reset', 'clear-force-reset', 'invalidate-sessions', 'enable-ai', 'disable-ai', 'unlock', 'lock'}
    if not _master_admin_guard_for_user_mutation(target_user, allow_self=True, allow_school_root=allow_school_root):
        return redirect(url_for('platform.platform_user_detail', user_id=user_id))
    if (
        target_user.has_role(User.ROLE_MASTER_ADMIN)
        and action in {'deactivate', 'lock', 'delete'}
        and count_active_master_admins() <= 1
    ):
        flash('The last active master admin cannot be locked, deactivated, or deleted.', 'danger')
        return redirect(url_for('platform.platform_user_detail', user_id=user_id))

    success_message = None
    if action == 'activate':
        target_user.activate_account()
        success_message = 'User activated.'
    elif action == 'deactivate':
        target_user.deactivate_account()
        success_message = 'User deactivated.'
    elif action == 'lock':
        target_user.lock_account()
        success_message = 'User locked.'
    elif action == 'unlock':
        target_user.unlock_account()
        success_message = 'User unlocked.'
    elif action == 'force-reset':
        target_user.force_password_reset = True
        target_user.invalidate_sessions()
        success_message = 'User will be required to reset the password.'
    elif action == 'clear-force-reset':
        target_user.force_password_reset = False
        success_message = 'Forced password reset cleared.'
    elif action == 'invalidate-sessions':
        target_user.invalidate_sessions()
        success_message = 'All active sessions were invalidated.'
    elif action == 'disable-ai':
        target_user.ai_access_enabled = False
        success_message = 'AI access disabled for the user.'
    elif action == 'enable-ai':
        target_user.ai_access_enabled = True
        success_message = 'AI access enabled for the user.'
    elif action == 'delete':
        target_user.soft_delete()
        target_user.deactivate_account()
        success_message = 'User archived.'
    elif action == 'restore':
        target_user.restore()
        target_user.activate_account()
        success_message = 'User restored.'
    else:
        flash('Unsupported user action.', 'danger')
        return redirect(url_for('platform.platform_user_detail', user_id=user_id))

    add_audit_log(
        action.replace('-', '_'),
        'user',
        entity_id=target_user.id,
        details={'username': target_user.username},
    )
    invalidate_platform_cache()
    if not _commit_or_rollback(f"Platform user action action={action} user_id={target_user.id}"):
        flash('The action could not be completed right now.', 'danger')
    else:
        flash(success_message, 'success')
    return redirect(url_for('platform.platform_user_detail', user_id=user_id))


@platform.route('/platform/analytics')
@roles_required(User.ROLE_MASTER_ADMIN)
def platform_analytics():
    days = _parse_int(request.args.get('days'), minimum=7, maximum=90, default=30)
    return render_template('platform_analytics.html', days=days)


@platform.route('/platform/analytics/data')
@roles_required(User.ROLE_MASTER_ADMIN)
def platform_analytics_data():
    days = _parse_int(request.args.get('days'), minimum=7, maximum=90, default=30)
    return jsonify(analytics_payload(days=days))


@platform.route('/platform/ai-controls', methods=['GET', 'POST'])
@roles_required(User.ROLE_MASTER_ADMIN)
def platform_ai_controls():
    if request.method == 'POST':
        form_type = (request.form.get('form_type') or '').strip().lower()
        if form_type == 'global-limits':
            for feature in ALL_FEATURES:
                limit_value = _parse_int(request.form.get(f'limit_{feature}'), minimum=1, maximum=10000, default=daily_limit_for(feature, current_app.config))
                set_platform_setting(
                    f'ai.daily_limit.{feature}',
                    limit_value,
                    description=f'Global daily limit for {feature}',
                    actor=current_user,
                )
            add_audit_log('update_ai_global_limits', 'platform_setting', details={'features': list(ALL_FEATURES)})
            invalidate_platform_cache()
            if _commit_or_rollback('Update AI global limits'):
                flash('Global AI limits updated.', 'success')
            else:
                flash('The global AI limits could not be updated right now.', 'danger')
            return redirect(url_for('platform.platform_ai_controls'))

        if form_type == 'policy':
            scope_type = (request.form.get('scope_type') or '').strip().lower()
            feature = (request.form.get('feature') or '*').strip().lower()
            daily_limit = _parse_int(request.form.get('daily_limit'), minimum=1, maximum=10000, default=None)
            is_enabled = _parse_bool(request.form.get('is_enabled', '1'))
            notes = (request.form.get('notes') or '').strip() or None
            school_id = request.form.get('school_id', type=int) if scope_type == 'school' else None
            user_id = request.form.get('user_id', type=int) if scope_type == 'user' else None
            upsert_ai_policy(
                feature=feature,
                school_id=school_id,
                user_id=user_id,
                daily_limit=daily_limit,
                is_enabled=is_enabled,
                notes=notes,
                actor=current_user,
            )
            add_audit_log(
                'update_ai_policy',
                'ai_policy',
                details={'scope_type': scope_type, 'feature': feature, 'school_id': school_id, 'user_id': user_id},
            )
            invalidate_platform_cache()
            if _commit_or_rollback('Update AI policy'):
                flash('AI policy saved.', 'success')
            else:
                flash('The AI policy could not be saved right now.', 'danger')
            return redirect(url_for('platform.platform_ai_controls'))

    schools = school_roots_query().order_by(User.school_name.asc(), User.username.asc()).all()
    users = User.query.execution_options(include_deleted=True).filter(User.is_deleted.is_(False)).order_by(User.username.asc()).limit(200).all()
    return render_template(
        'platform_ai_controls.html',
        global_limits=ai_global_limits(),
        usage_by_school=ai_usage_by_school(),
        usage_by_user=ai_usage_by_user(),
        policies=AIAccessPolicy.query.order_by(AIAccessPolicy.updated_at.desc()).limit(100).all(),
        schools=schools,
        users=users,
        features=('*',) + ALL_FEATURES,
    )


@platform.route('/platform/security', methods=['GET', 'POST'])
@roles_required(User.ROLE_MASTER_ADMIN)
def platform_security():
    if request.method == 'POST':
        hooks = {
            'security.rate_limit.login_per_minute': _parse_int(request.form.get('login_per_minute'), minimum=1, maximum=1000, default=10),
            'security.rate_limit.password_reset_per_hour': _parse_int(request.form.get('password_reset_per_hour'), minimum=1, maximum=1000, default=20),
            'security.rate_limit.ai_per_minute': _parse_int(request.form.get('ai_per_minute'), minimum=1, maximum=1000, default=30),
        }
        for key, value in hooks.items():
            set_platform_setting(key, value, description='Rate-limit configuration hook', actor=current_user)
        add_audit_log('update_security_hooks', 'platform_setting', details=hooks)
        if _commit_or_rollback('Update security hooks'):
            flash('Security hooks updated.', 'success')
        else:
            flash('The security hooks could not be updated right now.', 'danger')
        return redirect(url_for('platform.platform_security'))

    locked_users = User.query.execution_options(include_deleted=True).filter_by(is_locked=True).order_by(User.updated_at.desc()).limit(50).all()
    forced_reset_users = User.query.execution_options(include_deleted=True).filter_by(force_password_reset=True).order_by(User.updated_at.desc()).limit(50).all()
    context = {
        'login_per_minute': get_platform_setting('security.rate_limit.login_per_minute', 10),
        'password_reset_per_hour': get_platform_setting('security.rate_limit.password_reset_per_hour', 20),
        'ai_per_minute': get_platform_setting('security.rate_limit.ai_per_minute', 30),
        'locked_users': locked_users,
        'forced_reset_users': forced_reset_users,
    }
    return render_template('platform_security.html', **context)


@platform.route('/platform/notifications/broadcast', methods=['POST'])
@roles_required(User.ROLE_MASTER_ADMIN)
def platform_broadcast_notification():
    title = (request.form.get('title') or '').strip()
    message = (request.form.get('message') or '').strip()
    category = (request.form.get('category') or 'info').strip().lower()
    scope = (request.form.get('scope') or 'all').strip().lower()
    school_id = request.form.get('school_id', type=int) if scope == 'school' else None
    if not title or not message:
        flash('Title and message are required for platform broadcasts.', 'danger')
        return redirect(url_for('platform.notifications_dashboard'))
    sent_count = broadcast_platform_notification(
        title,
        message,
        category=category,
        link=request.form.get('link') or None,
        school_id=school_id,
    )
    add_audit_log(
        'broadcast_notification',
        'notification',
        details={'scope': scope, 'school_id': school_id, 'title': title, 'sent_count': sent_count},
    )
    if _commit_or_rollback(f"Broadcast notification scope={scope} school_id={school_id or 'all'}"):
        flash(f'Broadcast delivered to {sent_count} account(s).', 'success')
    else:
        flash('The broadcast could not be delivered right now.', 'danger')
    return redirect(url_for('platform.notifications_dashboard'))


@platform.route('/platform/exports/users.csv')
@roles_required(User.ROLE_MASTER_ADMIN)
def export_platform_users_csv():
    rows = []
    users = User.query.execution_options(include_deleted=True).order_by(User.id.asc()).all()
    for user in users:
        rows.append(
            {
                'id': user.id,
                'username': user.username,
                'full_name': user.full_name,
                'email': user.email,
                'role': user.normalized_role,
                'school_id': user.school_scope_id,
                'is_active': user.is_active,
                'is_locked': user.is_locked,
                'force_password_reset': user.force_password_reset,
                'ai_access_enabled': user.ai_access_enabled,
                'is_deleted': user.is_deleted,
                'created_at': user.created_at,
            }
        )
    add_audit_log('export_users_csv', 'user', details={'row_count': len(rows)})
    _commit_or_rollback('Export users CSV audit')
    headers = list(rows[0].keys()) if rows else ['id', 'username', 'full_name', 'email', 'role', 'school_id', 'is_active', 'is_locked', 'force_password_reset', 'ai_access_enabled', 'is_deleted', 'created_at']
    return csv_response('platform-users.csv', headers, rows)


@platform.route('/platform/exports/schools.csv')
@roles_required(User.ROLE_MASTER_ADMIN)
def export_platform_schools_csv():
    rows = []
    for school in school_roots_query(include_deleted=True).order_by(User.id.asc()).all():
        dependencies = school_dependency_summary(school.id)
        rows.append(
            {
                'school_id': school.id,
                'username': school.username,
                'school_name': school.school_name,
                'email': school.email,
                'is_active': school.is_active,
                'is_deleted': school.is_deleted,
                'users': dependencies['users'],
                'students': dependencies['students'],
                'meal_plans': dependencies['meal_plans'],
                'notifications': dependencies['notifications'],
                'audit_logs': dependencies['audit_logs'],
            }
        )
    add_audit_log('export_schools_csv', 'school', details={'row_count': len(rows)})
    _commit_or_rollback('Export schools CSV audit')
    headers = list(rows[0].keys()) if rows else ['school_id', 'username', 'school_name', 'email', 'is_active', 'is_deleted', 'users', 'students', 'meal_plans', 'notifications', 'audit_logs']
    return csv_response('platform-schools.csv', headers, rows)


@platform.route('/platform/exports/schools/<int:school_id>.csv')
@roles_required(User.ROLE_MASTER_ADMIN)
def export_platform_school_detail_csv(school_id):
    school = _school_root_or_404(school_id)
    rows = []
    users = User.query.execution_options(include_deleted=True).filter(
        or_(User.id == school.id, User.school_id == school.id)
    ).order_by(User.id.asc()).all()
    for user in users:
        rows.append(
            {
                'school_id': school.id,
                'school_name': school.school_name or school.username,
                'user_id': user.id,
                'username': user.username,
                'role': user.normalized_role,
                'email': user.email,
                'is_active': user.is_active,
                'is_deleted': user.is_deleted,
            }
        )
    add_audit_log('export_school_csv', 'school', entity_id=school.id, details={'row_count': len(rows)})
    _commit_or_rollback(f'Export school CSV school_id={school.id}')
    headers = list(rows[0].keys()) if rows else ['school_id', 'school_name', 'user_id', 'username', 'role', 'email', 'is_active', 'is_deleted']
    return csv_response(f'school-{school.id}-export.csv', headers, rows)


@platform.route('/platform/exports/audit.csv')
@roles_required(User.ROLE_MASTER_ADMIN)
def export_platform_audit_csv():
    rows = []
    for log in AuditLog.query.order_by(AuditLog.created_at.desc()).limit(5000).all():
        rows.append(
            {
                'id': log.id,
                'school_id': log.school_id,
                'actor_user_id': log.actor_user_id,
                'action': log.action,
                'entity_type': log.entity_type,
                'entity_id': log.entity_id,
                'status': log.status,
                'ip_address': log.ip_address,
                'created_at': log.created_at,
                'details': log.details,
            }
        )
    add_audit_log('export_audit_csv', 'audit_log', details={'row_count': len(rows)})
    _commit_or_rollback('Export audit CSV audit')
    headers = list(rows[0].keys()) if rows else ['id', 'school_id', 'actor_user_id', 'action', 'entity_type', 'entity_id', 'status', 'ip_address', 'created_at', 'details']
    return csv_response('platform-audit.csv', headers, rows)


@platform.route('/account/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form.get('current_password') or ''
        new_password = request.form.get('new_password') or ''
        confirm_password = request.form.get('confirm_password') or ''

        if not current_user.check_password(current_password):
            flash('The current password you entered is not correct.', 'danger')
            return render_template('change_password.html')
        if len(new_password) < 8:
            flash('Choose a password with at least 8 characters.', 'danger')
            return render_template('change_password.html')
        if new_password != confirm_password:
            flash('The new password confirmation did not match.', 'danger')
            return render_template('change_password.html')

        current_user.set_password(new_password)
        add_audit_log('change_password', 'user', entity_id=current_user.id, details={'username': current_user.username})
        add_notification(
            current_user.id,
            'Password updated',
            'Your account password was changed successfully.',
            category='success',
            school_id=getattr(current_user, 'school_scope_id', None),
        )
        if not _commit_or_rollback(f"Change password user_id={current_user.id}"):
            flash('We could not update your password right now. Please try again.', 'danger')
            return render_template('change_password.html')

        flash('Your password has been updated.', 'success')
        return redirect(url_for('main.dashboard'))

    return render_template('change_password.html')


@platform.route('/password-reset/request', methods=['GET', 'POST'])
def password_reset_request():
    reset_link = None
    if request.method == 'POST':
        identifier = (request.form.get('identifier') or '').strip()
        if not identifier:
            flash('Enter a username or email address.', 'danger')
            return render_template('password_reset_request.html', reset_link=None)

        user = User.query.filter(
            or_(User.username == identifier, User.email == identifier)
        ).first()
        if user is not None and not getattr(user, 'is_deleted', False):
            raw_token, _ = issue_password_reset_token(
                user,
                requested_ip=request.headers.get('X-Forwarded-For', request.remote_addr),
            )
            add_audit_log(
                'password_reset_requested',
                'user',
                entity_id=user.id,
                actor_user=user,
                details={'username': user.username},
            )
            add_notification(
                user.id,
                'Password reset requested',
                'A password reset was requested for your account.',
                category='warning',
                school_id=getattr(user, 'school_scope_id', None),
            )
            _commit_or_rollback(f"Password reset request user_id={user.id}")
            reset_link = url_for('platform.password_reset_form', token=raw_token, _external=not current_app.testing)
            logger.info("Generated password reset link for user_id=%s", user.id)

        if current_app.config.get('APP_ENV') == 'production':
            flash('If an account matched that identifier, a reset link has been generated for the configured delivery workflow.', 'info')
        else:
            flash('If an account matched that identifier, a reset link is ready below for testing.', 'info')

    return render_template('password_reset_request.html', reset_link=reset_link)


@platform.route('/password-reset/<token>', methods=['GET', 'POST'])
def password_reset_form(token):
    token_record = resolve_password_reset_token(token)
    if token_record is None:
        flash('This password reset link is invalid or has expired.', 'danger')
        return render_template('password_reset_form.html', token_valid=False)

    if request.method == 'POST':
        new_password = request.form.get('new_password') or ''
        confirm_password = request.form.get('confirm_password') or ''
        if len(new_password) < 8:
            flash('Choose a password with at least 8 characters.', 'danger')
            return render_template('password_reset_form.html', token_valid=True)
        if new_password != confirm_password:
            flash('The password confirmation did not match.', 'danger')
            return render_template('password_reset_form.html', token_valid=True)

        consumed_token = consume_password_reset_token(token)
        if consumed_token is None:
            flash('This password reset link is invalid or has expired.', 'danger')
            return render_template('password_reset_form.html', token_valid=False)

        user = consumed_token.user
        user.set_password(new_password)
        add_audit_log('password_reset_completed', 'user', entity_id=user.id, actor_user=user, details={'username': user.username})
        add_notification(
            user.id,
            'Password reset completed',
            'Your account password was reset successfully.',
            category='success',
            school_id=getattr(user, 'school_scope_id', None),
        )
        if not _commit_or_rollback(f"Complete password reset user_id={user.id}"):
            flash('The password could not be reset right now. Please try again.', 'danger')
            return render_template('password_reset_form.html', token_valid=True)

        flash('Your password has been reset. Please sign in with the new password.', 'success')
        return redirect(url_for('main.login'))

    return render_template('password_reset_form.html', token_valid=True)


@platform.route('/notifications')
@login_required
def notifications_dashboard():
    if current_user.has_role(User.ROLE_MASTER_ADMIN):
        page = _parse_int(request.args.get('page'), minimum=1, default=1)
        category = (request.args.get('category') or '').strip().lower() or None
        school_id = request.args.get('school_id', type=int)
        user_id = request.args.get('user_id', type=int)
        notifications_page = list_notifications(
            page=page,
            per_page=25,
            category=category,
            school_id=school_id,
            user_id=user_id,
        )
        return render_template(
            'notifications.html',
            notifications=notifications_page.items,
            notifications_page=notifications_page,
            global_mode=True,
            schools=school_roots_query().order_by(User.school_name.asc(), User.username.asc()).all(),
            users=User.query.execution_options(include_deleted=True).filter(User.is_deleted.is_(False)).order_by(User.username.asc()).limit(200).all(),
            selected_category=category,
            selected_school_id=school_id,
            selected_user_id=user_id,
        )
    notifications = current_user.notifications.order_by(Notification.created_at.desc()).limit(100).all()
    return render_template('notifications.html', notifications=notifications, global_mode=False)


@platform.route('/notifications/<int:notification_id>/read', methods=['POST'])
@login_required
def mark_notification_read(notification_id):
    if current_user.has_role(User.ROLE_MASTER_ADMIN):
        notification = Notification.query.filter_by(id=notification_id).first_or_404()
    else:
        notification = Notification.query.filter_by(id=notification_id, user_id=current_user.id).first_or_404()
    notification.mark_read()
    _commit_or_rollback(f"Mark notification read notification_id={notification_id}")
    return redirect(url_for('platform.notifications_dashboard'))


@platform.route('/notifications/<int:notification_id>/unread', methods=['POST'])
@login_required
def mark_notification_unread(notification_id):
    if current_user.has_role(User.ROLE_MASTER_ADMIN):
        notification = Notification.query.filter_by(id=notification_id).first_or_404()
    else:
        notification = Notification.query.filter_by(id=notification_id, user_id=current_user.id).first_or_404()
    notification.is_read = False
    notification.read_at = None
    _commit_or_rollback(f"Mark notification unread notification_id={notification_id}")
    return redirect(url_for('platform.notifications_dashboard'))


@platform.route('/notifications/read-all', methods=['POST'])
@login_required
def mark_all_notifications_read():
    if current_user.has_role(User.ROLE_MASTER_ADMIN):
        flash('Use the global notification filters to review platform broadcasts individually.', 'info')
        return redirect(url_for('platform.notifications_dashboard'))
    notifications = current_user.notifications.filter_by(is_read=False).all()
    for notification in notifications:
        notification.mark_read()
    _commit_or_rollback(f"Mark all notifications read user_id={current_user.id}")
    return redirect(url_for('platform.notifications_dashboard'))


@platform.route('/staff', methods=['GET', 'POST'])
@roles_required(User.ROLE_SCHOOL_ADMIN)
def staff_management():
    denial = _ensure_school_permission('can_manage_staff_effective')
    if denial is not None:
        return denial

    school_scope_id = _school_scope_id()
    school_students = StudentDetail.query.filter_by(school_id=school_scope_id).order_by(StudentDetail.full_name.asc()).all()

    if request.method == 'POST':
        account_type = request.form.get('account_type') or 'staff'
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        full_name = (request.form.get('full_name') or '').strip()
        email = (request.form.get('email') or '').strip() or None

        if not username or len(password) < 8 or not full_name:
            flash('Provide a username, full name, and a password with at least 8 characters.', 'danger')
            return redirect(url_for('platform.staff_management'))
        if User.query.filter_by(username=username).first():
            flash(f'The username "{username}" is already in use.', 'danger')
            return redirect(url_for('platform.staff_management'))

        if account_type == 'guardian':
            student_id = request.form.get('student_id', type=int)
            student = StudentDetail.query.filter_by(id=student_id, school_id=school_scope_id).first()
            if student is None:
                flash('Choose a valid student to link this guardian account.', 'danger')
                return redirect(url_for('platform.staff_management'))

            new_user = User(
                username=username,
                email=email,
                full_name=full_name,
                role=User.ROLE_USER,
                school_id=school_scope_id,
                primary_student_id=student.id,
            )
            new_user.set_password(password)
            db.session.add(new_user)
            db.session.flush()
            add_audit_log('create', 'guardian_account', entity_id=username, details={'student_id': student.id, 'school_id': school_scope_id})
            add_notification(
                new_user.id,
                'Guardian account created',
                f'Your account is linked to {student.full_name}.',
                category='success',
                school_id=school_scope_id,
            )
            if not _commit_or_rollback(f"Create guardian account username={username} school_id={school_scope_id}"):
                flash('The guardian account could not be created right now.', 'danger')
                return redirect(url_for('platform.staff_management'))
            flash('Guardian account created successfully.', 'success')
            return redirect(url_for('platform.staff_management'))

        new_staff = User(
            username=username,
            email=email,
            full_name=full_name,
            role=User.ROLE_SCHOOL_ADMIN,
            school_id=school_scope_id,
            can_manage_students=_parse_bool(request.form.get('can_manage_students')),
            can_manage_meals=_parse_bool(request.form.get('can_manage_meals')),
            can_manage_attendance=_parse_bool(request.form.get('can_manage_attendance')),
            can_view_reports=_parse_bool(request.form.get('can_view_reports')),
            can_manage_staff=_parse_bool(request.form.get('can_manage_staff')),
            can_approve_workflows=_parse_bool(request.form.get('can_approve_workflows')),
        )
        new_staff.set_password(password)
        db.session.add(new_staff)
        db.session.flush()
        add_audit_log('create', 'staff_account', entity_id=username, details={'school_id': school_scope_id})
        add_notification(
            new_staff.id,
            'Staff account created',
            'Your school staff account is ready.',
            category='success',
            school_id=school_scope_id,
        )
        if not _commit_or_rollback(f"Create staff account username={username} school_id={school_scope_id}"):
            flash('The staff account could not be created right now.', 'danger')
            return redirect(url_for('platform.staff_management'))
        flash('Staff account created successfully.', 'success')
        return redirect(url_for('platform.staff_management'))

    staff_members = _school_staff_query(school_scope_id).all()
    archived_staff = _school_staff_query(school_scope_id, include_deleted=True).filter_by(is_deleted=True).all()
    guardians = _guardian_accounts_query(school_scope_id).all()
    return render_template(
        'staff_management.html',
        staff_members=staff_members,
        archived_staff=archived_staff,
        guardians=guardians,
        students=school_students,
    )


@platform.route('/staff/<int:user_id>/permissions', methods=['POST'])
@roles_required(User.ROLE_SCHOOL_ADMIN)
def update_staff_permissions(user_id):
    denial = _ensure_school_permission('can_manage_staff_effective')
    if denial is not None:
        return denial

    school_scope_id = _school_scope_id()
    staff_member = User.query.filter_by(id=user_id, school_id=school_scope_id).first_or_404()
    staff_member.can_manage_students = _parse_bool(request.form.get('can_manage_students'))
    staff_member.can_manage_meals = _parse_bool(request.form.get('can_manage_meals'))
    staff_member.can_manage_attendance = _parse_bool(request.form.get('can_manage_attendance'))
    staff_member.can_view_reports = _parse_bool(request.form.get('can_view_reports'))
    staff_member.can_manage_staff = _parse_bool(request.form.get('can_manage_staff'))
    staff_member.can_approve_workflows = _parse_bool(request.form.get('can_approve_workflows'))
    add_audit_log('update', 'staff_permissions', entity_id=user_id, details={'school_id': school_scope_id})
    _commit_or_rollback(f"Update staff permissions user_id={user_id} school_id={school_scope_id}")
    return redirect(url_for('platform.staff_management'))


@platform.route('/staff/<int:user_id>/deactivate', methods=['POST'])
@roles_required(User.ROLE_SCHOOL_ADMIN)
def deactivate_account(user_id):
    denial = _ensure_school_permission('can_manage_staff_effective')
    if denial is not None:
        return denial

    school_scope_id = _school_scope_id()
    account = User.query.execution_options(include_deleted=True).filter_by(id=user_id).first_or_404()
    if account.school_scope_id != school_scope_id:
        flash('You do not have permission to manage this account.', 'danger')
        return redirect(url_for('platform.staff_management'))
    account.soft_delete()
    if account.portal_student_detail is not None and account.primary_student_id is None:
        account.portal_student_detail.soft_delete()
    add_audit_log('soft_delete', 'user', entity_id=user_id, details={'school_id': school_scope_id, 'username': account.username})
    _commit_or_rollback(f"Deactivate account user_id={user_id} school_id={school_scope_id}")
    return redirect(url_for('platform.staff_management'))


@platform.route('/staff/<int:user_id>/restore', methods=['POST'])
@roles_required(User.ROLE_SCHOOL_ADMIN)
def restore_account(user_id):
    denial = _ensure_school_permission('can_manage_staff_effective')
    if denial is not None:
        return denial

    school_scope_id = _school_scope_id()
    account = User.query.execution_options(include_deleted=True).filter_by(id=user_id).first_or_404()
    if account.school_scope_id != school_scope_id:
        flash('You do not have permission to restore this account.', 'danger')
        return redirect(url_for('platform.staff_management'))
    account.restore()
    if account.portal_student_detail is not None and account.primary_student_id is None:
        account.portal_student_detail.restore()
    add_audit_log('restore', 'user', entity_id=user_id, details={'school_id': school_scope_id, 'username': account.username})
    _commit_or_rollback(f"Restore account user_id={user_id} school_id={school_scope_id}")
    return redirect(url_for('platform.staff_management'))


@platform.route('/meal-templates', methods=['GET', 'POST'])
@roles_required(User.ROLE_SCHOOL_ADMIN)
def meal_templates_dashboard():
    denial = _ensure_school_permission('can_manage_meals_effective')
    if denial is not None:
        return denial

    school_scope_id = _school_scope_id()
    foods = _school_food_query(school_scope_id).order_by(Food.name.asc()).all()

    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        description = (request.form.get('description') or '').strip() or None
        if not name:
            flash('Template name is required.', 'danger')
            return redirect(url_for('platform.meal_templates_dashboard'))

        template = MealTemplate(
            school_id=school_scope_id,
            name=name,
            description=description,
            created_by_user_id=current_user.id,
        )
        db.session.add(template)
        db.session.flush()

        for meal_type in ('breakfast', 'lunch', 'dinner'):
            for value in request.form.getlist(f'{meal_type}_foods'):
                try:
                    food_id = int(value)
                except (TypeError, ValueError):
                    continue
                if food_id not in {food.id for food in foods}:
                    continue
                db.session.add(MealTemplateItem(template_id=template.id, food_id=food_id, meal_type=meal_type.capitalize()))

        add_audit_log('create', 'meal_template', entity_id=template.id, details={'school_id': school_scope_id, 'name': template.name})
        if not _commit_or_rollback(f"Create meal template template_id={template.id} school_id={school_scope_id}"):
            flash('The meal template could not be saved right now.', 'danger')
            return redirect(url_for('platform.meal_templates_dashboard'))

        flash('Meal template created successfully.', 'success')
        return redirect(url_for('platform.meal_templates_dashboard'))

    templates = _meal_template_query(school_scope_id).all()
    return render_template('meal_templates.html', templates=templates, foods=foods, today=date.today())


@platform.route('/meal-templates/<int:template_id>/clone', methods=['POST'])
@roles_required(User.ROLE_SCHOOL_ADMIN)
def clone_meal_template(template_id):
    denial = _ensure_school_permission('can_manage_meals_effective')
    if denial is not None:
        return denial

    school_scope_id = _school_scope_id()
    source_template = MealTemplate.query.filter_by(id=template_id, school_id=school_scope_id).first_or_404()
    cloned_template = MealTemplate(
        school_id=school_scope_id,
        name=f'{source_template.name} Copy',
        description=source_template.description,
        created_by_user_id=current_user.id,
    )
    db.session.add(cloned_template)
    db.session.flush()
    for item in source_template.items:
        db.session.add(MealTemplateItem(template_id=cloned_template.id, food_id=item.food_id, meal_type=item.meal_type))

    add_audit_log('clone', 'meal_template', entity_id=template_id, details={'school_id': school_scope_id, 'cloned_template_id': cloned_template.id})
    _commit_or_rollback(f"Clone meal template template_id={template_id} school_id={school_scope_id}")
    return redirect(url_for('platform.meal_templates_dashboard'))


def _create_plan_from_template(template, plan_date, *, approved):
    existing_plan = MealPlan.query.filter_by(school_id=template.school_id, plan_date=plan_date).first()
    if existing_plan is not None:
        return None

    plan = MealPlan(
        school_id=template.school_id,
        plan_date=plan_date,
        title=template.name,
        notes=template.description,
        template_id=template.id,
        recurrence_label=template.name,
        status='approved' if approved else 'pending',
        created_by_user_id=current_user.id,
        approved_by_user_id=current_user.id if approved else None,
        approved_at=utcnow() if approved else None,
    )
    db.session.add(plan)
    db.session.flush()
    for item in template.items:
        db.session.add(MealPlanItem(meal_plan_id=plan.id, food_id=item.food_id, meal_type=item.meal_type))
    if not approved:
        db.session.add(
            ApprovalRequest(
                school_id=template.school_id,
                request_type='meal_plan_approval',
                target_model='MealPlan',
                target_id=str(plan.id),
                requester_user_id=current_user.id,
                payload={'plan_date': plan_date.isoformat(), 'template_id': template.id},
            )
        )
    return plan


@platform.route('/meal-templates/<int:template_id>/apply', methods=['POST'])
@roles_required(User.ROLE_SCHOOL_ADMIN)
def apply_meal_template(template_id):
    denial = _ensure_school_permission('can_manage_meals_effective')
    if denial is not None:
        return denial

    school_scope_id = _school_scope_id()
    template = MealTemplate.query.filter_by(id=template_id, school_id=school_scope_id).first_or_404()
    start_date = request.form.get('start_date') or date.today().isoformat()
    recurrence_count = max(1, min(request.form.get('recurrence_count', type=int) or 1, 12))
    recurrence = (request.form.get('recurrence') or 'daily').strip().lower()

    try:
        base_date = date.fromisoformat(start_date)
    except ValueError:
        flash('Choose a valid start date.', 'danger')
        return redirect(url_for('platform.meal_templates_dashboard'))

    approved = current_user.can_approve_workflows_effective
    created_count = 0
    for offset in range(recurrence_count):
        if recurrence == 'monthly':
            plan_date = base_date + timedelta(days=30 * offset)
        elif recurrence == 'weekly':
            plan_date = base_date + timedelta(days=7 * offset)
        else:
            plan_date = base_date + timedelta(days=offset)
        if _create_plan_from_template(template, plan_date, approved=approved) is not None:
            created_count += 1

    add_audit_log('apply_template', 'meal_template', entity_id=template.id, details={'school_id': school_scope_id, 'created_count': created_count, 'recurrence': recurrence})
    if not _commit_or_rollback(f"Apply meal template template_id={template.id} school_id={school_scope_id}"):
        flash('The template could not be applied right now.', 'danger')
        return redirect(url_for('platform.meal_templates_dashboard'))

    broadcast_school_notification(
        school_scope_id,
        'Meal plans generated',
        f'{created_count} meal plan(s) were generated from template {template.name}.',
        category='success' if approved else 'warning',
        link=url_for('main.dashboard'),
    )
    _commit_or_rollback(f"Meal template apply notifications school_id={school_scope_id}")
    flash(f'{created_count} meal plan(s) generated from the template.', 'success')
    return redirect(url_for('platform.meal_templates_dashboard'))


@platform.route('/meal-plans/bulk-create', methods=['POST'])
@roles_required(User.ROLE_SCHOOL_ADMIN)
def bulk_create_meal_plans():
    denial = _ensure_school_permission('can_manage_meals_effective')
    if denial is not None:
        return denial

    template_id = request.form.get('template_id', type=int)
    template = MealTemplate.query.filter_by(id=template_id, school_id=_school_scope_id()).first_or_404()
    return apply_meal_template(template.id)


@platform.route('/students/import-csv', methods=['POST'])
@roles_required(User.ROLE_SCHOOL_ADMIN)
def import_students_csv():
    denial = _ensure_school_permission('can_manage_students_effective')
    if denial is not None:
        return denial

    school_scope_id = _school_scope_id()
    job = enqueue_job('student_csv_import', user=current_user, school_id=school_scope_id)
    db.session.flush()
    try:
        mark_job_running(job)
        reader = _parse_csv_upload(request.files.get('file'))
        created_students = 0
        skipped_rows = []
        for index, row in enumerate(reader, start=2):
            username = (row.get('username') or '').strip()
            full_name = (row.get('full_name') or '').strip()
            password = (row.get('password') or '').strip()
            if not username or not full_name or len(password) < 8:
                skipped_rows.append({'row': index, 'reason': 'missing required fields'})
                continue
            if User.query.filter_by(username=username).first():
                skipped_rows.append({'row': index, 'reason': 'username already exists'})
                continue
            try:
                student_dob = date.fromisoformat((row.get('dob') or '').strip())
                roll_no = int(row.get('roll_no'))
                grade = int(row.get('grade'))
            except (TypeError, ValueError):
                skipped_rows.append({'row': index, 'reason': 'invalid roll number, grade, or date'})
                continue

            new_user = User(
                username=username,
                full_name=full_name,
                role=User.ROLE_USER,
                school_id=school_scope_id,
            )
            new_user.set_password(password)
            student = StudentDetail(
                user=new_user,
                school_id=school_scope_id,
                full_name=full_name,
                roll_no=roll_no,
                dob=student_dob,
                sex=(row.get('sex') or 'Female').strip() or 'Female',
                grade=grade,
                section=(row.get('section') or 'A').strip() or 'A',
                allergies=(row.get('allergies') or '').strip() or None,
                guardian_name=(row.get('guardian_name') or '').strip() or None,
                guardian_email=(row.get('guardian_email') or '').strip() or None,
                guardian_phone=(row.get('guardian_phone') or '').strip() or None,
            )
            db.session.add(new_user)
            height_value = row.get('height_cm')
            weight_value = row.get('weight_kg')
            if height_value and weight_value:
                try:
                    db.session.add(
                        HealthMetric(
                            student_detail=student,
                            record_date=date.today(),
                            height_cm=float(height_value),
                            weight_kg=float(weight_value),
                        )
                    )
                except ValueError:
                    skipped_rows.append({'row': index, 'reason': 'invalid height or weight'})
            created_students += 1

        add_audit_log('bulk_import', 'student', entity_id=school_scope_id, details={'created_students': created_students, 'skipped_rows': skipped_rows[:10]})
        mark_job_complete(job, result={'created_students': created_students, 'skipped_rows': skipped_rows})
        if not _commit_or_rollback(f"Import students CSV school_id={school_scope_id}"):
            flash('The CSV import could not be completed right now.', 'danger')
            return redirect(url_for('platform.staff_management'))

        flash(f'Student import completed. Created {created_students} accounts.', 'success')
        return redirect(url_for('platform.staff_management'))
    except Exception as exc:
        mark_job_failed(job, error_message=str(exc))
        _commit_or_rollback(f"Import students CSV failed school_id={school_scope_id}")
        flash(f'Student import failed: {exc}', 'danger')
        return redirect(url_for('platform.staff_management'))


@platform.route('/attendance/import-csv', methods=['POST'])
@roles_required(User.ROLE_SCHOOL_ADMIN)
def import_attendance_csv():
    denial = _ensure_school_permission('can_manage_attendance_effective')
    if denial is not None:
        return denial

    school_scope_id = _school_scope_id()
    job = enqueue_job('attendance_csv_import', user=current_user, school_id=school_scope_id)
    db.session.flush()
    try:
        mark_job_running(job)
        reader = _parse_csv_upload(request.files.get('file'))
        updated_rows = 0
        skipped_rows = []
        students_by_roll = {
            student.roll_no: student
            for student in StudentDetail.query.filter_by(school_id=school_scope_id).all()
        }

        for index, row in enumerate(reader, start=2):
            try:
                target_date = date.fromisoformat((row.get('attendance_date') or row.get('date') or '').strip())
                roll_no = int(row.get('roll_no'))
            except (TypeError, ValueError):
                skipped_rows.append({'row': index, 'reason': 'invalid date or roll number'})
                continue

            student = students_by_roll.get(roll_no)
            if student is None:
                skipped_rows.append({'row': index, 'reason': f'unknown roll number {roll_no}'})
                continue

            record = Attendance.query.filter_by(student_id=student.id, attendance_date=target_date).first()
            if record is None:
                record = Attendance(student_id=student.id, attendance_date=target_date)
                db.session.add(record)
            record.recorded_by_user_id = current_user.id
            record.approval_status = 'approved' if current_user.can_approve_workflows_effective else 'pending'
            record.ate_breakfast = _csv_bool(row.get('breakfast'))
            record.ate_lunch = _csv_bool(row.get('lunch'))
            record.ate_dinner = _csv_bool(row.get('dinner'))
            updated_rows += 1

        add_audit_log('bulk_import', 'attendance', entity_id=school_scope_id, details={'updated_rows': updated_rows, 'skipped_rows': skipped_rows[:10]})
        mark_job_complete(job, result={'updated_rows': updated_rows, 'skipped_rows': skipped_rows})
        if not _commit_or_rollback(f"Import attendance CSV school_id={school_scope_id}"):
            flash('The attendance import could not be completed right now.', 'danger')
            return redirect(url_for('platform.reports_dashboard'))

        flash(f'Attendance import completed. Updated {updated_rows} rows.', 'success')
        return redirect(url_for('platform.reports_dashboard'))
    except Exception as exc:
        mark_job_failed(job, error_message=str(exc))
        _commit_or_rollback(f"Import attendance CSV failed school_id={school_scope_id}")
        flash(f'Attendance import failed: {exc}', 'danger')
        return redirect(url_for('platform.reports_dashboard'))


@platform.route('/students/search')
@roles_required(User.ROLE_SCHOOL_ADMIN)
def student_search():
    school_scope_id = _school_scope_id()
    students = StudentDetail.query.filter_by(school_id=school_scope_id)
    query = (request.args.get('q') or '').strip().lower()
    grade = request.args.get('grade', type=int)
    status = (request.args.get('status') or '').strip().lower()
    min_attendance = request.args.get('min_attendance', type=float)

    if query:
        students = students.filter(
            or_(
                func.lower(StudentDetail.full_name).contains(query),
                func.lower(StudentDetail.section).contains(query),
                func.cast(StudentDetail.roll_no, db.String).contains(query),
            )
        )
    if grade:
        students = students.filter_by(grade=grade)
    if status:
        students = students.filter_by(status=status)

    results = []
    for student in students.order_by(StudentDetail.full_name.asc()).limit(100).all():
        attendance_percent = _attendance_percentage(student.id)
        if min_attendance is not None and attendance_percent is not None and attendance_percent < min_attendance:
            continue
        results.append(
            {
                'id': student.id,
                'full_name': student.full_name,
                'grade': student.grade,
                'section': student.section,
                'roll_no': student.roll_no,
                'status': student.status,
                'attendance_percent': attendance_percent,
            }
        )
    return jsonify(results)


@platform.route('/reports')
@roles_required(User.ROLE_SCHOOL_ADMIN)
def reports_dashboard():
    school_scope_id = _school_scope_id()
    school = db.session.get(User, school_scope_id)
    students = StudentDetail.query.filter_by(school_id=school_scope_id).count()
    meal_plans = MealPlan.query.filter_by(school_id=school_scope_id).count()
    attendance_rows = Attendance.query.join(StudentDetail).filter(StudentDetail.school_id == school_scope_id).count()
    templates = MealTemplate.query.filter_by(school_id=school_scope_id).count()
    return render_template(
        'reports.html',
        school=school,
        student_count=students,
        meal_plan_count=meal_plans,
        attendance_count=attendance_rows,
        template_count=templates,
    )


@platform.route('/reports/students.csv')
@roles_required(User.ROLE_SCHOOL_ADMIN)
def export_students_report():
    school_scope_id = _school_scope_id()
    rows = []
    for student in StudentDetail.query.filter_by(school_id=school_scope_id).order_by(StudentDetail.roll_no.asc()).all():
        rows.append(
            {
                'full_name': student.full_name,
                'roll_no': student.roll_no,
                'grade': student.grade,
                'section': student.section,
                'status': student.status,
                'attendance_percent': _attendance_percentage(student.id),
                'latest_height_cm': student.latest_height,
                'latest_weight_kg': student.latest_weight,
                'bmi': round(student.bmi, 1) if student.bmi else None,
            }
        )
    return csv_response('students-report.csv', list(rows[0].keys()) if rows else ['full_name', 'roll_no', 'grade', 'section', 'status', 'attendance_percent', 'latest_height_cm', 'latest_weight_kg', 'bmi'], rows)


@platform.route('/reports/attendance.csv')
@roles_required(User.ROLE_SCHOOL_ADMIN)
def export_attendance_report():
    school_scope_id = _school_scope_id()
    rows = []
    records = (
        Attendance.query.join(StudentDetail)
        .filter(StudentDetail.school_id == school_scope_id)
        .order_by(Attendance.attendance_date.desc(), StudentDetail.roll_no.asc())
        .all()
    )
    for record in records:
        rows.append(
            {
                'attendance_date': record.attendance_date,
                'student_name': record.student_detail.full_name,
                'roll_no': record.student_detail.roll_no,
                'breakfast': record.ate_breakfast,
                'lunch': record.ate_lunch,
                'dinner': record.ate_dinner,
                'approval_status': record.approval_status,
                'recorded_by_user_id': record.recorded_by_user_id,
            }
        )
    headers = list(rows[0].keys()) if rows else ['attendance_date', 'student_name', 'roll_no', 'breakfast', 'lunch', 'dinner', 'approval_status', 'recorded_by_user_id']
    return csv_response('attendance-report.csv', headers, rows)


@platform.route('/reports/meals.csv')
@roles_required(User.ROLE_SCHOOL_ADMIN)
def export_meals_report():
    school_scope_id = _school_scope_id()
    rows = []
    plans = MealPlan.query.filter_by(school_id=school_scope_id).order_by(MealPlan.plan_date.desc()).all()
    for plan in plans:
        for item in plan.items:
            rows.append(
                {
                    'plan_date': plan.plan_date,
                    'meal_plan_id': plan.id,
                    'status': plan.status,
                    'meal_type': item.meal_type,
                    'food_name': item.food.name if item.food else 'Missing Food',
                    'calories': item.food.calories if item.food else None,
                    'template_id': plan.template_id,
                }
            )
    headers = list(rows[0].keys()) if rows else ['plan_date', 'meal_plan_id', 'status', 'meal_type', 'food_name', 'calories', 'template_id']
    return csv_response('meal-plan-report.csv', headers, rows)


@platform.route('/approvals')
@roles_required(User.ROLE_SCHOOL_ADMIN)
def approvals_dashboard():
    school_scope_id = _school_scope_id()
    approvals = ApprovalRequest.query.filter_by(school_id=school_scope_id).order_by(ApprovalRequest.created_at.desc()).all()
    return render_template('approvals.html', approvals=approvals)


@platform.route('/approvals/<int:approval_id>/<action>', methods=['POST'])
@roles_required(User.ROLE_SCHOOL_ADMIN)
def resolve_approval(approval_id, action):
    denial = _ensure_school_permission('can_approve_workflows_effective')
    if denial is not None:
        return denial

    school_scope_id = _school_scope_id()
    approval = ApprovalRequest.query.filter_by(id=approval_id, school_id=school_scope_id).first_or_404()
    notes = (request.form.get('notes') or '').strip() or None
    target_plan = None

    if action == 'approve':
        approval.approve(current_user, notes=notes)
        if approval.target_model == 'MealPlan':
            target_plan = MealPlan.query.filter_by(id=int(approval.target_id or 0), school_id=school_scope_id).first()
            if target_plan is not None:
                target_plan.status = 'approved'
                target_plan.approved_by_user_id = current_user.id
                target_plan.approved_at = utcnow()
        elif approval.target_model == 'Attendance':
            try:
                target_date = date.fromisoformat(approval.target_id or '')
            except ValueError:
                target_date = None
            if target_date is not None:
                attendance_rows = (
                    Attendance.query.join(StudentDetail)
                    .filter(
                        StudentDetail.school_id == school_scope_id,
                        Attendance.attendance_date == target_date,
                        Attendance.approval_status == 'pending',
                    )
                    .all()
                )
                for row in attendance_rows:
                    row.approval_status = 'approved'
        add_audit_log('approve', 'approval_request', entity_id=approval_id, details={'school_id': school_scope_id})
        if target_plan is not None:
            broadcast_school_notification(
                school_scope_id,
                'Meal plan approved',
                f'Meal plan for {target_plan.plan_date.strftime("%d %B, %Y")} was approved.',
                category='success',
                link=url_for('main.dashboard'),
            )
    else:
        approval.reject(current_user, notes=notes)
        add_audit_log('reject', 'approval_request', entity_id=approval_id, details={'school_id': school_scope_id})

    _commit_or_rollback(f"Resolve approval approval_id={approval_id} school_id={school_scope_id}")
    return redirect(url_for('platform.approvals_dashboard'))


@platform.route('/attendance/request-correction', methods=['POST'])
@roles_required(User.ROLE_SCHOOL_ADMIN)
def request_attendance_correction():
    school_scope_id = _school_scope_id()
    correction_date = request.form.get('attendance_date') or ''
    notes = (request.form.get('notes') or '').strip()
    if not correction_date:
        flash('Choose an attendance date for the correction request.', 'danger')
        return redirect(url_for('platform.approvals_dashboard'))

    approval = ApprovalRequest(
        school_id=school_scope_id,
        request_type='attendance_correction',
        target_model='AttendanceCorrection',
        target_id=correction_date,
        requester_user_id=current_user.id,
        payload={'attendance_date': correction_date, 'notes': notes},
    )
    db.session.add(approval)
    add_audit_log('create', 'attendance_correction_request', entity_id=correction_date, details={'school_id': school_scope_id})
    _commit_or_rollback(f"Create attendance correction request school_id={school_scope_id} date={correction_date}")
    flash('Attendance correction request submitted.', 'success')
    return redirect(url_for('platform.approvals_dashboard'))


@platform.route('/feedback', methods=['GET', 'POST'])
@login_required
def feedback_portal():
    school_scope_id = getattr(current_user, 'school_scope_id', None)
    student = getattr(current_user, 'portal_student_detail', None)

    if request.method == 'POST':
        subject = (request.form.get('subject') or '').strip()
        message = (request.form.get('message') or '').strip()
        if not subject or not message or not school_scope_id:
            flash('Please complete both the subject and the message.', 'danger')
            return redirect(url_for('platform.feedback_portal'))

        entry = UserFeedback(
            school_id=school_scope_id,
            user_id=current_user.id,
            student_id=getattr(student, 'id', None),
            subject=subject,
            message=message,
        )
        db.session.add(entry)
        add_audit_log('create', 'feedback', entity_id=subject, details={'school_id': school_scope_id, 'user_id': current_user.id})
        broadcast_school_notification(
            school_scope_id,
            'New portal feedback',
            f'{current_user.display_name} submitted new feedback.',
            category='info',
            link=url_for('platform.feedback_portal'),
        )
        _commit_or_rollback(f"Create feedback user_id={current_user.id} school_id={school_scope_id}")
        flash('Your feedback has been submitted.', 'success')
        return redirect(url_for('platform.feedback_portal'))

    if current_user.has_role(User.ROLE_SCHOOL_ADMIN):
        entries = UserFeedback.query.filter_by(school_id=school_scope_id).order_by(UserFeedback.created_at.desc()).all()
    else:
        entries = UserFeedback.query.filter_by(user_id=current_user.id).order_by(UserFeedback.created_at.desc()).all()
    return render_template('feedback.html', entries=entries)


@platform.route('/feedback/<int:feedback_id>/resolve', methods=['POST'])
@roles_required(User.ROLE_SCHOOL_ADMIN)
def resolve_feedback(feedback_id):
    entry = UserFeedback.query.filter_by(id=feedback_id, school_id=_school_scope_id()).first_or_404()
    entry.status = 'resolved'
    entry.responded_at = utcnow()
    add_audit_log('resolve', 'feedback', entity_id=feedback_id, details={'school_id': _school_scope_id()})
    add_notification(
        entry.user_id,
        'Feedback updated',
        f'Your feedback "{entry.subject}" has been reviewed by the school.',
        category='success',
        school_id=_school_scope_id(),
        link=url_for('platform.feedback_portal'),
    )
    _commit_or_rollback(f"Resolve feedback feedback_id={feedback_id} school_id={_school_scope_id()}")
    return redirect(url_for('platform.feedback_portal'))


@platform.route('/activity')
@login_required
def activity_logs():
    if current_user.has_role(User.ROLE_MASTER_ADMIN):
        page = _parse_int(request.args.get('page'), minimum=1, default=1)
        actor_user_id = request.args.get('user_id', type=int)
        action = (request.args.get('action') or '').strip() or None
        date_from_raw = (request.args.get('date_from') or '').strip() or None
        date_to_raw = (request.args.get('date_to') or '').strip() or None
        try:
            date_from = date.fromisoformat(date_from_raw) if date_from_raw else None
        except ValueError:
            date_from = None
        try:
            date_to = date.fromisoformat(date_to_raw) if date_to_raw else None
        except ValueError:
            date_to = None
        logs_page = list_audit_logs(
            page=page,
            per_page=30,
            actor_user_id=actor_user_id,
            action=action,
            date_from=date_from,
            date_to=(date_to + timedelta(days=1)) if date_to else None,
        )
        return render_template(
            'activity_logs.html',
            logs=logs_page.items,
            logs_page=logs_page,
            global_mode=True,
            users=User.query.execution_options(include_deleted=True).filter(User.is_deleted.is_(False)).order_by(User.username.asc()).limit(250).all(),
            selected_user_id=actor_user_id,
            selected_action=action,
            selected_date_from=date_from_raw or '',
            selected_date_to=date_to_raw or '',
        )
    elif current_user.has_role(User.ROLE_SCHOOL_ADMIN):
        logs = AuditLog.query.filter_by(school_id=_school_scope_id()).order_by(AuditLog.created_at.desc()).limit(250).all()
    else:
        flash('You do not have access to platform activity logs.', 'danger')
        return redirect(url_for('main.dashboard'))
    return render_template('activity_logs.html', logs=logs, global_mode=False)


@platform.route('/jobs')
@login_required
def jobs_dashboard():
    if current_user.has_role(User.ROLE_MASTER_ADMIN):
        page = _parse_int(request.args.get('page'), minimum=1, default=1)
        status = (request.args.get('status') or '').strip().lower() or None
        jobs_page = list_jobs(page=page, per_page=25, status=status)
        return render_template(
            'jobs.html',
            jobs=jobs_page.items,
            jobs_page=jobs_page,
            global_mode=True,
            selected_status=status,
        )
    elif current_user.has_role(User.ROLE_SCHOOL_ADMIN):
        jobs = PlatformJob.query.filter_by(school_id=_school_scope_id()).order_by(PlatformJob.created_at.desc()).limit(100).all()
    else:
        flash('You do not have access to job telemetry.', 'danger')
        return redirect(url_for('main.dashboard'))
    return render_template('jobs.html', jobs=jobs, global_mode=False)


@platform.route('/jobs/<int:job_id>/retry', methods=['POST'])
@login_required
def retry_platform_job(job_id):
    job = PlatformJob.query.filter_by(id=job_id).first_or_404()
    if current_user.has_role(User.ROLE_MASTER_ADMIN):
        pass
    elif current_user.has_role(User.ROLE_SCHOOL_ADMIN) and job.school_id == _school_scope_id():
        pass
    else:
        flash('You do not have permission to retry that job.', 'danger')
        return redirect(url_for('main.dashboard'))

    retried_job = enqueue_job(
        job.job_type,
        user=current_user,
        school_id=job.school_id,
        payload={'retry_of_job_id': job.id, 'original_payload': job.payload or {}},
        scheduled_for=utcnow(),
    )
    db.session.flush()
    add_audit_log('retry_job', 'platform_job', entity_id=job.id, details={'new_job_id': retried_job.id})
    if not _commit_or_rollback(f"Retry job job_id={job.id} retried_job_id={retried_job.id}"):
        flash('The job could not be retried right now.', 'danger')
    else:
        flash('Job re-queued successfully.', 'success')
    return redirect(url_for('platform.jobs_dashboard'))


@platform.route('/exports/users/<int:user_id>')
@login_required
def export_user_data(user_id):
    target_user = User.query.execution_options(include_deleted=True).filter_by(id=user_id).first_or_404()

    allowed = current_user.id == target_user.id
    allowed = allowed or current_user.has_role(User.ROLE_MASTER_ADMIN)
    allowed = allowed or (
        current_user.has_role(User.ROLE_SCHOOL_ADMIN)
        and target_user.school_scope_id == _school_scope_id()
    )
    if not allowed:
        flash('You do not have permission to export that user profile.', 'danger')
        return redirect(url_for('main.dashboard'))

    student = target_user.portal_student_detail
    payload = {
        'user': {
            'id': target_user.id,
            'username': target_user.username,
            'full_name': target_user.full_name,
            'email': target_user.email,
            'role': target_user.normalized_role,
            'school_id': target_user.school_id,
            'is_active': target_user.is_active,
            'is_locked': target_user.is_locked,
            'force_password_reset': target_user.force_password_reset,
            'ai_access_enabled': target_user.ai_access_enabled,
            'is_deleted': target_user.is_deleted,
            'created_at': target_user.created_at,
            'updated_at': target_user.updated_at,
        },
        'student': None,
        'attendance': [],
        'feedback': [],
        'notifications': [_serialize_notification(notification) for notification in target_user.notifications.order_by(Notification.created_at.desc()).limit(100).all()],
        'audit_logs': [
            {
                'action': log.action,
                'entity_type': log.entity_type,
                'entity_id': log.entity_id,
                'status': log.status,
                'created_at': log.created_at,
                'details': log.details,
            }
            for log in target_user.actor_audit_logs.order_by(AuditLog.created_at.desc()).limit(100).all()
        ],
    }
    if student is not None:
        payload['student'] = {
            'id': student.id,
            'full_name': student.full_name,
            'roll_no': student.roll_no,
            'grade': student.grade,
            'section': student.section,
            'allergies': student.allergies,
            'guardian_name': student.guardian_name,
            'guardian_email': student.guardian_email,
            'guardian_phone': student.guardian_phone,
            'status': student.status,
            'latest_height_cm': student.latest_height,
            'latest_weight_kg': student.latest_weight,
            'bmi': round(student.bmi, 1) if student.bmi else None,
        }
        payload['attendance'] = [
            {
                'attendance_date': record.attendance_date,
                'breakfast': record.ate_breakfast,
                'lunch': record.ate_lunch,
                'dinner': record.ate_dinner,
                'approval_status': record.approval_status,
            }
            for record in student.attendance_records.order_by(Attendance.attendance_date.desc()).all()
        ]
        payload['feedback'] = [
            {
                'subject': entry.subject,
                'message': entry.message,
                'status': entry.status,
                'created_at': entry.created_at,
            }
            for entry in student.feedback_entries.order_by(UserFeedback.created_at.desc()).all()
        ]

    add_audit_log('export', 'user_data', entity_id=user_id, details={'requested_by': current_user.id})
    _commit_or_rollback(f"Export user data audit user_id={user_id}")
    return json_response(f'user-{user_id}-export.json', payload)
