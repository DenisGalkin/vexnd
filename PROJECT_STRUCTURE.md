# Project structure

```text
.
├── Dockerfile                  # container image for web and bot services
├── docker-compose.yml          # local/production service wiring
├── requirements.txt            # Python dependencies
├── deploy/
│   └── caddy/                  # reverse-proxy configuration
├── bot.py                      # Telegram bot entrypoint
├── web.py                      # Flask/Gunicorn entrypoint
│   ├── web.py                  # Flask/Gunicorn app object
│   └── bot.py                  # Telegram bot runner
└── app/                        # main application package
    ├── bot/                    # Telegram bot runtime, handlers, keyboards, content, bot DB models
    ├── core/                   # app-wide config, extensions, translations/i18n data
    ├── domain/                 # database models and plan/catalog rules
    ├── http/                   # Flask-facing code
    │   ├── routes/             # page/API/auth/dashboard/payment route registration
    │   ├── security/           # webhook/payment locking helpers
    │   └── helpers.py          # request hooks, locale helpers, public URLs, SEO files
    ├── services/               # business logic and external service orchestration
    │   └── payments/           # payment provider adapters
    └── ui/                     # Flask-owned frontend files
        ├── static/             # CSS/images
        └── templates/          # Jinja templates and legal source snippets
```

## What changed

- Renamed the main package from `vexnd_app` to `app`.
- Split the old flat package into `core`, `domain`, `http`, `services`, `bot`, and `ui` areas.
- Moved Flask templates/static files from `web_assets` to `app/ui`.
- Moved route modules into `app/http/routes` and webhook helpers into `app/http/security`.
- Moved config/extensions/translations into `app/core`.
- Moved database models and plan/catalog helpers into `app/domain`.
- Updated imports, Flask asset/template paths, entrypoints, Docker-related references, and docs.
- Removed generated caches and archive metadata.
```
