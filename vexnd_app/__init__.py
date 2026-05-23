from __future__ import annotations

import os

from dotenv import load_dotenv
from flask import Flask

from vexnd_app.config import _env_bool, apply_flask_config
from vexnd_app.extensions import db, login_manager
from vexnd_app.routes.api import register as register_api_routes
from vexnd_app.routes.auth import register as register_auth_routes
from vexnd_app.routes.dashboard import register as register_dashboard_routes
from vexnd_app.routes.pages import register as register_page_routes
from vexnd_app.routes.payments import register as register_payment_routes
from vexnd_app.web import init_app as init_web


def create_app() -> Flask:
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
    app = Flask(__name__, static_folder="../static", template_folder="../templates")
    apply_flask_config(app)
    db.init_app(app)
    login_manager.init_app(app)
    init_web(app)
    register_page_routes(app)
    register_auth_routes(app)
    register_dashboard_routes(app)
    register_api_routes(app)
    register_payment_routes(app)

    @app.cli.command("create-test-user")
    def create_test_user():
        from datetime import datetime, timedelta

        from vexnd_app.models import Subscription, User

        with app.app_context():
            if not User.query.filter_by(email="test@example.com").first():
                user = User(email="test@example.com")
                user.set_password("123456")
                db.session.add(user)
                db.session.commit()
                sub = Subscription(user_id=user.id, expiry_date=datetime.utcnow() + timedelta(days=30), subscription_url="https://example.com/remnawave-subscription-test")
                db.session.add(sub)
                db.session.commit()
                print("✅ Тестовый пользователь создан!")
            else:
                print("ℹ️ Тестовый пользователь уже существует")
            print("📧 Email: test@example.com")
            print("🔑 Пароль: 123456")

    return app
