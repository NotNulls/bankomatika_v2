"""Administrative console for support, plan and retention operations."""

from __future__ import annotations

import hmac
import math
import secrets
from datetime import timedelta
from functools import wraps

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func, or_

from app.extensions import db
from app.models import (
    PLAN_MONTHLY_QUOTA,
    AdminAuditLog,
    ApiKey,
    ConversionJob,
    UsageLog,
    User,
    add_one_month,
    utcnow,
)
from app.services.rate_limiter import hash_ip

admin_bp = Blueprint("admin", __name__)

PER_PAGE = 25
CSRF_SESSION_KEY = "_admin_csrf_token"


def _csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _csrf_valid() -> bool:
    expected = session.get(CSRF_SESSION_KEY)
    supplied = (
        request.form.get("_csrf_token")
        or request.headers.get("X-CSRF-Token")
        or ""
    )
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


@admin_bp.before_request
def protect_admin_forms():
    _csrf_token()
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _csrf_valid():
        abort(400, description="Invalid or expired admin CSRF token.")


@admin_bp.context_processor
def inject_admin_helpers():
    return {"admin_csrf_token": _csrf_token}


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def _audit(action: str, target_type: str, target_id, details: str = "") -> None:
    db.session.add(
        AdminAuditLog(
            admin_user_id=current_user.id,
            action=action,
            target_type=target_type,
            target_id=str(target_id)[:64],
            details=(details or "")[:4000],
            ip_hash=hash_ip(),
        )
    )


def _page_number() -> int:
    try:
        return max(1, int(request.args.get("page", "1")))
    except ValueError:
        return 1


@admin_bp.get("/")
@admin_required
def index():
    now = utcnow()
    day_ago = now - timedelta(hours=24)
    total_users = User.query.count()
    paid_users = User.query.filter(User.plan_tier != "free_registered").count()
    active_keys = ApiKey.query.filter_by(is_active=True).count()
    jobs_24h = ConversionJob.query.filter(ConversionJob.created_at >= day_ago).count()
    pages_24h = (
        db.session.query(func.coalesce(func.sum(UsageLog.pages_converted), 0))
        .filter(UsageLog.created_at >= day_ago)
        .scalar()
        or 0
    )
    failed_jobs = ConversionJob.query.filter_by(status="failed").count()
    recent_jobs = ConversionJob.query.order_by(ConversionJob.created_at.desc()).limit(8).all()
    recent_users = User.query.order_by(User.created_at.desc()).limit(6).all()
    recent_audit = AdminAuditLog.query.order_by(AdminAuditLog.created_at.desc()).limit(8).all()
    return render_template(
        "admin/index.html",
        total_users=total_users,
        paid_users=paid_users,
        active_keys=active_keys,
        jobs_24h=jobs_24h,
        pages_24h=int(pages_24h),
        failed_jobs=failed_jobs,
        recent_jobs=recent_jobs,
        recent_users=recent_users,
        recent_audit=recent_audit,
    )


@admin_bp.get("/users")
@admin_required
def users():
    query = User.query
    q = (request.args.get("q") or "").strip()
    plan = (request.args.get("plan") or "").strip()
    status = (request.args.get("status") or "").strip()
    if q:
        like = f"%{q}%"
        query = query.filter(or_(User.email.ilike(like), User.full_name.ilike(like)))
    if plan in PLAN_MONTHLY_QUOTA:
        query = query.filter(User.plan_tier == plan)
    if status == "active":
        query = query.filter(User.is_active.is_(True))
    elif status == "disabled":
        query = query.filter(User.is_active.is_(False))
    total = query.count()
    page = _page_number()
    pages = max(1, math.ceil(total / PER_PAGE))
    page = min(page, pages)
    items = (
        query.order_by(User.created_at.desc())
        .offset((page - 1) * PER_PAGE)
        .limit(PER_PAGE)
        .all()
    )
    return render_template(
        "admin/users.html",
        users=items,
        total=total,
        page=page,
        pages=pages,
        q=q,
        plan=plan,
        status=status,
        plan_labels=PLAN_MONTHLY_QUOTA,
    )


