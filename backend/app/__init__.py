import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from flask_login import LoginManager, current_user
from flask_wtf.csrf import CSRFError
from sqlalchemy.engine import make_url
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .bootstrap import bootstrap_database, register_bootstrap_commands
from .config import DevelopmentConfig, ProductionConfig, TestingConfig, sqlalchemy_engine_options
from .extensions import bcrypt, csrf, limiter, migrate
from .models import User, db
from .observability import capture_exception, init_error_tracking
from .security import register_security_hooks


logger = logging.getLogger(__name__)

# Create the LoginManager instance
login_manager = LoginManager()
login_manager.login_view = 'main.login'
login_manager.session_protection = 'strong'


@login_manager.user_loader
def load_user(user_id):
    """Load the current user from the database."""
    try:
        user = db.session.get(User, int(user_id))
        if user is None or getattr(user, 'is_deleted', False):
            return None
        return user
    except (TypeError, ValueError):
        return None


def _select_config_class(config_class=None):
    if config_class is not None:
        return config_class

    app_env = os.environ.get('APP_ENV', os.environ.get('FLASK_ENV', 'development')).lower()
    if app_env in {'production', 'prod'}:
        return ProductionConfig
    if app_env in {'testing', 'test'}:
        return TestingConfig
    return DevelopmentConfig


def _configure_cors(app):
    cors_origins = os.environ.get('CORS_ORIGINS', '').strip()
    if not cors_origins:
        return

    allowed_origins = [origin.strip() for origin in cors_origins.split(',') if origin.strip()]
    if allowed_origins:
        CORS(app, resources={r"/*": {"origins": allowed_origins}}, supports_credentials=True)


def _env_flag(name, default):
    return os.environ.get(name, default).lower() in {'1', 'true', 'yes', 'on'}


def _should_auto_bootstrap(app):
    if app.testing or not _env_flag('AUTO_BOOTSTRAP_DATABASE', '1'):
        return False

    cli_command = sys.argv[1] if len(sys.argv) > 1 else ''
    if cli_command in {'db', 'bootstrap-db'}:
        return False

    return True


def _validate_runtime_configuration(app):
    secret_key = (app.config.get("SECRET_KEY") or "").strip()
    if not secret_key:
        raise RuntimeError("SECRET_KEY is required.")

    database_uri = (app.config.get("SQLALCHEMY_DATABASE_URI") or "").strip()
    if not database_uri:
        raise RuntimeError("DATABASE_URL is required.")

    try:
        make_url(database_uri)
    except Exception as exc:
        raise RuntimeError("DATABASE_URL is invalid or unsupported.") from exc

    app_env = str(app.config.get("APP_ENV", "")).lower()
    if app_env in {"production", "prod"} and not app.testing and database_uri.startswith("sqlite"):
        raise RuntimeError("SQLite is not allowed in production. Set DATABASE_URL to PostgreSQL.")


def _configure_logging(app):
    log_level_name = os.environ.get('LOG_LEVEL', 'INFO').upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    formatter = logging.Formatter(
        fmt='%(asctime)s %(levelname)s %(name)s %(message)s',
    )

    handlers = [logging.StreamHandler()]
    log_file = os.environ.get('LOG_FILE', '').strip()
    if log_file:
        log_path = Path(log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3))

    for handler in handlers:
        handler.setFormatter(formatter)

    app.logger.handlers.clear()
    for handler in handlers:
        app.logger.addHandler(handler)
    app.logger.setLevel(log_level)
    app.logger.propagate = False

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    if not root_logger.handlers:
        for handler in handlers:
            root_logger.addHandler(handler)


def _wants_json_response():
    if request.path in {'/health'} or request.path.startswith('/get-') or request.path.startswith('/search-'):
        return True

    accepted = request.accept_mimetypes
    return accepted.best_match(['application/json', 'text/html']) == 'application/json' and accepted['application/json'] >= accepted['text/html']


