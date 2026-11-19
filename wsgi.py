"""WSGI entry point for Gunicorn/uWSGI deployments."""

from run import app

__all__ = ["app"]
