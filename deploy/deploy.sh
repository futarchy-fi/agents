#!/usr/bin/env bash
# deploy.sh — pull latest main and restart the API.
#
# Uses flock to prevent concurrent runs.  If a second push arrives
# while this script is running, it exits immediately (the first
# invocation already fetched the latest commit).
set -euo pipefail

REPO_DIR="/opt/futarchy/agents"
LOCK_FILE="/tmp/futarchy-deploy.lock"
VENV_DIR="${REPO_DIR}/.venv"

exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    echo "Deploy already in progress, skipping."
    exit 0
fi

cd "$REPO_DIR"

# Capture current requirements hash before pull.
OLD_REQ_HASH=""
if [ -f requirements.txt ]; then
    OLD_REQ_HASH=$(sha256sum requirements.txt | cut -d' ' -f1)
fi

# Hard reset to origin/main — server should never have local changes.
git fetch origin main
git reset --hard origin/main

# Reinstall Python deps only if requirements.txt changed.
NEW_REQ_HASH=""
if [ -f requirements.txt ]; then
    NEW_REQ_HASH=$(sha256sum requirements.txt | cut -d' ' -f1)
fi

if [ "$OLD_REQ_HASH" != "$NEW_REQ_HASH" ]; then
    echo "requirements.txt changed, reinstalling dependencies..."
    if [ ! -d "$VENV_DIR" ]; then
        python3 -m venv "$VENV_DIR"
    fi
    "$VENV_DIR/bin/pip" install -r requirements.txt
fi

sudo /usr/bin/systemctl restart futarchy.service

echo "Deploy complete at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
