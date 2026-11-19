"""Authenticated dashboard: quota, history and API key management."""

from __future__ import annotations

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_babel import gettext as _
from flask_login import current_user, login_required
from sqlalchemy import func

from app.extensions import db
from app.models import ApiKey, ConversionJob
from app.services.rate_limiter import get_quota_state, hash_ip
from app.utils.tokens import generate_job_token

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.get("/")
@login_required
def index():
    quota = get_quota_state(
        current_user, hash_ip(), pages_requested=0, commit_cycle_reset=True
    )
    jobs = (
        current_user.conversion_jobs.order_by(ConversionJob.created_at.desc())
        .limit(25)
        .all()
    )
    history = []
    for job in jobs:
        available = job.payload_is_available()
        history.append(
            {
                "job": job,
                "available": available,
                "token": generate_job_token(job.id) if available else None,
            }
        )

    total_jobs = current_user.conversion_jobs.count()
    total_pages = (
        db.session.query(func.coalesce(func.sum(ConversionJob.page_count), 0))
        .filter(ConversionJob.user_id == current_user.id)
        .scalar()
        or 0
    )
    total_rows = (
        db.session.query(func.coalesce(func.sum(ConversionJob.row_count), 0))
        .filter(ConversionJob.user_id == current_user.id)
        .scalar()
        or 0
    )
    api_keys = (
        current_user.api_keys.order_by(ApiKey.created_at.desc()).all()
        if current_user.has_api_access
        else []
    )
    new_api_key = session.pop("new_api_key", None)

    return render_template(
        "dashboard/index.html",
        quota=quota,
        history=history,
        total_jobs=total_jobs,
        total_pages=int(total_pages),
        total_rows=int(total_rows),
        api_keys=api_keys,
        active_api_keys=sum(1 for key in api_keys if key.is_active),
        new_api_key=new_api_key,
    )


@dashboard_bp.post("/api-keys")
@login_required
def create_api_key():
    if not current_user.has_api_access:
        flash(_("API access is available on the Business / Enterprise plan."), "info")
        return redirect(url_for("main.pricing", _anchor="enterprise"))

    name = (request.form.get("name") or "Default key").strip()[:120]
    active_count = (
        current_user.api_keys.filter_by(is_active=True).count()
        if hasattr(current_user.api_keys, "filter_by")
        else 0
    )
    if active_count >= 5:
        flash(_("You can have at most five active API keys. Revoke one first."), "danger")
        return redirect(url_for("dashboard.index"))

    api_key, raw_key = ApiKey.issue(current_user, name=name or "Default key")
    db.session.add(api_key)
    db.session.commit()
    session["new_api_key"] = raw_key
    current_app.logger.info("Issued API key %s for user %s", api_key.key_prefix, current_user.id)
    flash(_("Your new API key has been created. Copy it now — it is shown only once."), "success")
    return redirect(url_for("dashboard.index"))


@dashboard_bp.post("/api-keys/<int:key_id>/revoke")
@login_required
def revoke_api_key(key_id: int):
    api_key = ApiKey.query.filter_by(id=key_id, user_id=current_user.id).first()
    if api_key is None:
        flash(_("API key not found."), "danger")
        return redirect(url_for("dashboard.index"))
    api_key.is_active = False
    db.session.commit()
    flash(_("API key revoked."), "info")
    return redirect(url_for("dashboard.index"))
