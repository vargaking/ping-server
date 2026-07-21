#!/usr/bin/env bash
#
# Server-side deploy script for ping-server.
# Executed on the remote Ubuntu server by the GitHub Actions workflow
# (piped in over SSH). Assumes the one-time setup in DEPLOYMENT.md is done:
#   - repo cloned at $APP_DIR
#   - venv created at $APP_DIR/venv
#   - .env present at $APP_DIR/.env (NOT in git)
#   - systemd unit $SERVICE installed and enabled
#
# Environment is selected via the ENVIRONMENT variable ("production" | "staging"),
# which the workflow exports before invoking this script. Everything else derives
# from it, with per-var overrides available.
#
set -euo pipefail

ENVIRONMENT="${ENVIRONMENT:-production}"

case "$ENVIRONMENT" in
  production)
    APP_DIR="${APP_DIR:-/opt/ping-server-production}"
    BRANCH="${BRANCH:-master}"
    SERVICE="${SERVICE:-ping-server@production}"
    # Production always applies pending migrations.
    RUN_MIGRATIONS="${RUN_MIGRATIONS:-true}"
    ;;
  staging)
    APP_DIR="${APP_DIR:-/opt/ping-server-staging}"
    BRANCH="${BRANCH:-staging}"
    SERVICE="${SERVICE:-ping-server@staging}"
    # Staging SHARES the production database, so migrations are OFF by default:
    # a WIP migration on the staging branch must not silently alter prod data.
    # Opt in for a given deploy by setting RUN_MIGRATIONS=true in the workflow.
    RUN_MIGRATIONS="${RUN_MIGRATIONS:-false}"
    ;;
  *)
    echo "!! Unknown ENVIRONMENT: $ENVIRONMENT (expected 'production' or 'staging')"
    exit 1
    ;;
esac

echo "==> Deploying ping-server [$ENVIRONMENT] to $APP_DIR (branch: $BRANCH)"
cd "$APP_DIR"

echo "==> Fetching latest code"
git fetch --all --prune
git reset --hard "origin/$BRANCH"

echo "==> Activating venv"
# shellcheck disable=SC1091
source venv/bin/activate

echo "==> Installing dependencies"
pip install --upgrade pip
pip install -r requirements.txt

if [ "$RUN_MIGRATIONS" = "true" ]; then
  echo "==> Running database migrations (aerich upgrade)"
  aerich upgrade
else
  echo "==> Skipping migrations (RUN_MIGRATIONS=false for $ENVIRONMENT)"
fi

echo "==> Restarting service"
# Requires a sudoers rule allowing the deploy user to restart this unit
# without a password (see DEPLOYMENT.md).
sudo systemctl restart "$SERVICE"

echo "==> Waiting for service to become active"
sleep 2
if sudo systemctl is-active --quiet "$SERVICE"; then
  echo "==> $SERVICE is active"
else
  echo "!! $SERVICE failed to start; recent logs:"
  sudo journalctl -u "$SERVICE" -n 40 --no-pager
  exit 1
fi

echo "==> Deploy complete [$ENVIRONMENT]"
