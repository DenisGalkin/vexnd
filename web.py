from __future__ import annotations

import os

from app import create_app
from app.core.config import _env_bool


app = create_app()


if __name__ == "__main__":
    host = os.getenv("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    print(f"Server started on http://{host}:{port}")
    # The app already loads .env in create_app(), so skip Flask's cwd-based dotenv lookup.
    app.run(host=host, port=port, debug=_env_bool("FLASK_DEBUG", False), load_dotenv=False)
