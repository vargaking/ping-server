# Self-hosting with Docker

`compose.yaml` runs the whole backend on one machine:

| Service | What it does |
|---------|--------------|
| `api` | ping-server (one Gunicorn worker). Applies database migrations on start. |
| `db` | Postgres 16, data in the `db_data` volume. |
| `livekit` | LiveKit server for voice. |
| `proxy` | Caddy. Gets a TLS certificate for your domain and proxies API and WebSocket traffic to `api`. |

Uploads, attachments, imports and logs live in the `api_data` volume.

## Prerequisites

- A Linux host with Docker and the Compose plugin.
- A domain whose DNS points at the host.
- Open ports: 80/tcp and 443/tcp+udp (proxy), 7443/tcp (LiveKit signalling through the proxy), 7881/tcp and 7882/udp (LiveKit media).

## Bring it up

```bash
cp deploy/docker/.env.example .env
# fill in .env; generate each secret with: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
docker compose up -d --build
docker compose logs -f api        # wait for "Application startup complete"
```

Register an account in the web client, then make it a platform admin:

```bash
docker compose exec api python -m app.scripts.platform_admin <username> --grant
```

The web client is not part of this stack. Host it separately, add its origin to `ALLOWED_ORIGINS`, and build it against this server:

```
PUBLIC_BASE_URL=https://<DOMAIN>
PUBLIC_WS_URL=wss://<DOMAIN>/ws
```

## Voice

LiveKit media goes straight to the host on 7881/tcp and 7882/udp, not through Caddy. It detects the public IP on start. If the host sits behind NAT, forward those ports to it. Without them voice connects but carries no audio.

## Update

```bash
git pull
docker compose up -d --build
```

Migrations run when `api` starts. Set `RUN_MIGRATIONS=false` in `.env` to skip them.

## Backup

```bash
docker compose exec -T db pg_dump -U ping ping | gzip > ping-$(date +%F).sql.gz
```

Also back up the `api_data` volume if you care about uploads.

## Limits

The backend must run a single worker: live WebSocket connections are held in process memory. Do not raise `GUNICORN_WORKERS`.
