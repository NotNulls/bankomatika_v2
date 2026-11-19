"""Seed demo accounts and a Business API key.

Usage:
    python seed.py

The script is idempotent and safe to re-run. It prints the demo API key so it
can be used immediately; the database itself stores only a keyed hash.
"""

from __future__ import annotations

import os

from app import create_app
from app.extensions import db
from app.models import (
    PLAN_BUSINESS,
    PLAN_FREE,
    PLAN_STARTER,
    ApiKey,
    User,
    add_one_month,
    utcnow,
)

DEMO_PASSWORD = "demo1234"
DEMO_API_KEY = os.getenv(
    "DEMO_API_KEY", "bsk_live_demo_business_8f3a1c9e7b2d4f6a"
)


def upsert_user(email: str, full_name: str, plan: str, pages_used: int = 0) -> User:
    user = User.query.filter_by(email=email).first()
    if user is None:
        user = User(email=email)
        db.session.add(user)
    user.full_name = full_name
    user.set_password(DEMO_PASSWORD)
    user.plan_tier = plan
    user.monthly_page_quota = {PLAN_FREE: 0, PLAN_STARTER: 500, PLAN_BUSINESS: 2500}[plan]
    user.pages_used_this_cycle = pages_used
    user.cycle_reset_at = add_one_month(utcnow())
    user.preferred_lang = "en"
    return user


def upsert_demo_api_key(user: User) -> ApiKey:
    key_hash = ApiKey.hash_key(DEMO_API_KEY)
    api_key = ApiKey.query.filter_by(key_hash=key_hash).first()
    if api_key is None:
        api_key = ApiKey(
            user=user,
            key_prefix=DEMO_API_KEY[:16],
            key_hash=key_hash,
            name="Seeded demo key",
            is_active=True,
        )
        db.session.add(api_key)
    else:
        api_key.user = user
        api_key.is_active = True
        api_key.name = "Seeded demo key"
    return api_key


def main() -> None:
    app = create_app(os.getenv("FLASK_ENV") or "development")
    with app.app_context():
        db.create_all()

        pro = upsert_user("pro@example.com", "Pro Demo", PLAN_STARTER, pages_used=12)
        biz = upsert_user(
            "biz@example.com", "Business Demo", PLAN_BUSINESS, pages_used=37
        )
        free = upsert_user("free@example.com", "Free Demo", PLAN_FREE, pages_used=0)
        db.session.flush()
        api_key = upsert_demo_api_key(biz)
        db.session.commit()

        print("=" * 72)
        print(" Demo data ready")
        print("=" * 72)
        print(f" Starter / Pro : pro@example.com  / {DEMO_PASSWORD}")
        print(f" Business      : biz@example.com  / {DEMO_PASSWORD}")
        print(f" Free account  : free@example.com / {DEMO_PASSWORD}")
        print(f" API key       : {DEMO_API_KEY}")
        print(f" API key id    : {api_key.id} ({api_key.key_prefix}…)")
        print(f" Database      : {app.config['SQLALCHEMY_DATABASE_URI']}")
        print("=" * 72)
        _ = (pro, free)


if __name__ == "__main__":
    main()
