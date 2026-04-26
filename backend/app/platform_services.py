import math
import time
from dataclasses import dataclass
from datetime import date, timedelta

from flask import current_app
from sqlalchemy import case, func, or_
from sqlalchemy.orm import aliased

from .ai_usage import ALL_FEATURES, daily_limit_for
from .models import (
    AIAccessPolicy,
    AIUsageLog,
    Attendance,
    AuditLog,
    MealPlan,
    MealPlanItem,
    Notification,
    PlatformJob,
    PlatformSetting,
    StudentDetail,
    User,
    db,
)


_CACHE = {}
_CACHE_TTL_SECONDS = 60


@dataclass
class PageResult:
    items: list
    page: int
    per_page: int
    total: int

    @property
    def pages(self):
        return max(1, math.ceil(self.total / self.per_page)) if self.per_page else 1

    @property
    def has_prev(self):
        return self.page > 1

    @property
    def has_next(self):
        return self.page < self.pages

    @property
    def prev_num(self):
        return self.page - 1 if self.has_prev else None

    @property
    def next_num(self):
        return self.page + 1 if self.has_next else None


def paginate_query(query, *, page=1, per_page=20):
    page = max(page or 1, 1)
    per_page = max(1, min(per_page or 20, 100))
    total = query.order_by(None).count()
    items = query.limit(per_page).offset((page - 1) * per_page).all()
    return PageResult(items=items, page=page, per_page=per_page, total=total)


def _cache_key(*parts):
    return "|".join(str(part) for part in parts)


def cached_value(key, builder, *, ttl=_CACHE_TTL_SECONDS):
    now = time.time()
    hit = _CACHE.get(key)
    if hit is not None and hit["expires_at"] > now:
        return hit["value"]

    value = builder()
    _CACHE[key] = {"value": value, "expires_at": now + ttl}
    return value


def invalidate_platform_cache(prefix=None):
    if prefix is None:
        _CACHE.clear()
        return

    matching_keys = [key for key in _CACHE if key.startswith(prefix)]
    for key in matching_keys:
        _CACHE.pop(key, None)


def school_roots_query(*, include_deleted=False):
    query = User.query.filter(
        User.school_id.is_(None),
        User.role.in_(["school", User.ROLE_SCHOOL_ADMIN]),
    )
    if include_deleted:
        query = query.execution_options(include_deleted=True)
    return query


def school_scope_case():
    return case((User.school_id.is_(None), User.id), else_=User.school_id)


def count_active_master_admins():
    return User.query.execution_options(include_deleted=True).filter(
        User.role.in_(["admin", User.ROLE_MASTER_ADMIN]),
        User.is_deleted.is_(False),
        User.is_active.is_(True),
    ).count()


def dashboard_summary():
    def _build():
        today = date.today()
        jobs_by_status = {
            status: count
            for status, count in db.session.query(
                PlatformJob.status,
                func.count(PlatformJob.id),
            ).group_by(PlatformJob.status).all()
        }
        ai_daily = {
            feature: {"requests": count, "units": units or 0}
            for feature, count, units in db.session.query(
                AIUsageLog.feature,
                func.count(AIUsageLog.id),
                func.coalesce(func.sum(AIUsageLog.request_units), 0),
            ).filter(
                func.date(AIUsageLog.created_at) == today
            ).group_by(AIUsageLog.feature).all()
        }
        ai_total = {
            feature: {"requests": count, "units": units or 0}
            for feature, count, units in db.session.query(
                AIUsageLog.feature,
                func.count(AIUsageLog.id),
                func.coalesce(func.sum(AIUsageLog.request_units), 0),
            ).group_by(AIUsageLog.feature).all()
        }
        failed_jobs = PlatformJob.query.filter_by(status="failed").order_by(PlatformJob.updated_at.desc()).limit(5).all()
        failed_audits = AuditLog.query.filter_by(status="failed").order_by(AuditLog.created_at.desc()).limit(5).all()
        school_counts = db.session.query(
            func.count(User.id),
            func.coalesce(func.sum(case((User.is_active.is_(True), 1), else_=0)), 0),
            func.coalesce(func.sum(case((User.is_active.is_(False), 1), else_=0)), 0),
        ).filter(
            User.school_id.is_(None),
            User.role.in_(["school", User.ROLE_SCHOOL_ADMIN]),
        ).one()

        return {
            "total_schools": school_counts[0] or 0,
            "active_schools": school_counts[1] or 0,
            "inactive_schools": school_counts[2] or 0,
            "total_users": User.query.count(),
            "total_students": StudentDetail.query.count(),
            "recent_audit_logs": AuditLog.query.order_by(AuditLog.created_at.desc()).limit(12).all(),
            "system_alerts": failed_jobs + failed_audits,
            "ai_daily": ai_daily,
            "ai_total": ai_total,
            "jobs_by_status": jobs_by_status,
            "locked_user_count": User.query.execution_options(include_deleted=True).filter_by(is_locked=True).count(),
        }

    return cached_value(_cache_key("dashboard"), _build)


