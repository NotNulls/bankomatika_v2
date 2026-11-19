"""Flask extension singletons.

Kept in a dedicated module so models, blueprints and the app factory can import
them without circular imports.
"""

from __future__ import annotations

from flask_babel import Babel
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
babel = Babel()


@login_manager.user_loader
def load_user_by_id(user_id):
    """Reload the signed-in user from the session on each request."""
    from app.models import User

    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None
