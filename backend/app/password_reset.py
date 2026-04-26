import hashlib
import logging
import secrets
from datetime import timedelta

from flask import current_app

from .models import PasswordResetToken, db, utcnow


logger = logging.getLogger(__name__)


def _token_ttl_minutes():
    try:
        return int(current_app.config.get('PASSWORD_RESET_TOKEN_TTL_MINUTES', 30))
    except (TypeError, ValueError):
        return 30


def _hash_token(raw_token):
    return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()


def issue_password_reset_token(user, *, requested_ip=None):
    raw_token = secrets.token_urlsafe(32)
    token = PasswordResetToken(
        user_id=user.id,
        token_hash=_hash_token(raw_token),
        requested_ip=requested_ip,
        expires_at=utcnow() + timedelta(minutes=_token_ttl_minutes()),
    )
    db.session.add(token)
    return raw_token, token


def resolve_password_reset_token(raw_token):
    if not raw_token:
        return None

    token_hash = _hash_token(raw_token)
    token = PasswordResetToken.query.filter_by(token_hash=token_hash).first()
    if token is None or not token.is_active:
        return None
    return token


def consume_password_reset_token(raw_token):
    token = resolve_password_reset_token(raw_token)
    if token is None:
        return None

    token.consumed_at = utcnow()
    return token