def list_schools(*, page=1, per_page=15, search=None, status="active"):
    student_counts = db.session.query(
        StudentDetail.school_id.label("school_id"),
        func.count(StudentDetail.id).label("student_count"),
    ).group_by(StudentDetail.school_id).subquery()

    staff_counts = db.session.query(
        school_scope_case().label("school_scope_id"),
        func.count(User.id).label("staff_count"),
    ).filter(
        User.role.in_(["school", User.ROLE_SCHOOL_ADMIN])
    ).group_by(school_scope_case()).subquery()

    ai_usage = db.session.query(
        AIUsageLog.school_id.label("school_id"),
        func.coalesce(func.sum(AIUsageLog.request_units), 0).label("ai_units"),
    ).group_by(AIUsageLog.school_id).subquery()

    recent_activity = db.session.query(
        AuditLog.school_id.label("school_id"),
        func.max(AuditLog.created_at).label("last_activity_at"),
    ).group_by(AuditLog.school_id).subquery()

    query = db.session.query(
        User,
        func.coalesce(student_counts.c.student_count, 0).label("student_count"),
        func.coalesce(staff_counts.c.staff_count, 0).label("staff_count"),
        func.coalesce(ai_usage.c.ai_units, 0).label("ai_units"),
        recent_activity.c.last_activity_at.label("last_activity_at"),
    ).execution_options(include_deleted=True).outerjoin(
        student_counts,
        student_counts.c.school_id == User.id,
    ).outerjoin(
        staff_counts,
        staff_counts.c.school_scope_id == User.id,
    ).outerjoin(
        ai_usage,
        ai_usage.c.school_id == User.id,
    ).outerjoin(
        recent_activity,
        recent_activity.c.school_id == User.id,
    ).filter(
        User.school_id.is_(None),
        User.role.in_(["school", User.ROLE_SCHOOL_ADMIN]),
    )

    if search:
        search_value = f"%{search.strip().lower()}%"
        query = query.filter(
            or_(
                func.lower(User.username).like(search_value),
                func.lower(User.school_name).like(search_value),
                func.lower(User.email).like(search_value),
            )
        )

    if status == "deleted":
        query = query.filter(User.is_deleted.is_(True))
    elif status == "inactive":
        query = query.filter(User.is_deleted.is_(False), User.is_active.is_(False))
    elif status == "all":
        pass
    else:
        query = query.filter(User.is_deleted.is_(False), User.is_active.is_(True))

    query = query.order_by(func.coalesce(User.school_name, User.username).asc())
    return paginate_query(query, page=page, per_page=per_page)


def school_dependency_summary(school_id):
    return {
        "users": User.query.execution_options(include_deleted=True).filter(
            or_(User.id == school_id, User.school_id == school_id)
        ).count(),
        "students": StudentDetail.query.execution_options(include_deleted=True).filter_by(school_id=school_id).count(),
        "meal_plans": MealPlan.query.execution_options(include_deleted=True).filter_by(school_id=school_id).count(),
        "notifications": Notification.query.filter_by(school_id=school_id).count(),
        "audit_logs": AuditLog.query.filter_by(school_id=school_id).count(),
    }


