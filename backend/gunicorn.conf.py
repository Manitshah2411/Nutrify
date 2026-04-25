import math
import os


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_cpu_count(default):
    try:
        return max(1, math.ceil(float(os.environ.get("RENDER_CPU_COUNT", default))))
    except (TypeError, ValueError):
        return default


bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gthread")
workers = _env_int("WEB_CONCURRENCY", max(2, _env_cpu_count(1) * 2))
threads = _env_int("GUNICORN_THREADS", 4)
timeout = _env_int("GUNICORN_TIMEOUT", 120)
graceful_timeout = _env_int("GUNICORN_GRACEFUL_TIMEOUT", 30)
keepalive = _env_int("GUNICORN_KEEPALIVE", 5)
max_requests = _env_int("GUNICORN_MAX_REQUESTS", 1000)
max_requests_jitter = _env_int("GUNICORN_MAX_REQUESTS_JITTER", 100)
accesslog = "-"
errorlog = "-"
capture_output = True
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "*")
