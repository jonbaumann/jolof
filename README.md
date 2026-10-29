# Jolof

A self-hosted recipe manager and meal planner for a household or a small group
of people. Built with Django, Postgres, htmx and Bootstrap.

## Features

- **Recipes:** ingredients, instructions, photos, ratings (complexity, effort,
  health), tags, dietary flags, favorites and a "cooked" marker. Search,
  filter and sort in a card or list view.
- **Import:** from a recipe web page (one URL or a batch), or from CSV with
  column mapping. **Export** to CSV.
- **Meal planner:** a calendar with drag and drop, a workspace of recipes to
  plan, a freezer list, and live updates when someone else edits the plan.
- **Households:** people who share one planner, workspace and freezer.
- **Sharing:** public, read-only links to individual recipes.
- **Users and roles:** in-app user management, built-in and custom roles, and
  an audit log of changes.
- **REST API:** read-only access to recipes, tags and the planner, with an
  in-app reference and Swagger docs.
- **Tools:** find duplicate recipes, manage tags, scrape missing photos.
- Light and dark themes, and per-user time zones.

## Requirements

- Python 3.12 (3.10 or newer)
- PostgreSQL (16 is what it's run on)
- **Amazon S3**: two buckets in one region. Recipe photos go in a *media*
  bucket, which can stay private (the app hands out signed URLs). CSS,
  JavaScript and images go in a *static* bucket, which browsers must be able
  to read (public read access, or a CDN in front). The app has no local-disk
  storage option at the moment.

## Configuration

All configuration comes from environment variables, read from `.env` in the
project root when present. Copy `.env.example` to `.env` and fill it in.

| Variable | |
|---|---|
| `SECRET_KEY` | A long random string. Keep it secret. |
| `DJANGO_SETTINGS_MODULE` | `jolof.settings.development` or `jolof.settings.production` |
| `ALLOWED_HOSTS` | Comma-separated hostnames the site is served on. Required in production. |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | Postgres connection. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_S3_REGION_NAME` | S3 credentials and region. |
| `AWS_MEDIA_BUCKET_NAME`, `AWS_STATIC_BUCKET_NAME` | The two buckets. |

Write values without quotes. Docker's `--env-file` keeps quotes as part of the
value.

## Running it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # then edit it
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
python manage.py runserver
```

Then open http://localhost:8000. The first account (or any made with
`createsuperuser`) is an Administrator; add everyone else from **Users** in the
gear menu. Run the tests with `python manage.py test main`.

## Running with Docker Compose

`docker-compose.yml` runs the app (under gunicorn) and Postgres. Set
`DJANGO_SETTINGS_MODULE=jolof.settings.production` and `ALLOWED_HOSTS` in
`.env`, then:

```bash
docker compose up -d --build
docker compose exec app python manage.py migrate
docker compose exec app python manage.py collectstatic --noinput
docker compose exec app python manage.py createsuperuser
```

The app listens on `127.0.0.1:8000`.

For a single host with backups, health checks and rollback, there's also an
optional deploy script: see [deployment/DEPLOY.md](deployment/DEPLOY.md).

## Running in production

Jolof doesn't handle HTTPS itself. Put it behind a reverse proxy (nginx,
Caddy, Traefik, a load balancer…) that terminates TLS and forwards to port
8000. The app should never be reachable over plain HTTP from the internet.

What's left to you, depending on your setup:

- **Proxy headers.** Have the proxy pass the original `Host` header, and
  `X-Forwarded-Proto: https`.
- **HTTPS-aware settings.** Django needs to be told it's behind HTTPS for
  secure cookies and CSRF checks to work as intended. Add whatever your setup
  needs to `jolof/settings/production.py`. Typically:

  ```python
  SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
  SESSION_COOKIE_SECURE = True
  CSRF_COOKIE_SECURE = True
  CSRF_TRUSTED_ORIGINS = ['https://recipes.example.com']
  SECURE_HSTS_SECONDS = 31536000   # once you're sure HTTPS works everywhere
  ```

  `python manage.py check --deploy` lists anything still missing.
- **One app process.** Background jobs (URL and CSV imports, the duplicate
  finder) run in threads and keep their progress in memory, so run a single
  gunicorn worker with several threads, as the `Dockerfile` does. More workers
  would need a shared cache such as Redis.
- **Backups** of the Postgres database and the media bucket.

### Security notes

- Importing from a URL makes the server fetch that address. Private, loopback
  and link-local addresses are refused (`main/safe_fetch.py`), but anyone
  with recipe-write access can still make the server request public URLs.
- Django admin is available at `/admin/` for superusers. Consider restricting
  it at the proxy.

## Documentation

- [docs/api.md](docs/api.md): the REST API.
- [docs/households.md](docs/households.md): shared planners.
- [deployment/DEPLOY.md](deployment/DEPLOY.md): the example deploy script.
- [CHANGELOG.md](CHANGELOG.md): release notes, also shown in the app's What's
  New popup.

## Credits

Written by Jon Baumann, with early contributions to the recipe pages from
Molly Anderson. Since mid-2026, most new code has been written with Claude
Code (Anthropic's AI coding assistant) under Jon's direction and review.

## License

[MIT](LICENSE)
