import logging

from sqlalchemy import or_

from .models import Notification, StudentDetail, User, db


logger = logging.getLogger(__name__)


def add_notification(user_id, title, message, *, category='info', link=None, school_id=None):
    notification = Notification(
        school_id=school_id,
        user_id=user_id,
        title=title,
        message=message,
        category=category,
        link=link,
    )
    db.session.add(notification)
    return notification


def school_portal_user_ids(school_scope_id):
    if not school_scope_id:
        return []

    direct_staff_ids = [
        user_id for (user_id,) in db.session.query(User.id).filter(
            or_(User.school_id == school_scope_id, User.id == school_scope_id)
        ).all()
    ]
    student_user_ids = [
        user_id for (user_id,) in db.session.query(User.id)
        .join(StudentDetail, StudentDetail.user_id == User.id)
        .filter(StudentDetail.school_id == school_scope_id)
        .all()
    ]
    guardian_user_ids = [
        user_id for (user_id,) in db.session.query(User.id)
        .join(StudentDetail, User.primary_student_id == StudentDetail.id)
        .filter(StudentDetail.school_id == school_scope_id)
        .all()
    ]
    return sorted(set(direct_staff_ids + student_user_ids + guardian_user_ids))


def broadcast_school_notification(school_scope_id, title, message, *, category='info', link=None):
    user_ids = school_portal_user_ids(school_scope_id)
    for user_id in user_ids:
        add_notification(user_id, title, message, category=category, link=link, school_id=school_scope_id)
    logger.info("Queued %s notifications for school_scope_id=%s", len(user_ids), school_scope_id)
    return len(user_ids)
