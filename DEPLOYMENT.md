# Deploying ping-server

CI/CD: **GitHub Actions → self-hosted runner on the server**, running the app
via **systemd + venv** behind **Nginx (HTTPS)**. Two environments on one server:

| Env | Branch | Directory | systemd unit | Port | DB |
|-----|--------|-----------|--------------|------|----|
| production | `master`  | `/opt/ping-server-production` | `ping-server@production` | 8000 | prod DB |
| staging    | `staging` | `/opt/ping-server-staging`    | `ping-server@staging`    | 8001 | **shares prod DB** |

> **To be confirmed:** that `staging` is the branch in use and that staging
> still shares the production database. The workflow and `deploy.sh` are
> written on both assumptions.

## How it works

```
push to master    ──► Test ──► Deploy ──► deploy.sh (ENVIRONMENT=production)
push to staging   ──► Test ──► Deploy ──► deploy.sh (ENVIRONMENT=staging)
manual run        ──► Test ──► Deploy ──► master = production, any other ref = staging

Test    GitHub-hosted (ubuntu-latest, Python 3.13):
          pip install -r requirements-dev.txt, compileall, pytest
Deploy  self-hosted runner on the server (labels self-hosted, ping-server), runs locally:
          bash /opt/ping-server-<env>/scripts/deploy.sh
            git fetch, git reset --hard origin/<branch>
            pip install -r requirements.txt
            aerich upgrade   (production always; staging only if asked, see below)
            sudo systemctl restart ping-server@<env>, then check the unit is active
```

Deploy waits for Test. Deploys are serialized per branch and never cancelled
midway. The runner is on the server, so nothing connects in from GitHub.
`deploy.sh` runs from the environment's own checkout, before that deploy's
`git reset`: a change to the script takes effect on the deploy after the one
that pulls it.

Each env is served by Gunicorn (Uvicorn workers) on `127.0.0.1:<port>`, managed
by a systemd **template** unit, with Nginx reverse-proxying and handling TLS +
the `/ws` WebSocket upgrade.

> ⚠️ **Single Gunicorn worker required.** `deploy/gunicorn.conf.py`
> pins `workers = 1` (overridable via `GUNICORN_WORKERS`, but leave it at 1).
> Live WebSocket connections are tracked in an in-process `ConnectionManager`
> dict that is **not** shared across workers, so with >1 worker a message or
> presence update only reaches users on the sender's worker — delivery silently
> breaks ("refresh to see it") while messages still persist. Scaling past one
> worker is safe only once a cross-worker Redis pub/sub backplane lands
> (tracked separately).

> ⚠️ **Staging shares the production database.** `deploy.sh` therefore
> **skips migrations on staging by default** (`RUN_MIGRATIONS=false`) so a WIP
> migration on the `staging` branch can't alter prod data. To run a migration
> from staging deliberately, trigger the workflow manually (Actions → Run
> workflow → check "Run migrations"; the input defaults to off). Production
> always migrates, including on a manual run.

Files that make this up:

