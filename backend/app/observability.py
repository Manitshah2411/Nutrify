import logging


logger = logging.getLogger(__name__)


def init_error_tracking(app):
    dsn = (app.config.get("ERROR_TRACKING_DSN") or "").strip()
    if not dsn:
        app.logger.info("Error tracking DSN not configured; using structured logs only.")
        return

    app.logger.info("Error tracking hook configured for DSN=%s", dsn)


def capture_exception(error, *, context=None):
    logger.exception("Captured application exception with context=%s: %s", context or {}, error)
