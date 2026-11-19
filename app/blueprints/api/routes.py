"""Bearer-authenticated REST API v1."""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, g, jsonify, request, url_for

from app.extensions import db
from app.models import ApiKey, ConversionJob
from app.services.cleanup import persist_upload, remove_file
from app.services.pdf_parser import PdfParseError, count_pdf_pages, parse_statement
from app.services.rate_limiter import (
    get_quota_state,
    hash_ip,
    record_usage,
)
from app.utils.tokens import generate_job_token

logger = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__)


def _api_error(message: str, status: int, code: str, **extra):
    payload = {"success": False, "error": code, "message": message}
    payload.update(extra)
    response = jsonify(payload)
    response.status_code = status
    return response


def _quota_error(state):
    response = _api_error(
        "Monthly API page quota exceeded. Upgrade your plan or wait for the next cycle.",
        429,
        "quota_exceeded",
        quota=state.to_dict(),
    )
    response.headers["Retry-After"] = str(state.retry_after)
    return response


@api_bp.before_request
def authenticate_api_key():
    if request.method == "OPTIONS":
        return ("", 204)
    header = request.headers.get("Authorization", "")
    raw_key = ""
    if header.lower().startswith("bearer "):
        raw_key = header[7:].strip()
    if not raw_key:
        raw_key = (request.headers.get("X-API-Key") or "").strip()
    if not raw_key:
        response = _api_error(
            "Provide an API key using the Authorization: Bearer header.",
            401,
            "missing_api_key",
        )
        response.headers["WWW-Authenticate"] = 'Bearer realm="api"'
        return response

    key_hash = ApiKey.hash_key(raw_key)
    api_key = ApiKey.query.filter_by(key_hash=key_hash, is_active=True).first()
    if api_key is None or api_key.user is None:
        response = _api_error("Invalid or revoked API key.", 401, "invalid_api_key")
        response.headers["WWW-Authenticate"] = 'Bearer realm="api", error="invalid_token"'
        return response
    if not api_key.user.has_api_access:
        return _api_error(
            "API access requires the Business / Enterprise plan.",
            403,
            "api_plan_required",
        )
    api_key.touch()
    db.session.commit()
    g.api_key = api_key
    g.api_user = api_key.user
    return None


@api_bp.post("/convert")
def convert():
    uploaded = request.files.get("file") or request.files.get("pdf")
    if uploaded is None or not uploaded.filename:
        return _api_error("Attach a PDF using the multipart field 'file'.", 400, "no_file")
    if not uploaded.filename.lower().endswith(".pdf"):
        return _api_error("Only PDF bank statements are supported.", 400, "invalid_file_type")

    user = g.api_user
    ip_hash_value = hash_ip()
    path = None
    try:
        try:
            data, path, display_name = persist_upload(uploaded)
        except ValueError as exc:
            return _api_error(str(exc), 400, "invalid_pdf")
        except OSError:
            logger.exception("API upload storage failed")
            return _api_error("The upload could not be stored.", 500, "upload_failed")

        try:
            page_count = count_pdf_pages(data)
        except PdfParseError as exc:
            return _api_error(str(exc), 422, exc.code)
        if page_count > current_app.config.get("MAX_PDF_PAGES", 500):
            return _api_error(
                f"This statement has {page_count} pages; the maximum is "
                f"{current_app.config.get('MAX_PDF_PAGES', 500)}.",
                422,
                "page_limit",
            )

        state = get_quota_state(user, ip_hash_value, pages_requested=page_count)
        if not state.allowed:
            return _quota_error(state)

        try:
            parsed = parse_statement(
                data,
                filename=display_name,
                max_pages=current_app.config.get("MAX_PDF_PAGES", 500),
                max_rows=current_app.config.get("MAX_ROWS_PER_STATEMENT", 50000),
            )
        except PdfParseError as exc:
            return _api_error(str(exc), 422, exc.code)

        job = ConversionJob(
            user_id=user.id,
            original_filename=display_name,
            page_count=page_count,
            detected_bank=parsed.detected_bank,
            status="completed",
        )
        job.set_rows(parsed.rows)
        db.session.add(job)
        db.session.flush()
        record_usage(user, ip_hash_value, page_count, commit=False)
        db.session.commit()

        token = generate_job_token(job.id)
        next_state = get_quota_state(user, ip_hash_value, pages_requested=0)
        logger.info(
            "API conversion user=%s pages=%s rows=%s bank=%s job=%s",
            user.id,
            page_count,
            len(parsed.rows),
            parsed.detected_bank,
            job.id,
        )
        response = jsonify(
            {
                "success": True,
                "api_version": "v1",
                "job_id": job.id,
                "token": token,
                "original_filename": display_name,
                "page_count": page_count,
                "row_count": len(parsed.rows),
                "detected_bank": parsed.detected_bank,
                "warnings": parsed.warnings,
                "rows": parsed.rows,
                "quota": next_state.to_dict(),
                "downloads": {
                    "xlsx": url_for(
                        "converter.download",
                        token=token,
                        export_format="xlsx",
                        _external=True,
                    ),
                    "csv": url_for(
                        "converter.download",
                        token=token,
                        export_format="csv",
                        _external=True,
                    ),
                },
            }
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-API-Version"] = "v1"
        return response
    except Exception:
        db.session.rollback()
        logger.exception("Unexpected API conversion failure")
        return _api_error(
            "An unexpected error occurred while converting the statement.",
            500,
            "conversion_failed",
        )
    finally:
        remove_file(path)
