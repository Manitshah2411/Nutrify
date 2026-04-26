import logging
from datetime import date

from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError

from .models import AIAccessPolicy, AIUsageLog, PlatformSetting, db


logger = logging.getLogger(__name__)


DEFAULT_DAILY_LIMITS = {
    'nutrition_lookup': 100,
    'recipe_lookup': 60,
    'meal_generator': 30,
    'health_insights': 30,
}
ALL_FEATURES = tuple(DEFAULT_DAILY_LIMITS.keys())
GLOBAL_POLICY_SCOPE = "global"


def _platform_setting_value(key):
    try:
        setting = PlatformSetting.query.filter_by(key=key).first()
    except SQLAlchemyError:
        db.session.rollback()
        logger.warning("Unable to resolve platform setting %s; falling back to app config.", key, exc_info=True)
        return None
    return None if setting is None else setting.value


def _policy_query(feature, *, school_id=None, user_id=None):
    query = AIAccessPolicy.query.filter_by(feature=feature)
    if school_id is None:
        query = query.filter(AIAccessPolicy.school_id.is_(None))
    else:
        query = query.filter_by(school_id=school_id)
    if user_id is None:
        query = query.filter(AIAccessPolicy.user_id.is_(None))
    else:
        query = query.filter_by(user_id=user_id)
    return query.order_by(AIAccessPolicy.created_at.desc())


def _resolve_policy(user, feature):
    school_id = getattr(user, 'school_scope_id', None) if user else None
    user_id = getattr(user, 'id', None) if user else None
    candidates = (
        _policy_query(feature, school_id=school_id, user_id=user_id).first() if user_id else None,
        _policy_query(feature, school_id=school_id, user_id=None).first() if school_id else None,
        _policy_query(feature, school_id=None, user_id=None).first(),
        _policy_query('*', school_id=school_id, user_id=user_id).first() if user_id else None,
        _policy_query('*', school_id=school_id, user_id=None).first() if school_id else None,
        _policy_query('*', school_id=None, user_id=None).first(),
    )
    for policy in candidates:
        if policy is not None:
            return policy
    return None


def estimate_request_units(*parts):
    total_chars = sum(len(part or '') for part in parts)
    return max(1, total_chars // 50)


def daily_limit_for(feature, app_config):
    setting_value = _platform_setting_value(f'ai.daily_limit.{feature}')
    if isinstance(setting_value, int):
        return setting_value
    if isinstance(setting_value, str):
        try:
            return int(setting_value)
        except ValueError:
            logger.warning("Platform setting ai.daily_limit.%s=%r is not an integer.", feature, setting_value)

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
    if not getattr(user, 'ai_access_enabled', True):
        logger.warning("AI access disabled at user level for user_id=%s feature=%s", user.id, feature)
        return False

    try:
        policy = _resolve_policy(user, feature)
    except SQLAlchemyError:
        db.session.rollback()
        logger.warning("Unable to resolve AI policy for user_id=%s feature=%s; using fallback limits.", user.id, feature, exc_info=True)
        policy = None

    if policy is not None and not policy.is_enabled:
        logger.warning("AI policy disabled access for user_id=%s feature=%s policy_id=%s", user.id, feature, policy.id)
        return False

    limit = policy.daily_limit if policy is not None and policy.daily_limit is not None else daily_limit_for(feature, app_config)
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