| File | Purpose |
|------|---------|
| `.github/workflows/deploy.yml` | Pipeline: Test, then Deploy on the self-hosted runner; maps branch → environment |
| `scripts/deploy.sh` | Server-side deploy, parameterized by `ENVIRONMENT` |
| `deploy/gunicorn.conf.py` | Gunicorn/Uvicorn config (`GUNICORN_BIND` from env) |
| `deploy/ping-server@.service` | systemd **template** unit (`@production` / `@staging`) |
| `deploy/nginx.conf` | Nginx reverse proxy (prod + staging API and LiveKit WSS, WebSocket-aware) |
| `deploy/cloudflare/worker.js` | Cloudflare Worker in front of the API, see [Behind the Cloudflare Worker](#behind-the-cloudflare-worker) |

---

## One-time server setup

Ubuntu 24.04. Run once. `<env>` is `production` or `staging` — repeat the
per-environment steps for each.

### 1. Packages

```bash
sudo apt update
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.13 python3.13-venv python3.13-dev git nginx
```

### 2. Service user

A locked-down `ping` system user *runs* the app; the deploy user (the account the
runner runs as, see [Runner and secrets](#runner-and-secrets)) *owns and deploys*
the code.

```bash
sudo useradd --system --shell /usr/sbin/nologin --home-dir /opt/ping-server-production ping
sudo usermod -aG ping <deploy-user>   # so the deploy user shares the group
```

### 3. Per-environment directory, clone, venv  (repeat for each env)

```bash
ENV=production   # then repeat with ENV=staging
DIR=/opt/ping-server-$ENV

sudo mkdir -p "$DIR"
sudo chown <deploy-user>:ping "$DIR"
sudo chmod 2775 "$DIR"                 # setgid: new files inherit the ping group

git clone https://github.com/vargaking/ping-server.git "$DIR"
cd "$DIR"
# staging tracks the staging branch:
[ "$ENV" = staging ] && git checkout staging

python3.13 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

mkdir -p logs
sudo chgrp -R ping "$DIR" && sudo chmod -R g+rwX "$DIR"
```

### 4. `.env` per environment

`.env` is **not** in git. Create one in each env directory.
[`.env.example`](.env.example) lists every variable; the block below is the
production minimum plus voice and push. The key difference
is `GUNICORN_BIND` (port) — and for staging, remember it points at the **same**
`DB_CONNECTION_STRING` as production.

```bash
# production: /opt/ping-server-production/.env  (GUNICORN_BIND=127.0.0.1:8000)
# staging:    /opt/ping-server-staging/.env     (GUNICORN_BIND=127.0.0.1:8001)
sudo -u <deploy-user> tee /opt/ping-server-$ENV/.env >/dev/null <<EOF
DB_CONNECTION_STRING=postgres://user:pass@your-db-host:5432/ping
# Comma-separated browser origins allowed for CORS + the WebSocket handshake.
ALLOWED_ORIGINS=https://dpkchat.vercel.app
# Regex for extra CORS + /ws origins that can't be enumerated (Vercel branch
# previews get randomized subdomains). Anchor it on the project prefix AND the
# team-slug suffix — the suffix is the security boundary; a bare .vercel.app
# pattern would let any Vercel app hit the API with credentials. Leave empty to
# disable. Example for our team:
ALLOWED_ORIGIN_REGEX=^https://ping-frontend-[a-z0-9-]+-vargakings-projects\.vercel\.app$
# Local-disk media storage (served by the app's StaticFiles mount, or Nginx).
MEDIA_ROOT=/opt/ping-server-$ENV/uploads
MEDIA_BASE_URL=/media
# Export zips uploaded for server imports, and their unpacked bundles. Never served.
# Put it on a volume with room for the largest export: IMPORT_MAX_BYTES (zip, default 5 GiB,
# 0 turns imports off) and IMPORT_MAX_UNPACKED_BYTES (default twice that). Uploads arrive in
# IMPORT_CHUNK_BYTES pieces (default 8 MiB), which must stay under nginx's client_max_body_size.
IMPORTS_ROOT=/opt/ping-server-$ENV/imports
# Signs short-lived attachment download links. Use a different value per env.
# Generate: python -c "import secrets; print(secrets.token_urlsafe(32))"
ATTACHMENT_URL_KEY=<generated secret, 32+ bytes>
# Voice (LiveKit). Voice is off unless all three are set.
LIVEKIT_URL=wss://livekit.example.com
LIVEKIT_API_KEY=<livekit api key>
LIVEKIT_API_SECRET=<livekit api secret>
# The server calls LiveKit's API here directly, not through Nginx (7882 production, 7880 staging).
LIVEKIT_API_URL=http://127.0.0.1:7882
# Web Push, see "Web Push (VAPID)" below. Push is off unless all three are set.
VAPID_PUBLIC_KEY=<generated>
VAPID_PRIVATE_KEY=<generated>
VAPID_SUBJECT=mailto:you@example.com
# Admin stats history (production only; staging shares the database).
STATS_SAMPLING=true
LOG_DIR=/opt/ping-server-$ENV/logs
GUNICORN_BIND=127.0.0.1:8000   # 8001 for staging
DEBUG=false
EOF
chmod 600 /opt/ping-server-$ENV/.env
```

> **CORS:** origins come entirely from `ALLOWED_ORIGINS` (comma-separated),
> read in `app/settings.py` — there are no hardcoded hosts. For origins you
> can't enumerate (Vercel branch previews), set `ALLOWED_ORIGIN_REGEX` in `.env`
> and always anchor it on the team slug (`-vargakings-projects.vercel.app`): a
> bare `.vercel.app` pattern would let anyone's deployment reach the API with
> credentials. The regex covers both CORS and the `/ws` handshake.

> **Behind the Cloudflare Worker:** set `TRUSTED_PROXY_KEY` in production's `.env` to the
> same value as the Worker's `PROXY_KEY` secret (`deploy/cloudflare/worker.js`), or every login shares one rate limit.

### Behind the Cloudflare Worker

In production the API sits behind a Cloudflare Worker (`deploy/cloudflare/worker.js`,
tests in `worker.test.mjs`). It forwards every request to the address in its
`ORIGIN` binding and adds `X-Zet-Client-IP` (the visitor's `CF-Connecting-IP`)
and `X-Zet-Proxy-Key` (its `PROXY_KEY` secret); it strips both from incoming
requests first. The server believes the client address only when the key matches
`TRUSTED_PROXY_KEY`. Without that, rate limits key on the connecting address,
so every client behind the Worker shares one limit.

> **To be confirmed:** where the Worker runs (account, route or domain), and
> whether it is also in front of staging.

### Web Push (VAPID)

Generate the keys once, from the env directory:

```bash
./venv/bin/python -m app.scripts.gen_vapid
```

Put all three `VAPID_*` lines in `.env` and keep them stable. **Rotating the
keys kills every existing push subscription**: users have to turn "Notify me
when Zeta is closed" off and on again. Details are in the README's
[Web Push](README.md#web-push) section. The keys were set on 30 Sep.

### 5. systemd (template unit)

Install the template once, then enable an instance per environment:

```bash
sudo cp /opt/ping-server-production/deploy/ping-server@.service \
        /etc/systemd/system/ping-server@.service
sudo systemctl daemon-reload
sudo systemctl enable --now ping-server@production
sudo systemctl enable --now ping-server@staging
sudo systemctl status ping-server@production
```

### 6. Let the deploy user restart the services without a password

`deploy.sh` runs `sudo systemctl restart ping-server@<env>`, then
`sudo systemctl is-active --quiet ping-server@<env>`, and on failure
`sudo journalctl -u ping-server@<env> -n 40 --no-pager`. Grant just those:

```bash
sudo tee /etc/sudoers.d/ping-server >/dev/null <<'EOF'
<deploy-user> ALL=(root) NOPASSWD: /usr/bin/systemctl restart ping-server@production, /usr/bin/systemctl restart ping-server@staging, /usr/bin/systemctl is-active --quiet ping-server@production, /usr/bin/systemctl is-active --quiet ping-server@staging, /usr/bin/journalctl -u ping-server@production *, /usr/bin/journalctl -u ping-server@staging *
EOF
sudo chmod 440 /etc/sudoers.d/ping-server
sudo visudo -cf /etc/sudoers.d/ping-server   # validate
```

> **To be confirmed:** that this is the rule on the box, and which user the
> runner runs as (`<deploy-user>` here).

### 7. Nginx + HTTPS

`deploy/nginx.conf` terminates TLS with one certificate and key pair,
`/etc/ssl/ping/ping.crt` and `ping.key` (its header calls it self-signed, for VPN
and IP access). Every server block uses `server_name _`, so there are no domains
to fill in. Nginx fails `nginx -t` until the pair exists.

```bash
sudo cp /opt/ping-server-production/deploy/nginx.conf /etc/nginx/sites-available/ping-server
sudo ln -sf /etc/nginx/sites-available/ping-server /etc/nginx/sites-enabled/ping-server
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

What it listens on:

| Port | Does |
|------|------|
| 443 | production API (`127.0.0.1:8000`) |
| 8443 | staging API (`127.0.0.1:8001`) |
| 7444 | production LiveKit WSS (`127.0.0.1:7882`) |
| 7443 | staging LiveKit WSS (`127.0.0.1:7880`) |
| 80 | redirect to HTTPS |
| 8080 | redirect to HTTPS on 8443 (staging) |

> **To be confirmed:** which TLS setup production and staging really use:
> the self-signed pair above behind the Worker, Cloudflare in front with an
> origin certificate, or Let's Encrypt on the box (an earlier version of this
> guide used certbot with real domains; nothing in the repo configures it).
> Also how the pair at `/etc/ssl/ping/` was made.

> **To be confirmed:** that all six ports are in use, and what each is for
> beyond the comments in `nginx.conf`.

---

## Runner and secrets

`deploy.yml` needs:

- A self-hosted runner on this server with the label `ping-server`. The Deploy
  job is `runs-on: [self-hosted, ping-server]`.
- The two checkouts at `/opt/ping-server-production` and
  `/opt/ping-server-staging`, each with its `venv` and `.env` (steps 3 and 4).
  The paths are hardcoded in the workflow.
- A runner user that can write to both checkouts and run the `sudo systemctl`
  commands from step 6.

The workflow uses no repository secrets and no deploy key. Any left from the
earlier setup can be removed.

> **To be confirmed:** how the runner was installed (the user it runs as, its
> labels, and whether one runner serves both environments).

---

## Deploying

- **Production:** push/merge to `master`.
- **Staging:** push to `staging`.
- **Manual (+ optional migration on staging):** Actions → *Deploy ping-server* →
  Run workflow → pick branch, optionally check **Run migrations**. `master`
  deploys production and always migrates. Any other branch deploys staging,
  which resets to `origin/staging` whatever branch you picked.

## Frontend (Vercel)

The frontend deploys from ping-frontend's `master` on Vercel.
`adapter-auto` resolves to `adapter-vercel`, which is enough for the one SSR
route (`/invite/[code]`, used for link previews). If the frontend is ever
served from the homelab instead, it needs `adapter-node`.

Vercel env vars:

- `PUBLIC_BASE_URL` and `PUBLIC_WS_URL`: where the browser reaches the API.
- `API_URL`: the absolute, publicly reachable API base that the invite-preview
  server load fetches from (once the invite-preview PR is merged). Never a
  homelab-internal host: the load runs on Vercel.

## Merge and deploy order

WS/API contract changes ship server first, frontend second. Frontend merges
auto-deploy on Vercel, so merge order is deploy order. Migrations run on the
production deploy via `aerich upgrade`; staging skips them by default (see the
staging warning under [How it works](#how-it-works)).

## Rollback

```bash
cd /opt/ping-server-production
git reset --hard <good-sha>
./venv/bin/pip install -r requirements.txt
sudo systemctl restart ping-server@production
```
> `git reset` does not undo an applied DB migration. If a migration is bad, run
> `aerich downgrade` before reverting code.

## Operating

```bash
sudo systemctl status ping-server@production
sudo journalctl -u ping-server@production -f      # live server and app logs
tail -f /opt/ping-server-production/logs/app.log  # same app logs, in a file
# lines look like: <time> <LEVEL> [logger] [request_id] message
sudo systemctl restart ping-server@staging
```

Client crash reports (`POST /api/client-errors`) are logged under the
`app.client` logger into the same app log (`logs/app.log` and journald). To
find them: `grep app.client /opt/ping-server-production/logs/app.log`. There is
no external service.

## Alpha checklist

To be ticked by hand on the real box. Nothing here has been checked yet.

- [ ] Single Gunicorn worker: `GUNICORN_WORKERS` unset or `1`. Check: `ps -ef | grep gunicorn` shows one worker under the master.
- [ ] `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` set; voice joins work.
- [ ] The Voice card on /admin shows numbers, not "not configured" or "Can't reach LiveKit".
- [ ] `STATS_SAMPLING=true` on production only. Check: after a few minutes the history charts on /admin show points.
- [ ] TURN reachable from a typical home network **and** from a phone on mobile data (not just home Wi-Fi): audio both ways.
- [ ] HTTPS on API and client, `DEBUG=false`, and the session cookie arrives as `SameSite=None; Secure`. Check in a real browser: devtools, Application, Cookies.
- [ ] `ALLOWED_ORIGINS` / `ALLOWED_ORIGIN_REGEX` from env cover the prod frontend (and previews if wanted). A WS connect from the prod origin succeeds.
- [ ] VAPID keys set and stable; the push toggle shows in the app; a DM to a closed tab arrives.
- [ ] `ATTACHMENT_URL_KEY` set (32+ bytes). Check: open an attachment link, restart the service, request a fresh link; no `ATTACHMENT_URL_KEY unset` warning in the log.
- [ ] Nightly Postgres backup, plus a backup of `MEDIA_ROOT` and `ATTACHMENTS_ROOT`. Sample root crontab (placeholder paths):

  ```cron
  15 3 * * * pg_dump -Fc "$DB_URL" > /var/backups/ping/db-$(date +\%F).dump
  30 3 * * * tar -czf /var/backups/ping/files-$(date +\%F).tgz /opt/ping-server-production/uploads /opt/ping-server-production/attachments
  ```

  Delete old dumps on a schedule too, and copy them off the box.
- [ ] Restore test: last night's dump loads into a scratch DB and has yesterday's messages.

  ```bash
  createdb ping_restore_test
  pg_restore -d ping_restore_test /var/backups/ping/db-<date>.dump
  psql ping_restore_test -c "select count(*) from messages where created_at > now() - interval '1 day'"
  dropdb ping_restore_test
  ```

- [ ] Crash reports land in the app log: trigger one from the browser console, then `grep app.client` the log.
- [ ] Frontend: Vercel env (`PUBLIC_*`, `API_URL`) set. Check: `curl -s https://<frontend>/invite/<code>/ | grep og:` shows tags.
- [ ] Branch protection on `master` and `staging` in both repos (require PR, require CI). `deploy.yml` runs only on push to `master`/`staging` and `workflow_dispatch`, never `pull_request`; fork-PR workflows need approval.
- [ ] `.env.example` in both repos matches what prod actually sets.

Smoke test, from a fresh browser on another network:

- [ ] Register a new account.
- [ ] Join a server via an invite link.
- [ ] Send a message.
- [ ] Join voice with a second person on mobile data.

Checked on: ____ by ____
