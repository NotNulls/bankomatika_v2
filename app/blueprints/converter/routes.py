"""Browser conversion flow: upload, preview, edit and download."""

from __future__ import annotations

import logging

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    request,
    send_file,
    url_for,
)
from flask_babel import gettext as _
from flask_login import current_user

from app.extensions import db
from app.models import ConversionJob
from app.services.cleanup import persist_upload, remove_file
from app.services.exporter import ExportError, build_export, normalise_export_rows
from app.services.pdf_parser import (
    PdfParseError,
    count_pdf_pages,
    parse_statement,
)
from app.services.rate_limiter import (
    QuotaExceeded,
    get_quota_state,
    hash_ip,
    record_usage,
)
from app.utils.tokens import generate_job_token, read_job_token

logger = logging.getLogger(__name__)

converter_bp = Blueprint("converter", __name__)


def _current_user_or_none():
    return current_user if getattr(current_user, "is_authenticated", False) else None


def _error(message: str, status: int, code: str, **extra):
    payload = {"error": code, "message": message}
    payload.update(extra)
    return jsonify(payload), status


def _quota_response(state) -> tuple:
    response = jsonify(
        {
            "error": "quota_exceeded",
            "message": _(
                "Your conversion quota has been reached. "
                "Create a free account for 5 pages per day or upgrade for more."
            ),
            "quota": state.to_dict(),
        }
    )
    response.status_code = 429
    response.headers["Retry-After"] = str(state.retry_after)
    return response


def _download_payload(token: str) -> dict[str, str]:
    return {
        "xlsx": url_for(
            "converter.download", token=token, export_format="xlsx", _external=False
        ),
        "csv": url_for(
            "converter.download", token=token, export_format="csv", _external=False
        ),
    }


@converter_bp.get("/convert")
def convert_get():
    return redirect(url_for("main.index"))


@converter_bp.post("/convert")
def convert():
    uploaded = request.files.get("file") or request.files.get("pdf")
    if uploaded is None or not uploaded.filename:
        return _error("Please choose a PDF bank statement to upload.", 400, "no_file")
    if not uploaded.filename.lower().endswith(".pdf"):
        return _error("Only PDF bank statements are supported.", 400, "invalid_file_type")

    user = _current_user_or_none()
    ip_hash_value = hash_ip()
    path = None
    try:
        try:
            data, path, display_name = persist_upload(uploaded)
        except ValueError as exc:
            return _error(str(exc), 400, "invalid_pdf")
        except OSError:
            logger.exception("Could not save an uploaded PDF")
            return _error(
                "The upload could not be stored. Please try again.", 500, "upload_failed"
            )

        try:
            page_count = count_pdf_pages(data)
        except PdfParseError as exc:
            return _error(str(exc), 422, exc.code)
        if page_count > current_app.config.get("MAX_PDF_PAGES", 500):
            return _error(
                f"This statement has {page_count} pages; the maximum is "
                f"{current_app.config.get('MAX_PDF_PAGES', 500)} pages in one conversion.",
                422,
                "page_limit",
            )

        state = get_quota_state(user, ip_hash_value, pages_requested=page_count)
        if not state.allowed:
            return _quota_response(state)

        try:
            parsed = parse_statement(
                data,
                filename=display_name,
                max_pages=current_app.config.get("MAX_PDF_PAGES", 500),
                max_rows=current_app.config.get("MAX_ROWS_PER_STATEMENT", 50000),
            )
        except PdfParseError as exc:
            return _error(str(exc), 422, exc.code)

        job = ConversionJob(
            user_id=user.id if user else None,
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
            "Converted %s pages=%s rows=%s bank=%s job=%s",
            display_name,
            page_count,
            len(parsed.rows),
            parsed.detected_bank,
            job.id,
        )
        return jsonify(
            {
                "success": True,
                "job_id": job.id,
                "token": token,
                "original_filename": display_name,
                "page_count": page_count,
                "row_count": len(parsed.rows),
                "detected_bank": parsed.detected_bank,
                "warnings": parsed.warnings,
                "rows": parsed.rows,
                "quota": next_state.to_dict(),
                "downloads": _download_payload(token),
            }
        )
    except QuotaExceeded as exc:
        return _quota_response(exc.state)
    except Exception:
        db.session.rollback()
        logger.exception("Unexpected conversion failure")
        return _error(
            "Something went wrong while converting this statement. Please try again.",
            500,
            "conversion_failed",
        )
    finally:
        # Zero-retention: the raw PDF never survives the request.
        remove_file(path)


@converter_bp.get("/convert/jobs/<token>")
def preview(token: str):
    job_id = read_job_token(token)
    job = db.session.get(ConversionJob, job_id) if job_id else None
    if job is None:
        return _error("This conversion link is invalid.", 404, "not_found")
    if not job.payload_is_available():
        return _error(
            "This preview has expired. Please upload the statement again.",
            410,
            "expired",
        )
    state = get_quota_state(_current_user_or_none(), hash_ip(), pages_requested=0)
    return jsonify(
        {
            "success": True,
            "job_id": job.id,
            "token": token,
            "original_filename": job.original_filename,
            "page_count": job.page_count,
            "row_count": job.row_count,
            "detected_bank": job.detected_bank,
            "rows": job.parsed_rows,
            "quota": state.to_dict(),
            "downloads": _download_payload(token),
        }
    )


@converter_bp.route(
    "/convert/download/<token>/<export_format>", methods=["GET", "POST"]
)
def download(token: str, export_format: str):
    job_id = read_job_token(token)
    job = db.session.get(ConversionJob, job_id) if job_id else None
    if job is None:
        return _error("This download link is invalid.", 404, "not_found")
    if not job.payload_is_available():
        return _error(
            "This download has expired for security reasons. Please convert the "
            "statement again.",
            410,
            "expired",
        )

    rows = job.parsed_rows
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        if "rows" in payload:
            try:
                rows = normalise_export_rows(payload.get("rows"))
            except ExportError as exc:
                return _error(str(exc), 400, "invalid_rows")
            if rows:
                job.set_rows(rows)
                db.session.commit()

    if not rows:
        return _error("There are no transactions to export.", 422, "no_rows")

    try:
        buffer, filename, mimetype = build_export(
            export_format, rows, job.original_filename
        )
    except ExportError as exc:
        return _error(str(exc), 400, "invalid_format")

    response = send_file(
        buffer,
        mimetype=mimetype,
        as_attachment=True,
        download_name=filename,
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    return response


@converter_bp.get("/convert/limits")
def limits():
    user = _current_user_or_none()
    state = get_quota_state(user, hash_ip(), pages_requested=0)
    return jsonify({"quota": state.to_dict()})
