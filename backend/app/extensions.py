import logging
import os
import re
import threading
import time
from collections import defaultdict
from functools import wraps

from flask import abort, current_app, request
from flask_bcrypt import Bcrypt
from flask_migrate import Migrate
from flask_wtf import CSRFProtect

try:
    from flask_limiter import Limiter as FlaskLimiter
    from flask_limiter.util import get_remote_address
except ImportError:  # pragma: no cover - exercised when Flask-Limiter is absent locally
    FlaskLimiter = None
    get_remote_address = None


bcrypt = Bcrypt()
csrf = CSRFProtect()
migrate = Migrate()
logger = logging.getLogger(__name__)


class SimpleLimiter:
    """Fallback limiter used when Flask-Limiter is unavailable locally."""

    def __init__(self):
        self._app = None
        self._hits = defaultdict(list)
        self._lock = threading.Lock()

    def init_app(self, app):
        self._app = app
        with self._lock:
            self._hits.clear()
        app.extensions["simple_limiter"] = self

    @staticmethod
    def _client_identifier():
        forwarded_for = request.headers.get("X-Forwarded-For", "").strip()
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        return request.remote_addr or "anonymous"

    @staticmethod
    def _window_seconds(window_text):
        window_text = window_text.strip().lower()
        match = re.match(r"^(?:(\d+)\s*)?(second|seconds|minute|minutes|hour|hours|day|days)$", window_text)
        if not match:
            return 60

        quantity = int(match.group(1) or 1)
        unit = match.group(2).rstrip("s")
        base_seconds = {
            "second": 1,
            "minute": 60,
            "hour": 60 * 60,
            "day": 60 * 60 * 24,
        }[unit]
        return quantity * base_seconds

    @classmethod
    def _parse_limit(cls, limit_value):
        text = str(limit_value).strip().lower()
        match = re.match(r"^(?P<count>\d+)\s*(?:/|per\s+)?\s*(?P<window>.+)$", text)
        if not match:
            return 60, 60

        count = int(match.group("count"))
        window_seconds = cls._window_seconds(match.group("window"))
        return count, window_seconds

    def limit(self, limit_value, methods=None):
        limit_count, window_seconds = self._parse_limit(limit_value)
        allowed_methods = {method.upper() for method in methods} if methods else None

        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                if allowed_methods is not None and request.method.upper() not in allowed_methods:
                    return func(*args, **kwargs)
                if current_app.config.get("RATELIMIT_ENABLED", True) is False or current_app.testing:
                    return func(*args, **kwargs)

                client_identifier = self._client_identifier()
                key = f"{request.endpoint or func.__name__}:{client_identifier}"
                now = time.monotonic()

                with self._lock:
                    recent_hits = [timestamp for timestamp in self._hits[key] if now - timestamp < window_seconds]
                    if len(recent_hits) >= limit_count:
                        abort(429)
                    recent_hits.append(now)
                    self._hits[key] = recent_hits

                return func(*args, **kwargs)

            return wrapper

        return decorator


def _create_limiter():
    storage_uri = os.environ.get("RATELIMIT_STORAGE_URL") or os.environ.get("REDIS_URL") or "memory://"
    if FlaskLimiter is None:
        logger.debug("Flask-Limiter is not installed; using the local fallback limiter.")
        return SimpleLimiter()

    return FlaskLimiter(key_func=get_remote_address, default_limits=[], storage_uri=storage_uri)


limiter = _create_limiter()
