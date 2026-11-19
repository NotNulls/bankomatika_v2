"""HTTP helpers shared by auth and web blueprints."""

from __future__ import annotations

from urllib.parse import urlsplit

from flask import request


def safe_next_url(target: str | None, fallback: str) -> str:
    """Only allow same-site relative redirect targets (open-redirect guard)."""
    if not target:
        return fallback
    target = target.strip()
    if not target.startswith("/") or target.startswith("//"):
        return fallback
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return fallback
    return target


def wants_json_response() -> bool:
    if request.path.startswith("/api/"):
        return True
    if request.is_json:
        return True
    best = request.accept_mimetypes.best
    return bool(
        best == "application/json"
        and request.accept_mimetypes[best] > request.accept_mimetypes["text/html"]
    )
