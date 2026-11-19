"""Authentication blueprint: register, login and logout."""

from __future__ import annotations

import re

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
from flask_login import current_user, login_user, logout_user
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import PLAN_FREE, User, add_one_month, utcnow
from app.utils.http import safe_next_url
from app.utils.localization import normalize_locale, remember_language, select_locale

auth_bp = Blueprint("auth", __name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MIN_PASSWORD_LENGTH = 8


def _redirect_authenticated():
    return redirect(url_for("dashboard.index"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return _redirect_authenticated()

    next_target = request.args.get("next") or request.form.get("next")
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        remember = bool(request.form.get("remember"))
        user = User.query.filter_by(email=email).first() if email else None
        if user is None or not user.check_password(password):
            flash(_("Incorrect email or password. Please try again."), "danger")
            return (
                render_template(
                    "auth/login.html",
                    email=email,
                    next_target=next_target,
                ),
                401,
            )
        login_user(user, remember=remember)
        session.permanent = True
        if user.preferred_lang:
            remember_language(user.preferred_lang)
        flash(_("Welcome back, %(name)s.", name=user.display_name), "success")
        return redirect(
            safe_next_url(next_target, url_for("dashboard.index"))
        )

    return render_template("auth/login.html", email="", next_target=next_target)


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return _redirect_authenticated()

    if request.method == "POST":
        full_name = (
            request.form.get("full_name") or request.form.get("name") or ""
        ).strip()[:120]
        email = (request.form.get("email") or "").strip().lower()[:255]
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or request.form.get(
            "password_confirm"
        )
        language = normalize_locale(request.form.get("lang")) or select_locale()
        errors: list[str] = []

        if not full_name:
            errors.append(_("Please enter your full name."))
        if not _EMAIL_RE.match(email):
            errors.append(_("Please enter a valid email address."))
        if len(password) < _MIN_PASSWORD_LENGTH:
            errors.append(
                _("Your password must be at least %(count)s characters.", count=_MIN_PASSWORD_LENGTH)
            )
        if confirm and confirm != password:
            errors.append(_("The passwords do not match."))
        if not errors and User.query.filter_by(email=email).first():
            errors.append(_("An account with this email already exists."))

        if errors:
            for error in errors:
                flash(error, "danger")
            return (
                render_template(
                    "auth/register.html",
                    form={"full_name": full_name, "email": email},
                    selected_lang=language,
                ),
                400,
            )

        user = User(
            email=email,
            full_name=full_name,
            plan_tier=PLAN_FREE,
            monthly_page_quota=0,
            pages_used_this_cycle=0,
            cycle_reset_at=add_one_month(utcnow()),
            preferred_lang=language,
        )
        user.set_password(password)
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash(_("An account with this email already exists."), "danger")
            return render_template("auth/register.html", form={"full_name": full_name, "email": email}), 409

        login_user(user)
        session.permanent = True
        remember_language(language)
        flash(
            _("Your free account is ready. You now have 5 pages every 24 hours."),
            "success",
        )
        current_app.logger.info("New registered user: %s", user.email)
        return redirect(url_for("dashboard.index"))

    return render_template(
        "auth/register.html", form={}, selected_lang=select_locale()
    )


@auth_bp.route("/logout", methods=["GET", "POST"])
def logout():
    if current_user.is_authenticated:
        logout_user()
    session.pop("new_api_key", None)
    flash(_("You have been signed out."), "info")
    return redirect(url_for("main.index"))
