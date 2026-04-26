import logging
from functools import wraps

from flask import flash, jsonify, redirect, request, session, url_for
from flask_login import current_user, login_required, logout_user

from .models import User


logger = logging.getLogger(__name__)


ROLE_MASTER_ADMIN = User.ROLE_MASTER_ADMIN
ROLE_SCHOOL_ADMIN = User.ROLE_SCHOOL_ADMIN
ROLE_USER = User.ROLE_USER


def wants_json_response():
    accepted = request.accept_mimetypes
    return accepted.best_match(['application/json', 'text/html']) == 'application/json' and accepted['application/json'] >= accepted['text/html']


def normalize_role(role):
    return User.LEGACY_ROLE_ALIASES.get((role or '').strip().lower(), (role or '').strip().lower())


def establish_session(user):
    session.permanent = True
    session['session_version'] = getattr(user, 'session_version', 1)


def _access_denied(message='Unauthorized access.'):
    if wants_json_response():
        return jsonify({'error': message}), 403

    flash(message, 'danger')
    return redirect(url_for('main.dashboard' if current_user.is_authenticated else 'main.login'))


def roles_required(*roles):
    normalized_roles = {normalize_role(role) for role in roles}

    def decorator(view):
        @login_required
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            if not current_user.is_authenticated:
                return _access_denied()
            if not current_user.has_role(*normalized_roles):
                logger.warning(
                    "User %s with role=%s was denied access to %s requiring roles=%s",
                    getattr(current_user, 'id', None),
                    getattr(current_user, 'role', None),
                    request.path,
                    ",".join(sorted(normalized_roles)),
                )
                return _access_denied()
            return view(*args, **kwargs)

        return wrapped_view

    return decorator


def school_admin_required(view):
    return roles_required(ROLE_SCHOOL_ADMIN, ROLE_MASTER_ADMIN)(view)


def platform_admin_required(view):
    return roles_required(ROLE_MASTER_ADMIN)(view)


def user_portal_required(view):
    return roles_required(ROLE_USER)(view)


def register_security_hooks(app):
    @app.before_request
    def _validate_current_session():
        if not current_user.is_authenticated:
            return None

        if getattr(current_user, 'is_deleted', False):
            logger.warning("Soft-deleted user %s attempted to use an active session.", current_user.id)
            logout_user()
            session.clear()
            flash('Your account is no longer active. Please contact support.', 'danger')
            return redirect(url_for('main.login'))

        session_version = session.get('session_version')
        current_version = getattr(current_user, 'session_version', 1)
        if session_version is not None and session_version != current_version:
            logger.info("Session version mismatch for user %s. Logging out stale session.", current_user.id)
            logout_user()
            session.clear()
            flash('Your session expired because account security settings changed. Please sign in again.', 'warning')
            return redirect(url_for('main.login'))

        return None
