# ping-server

Backend for **ping**, a real-time chat app: a FastAPI service with a WebSocket
gateway for messaging and presence, Tortoise ORM over PostgreSQL, cookie-based
sessions, and LiveKit for voice.

- **HTTP API** — auth, users, servers, channels, invites, voice tokens.
- **WebSocket `/ws`** — chat delivery and presence; identity comes from the
  session cookie, established at the handshake (never from frame contents).
- **Storage** — uploads (avatars, server icons) are written to local disk and
  served by the app's StaticFiles mount (or Nginx in production).

## Requirements

- Python 3.13
- PostgreSQL (production/staging). The test suite uses in-memory SQLite.

## Running locally

```bash
python3.13 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt      # app deps + pytest

# Minimal .env (see "Environment variables" below)
export DB_CONNECTION_STRING="postgres://user:pass@localhost:5432/ping"
export ALLOWED_ORIGINS="http://localhost:5173"
export DEBUG=true

# Apply migrations to your database…
aerich upgrade
# …or, for a throwaway/scratch DB, let the app build the schema from the models:
export DB_GENERATE_SCHEMAS=true

uvicorn app.app:app --reload --port 8000
```

The interactive API docs are then at http://localhost:8000/docs.

## Environment variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `DB_CONNECTION_STRING` | yes | — | Tortoise DB URL (e.g. `postgres://…`, or `sqlite://:memory:`). |
| `ALLOWED_ORIGINS` | yes (prod) | *(empty)* | Comma-separated browser origins allowed for CORS **and** the `/ws` handshake. No hosts are hardcoded. |
| `DEBUG` | no | `false` | `true` enables FastAPI debug and non-secure (`SameSite=Lax`) cookies for http:// dev. |
| `DB_GENERATE_SCHEMAS` | no | `false` | `true` builds the schema from the models on startup. For throwaway DBs only — production owns its schema via Aerich. |
| `MEDIA_ROOT` | no | `media` | Directory uploads are written to. |
| `MEDIA_BASE_URL` | no | `/media` | Public URL prefix mapping to `MEDIA_ROOT`. |
| `MEDIA_MOUNT_PATH` | no | `/media` | Path the app serves `MEDIA_ROOT` at via StaticFiles. |
| `MAX_UPLOAD_BYTES` | no | `5242880` | Max accepted image upload size (5 MB). |
| `AUTH_RATE_LIMIT` | no | `10/minute` | Per-IP limit on `/auth/login` and `/auth/register`. |
| `INVITE_USE_RATE_LIMIT` | no | `20/minute` | Per-IP + per-invite limit on `POST /invites/{id}/use`. |
| `LIVEKIT_*` | for voice | — | LiveKit API host/key/secret used to mint voice tokens. |

> Session tokens expire 30 days after login/register; expired tokens are
> rejected and deleted on next use, and pruned on startup.

## Database migrations

Migrations are managed with [Aerich](https://github.com/tortoise/aerich) and
live in `migrations/`. Production is PostgreSQL — **every model change needs a
migration committed alongside it.**

```bash
aerich migrate --name <change>   # generate a migration from model changes
aerich upgrade                   # apply pending migrations
aerich downgrade                 # roll back the last migration
```

On deploy to `master`, `scripts/deploy.sh` runs `aerich upgrade` automatically
(see [DEPLOYMENT.md](DEPLOYMENT.md)). New columns will 500 the app until the
migration is applied, so never ship a model change without its migration.

## Running tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests spin up the app against a fresh in-memory SQLite database per run
(`DB_GENERATE_SCHEMAS=true`) and drive it through the real HTTP/WebSocket API,
so they double as contract tests for the frontend.

## Deployment

Production and staging run under systemd + Gunicorn (single Uvicorn worker)
behind Nginx, deployed by a self-hosted GitHub Actions runner. See
[DEPLOYMENT.md](DEPLOYMENT.md) for the full setup.

## License

See [LICENSE](LICENSE).