def _render_error_response(status_code, title, message):
    if _wants_json_response():
        return jsonify({
            'error': title,
            'message': message,
            'status': status_code,
        }), status_code

    return render_template('error.html', error_code=status_code, error_title=title, error_message=message), status_code


def _register_error_handlers(app):
    @app.errorhandler(CSRFError)
    def handle_csrf_error(error):
        app.logger.warning("CSRF validation failed for %s %s: %s", request.method, request.path, error.description)
        return _render_error_response(
            400,
            'Security check failed',
            'Your session expired or the form was missing a security token. Please refresh and try again.',
        )

    @app.errorhandler(HTTPException)
    def handle_http_exception(error):
        message_map = {
            400: ('Bad request', 'The request could not be processed. Please check the submitted data and try again.'),
            403: ('Forbidden', 'You do not have permission to access this page.'),
            404: ('Page not found', 'The page you requested could not be found.'),
            405: ('Method not allowed', 'That action is not allowed for this route.'),
            429: ('Too many requests', 'You are sending requests too quickly. Please wait a moment and try again.'),
        }
        title, message = message_map.get(error.code, ('Request error', error.description or 'Something went wrong.'))
        return _render_error_response(error.code or 500, title, message)

    @app.errorhandler(Exception)
    def handle_unexpected_exception(error):
        if isinstance(error, HTTPException):
            return error

        db.session.rollback()
        app.logger.exception("Unhandled exception while processing %s %s", request.method, request.path)
        capture_exception(error, context={"path": request.path, "method": request.method})
        return _render_error_response(
            500,
            'Internal server error',
            'An unexpected error occurred while processing your request. Please try again later.',
        )


def _configure_security_headers(app):
    # The school dashboard and student tools currently use Alpine's default CDN
    # runtime with inline expressions. In production, that requires unsafe-eval
    # or the interactive controls become inert under CSP.
    csp = (
        "default-src 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "img-src 'self' data: https:; "
        "font-src 'self' https://fonts.gstatic.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.tailwindcss.com; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com https://cdn.jsdelivr.net; "
        "connect-src 'self' https://generativelanguage.googleapis.com https://www.googleapis.com;"
    )

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault('Content-Security-Policy', csp)
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        response.headers.setdefault('Permissions-Policy', 'geolocation=(), microphone=(), camera=()')
        if request.is_secure:
            response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
        return response


def create_app(config_class=None):
    """The application factory."""
    selected_config = _select_config_class(config_class)

    app = Flask(__name__)
    app.config.from_object(selected_config)
    _validate_runtime_configuration(app)
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = sqlalchemy_engine_options(app.config["SQLALCHEMY_DATABASE_URI"])

    if os.environ.get('ENABLE_PROXY_FIX', '1' if selected_config is ProductionConfig else '0').lower() in {'1', 'true', 'yes'}:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    _configure_logging(app)
    _configure_cors(app)

    db.init_app(app)
    bcrypt.init_app(app)
    csrf.init_app(app)
    migrate.init_app(
        app,
        db,
        compare_type=True,
        render_as_batch=app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite'),
    )
    limiter.init_app(app)
    login_manager.init_app(app)
    register_security_hooks(app)
    init_error_tracking(app)

    _configure_security_headers(app)
    _register_error_handlers(app)
    register_bootstrap_commands(app)

    @app.context_processor
    def inject_current_user():
        return {
            'current_user': current_user,
            'show_demo_credentials': app.config.get('SHOW_DEMO_CREDENTIALS', False),
            'default_school_username': app.config.get('DEFAULT_SCHOOL_USERNAME', 'BestSchool'),
        }

    # Register the blueprint that contains all our routes
    from .routes import main as main_blueprint
    from .platform_routes import platform as platform_blueprint

    app.register_blueprint(main_blueprint)
    app.register_blueprint(platform_blueprint)

    if _should_auto_bootstrap(app):
        with app.app_context():
            bootstrap_database()

    app.logger.info("Nutrify application initialized in %s mode", app.config.get('APP_ENV', 'development'))
    return app