@admin_bp.get("/users/<int:user_id>")
@admin_required
def user_detail(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    jobs = (
        ConversionJob.query.filter_by(user_id=user.id)
        .order_by(ConversionJob.created_at.desc())
        .limit(15)
        .all()
    )
    keys = (
        ApiKey.query.filter_by(user_id=user.id)
        .order_by(ApiKey.created_at.desc())
        .all()
    )
    return render_template(
        "admin/user_detail.html",
        user=user,
        jobs=jobs,
        api_keys=keys,
        plan_labels=PLAN_MONTHLY_QUOTA,
    )


@admin_bp.post("/users/<int:user_id>/plan")
@admin_required
def update_plan(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    plan = (request.form.get("plan_tier") or "").strip()
    if plan not in PLAN_MONTHLY_QUOTA:
        flash("Invalid plan tier.", "danger")
        return redirect(url_for("admin.user_detail", user_id=user.id))
    previous = user.plan_tier
    user.set_plan(plan)
    _audit("user.plan_changed", "user", user.id, f"{previous} -> {plan}")
    db.session.commit()
    current_app.logger.info("Admin %s changed plan for %s to %s", current_user.id, user.id, plan)
    flash(f"Plan for {user.email} changed to {user.plan_label}.", "success")
    return redirect(url_for("admin.user_detail", user_id=user.id))


@admin_bp.post("/users/<int:user_id>/reset-quota")
@admin_required
def reset_quota(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    previous = user.pages_used_this_cycle
    user.pages_used_this_cycle = 0
    user.cycle_reset_at = add_one_month(utcnow())
    _audit("user.quota_reset", "user", user.id, f"used {previous} -> 0")
    db.session.commit()
    flash(f"Quota cycle reset for {user.email}.", "success")
    return redirect(url_for("admin.user_detail", user_id=user.id))


@admin_bp.post("/users/<int:user_id>/toggle-active")
@admin_required
def toggle_active(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    if user.id == current_user.id:
        flash("You cannot disable your own admin account.", "danger")
        return redirect(url_for("admin.user_detail", user_id=user.id))
    user.is_active = not user.is_active
    _audit(
        "user.status_changed",
        "user",
        user.id,
        "active" if user.is_active else "disabled",
    )
    db.session.commit()
    flash(
        f"{user.email} is now {'active' if user.is_active else 'disabled'}.",
        "success",
    )
    return redirect(url_for("admin.user_detail", user_id=user.id))


@admin_bp.post("/users/<int:user_id>/api-keys/<int:key_id>/revoke")
@admin_required
def revoke_api_key(user_id: int, key_id: int):
    key = ApiKey.query.filter_by(id=key_id, user_id=user_id).first()
    if key is None:
        abort(404)
    key.is_active = False
    _audit("api_key.revoked", "api_key", key.id, f"user={user_id}")
    db.session.commit()
    flash("API key revoked.", "info")
    return redirect(url_for("admin.user_detail", user_id=user_id))


@admin_bp.get("/jobs")
@admin_required
def jobs():
    query = ConversionJob.query
    status = (request.args.get("status") or "").strip()
    if status in {"completed", "failed"}:
        query = query.filter(ConversionJob.status == status)
    q = (request.args.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        query = query.outerjoin(User, ConversionJob.user_id == User.id).filter(
            or_(
                ConversionJob.original_filename.ilike(like),
                User.email.ilike(like),
            )
        )
    total = query.count()
    page = _page_number()
    pages = max(1, math.ceil(total / PER_PAGE))
    page = min(page, pages)
    items = (
        query.order_by(ConversionJob.created_at.desc())
        .offset((page - 1) * PER_PAGE)
        .limit(PER_PAGE)
        .all()
    )
    return render_template(
        "admin/jobs.html",
        jobs=items,
        total=total,
        page=page,
        pages=pages,
        status=status,
        q=q,
    )


@admin_bp.post("/jobs/<int:job_id>/purge")
@admin_required
def purge_job(job_id: int):
    job = db.session.get(ConversionJob, job_id)
    if job is None:
        abort(404)
    job.purge_payload()
    _audit("job.payload_purged", "conversion_job", job.id)
    db.session.commit()
    flash("Parsed payload purged.", "info")
    return redirect(url_for("admin.jobs"))


@admin_bp.get("/audit")
@admin_required
def audit():
    total = AdminAuditLog.query.count()
    page = _page_number()
    pages = max(1, math.ceil(total / PER_PAGE))
    page = min(page, pages)
    items = (
        AdminAuditLog.query.order_by(AdminAuditLog.created_at.desc())
        .offset((page - 1) * PER_PAGE)
        .limit(PER_PAGE)
        .all()
    )
    return render_template(
        "admin/audit.html", logs=items, total=total, page=page, pages=pages
    )