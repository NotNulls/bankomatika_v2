"""Database models for the bank statement converter SaaS."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from calendar import monthrange
from datetime import datetime, timedelta, timezone

from flask import current_app
from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db

PLAN_FREE = "free_registered"
PLAN_STARTER = "starter_25"
PLAN_BUSINESS = "business_35"

PLAN_LABELS = {
    PLAN_FREE: "Registered",
    PLAN_STARTER: "Starter / Pro",
    PLAN_BUSINESS: "Business / Enterprise",
}

PLAN_MONTHLY_QUOTA = {
    PLAN_FREE: 0,
    PLAN_STARTER: 500,
    PLAN_BUSINESS: 2500,
}

PLAN_DAILY_QUOTA = {
    "anonymous": 1,
    PLAN_FREE: 5,
}

SUPPORTED_LANGUAGE_CODES = ("en", "sr", "hr", "bs")


def utcnow() -> datetime:
    """Timezone-naive UTC, stored consistently in SQLite and PostgreSQL."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def add_one_month(value: datetime) -> datetime:
    """Return the same wall-clock day one calendar month later."""
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(120), nullable=True)
    plan_tier = db.Column(
        db.String(32), nullable=False, default=PLAN_FREE, server_default=PLAN_FREE
    )
    monthly_page_quota = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    pages_used_this_cycle = db.Column(
        db.Integer, nullable=False, default=0, server_default="0"
    )
    cycle_reset_at = db.Column(db.DateTime, nullable=True)
    preferred_lang = db.Column(db.String(8), nullable=False, default="en", server_default="en")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    conversion_jobs = db.relationship(
        "ConversionJob", back_populates="user", lazy="dynamic", passive_deletes=True
    )
    usage_logs = db.relationship(
        "UsageLog", back_populates="user", lazy="dynamic", passive_deletes=True
    )
    api_keys = db.relationship(
        "ApiKey", back_populates="user", lazy="dynamic", passive_deletes=True
    )

    def __init__(self, **kwargs):
        # Apply plan-aware defaults before SQLAlchemy's default constructor
        # assigns column defaults. This keeps direct model construction (seed
        # scripts, tests, admin tooling) consistent with the pricing table.
        plan = kwargs.get("plan_tier") or PLAN_FREE
        kwargs.setdefault("plan_tier", plan)
        kwargs.setdefault("monthly_page_quota", PLAN_MONTHLY_QUOTA.get(plan, 0))
        if plan in (PLAN_STARTER, PLAN_BUSINESS) and not kwargs.get("cycle_reset_at"):
            kwargs["cycle_reset_at"] = add_one_month(utcnow())
        super().__init__(**kwargs)

    @property
    def display_name(self) -> str:
        return self.full_name.strip() if self.full_name else self.email.split("@")[0]

    @property
    def initials(self) -> str:
        name = self.display_name.replace(".", " ").replace("_", " ").strip()
        parts = [part for part in name.split() if part]
        if len(parts) >= 2:
            return (parts[0][0] + parts[-1][0]).upper()
        return name[:2].upper() if name else "U"

    @property
    def plan_label(self) -> str:
        return PLAN_LABELS.get(self.plan_tier, "Registered")

    @property
    def effective_monthly_quota(self) -> int:
        if self.plan_tier in PLAN_MONTHLY_QUOTA:
            return self.monthly_page_quota or PLAN_MONTHLY_QUOTA[self.plan_tier]
        return self.monthly_page_quota

    @property
    def is_paid(self) -> bool:
        return self.plan_tier in (PLAN_STARTER, PLAN_BUSINESS)

    @property
    def has_api_access(self) -> bool:
        return self.plan_tier == PLAN_BUSINESS

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return bool(password) and check_password_hash(self.password_hash, password)

    def set_plan(self, plan_tier: str) -> None:
        if plan_tier not in PLAN_MONTHLY_QUOTA:
            raise ValueError(f"Unknown plan tier: {plan_tier}")
        self.plan_tier = plan_tier
        self.monthly_page_quota = PLAN_MONTHLY_QUOTA[plan_tier]
        self.pages_used_this_cycle = 0
        self.cycle_reset_at = add_one_month(utcnow())

    def reset_cycle_if_needed(self, now: datetime | None = None) -> bool:
        """Reset a paid monthly cycle when it has elapsed.

        Returns ``True`` when a reset occurred.  The caller owns the commit.
        """
        now = now or utcnow()
        if not self.is_paid:
            return False
        if self.cycle_reset_at is None or now >= self.cycle_reset_at:
            self.pages_used_this_cycle = 0
            self.cycle_reset_at = add_one_month(now)
            return True
        return False

    @property
    def pages_remaining(self) -> int:
        if not self.is_paid:
            return 0
        return max(self.effective_monthly_quota - self.pages_used_this_cycle, 0)

    @property
    def cycle_label(self) -> str:
        if self.cycle_reset_at:
            return self.cycle_reset_at.strftime("%d %b %Y")
        return "—"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.email} {self.plan_tier}>"


