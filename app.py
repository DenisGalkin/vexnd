from __future__ import annotations

from vexnd_app import create_app
from vexnd_app.config import _env_bool


app = create_app()


if __name__ == "__main__":
    print("🚀 Сервер запущен на http://localhost:5000")
    app.run(debug=_env_bool("FLASK_DEBUG", False), port=2000)
