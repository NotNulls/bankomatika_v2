"""Upload retention and cleanup routines.

Raw PDFs are written to a private upload directory only so that the parser can
work with a stable file handle.  The web and API routes remove that file in a
``finally`` block as soon as extraction finishes.  A periodic sweep is a second
line of defence for crashed workers and stale preview payloads.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import timedelta
from pathlib import Path

from flask import current_app
from sqlalchemy import or_

from app.extensions import db
from app.models import ConversionJob, utcnow

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"


def ensure_directories() -> None:
    for directory in (
        current_app.instance_path,
        current_app.config.get("UPLOAD_FOLDER"),
    ):
        if directory:
            Path(directory).mkdir(parents=True, exist_ok=True)


def clean_original_filename(filename: str | None) -> str:
    name = (filename or "statement.pdf").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if char.isprintable()).strip()
    return (name or "statement.pdf")[:512]


def persist_upload(file_storage) -> tuple[bytes, Path, str]:
    """Persist an upload and return ``(bytes, path, display_filename)``.

    The caller is responsible for removing the returned path in ``finally``.
    """
    upload_folder = Path(current_app.config["UPLOAD_FOLDER"])
    upload_folder.mkdir(parents=True, exist_ok=True)
    display_name = clean_original_filename(file_storage.filename)
    target = upload_folder / f"{uuid.uuid4().hex}.pdf"
    file_storage.save(target)
    try:
        data = target.read_bytes()
    except Exception:
        remove_file(target)
        raise
    if not data.lstrip().startswith(PDF_MAGIC):
        remove_file(target)
        raise ValueError("The uploaded file is not a valid PDF document.")
    return data, target, display_name


def remove_file(path: Path | str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("Could not remove temporary upload %s", path, exc_info=True)


def purge_expired_payloads(now=None) -> int:
    now = now or utcnow()
    retention = int(current_app.config.get("CONVERSION_RETENTION_SECONDS", 3600))
    cutoff = now - timedelta(seconds=retention)
    jobs = (
        db.session.query(ConversionJob)
        .filter(
            ConversionJob.parsed_data_json.is_not(None),
            ConversionJob.created_at < cutoff,
        )
        .all()
    )
    for job in jobs:
        job.purge_payload()
    if jobs:
        db.session.commit()
        logger.info("Purged parsed payloads for %s expired conversions", len(jobs))
    return len(jobs)


def purge_stale_uploads(now=None) -> int:
    now = now or utcnow()
    retention = int(current_app.config.get("CONVERSION_RETENTION_SECONDS", 3600))
    folder = Path(current_app.config.get("UPLOAD_FOLDER", ""))
    if not folder.exists():
        return 0
    cutoff = now.timestamp() - retention
    removed = 0
    for path in folder.iterdir():
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            logger.warning("Could not purge stale upload %s", path, exc_info=True)
    if removed:
        logger.info("Removed %s stale uploaded PDF(s)", removed)
    return removed


def cleanup_expired() -> dict[str, int]:
    """Delete stale files and clear expired previews. Safe to call often."""
    payloads = purge_expired_payloads()
    uploads = purge_stale_uploads()
    return {"payloads": payloads, "uploads": uploads}


def failed_job_count() -> int:
    """Small operational metric for dashboards/health checks."""
    return (
        db.session.query(ConversionJob)
        .filter(or_(ConversionJob.status == "failed"))
        .count()
    )
