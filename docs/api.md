# Jolof REST API (v1)

Read-only access to recipes, tags and the meal planner, under `/api/v1/`.

The reference lives in the app: **API Reference** in the user menu
(`/api-reference/`, template `main/templates/main/api_reference.html`), for
anyone whose role has an API permission. It covers logging in, every endpoint,
the recipe filters, sorts and paging, and examples. The generated Swagger docs
are at `/api/v1/docs/`, and the OpenAPI schema at `/api/v1/schema/`.

When an endpoint, filter or sort changes, update the API Reference page with
it. `ApiReferenceTests` fails if an endpoint or sort key is missing from the
page.

## Code map

| Path | What it does |
|---|---|
| `main/api/` | Views, serializers, auth, permissions, paging, error shape. |
| `main/api/authentication.py` | Session tokens: login, hashing, idle expiry, password-change revocation. |
| `main/recipe_query.py` | Recipe search, filtering and sorting, shared with the recipes page. |
| `main/planner.py` | Household or personal planner scoping, shared with the planner pages. |
| `ApiSession`, `ApiSettings` in `main/models.py` | The session tokens and the idle timeout setting. |
| `main/templates/main/api_reference.html` | The in-app API Reference page. |
