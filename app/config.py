"""Application configuration objects.

The production app is PostgreSQL-first.  SQLite is available for tests and for
zero-configuration local development, but production refuses to start without
an explicit PostgreSQL ``DATABASE_URL``.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.pool import StaticPool

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _normalise_database_url(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    # SQLAlchemy 2.1 prefers the psycopg v3 dialect for bare postgresql://
    # URLs. This project explicitly ships psycopg2-binary, so pin the dialect
    # even when the operator supplies the conventional DATABASE_URL form.
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://") :]
    return url


def _sqlite_uri(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


class BaseConfig:
    """Configuration shared by every environment."""

    # Core
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me-in-production")
    APP_NAME = "Bank Statement Converter"
    SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "support@bankstatementconverter.local")
    SALES_EMAIL = os.getenv("SALES_EMAIL", "sales@bankstatementconverter.local")

    # Uploads / retention
    MAX_CONTENT_LENGTH = (
        int(os.getenv("MAX_CONTENT_LENGTH_MB", "25")) * 1024 * 1024
    )  # 25 MB default
    UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", str(BASE_DIR / "instance" / "uploads"))
    CONVERSION_RETENTION_SECONDS = int(os.getenv("CONVERSION_RETENTION_SECONDS", "3600"))
    MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "500"))
    MAX_ROWS_PER_STATEMENT = int(os.getenv("MAX_ROWS_PER_STATEMENT", "50000"))
    CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "300"))

    # Database
    SQLALCHEMY_DATABASE_URI = _normalise_database_url(os.getenv("DATABASE_URL"))
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # Sessions / proxy
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = timedelta(days=14)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "0").lower() in {
        "1",
        "true",
        "yes",
    }

    # Localisation
    SUPPORTED_LOCALES = ("en", "sr", "hr", "bs")
    BABEL_DEFAULT_LOCALE = "en"
    BABEL_DEFAULT_TIMEZONE = "Europe/Podgorica"
    BABEL_TRANSLATION_DIRECTORIES = "../translations"

    # Quotas
    ANON_PAGE_LIMIT_24H = 1
    FREE_PAGE_LIMIT_24H = 5
    QUOTA_WINDOW_HOURS = 24
    IP_HASH_SALT = os.getenv("IP_HASH_SALT", "")
    API_KEY_PREFIX = "bsk_live_"
    API_KEY_HASH_PEPPER = os.getenv("API_KEY_HASH_PEPPER", "")

    # Form / request safety
    JSON_SORT_KEYS = False
    PREFERRED_URL_SCHEME = "https" if os.getenv("FORCE_HTTPS", "0") == "1" else "http"

    # CSP
    SECURITY_HEADERS = True


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me-in-production")
    SQLALCHEMY_DATABASE_URI = BaseConfig.SQLALCHEMY_DATABASE_URI or _sqlite_uri(
        BASE_DIR / "instance" / "bank_statement_converter.db"
    )
    SQLALCHEMY_ENGINE_OPTIONS = (
        {"pool_pre_ping": True}
        if not (SQLALCHEMY_DATABASE_URI or "").startswith("sqlite")
        else {"connect_args": {"check_same_thread": False}}
    )
    SESSION_COOKIE_SECURE = False


class ProductionConfig(BaseConfig):
    DEBUG = False
    TESTING = False
    # Secure by default. The bundled HTTP-only Docker demo opts out explicitly;
    # deployments behind TLS must keep both values enabled.
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    PREFERRED_URL_SCHEME = "https"


class TestingConfig(BaseConfig):
    TESTING = True
    DEBUG = False
    SECRET_KEY = "test-secret-key"
    SQLALCHEMY_DATABASE_URI = _normalise_database_url(
        os.getenv("TEST_DATABASE_URL")
    ) or "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = (
        {
            "connect_args": {"check_same_thread": False},
            "poolclass": StaticPool,
        }
        if SQLALCHEMY_DATABASE_URI.startswith("sqlite")
        else {"pool_pre_ping": True}
    )
    SESSION_COOKIE_SECURE = False
    CLEANUP_INTERVAL_SECONDS = 3600
    CONVERSION_RETENTION_SECONDS = 3600


CONFIG_MAP = {
    "development": DevelopmentConfig,
    "dev": DevelopmentConfig,
    "production": ProductionConfig,
    "prod": ProductionConfig,
    "testing": TestingConfig,
    "test": TestingConfig,
}


def get_config(name: str | None = None):
    """Return a config class by name, defaulting to ``FLASK_ENV``/development."""
    key = (name or os.getenv("FLASK_ENV") or "development").lower()
    if key not in CONFIG_MAP:
        raise RuntimeError(f"Unknown application configuration: {key}")
    return CONFIG_MAP[key]