def get_school_detail(school_id):
    school = school_roots_query(include_deleted=True).filter_by(id=school_id).first()
    if school is None:
        return None

    staff_members = User.query.execution_options(include_deleted=True).filter(
        or_(User.id == school_id, User.school_id == school_id)
    ).order_by(User.created_at.desc()).limit(25).all()
    students = StudentDetail.query.execution_options(include_deleted=True).filter_by(
        school_id=school_id
    ).order_by(StudentDetail.created_at.desc()).limit(25).all()
    recent_logs = AuditLog.query.filter_by(school_id=school_id).order_by(AuditLog.created_at.desc()).limit(20).all()
    usage = db.session.query(
        AIUsageLog.feature,
        func.count(AIUsageLog.id),
        func.coalesce(func.sum(AIUsageLog.request_units), 0),
    ).filter_by(school_id=school_id).group_by(AIUsageLog.feature).all()

    return {
        "school": school,
        "staff_members": staff_members,
        "students": students,
        "recent_logs": recent_logs,
        "usage_rows": usage,
        "dependency_summary": school_dependency_summary(school_id),
        "open_jobs": PlatformJob.query.filter_by(school_id=school_id).order_by(PlatformJob.created_at.desc()).limit(20).all(),
    }


def list_users(*, page=1, per_page=20, search=None, role=None, school_id=None, status="active"):
    student_detail_alias = aliased(StudentDetail)
    guardian_student_alias = aliased(StudentDetail)
    query = User.query.execution_options(include_deleted=True).outerjoin(
        student_detail_alias,
        User.id == student_detail_alias.user_id,
    ).outerjoin(
        guardian_student_alias,
        User.primary_student_id == guardian_student_alias.id,
    )

    if search:
        search_value = f"%{search.strip().lower()}%"
        query = query.filter(
            or_(
                func.lower(User.username).like(search_value),
                func.lower(func.coalesce(User.full_name, "")).like(search_value),
                func.lower(func.coalesce(User.email, "")).like(search_value),
            )
        )

    if role:
        if role == "legacy_school":
            query = query.filter(User.role == "school")
        elif role in {"master_admin", "school_admin", "user"}:
            alias_map = {
                "master_admin": ["admin", User.ROLE_MASTER_ADMIN],
                "school_admin": ["school", User.ROLE_SCHOOL_ADMIN],
                "user": ["student", User.ROLE_USER],
            }
            query = query.filter(User.role.in_(alias_map[role]))

    if school_id:
        query = query.filter(
            or_(
                User.id == school_id,
                User.school_id == school_id,
                student_detail_alias.school_id == school_id,
                guardian_student_alias.school_id == school_id,
            )
        )

    if status == "deleted":
        query = query.filter(User.is_deleted.is_(True))
    elif status == "inactive":
        query = query.filter(User.is_deleted.is_(False), User.is_active.is_(False))
    elif status == "locked":
        query = query.filter(User.is_deleted.is_(False), User.is_locked.is_(True))
    elif status == "all":
        pass
    else:
        query = query.filter(User.is_deleted.is_(False), User.is_active.is_(True), User.is_locked.is_(False))

    query = query.order_by(User.created_at.desc(), User.id.desc())
    return paginate_query(query.distinct(), page=page, per_page=per_page)


def get_user_detail(user_id):
    return User.query.execution_options(include_deleted=True).filter_by(id=user_id).first()