class UsageLog(db.Model):
    __tablename__ = "usage_logs"
    __table_args__ = (
        db.Index("ix_usage_logs_ip_created", "ip_hash", "created_at"),
        db.Index("ix_usage_logs_user_created", "user_id", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ip_hash = db.Column(db.String(64), nullable=False, index=True)
    pages_converted = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    user = db.relationship("User", back_populates="usage_logs")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UsageLog user={self.user_id} pages={self.pages_converted}>"


class ConversionJob(db.Model):
    __tablename__ = "conversion_jobs"
    __table_args__ = (
        db.CheckConstraint(
            "status IN ('completed', 'failed')", name="ck_conversion_jobs_status"
        ),
        db.Index("ix_conversion_jobs_user_created", "user_id", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    original_filename = db.Column(db.String(512), nullable=False)
    page_count = db.Column(db.Integer, nullable=False, default=0)
    row_count = db.Column(db.Integer, nullable=False, default=0)
    detected_bank = db.Column(db.String(120), nullable=True)
    parsed_data_json = db.Column(db.Text, nullable=True)
    status = db.Column(
        db.String(16), nullable=False, default="completed", server_default="completed"
    )
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    user = db.relationship("User", back_populates="conversion_jobs")

    @property
    def parsed_rows(self) -> list[dict]:
        if not self.parsed_data_json:
            return []
        try:
            data = json.loads(self.parsed_data_json)
            return data if isinstance(data, list) else []
        except (TypeError, ValueError):
            return []

    def set_rows(self, rows: list[dict]) -> None:
        self.parsed_data_json = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        self.row_count = len(rows)

    def purge_payload(self) -> None:
        self.parsed_data_json = None

    def payload_is_available(self, now: datetime | None = None) -> bool:
        if not self.parsed_data_json:
            return False
        retention = current_app.config.get("CONVERSION_RETENTION_SECONDS", 3600)
        now = now or utcnow()
        return self.created_at is not None and now <= self.created_at + timedelta(
            seconds=retention
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ConversionJob {self.id} {self.original_filename}>"


class ApiKey(db.Model):
    __tablename__ = "api_keys"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key_prefix = db.Column(db.String(32), nullable=False)
    key_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False, default="Default key")
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default="1")
    last_used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    user = db.relationship("User", back_populates="api_keys")

    @staticmethod
    def hash_key(raw_key: str) -> str:
        pepper = (
            current_app.config.get("API_KEY_HASH_PEPPER")
            or current_app.config.get("SECRET_KEY")
            or ""
        )
        return hmac.new(
            pepper.encode("utf-8"), raw_key.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    @classmethod
    def issue(
        cls, user: "User", name: str = "Default key", length: int = 32
    ) -> tuple["ApiKey", str]:
        prefix = current_app.config.get("API_KEY_PREFIX", "bsk_live_")
        raw_key = f"{prefix}{secrets.token_urlsafe(length)}"
        api_key = cls(
            user=user,
            key_prefix=raw_key[:16],
            key_hash=cls.hash_key(raw_key),
            name=(name or "Default key").strip()[:120],
            is_active=True,
        )
        return api_key, raw_key

    @property
    def masked_key(self) -> str:
        return f"{self.key_prefix}…"

    def touch(self) -> None:
        self.last_used_at = utcnow()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ApiKey {self.id} {self.key_prefix}>"
