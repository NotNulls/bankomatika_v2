"""Rolling anonymous/free quotas and monthly paid-plan quotas."""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from flask import current_app, request
from sqlalchemy import func

from app.extensions import db
from app.models import (
    PLAN_FREE,
    UsageLog,
    User,
    utcnow,
)


@dataclass
class QuotaState:
    scope: str
    limit: int
    used: int
    remaining: int
    window: str
    resets_at: datetime
    pages_requested: int = 0
    allowed: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def percent_used(self) -> int:
        if self.limit <= 0:
            return 100
        return min(100, max(0, round((self.used / self.limit) * 100)))

    @property
    def label(self) -> str:
        unit = "pages"
        if self.window == "24 hours":
            return f"{self.limit} {unit} / 24 hours"
        return f"{self.limit:,} {unit} / month"

    @property
    def retry_after(self) -> int:
        return max(1, int((self.resets_at - utcnow()).total_seconds()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "limit": self.limit,
            "used": self.used,
            "remaining": self.remaining,
            "window": self.window,
            "label": self.label,
            "percent_used": self.percent_used,
            "resets_at": self.resets_at.isoformat() + "Z",
            "retry_after": self.retry_after,
            "allowed": self.allowed,
            "pages_requested": self.pages_requested,
        }


class QuotaExceeded(Exception):
    def __init__(self, state: QuotaState):
        self.state = state
        super().__init__("Conversion quota exceeded")


def get_client_ip() -> str:
    """Return a best-effort client IP, honouring proxy headers only if trusted."""
    if current_app.config.get("TRUST_PROXY_HEADERS"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            first = forwarded.split(",", 1)[0].strip()
            if first:
                return _normalise_ip(first)
        for header in ("CF-Connecting-IP", "X-Real-IP", "True-Client-IP"):
            value = request.headers.get(header)
            if value:
                return _normalise_ip(value)
    return _normalise_ip(request.remote_addr or "unknown")


def _normalise_ip(value: str) -> str:
    value = (value or "").strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value[:100] or "unknown"


def hash_ip(ip_address: str | None = None) -> str:
    salt = current_app.config.get("IP_HASH_SALT") or current_app.config["SECRET_KEY"]
    value = ip_address or get_client_ip()
    return hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()


def _rolling_usage(*criteria) -> tuple[int, datetime | None]:
    cutoff = utcnow() - timedelta(hours=current_app.config.get("QUOTA_WINDOW_HOURS", 24))
    query = db.session.query(
        func.coalesce(func.sum(UsageLog.pages_converted), 0),
        func.min(UsageLog.created_at),
    ).filter(UsageLog.created_at >= cutoff, *criteria)
    used, first_use = query.one()
    return int(used or 0), first_use


def _daily_reset(first_use: datetime | None) -> datetime:
    window = int(current_app.config.get("QUOTA_WINDOW_HOURS", 24))
    if first_use is None:
        return utcnow() + timedelta(hours=window)
    return first_use + timedelta(hours=window)


def get_quota_state(
    user: User | None,
    ip_hash: str,
    pages_requested: int = 0,
    commit_cycle_reset: bool = True,
) -> QuotaState:
    """Build the current quota state and test whether ``pages_requested`` fit."""
    now = utcnow()
    requested = max(0, int(pages_requested))

    if user is not None and user.is_paid:
        reset = user.reset_cycle_if_needed(now)
        if reset and commit_cycle_reset:
            db.session.commit()
        limit = max(int(user.effective_monthly_quota or 0), 0)
        used = max(int(user.pages_used_this_cycle or 0), 0)
        resets_at = user.cycle_reset_at or now + timedelta(days=30)
        return QuotaState(
            scope="paid",
            limit=limit,
            used=used,
            remaining=max(limit - used, 0),
            window="month",
            resets_at=resets_at,
            pages_requested=requested,
            allowed=(used + requested) <= limit,
        )

    if user is not None:
        limit = int(current_app.config.get("FREE_PAGE_LIMIT_24H", 5))
        used, first_use = _rolling_usage(UsageLog.user_id == user.id)
        resets_at = _daily_reset(first_use)
        return QuotaState(
            scope="free",
            limit=limit,
            used=used,
            remaining=max(limit - used, 0),
            window="24 hours",
            resets_at=resets_at,
            pages_requested=requested,
            allowed=(used + requested) <= limit,
        )

    if not ip_hash:
        ip_hash = hash_ip()
    limit = int(current_app.config.get("ANON_PAGE_LIMIT_24H", 1))
    used, first_use = _rolling_usage(
        UsageLog.ip_hash == ip_hash, UsageLog.user_id.is_(None)
    )
    resets_at = _daily_reset(first_use)
    return QuotaState(
        scope="anonymous",
        limit=limit,
        used=used,
        remaining=max(limit - used, 0),
        window="24 hours",
        resets_at=resets_at,
        pages_requested=requested,
        allowed=(used + requested) <= limit,
    )


def quota_for_request(user: User | None, pages_requested: int = 0) -> QuotaState:
    return get_quota_state(
        user=user,
        ip_hash=hash_ip(),
        pages_requested=pages_requested,
    )


def record_usage(
    user: User | None,
    ip_hash_value: str,
    pages: int,
    commit: bool = True,
) -> None:
    """Record a successful conversion against the correct quota bucket."""
    pages = max(1, int(pages))
    log = UsageLog(user_id=user.id if user else None, ip_hash=ip_hash_value, pages_converted=pages)
    db.session.add(log)
    if user is not None and user.is_paid:
        if user.reset_cycle_if_needed():
            pass
        user.pages_used_this_cycle = int(user.pages_used_this_cycle or 0) + pages
    if commit:
        db.session.commit()


def record_request_usage(user: User | None, pages: int, commit: bool = True) -> None:
    record_usage(user=user, ip_hash_value=hash_ip(), pages=pages, commit=commit)


def ensure_quota(user: User | None, pages_requested: int) -> QuotaState:
    """Raise :class:`QuotaExceeded` when the conversion cannot fit."""
    state = quota_for_request(user, pages_requested)
    if not state.allowed:
        raise QuotaExceeded(state)
    return state


def paid_quota_available(user: User | None, pages_requested: int = 0) -> QuotaState:
    """Monthly quota view used by the dashboard."""
    return get_quota_state(
        user=user,
        ip_hash=hash_ip(),
        pages_requested=pages_requested,
    )


def free_daily_state(user: User | None) -> QuotaState:
    return get_quota_state(
        user=user,
        ip_hash=hash_ip(),
        pages_requested=0,
    )
