"""Development entry point.

Run with ``python run.py``.  For production use a WSGI server, e.g.
``gunicorn --bind 0.0.0.0:8000 run:app``.
"""

from __future__ import annotations

import os

from app import create_app

app = create_app(os.getenv("FLASK_ENV") or None)


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    app.run(host=host, port=port, debug=app.config.get("DEBUG", False))
