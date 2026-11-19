"""Public marketing, pricing, documentation and legal routes."""

from __future__ import annotations

import re

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_babel import gettext as _
from flask_login import current_user
from sqlalchemy import text

from app.extensions import db
from app.utils.http import safe_next_url
from app.utils.localization import normalize_locale, remember_language

main_bp = Blueprint("main", __name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@main_bp.get("/")
def index():
    return render_template("main/index.html")


@main_bp.get("/pricing")
def pricing():
    return render_template("main/pricing.html")


@main_bp.get("/api-docs")
def api_docs():
    return render_template(
        "main/api_docs.html", api_endpoint=url_for("api.convert", _external=True)
    )


@main_bp.get("/about")
def about():
    return render_template("main/about.html")


# Friendly top-level aliases (the canonical routes live in their blueprints).
@main_bp.get("/login")
def login_alias():
    target = request.args.get("next")
    return redirect(url_for("auth.login", **({"next": target} if target else {})))


@main_bp.get("/register")
def register_alias():
    return redirect(url_for("auth.register"))


@main_bp.get("/signup")
def signup_alias():
    return redirect(url_for("auth.register"))


@main_bp.get("/dashboard")
def dashboard_alias():
    return redirect(url_for("dashboard.index"))


@main_bp.get("/terms")
def terms():
    return render_template("main/terms.html")


@main_bp.get("/privacy")
def privacy():
    return render_template("main/privacy.html")


@main_bp.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:120]
        email = (request.form.get("email") or "").strip().lower()[:255]
        company = (request.form.get("company") or "").strip()[:160]
        message = (request.form.get("message") or "").strip()[:5000]
        plan = (request.form.get("plan") or "business").strip()[:80]
        honeypot = (request.form.get("website") or "").strip()
        if honeypot:
            flash(_("Thanks — our enterprise team will be in touch."), "success")
            return redirect(url_for("main.pricing", _anchor="enterprise"))
        if not name or not _EMAIL_RE.match(email) or not message:
            flash(_("Please complete your name, a valid email and a message."), "danger")
            return render_template(
                "main/contact.html",
                form={"name": name, "email": email, "company": company, "message": message},
                plan=plan,
            ), 400
        current_app.logger.info(
            "Enterprise inquiry plan=%s company=%s email=%s name=%s", plan, company, email, name
        )
        flash(_("Thanks — our enterprise team will be in touch within one business day."), "success")
        return redirect(url_for("main.pricing", _anchor="enterprise"))
    return render_template("main/contact.html", form={}, plan="business")


@main_bp.get("/language/<lang>")
def set_language(lang: str):
    locale = normalize_locale(lang) or "en"
    remember_language(locale)
    if getattr(current_user, "is_authenticated", False):
        current_user.preferred_lang = locale
        db.session.commit()
    target = safe_next_url(
        request.args.get("next") or request.referrer, url_for("main.index")
    )
    return redirect(target)


@main_bp.get("/health")
def health():
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify(
            {
                "status": "ok",
                "app": current_app.config["APP_NAME"],
                "version": current_app.config.get("APP_VERSION", "1.0.0"),
            }
        )
    except Exception:
        current_app.logger.exception("Health check failed")
        return jsonify({"status": "degraded"}), 503


@main_bp.get("/robots.txt")
def robots():
    sitemap = url_for("main.sitemap", _external=True)
    body = "User-agent: *\nAllow: /\nDisallow: /dashboard\nDisallow: /api/\n"
    body += f"Sitemap: {sitemap}\n"
    return current_app.response_class(body, mimetype="text/plain")


@main_bp.get("/sitemap.xml")
def sitemap():
    pages = ["main.index", "main.pricing", "main.api_docs", "main.about", "main.terms", "main.privacy"]
    entries = "".join(
        f"<url><loc>{url_for(page, _external=True)}</loc></url>" for page in pages
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{entries}</urlset>"
    )
    return current_app.response_class(xml, mimetype="application/xml")
