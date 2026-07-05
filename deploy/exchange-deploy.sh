#!/usr/bin/env bash
# exchange-deploy.sh — pull latest exchange-v2 and restart the exchange API.
#
# Driven by deploy/webhook.py (DEPLOY_SCRIPT) on the futarchy-exchange VM.
# Runs as user `futarchy`. The only privileged action is restarting the
# service, granted via a narrow NOPASSWD sudoers rule
# (/etc/sudoers.d/futarchy-exchange).
#
# Isolated from the bayes deployment: this touches ONLY branch exchange-v2
# and futarchy-exchange.service. It does not go near main / futarchy.service.
#
# flock prevents concurrent runs. After the reset it re-execs the fresh
# copy of itself so new deploy logic always runs.
set -euo pipefail

REPO_DIR="/opt/futarchy/agents"
BRANCH="exchange-v2"
LOCK_FILE="/tmp/futarchy-exchange-deploy.lock"
VENV_DIR="${REPO_DIR}/.venv"
SERVICE="futarchy-exchange.service"

exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    echo "Deploy already in progress, skipping."
    exit 0
fi

cd "$REPO_DIR"

OLD_REQ_HASH=""
[ -f requirements.txt ] && OLD_REQ_HASH=$(sha256sum requirements.txt | cut -d' ' -f1)

git fetch origin "$BRANCH"
git reset --hard "origin/${BRANCH}"

NEW_REQ_HASH=""
[ -f requirements.txt ] && NEW_REQ_HASH=$(sha256sum requirements.txt | cut -d' ' -f1)

if [ "$OLD_REQ_HASH" != "$NEW_REQ_HASH" ]; then
    echo "requirements.txt changed, reinstalling dependencies..."
    [ -d "$VENV_DIR" ] || python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install -r requirements.txt
fi

sudo /usr/bin/systemctl restart "$SERVICE"

# Give uvicorn a moment, then health-check. Fail loud if it didn't come up.
sleep 3
if curl -sf http://127.0.0.1:8000/v1/health >/dev/null; then
    echo "Deploy complete + healthy at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
else
    echo "Deploy restarted service but health check FAILED" >&2
    exit 1
fi
