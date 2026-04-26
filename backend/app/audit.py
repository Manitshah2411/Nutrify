import logging

from flask import request
from flask_login import current_user

from .models import AuditLog, db


logger = logging.getLogger(__name__)


def add_audit_log(action, entity_type, *, entity_id=None, school_id=None, actor_user=None, status='success', details=None):
    actor = actor_user or (current_user if getattr(current_user, 'is_authenticated', False) else None)
    resolved_school_id = school_id or getattr(actor, 'school_scope_id', None)
    audit_log = AuditLog(
        school_id=resolved_school_id,
        actor_user_id=getattr(actor, 'id', None),
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        status=status,
        ip_address=request.headers.get('X-Forwarded-For', request.remote_addr),
        details=details or {},
    )
    db.session.add(audit_log)
    return audit_log


def safe_audit(action, entity_type, **kwargs):
    try:
        add_audit_log(action, entity_type, **kwargs)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Failed to persist audit log for action=%s entity_type=%s", action, entity_type)