def attendance_trend_data(*, days=14):
    start_date = date.today() - timedelta(days=max(1, days) - 1)
    rows = db.session.query(
        Attendance.attendance_date,
        func.count(Attendance.id).label("total_rows"),
        func.coalesce(
            func.sum(
                case(
                    (
                        or_(
                            Attendance.ate_breakfast.is_(True),
                            Attendance.ate_lunch.is_(True),
                            Attendance.ate_dinner.is_(True),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label("present_rows"),
    ).filter(
        Attendance.attendance_date >= start_date
    ).group_by(
        Attendance.attendance_date
    ).order_by(
        Attendance.attendance_date.asc()
    ).all()
    return [
        {
            "date": target_date.isoformat(),
            "present": present_rows,
            "attendance_rows": total_rows,
        }
        for target_date, total_rows, present_rows in rows
    ]


def meal_usage_trend_data(*, days=14):
    start_date = date.today() - timedelta(days=max(1, days) - 1)
    rows = db.session.query(
        MealPlan.plan_date,
        MealPlanItem.meal_type,
        func.count(MealPlanItem.id),
    ).join(
        MealPlanItem,
        MealPlanItem.meal_plan_id == MealPlan.id,
    ).filter(
        MealPlan.plan_date >= start_date
    ).group_by(
        MealPlan.plan_date,
        MealPlanItem.meal_type,
    ).order_by(
        MealPlan.plan_date.asc(),
        MealPlanItem.meal_type.asc(),
    ).all()
    return [
        {
            "date": plan_date.isoformat(),
            "meal_type": meal_type,
            "items": count,
        }
        for plan_date, meal_type, count in rows
    ]


def school_comparison_data():
    student_counts = db.session.query(
        StudentDetail.school_id.label("school_id"),
        func.count(StudentDetail.id).label("student_count"),
    ).group_by(StudentDetail.school_id).subquery()
    meal_counts = db.session.query(
        MealPlan.school_id.label("school_id"),
        func.count(MealPlan.id).label("meal_plan_count"),
    ).group_by(MealPlan.school_id).subquery()
    attendance_counts = db.session.query(
        StudentDetail.school_id.label("school_id"),
        func.count(Attendance.id).label("attendance_row_count"),
    ).join(
        Attendance,
        Attendance.student_id == StudentDetail.id,
    ).group_by(StudentDetail.school_id).subquery()
    rows = db.session.query(
        User.id,
        User.school_name,
        User.username,
        func.coalesce(student_counts.c.student_count, 0),
        func.coalesce(meal_counts.c.meal_plan_count, 0),
        func.coalesce(attendance_counts.c.attendance_row_count, 0),
    ).outerjoin(
        student_counts,
        student_counts.c.school_id == User.id,
    ).outerjoin(
        meal_counts,
        meal_counts.c.school_id == User.id,
    ).outerjoin(
        attendance_counts,
        attendance_counts.c.school_id == User.id,
    ).filter(
        User.school_id.is_(None),
        User.role.in_(["school", User.ROLE_SCHOOL_ADMIN]),
        User.is_deleted.is_(False),
    ).order_by(func.coalesce(student_counts.c.student_count, 0).desc()).all()
    return [
        {
            "school_id": school_id,
            "school_name": school_name or username,
            "students": student_count,
            "meal_plans": meal_plan_count,
            "attendance_rows": attendance_row_count,
        }
        for school_id, school_name, username, student_count, meal_plan_count, attendance_row_count in rows
    ]


def user_growth_data(*, days=30):
    start_date = date.today() - timedelta(days=max(1, days) - 1)
    rows = db.session.query(
        func.date(User.created_at),
        func.count(User.id),
    ).filter(
        func.date(User.created_at) >= start_date
    ).group_by(
        func.date(User.created_at)
    ).order_by(
        func.date(User.created_at).asc()
    ).all()
    return [{"date": str(created_date), "users": count} for created_date, count in rows]


def analytics_payload(*, days=14):
    key = _cache_key("analytics", days)
    return cached_value(
        key,
        lambda: {
            "attendance_trends": attendance_trend_data(days=days),
            "meal_usage": meal_usage_trend_data(days=days),
            "school_comparisons": school_comparison_data(),
            "user_growth": user_growth_data(days=max(days, 30)),
        },
    )


def get_platform_setting(key, default=None):
    setting = PlatformSetting.query.filter_by(key=key).first()
    return default if setting is None else setting.value


def set_platform_setting(key, value, *, description=None, actor=None):
    setting = PlatformSetting.query.filter_by(key=key).first()
    if setting is None:
        setting = PlatformSetting(key=key)
        db.session.add(setting)
    setting.value = value
    setting.description = description
    setting.updated_by_user_id = getattr(actor, "id", None)
    invalidate_platform_cache("dashboard")
    invalidate_platform_cache("analytics")
    return setting


def ai_global_limits():
    limits = {}
    for feature in ALL_FEATURES:
        limits[feature] = get_platform_setting(
            f"ai.daily_limit.{feature}",
            daily_limit_for(feature, current_app.config),
        )
    return limits


def ai_policy_rows(*, school_id=None, user_id=None):
    query = AIAccessPolicy.query
    if school_id is not None:
        query = query.filter_by(school_id=school_id)
    if user_id is not None:
        query = query.filter_by(user_id=user_id)
    return query.order_by(AIAccessPolicy.updated_at.desc(), AIAccessPolicy.id.desc()).all()


def upsert_ai_policy(*, feature, school_id=None, user_id=None, daily_limit=None, is_enabled=True, notes=None, actor=None):
    policy = AIAccessPolicy.query.filter_by(
        school_id=school_id,
        user_id=user_id,
        feature=feature,
    ).order_by(AIAccessPolicy.id.desc()).first()
    if policy is None:
        policy = AIAccessPolicy(
            school_id=school_id,
            user_id=user_id,
            feature=feature,
            created_by_user_id=getattr(actor, "id", None),
        )
        db.session.add(policy)
    policy.daily_limit = daily_limit
    policy.is_enabled = is_enabled
    policy.notes = notes
    invalidate_platform_cache("dashboard")
    return policy


def ai_usage_by_school(*, limit=25):
    rows = db.session.query(
        AIUsageLog.school_id,
        User.school_name,
        User.username,
        func.count(AIUsageLog.id),
        func.coalesce(func.sum(AIUsageLog.request_units), 0),
    ).join(
        User,
        User.id == AIUsageLog.school_id,
    ).group_by(
        AIUsageLog.school_id,
        User.school_name,
        User.username,
    ).order_by(
        func.coalesce(func.sum(AIUsageLog.request_units), 0).desc()
    ).limit(limit).all()
    return [
        {
            "school_id": school_id,
            "school_name": school_name or username,
            "requests": requests,
            "units": units,
        }
        for school_id, school_name, username, requests, units in rows
    ]


def ai_usage_by_user(*, limit=25):
    rows = db.session.query(
        AIUsageLog.user_id,
        User.username,
        User.full_name,
        func.count(AIUsageLog.id),
        func.coalesce(func.sum(AIUsageLog.request_units), 0),
    ).join(
        User,
        User.id == AIUsageLog.user_id,
    ).group_by(
        AIUsageLog.user_id,
        User.username,
        User.full_name,
    ).order_by(
        func.coalesce(func.sum(AIUsageLog.request_units), 0).desc()
    ).limit(limit).all()
    return [
        {
            "user_id": user_id,
            "username": username,
            "full_name": full_name,
            "requests": requests,
            "units": units,
        }
        for user_id, username, full_name, requests, units in rows
    ]


def list_notifications(*, page=1, per_page=20, category=None, school_id=None, user_id=None):
    query = Notification.query
    if category:
        query = query.filter_by(category=category)
    if school_id:
        query = query.filter_by(school_id=school_id)
    if user_id:
        query = query.filter_by(user_id=user_id)
    query = query.order_by(Notification.created_at.desc())
    return paginate_query(query, page=page, per_page=per_page)


def list_audit_logs(*, page=1, per_page=25, actor_user_id=None, action=None, date_from=None, date_to=None):
    query = AuditLog.query
    if actor_user_id:
        query = query.filter_by(actor_user_id=actor_user_id)
    if action:
        query = query.filter(AuditLog.action == action)
    if date_from:
        query = query.filter(AuditLog.created_at >= date_from)
    if date_to:
        query = query.filter(AuditLog.created_at <= date_to)
    query = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    return paginate_query(query, page=page, per_page=per_page)


def list_jobs(*, page=1, per_page=25, status=None):
    query = PlatformJob.query
    if status:
        query = query.filter_by(status=status)
    query = query.order_by(PlatformJob.created_at.desc(), PlatformJob.id.desc())
    return paginate_query(query, page=page, per_page=per_page)
