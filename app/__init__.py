"""Flask application factory."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from babel.dates import format_date, format_datetime
from babel.numbers import format_decimal
from flask import Flask, jsonify, redirect, render_template, request, url_for
from flask_babel import get_locale, gettext, ngettext
from flask_login import current_user
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import get_config
from app.extensions import babel, db, login_manager, migrate
from app.utils.http import safe_next_url, wants_json_response
from app.utils.localization import (
    LOCALE_LABELS,
    LOCALE_SHORT_LABELS,
    SUPPORTED_LOCALES,
    select_locale,
)

__version__ = "1.0.0"

_DISPLAY_TIMEZONE = ZoneInfo("Europe/Podgorica")

_SECURITY_CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "object-src 'none'; "
    "script-src 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "font-src 'self' https://cdn.jsdelivr.net data:; "
    "img-src 'self' data:; "
    "connect-src 'self'"
)


def _register_extensions(app: Flask) -> None:
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    babel.init_app(app, locale_selector=select_locale)


def _register_login(app: Flask) -> None:
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "info"
    login_manager.session_protection = "strong"

    @login_manager.unauthorized_handler
    def unauthorized():
        if wants_json_response():
            return (
                jsonify(
                    {
                        "error": "unauthorized",
                        "message": "Authentication is required for this resource.",
                    }
                ),
                401,
            )
        target = safe_next_url(request.full_path, url_for("main.index"))
        return redirect(url_for("auth.login", next=target))


def _register_blueprints(app: Flask) -> None:
    from app.blueprints.api.routes import api_bp
    from app.blueprints.auth.routes import auth_bp
    from app.blueprints.converter.routes import converter_bp
    from app.blueprints.dashboard.routes import dashboard_bp
    from app.blueprints.main.routes import main_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(converter_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(dashboard_bp, url_prefix="/dashboard")
    app.register_blueprint(api_bp, url_prefix="/api/v1")


def _format_locale() -> str:
    """Use Latin script for date/number formatting in the Serbian locale."""
    code = str(select_locale())
    return "sr_Latn" if code == "sr" else code


def _register_template_helpers(app: Flask) -> None:
    @app.template_filter("babel_datetime")
    def babel_datetime_filter(value, fmt="medium"):
        if not value:
            return "—"
        try:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            local = value.astimezone(_DISPLAY_TIMEZONE)
            return format_datetime(local, format=fmt, locale=_format_locale())
        except Exception:
            return str(value)

    @app.template_filter("babel_date")
    def babel_date_filter(value, fmt="medium"):
        if not value:
            return "—"
        try:
            return format_date(value, format=fmt, locale=_format_locale())
        except Exception:
            return str(value)

    @app.template_filter("decimal")
    def decimal_filter(value, fmt="#,##0.00"):
        if value is None or value == "":
            return "—"
        try:
            return format_decimal(value, format=fmt, locale=_format_locale())
        except Exception:
            return str(value)

    @app.template_filter("initials")
    def initials_filter(value):
        text = str(value or "U").strip()
        return text[:1].upper() if text else "U"

    @app.context_processor
    def inject_globals():
        return {
            "app_name": app.config["APP_NAME"],
            "app_version": __version__,
            "current_year": datetime.now(_DISPLAY_TIMEZONE).year,
            "supported_locales": SUPPORTED_LOCALES,
            "locale_labels": LOCALE_LABELS,
            "locale_short_labels": LOCALE_SHORT_LABELS,
            "support_email": app.config.get("SUPPORT_EMAIL"),
            "sales_email": app.config.get("SALES_EMAIL"),
            "max_upload_mb": app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024),
        }


def _register_cleanup_hook(app: Flask) -> None:
    @app.before_request
    def cleanup_expired_conversions():
        if request.endpoint in {"static"}:
            return
        interval = int(app.config.get("CLEANUP_INTERVAL_SECONDS", 300))
        now = time.monotonic()
        last_run = app.extensions.get("last_cleanup_at", 0.0)
        if now - last_run < interval:
            return
        app.extensions["last_cleanup_at"] = now
        try:
            from app.services.cleanup import cleanup_expired

            cleanup_expired()
        except Exception:  # never let housekeeping break a request
            app.logger.warning("Background cleanup failed", exc_info=True)
            db.session.rollback()


def _register_error_handlers(app: Flask) -> None:
    from app.services.rate_limiter import QuotaExceeded

    @app.errorhandler(QuotaExceeded)
    def handle_quota_exceeded(error: QuotaExceeded):
        state = error.state
        if wants_json_response():
            response = jsonify(
                {
                    "error": "quota_exceeded",
                    "message": (
                        "Your conversion quota has been reached. "
                        "Create a free account for 5 pages per day or upgrade for more."
                    ),
                    "quota": state.to_dict(),
                }
            )
            response.status_code = 429
            response.headers["Retry-After"] = str(state.retry_after)
            return response
        return (
            render_template(
                "errors/429.html",
                quota=state,
                error_message=(
                    "Your conversion quota has been reached. "
                    "Create a free account for 5 pages per day or upgrade for more."
                ),
            ),
            429,
        )

    @app.errorhandler(400)
    def handle_bad_request(error):
        if wants_json_response():
            return jsonify({"error": "bad_request", "message": "Invalid request."}), 400
        return render_template("errors/400.html"), 400

    @app.errorhandler(404)
    def handle_not_found(error):
        if wants_json_response():
            return jsonify({"error": "not_found", "message": "Resource not found."}), 404
        return render_template("errors/404.html"), 404

    @app.errorhandler(405)
    def handle_method_not_allowed(error):
        if wants_json_response():
            return (
                jsonify(
                    {"error": "method_not_allowed", "message": "Method not allowed."}
                ),
                405,
            )
        return render_template("errors/404.html"), 405

    @app.errorhandler(413)
    def handle_file_too_large(error):
        if wants_json_response():
            return (
                jsonify(
                    {
                        "error": "file_too_large",
                        "message": "The PDF is larger than the 25 MB limit.",
                    }
                ),
                413,
            )
        return render_template("errors/413.html"), 413

    @app.errorhandler(429)
    def handle_too_many_requests(error):
        if wants_json_response():
            return (
                jsonify(
                    {
                        "error": "rate_limited",
                        "message": "Too many requests. Please try again later.",
                    }
                ),
                429,
            )
        return render_template("errors/429.html", quota=None), 429

    @app.errorhandler(500)
    def handle_server_error(error):
        db.session.rollback()
        app.logger.exception("Unhandled application error")
        if wants_json_response():
            return (
                jsonify(
                    {
                        "error": "server_error",
                        "message": "An unexpected error occurred. Please try again.",
                    }
                ),
                500,
            )
        return render_template("errors/500.html"), 500


def _register_security_headers(app: Flask) -> None:
    @app.after_request
    def add_security_headers(response):
        if not app.config.get("SECURITY_HEADERS", True):
            return response
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", _SECURITY_CSP)
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
            response.headers["Content-Security-Policy"] = (
                _SECURITY_CSP + "; upgrade-insecure-requests"
            )
        if request.path.startswith(("/dashboard", "/auth")):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


def _register_cli(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db_command():
        """Create database tables without running migrations."""
        db.create_all()
        app.logger.info("Database tables created")

    @app.cli.command("cleanup-now")
    def cleanup_now_command():
        """Run the retention sweep immediately."""
        from app.services.cleanup import cleanup_expired

        result = cleanup_expired()
        print(f"Cleanup complete: {result}")


def create_app(config_name: str | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    config_class = get_config(config_name)
    app.config.from_object(config_class)

    # Production guardrails are applied after from_object (classes are not
    # instantiated by Flask).
    resolved_name = (config_name or app.config.get("ENV") or "").lower()
    if resolved_name in {"production", "prod"} or config_class.__name__ == "ProductionConfig":
        database_url = app.config.get("SQLALCHEMY_DATABASE_URI") or ""
        if not database_url.startswith(("postgresql://", "postgresql+psycopg2://")):
            raise RuntimeError(
                "DATABASE_URL must point to PostgreSQL in production."
            )
        secret = app.config.get("SECRET_KEY")
        if not secret or secret == "dev-only-change-me-in-production":
            raise RuntimeError("A strong SECRET_KEY is required in production.")

    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True
    app.config["APP_VERSION"] = __version__
    app.jinja_env.globals["_"] = gettext
    app.jinja_env.globals["gettext"] = gettext
    app.jinja_env.globals["ngettext"] = ngettext
    app.jinja_env.globals["get_locale"] = get_locale

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    if app.config.get("TRUST_PROXY_HEADERS"):
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1
        )

    _register_extensions(app)
    _register_login(app)
    _register_blueprints(app)
    _register_template_helpers(app)
    _register_error_handlers(app)
    _register_cleanup_hook(app)
    _register_security_headers(app)
    _register_cli(app)

    database_uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if database_uri.startswith("sqlite") and not app.config.get("TESTING"):
        app.logger.warning(
            "Using SQLite at %s. Set DATABASE_URL to PostgreSQL for production.",
            database_uri,
        )

    return app
