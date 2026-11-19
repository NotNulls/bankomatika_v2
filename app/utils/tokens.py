"""Signed, expiring tokens for temporary conversion previews/downloads."""

from __future__ import annotations

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

TOKEN_SALT = "conversion-job-download-v1"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"], salt=TOKEN_SALT
    )


def generate_job_token(job_id: int) -> str:
    return _serializer().dumps({"job_id": int(job_id)})


def read_job_token(token: str, max_age: int | None = None) -> int | None:
    if not token:
        return None
    if max_age is None:
        max_age = current_app.config.get("CONVERSION_RETENTION_SECONDS", 3600) + 300
    try:
        payload = _serializer().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        job_id = int(payload.get("job_id"))
    except (TypeError, ValueError):
        return None
    return job_id if job_id > 0 else None
