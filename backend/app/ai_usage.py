import logging
from datetime import date

from sqlalchemy import func

from .models import AIUsageLog, db


logger = logging.getLogger(__name__)


DEFAULT_DAILY_LIMITS = {
    'nutrition_lookup': 100,
    'recipe_lookup': 60,
    'meal_generator': 30,
    'health_insights': 30,
}


def estimate_request_units(*parts):
    total_chars = sum(len(part or '') for part in parts)
    return max(1, total_chars // 50)


def daily_limit_for(feature, app_config):
    config_key = f"AI_DAILY_LIMIT_{feature.upper()}"
    try:
        return int(app_config.get(config_key, DEFAULT_DAILY_LIMITS.get(feature, 100)))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_LIMITS.get(feature, 100)


def usage_count_for_today(user_id, feature):
    today = date.today()
    return db.session.query(func.count(AIUsageLog.id)).filter(
        AIUsageLog.user_id == user_id,
        AIUsageLog.feature == feature,
        func.date(AIUsageLog.created_at) == today,
    ).scalar() or 0


def check_ai_quota(user, feature, app_config):
    if user is None:
        return True

    limit = daily_limit_for(feature, app_config)
    used = usage_count_for_today(user.id, feature)
    allowed = used < limit
    if not allowed:
        logger.warning("AI quota exceeded for user_id=%s feature=%s used=%s limit=%s", user.id, feature, used, limit)
    return allowed


def add_ai_usage_log(user, feature, *, status, request_units=0, latency_ms=None, details=None):
    entry = AIUsageLog(
        school_id=getattr(user, 'school_scope_id', None) if user else None,
        user_id=getattr(user, 'id', None) if user else None,
        feature=feature,
        status=status,
        request_units=request_units,
        latency_ms=latency_ms,
        details=details or {},
    )
    db.session.add(entry)
    return entry
