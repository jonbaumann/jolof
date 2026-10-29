# Jolof

A Django 5.2 recipe manager and meal planner. One app, `main`, plus a read-only
REST API in `main/api/`. Postgres for data, S3 for media and static files,
htmx and Bootstrap 5 for the front end.

## Running things

- Tests: `python manage.py test main`. They need a Postgres database you can
  create, set up through `.env` (see `.env.example`).
- `manage.py` uses `jolof.settings.development` unless `DJANGO_SETTINGS_MODULE`
  says otherwise. `wsgi.py` and `asgi.py` default to production.

## Conventions

- **Every view is guarded** with `@require(<permission>)` from
  `main/permissions.py`, which includes the login check. The permission codes
  and system roles are defined there. Only the public share page
  (`recipe_public`) is open.
- **Releases:** bump `VERSION` in `jolof/settings/base.py` and add a matching
  section at the top of `CHANGELOG.md`, which feeds the in-app What's New
  popup. Bullets are user-facing: feature name plus one sentence.
- **Audit log:** models whose changes are recorded are listed in `REGISTRY` in
  `main/audit.py`. Add new models there if their changes matter.
- **Planner scoping:** use `main/planner.py` (`owner_filter`) to decide whose
  meal plan, workspace and freezer rows a user sees. Never filter by user directly.
- **Fetching user-supplied URLs:** use `main/safe_fetch.get`, never
  `requests.get`, so private and local addresses are refused.
- **API changes:** update the in-app API Reference page
  (`main/templates/main/api_reference.html`). `ApiReferenceTests` fails if an
  endpoint or sort key is missing.

## Docs

- `docs/api.md`: the REST API's design and code map.
- `docs/households.md`: shared planners.
- `deployment/DEPLOY.md`: the example single-host Docker deploy.
