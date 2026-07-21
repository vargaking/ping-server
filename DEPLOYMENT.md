# Deploying ping-server

CI/CD: **GitHub Actions → SSH → Ubuntu server**, running the app via
**systemd + venv** behind **Nginx (HTTPS)**. Two environments on one server:

| Env | Branch | Directory | systemd unit | Port | DB |
|-----|--------|-----------|--------------|------|----|
| production | `master`  | `/opt/ping-server-production` | `ping-server@production` | 8000 | prod DB |
| staging    | `staging` | `/opt/ping-server-staging`    | `ping-server@staging`    | 8001 | **shares prod DB** |

## How it works

```
push to master  ──► GitHub Actions ──► SSH ──► deploy.sh (ENVIRONMENT=production)
push to staging ──► GitHub Actions ──► SSH ──► deploy.sh (ENVIRONMENT=staging)
                        │
                        ├─ test job:   install deps + compile-check (add pytest here)
                        └─ deploy job: git reset --hard origin/<branch>
                                        pip install -r requirements.txt
                                        aerich upgrade   (prod only by default)
                                        sudo systemctl restart ping-server@<env>
```

Each env is served by Gunicorn (Uvicorn workers) on `127.0.0.1:<port>`, managed
by a systemd **template** unit, with Nginx reverse-proxying and handling TLS +
the `/ws` WebSocket upgrade.

> ⚠️ **Staging shares the production database.** `deploy.sh` therefore
> **skips migrations on staging by default** (`RUN_MIGRATIONS=false`) so a WIP
> migration on the `staging` branch can't alter prod data. To run a migration
> from staging deliberately, trigger the workflow manually (Actions → Run
> workflow → check "Run migrations").

Files that make this up:

| File | Purpose |
|------|---------|
| `.github/workflows/deploy.yml` | Pipeline; maps branch → environment |
| `scripts/deploy.sh` | Server-side deploy, parameterized by `ENVIRONMENT` |
| `deploy/gunicorn.conf.py` | Gunicorn/Uvicorn config (`GUNICORN_BIND` from env) |
| `deploy/ping-server@.service` | systemd **template** unit (`@production` / `@staging`) |
| `deploy/nginx.conf` | Nginx reverse proxy (prod + staging, WebSocket-aware) |

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

A locked-down `ping` system user *runs* the app; the deploy user (the SSH login,
e.g. `dpkbeta`) *owns and deploys* the code.

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

`.env` is **not** in git. Create one in each env directory. The key difference
is `GUNICORN_BIND` (port) — and for staging, remember it points at the **same**
`DB_CONNECTION_STRING` as production.

```bash
# production: /opt/ping-server-production/.env  (GUNICORN_BIND=127.0.0.1:8000)
# staging:    /opt/ping-server-staging/.env     (GUNICORN_BIND=127.0.0.1:8001)
sudo -u <deploy-user> tee /opt/ping-server-$ENV/.env >/dev/null <<EOF
DB_CONNECTION_STRING=postgres://user:pass@your-db-host:5432/ping
JWT_SECRET_KEY=<strong random secret>
DEV_IP=
MEDIA_SERVER_URL=https://media.example.com
GOOGLE_CLOUD_PROJECT=your-gcp-project
GOOGLE_STORAGE_BUCKET=your-bucket
GOOGLE_APPLICATION_CREDENTIALS=/opt/ping-server-$ENV/gcloud-key.json
GUNICORN_BIND=127.0.0.1:8000   # 8001 for staging
DEBUG=false
EOF
chmod 600 /opt/ping-server-$ENV/.env
```

Copy the GCS key into each env dir (`scp gcloud-key.json <deploy-user>@<server>:/opt/ping-server-$ENV/`).

> **CORS:** `app/app.py` hardcodes localhost/LAN origins plus `DEV_IP`. Add your
> real frontend origin(s) there for production/staging.

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

`deploy.sh` runs `sudo systemctl restart ping-server@<env>`. Grant just that:

```bash
sudo tee /etc/sudoers.d/ping-server >/dev/null <<'EOF'
<deploy-user> ALL=(root) NOPASSWD: /usr/bin/systemctl restart ping-server@production, /usr/bin/systemctl restart ping-server@staging, /usr/bin/systemctl is-active ping-server@production, /usr/bin/systemctl is-active ping-server@staging, /usr/bin/journalctl -u ping-server@production *, /usr/bin/journalctl -u ping-server@staging *
EOF
sudo chmod 440 /etc/sudoers.d/ping-server
sudo visudo -cf /etc/sudoers.d/ping-server   # validate
```

### 7. Nginx + HTTPS

```bash
sudo cp /opt/ping-server-production/deploy/nginx.conf /etc/nginx/sites-available/ping-server
# set your real domains:
sudo sed -i 's/api.example.com/YOUR_DOMAIN/; s/staging.api.example.com/STAGING_DOMAIN/' \
  /etc/nginx/sites-available/ping-server
sudo ln -sf /etc/nginx/sites-available/ping-server /etc/nginx/sites-enabled/ping-server
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d YOUR_DOMAIN -d STAGING_DOMAIN
```

---

## GitHub setup

### Deploy SSH key

The workflow SSHes in as the deploy user with a dedicated key. Its **public**
half must be in `~<deploy-user>/.ssh/authorized_keys` on the server.

### Repository secrets

Repo → **Settings → Secrets and variables → Actions**:

| Secret | Value |
|--------|-------|
| `DEPLOY_HOST` | server IP / hostname |
| `DEPLOY_USER` | the deploy user (e.g. `dpkbeta`) |
| `DEPLOY_SSH_KEY` | the **private** deploy key |
| `DEPLOY_SSH_PORT` | *(optional)* SSH port if not `22` |

---

## Deploying

- **Production:** push/merge to `master`.
- **Staging:** push to `staging`.
- **Manual (+ optional migration on staging):** Actions → *Deploy ping-server* →
  Run workflow → pick branch, optionally check **Run migrations**.

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
sudo journalctl -u ping-server@production -f      # live gunicorn/uvicorn logs
tail -f /opt/ping-server-production/logs/app.log  # app file logs
sudo systemctl restart ping-server@staging
```
