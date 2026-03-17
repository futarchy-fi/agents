#!/usr/bin/env bash
# post-deploy.sh — runs AFTER git reset, so this is always the fresh version.
set -euo pipefail

cd /opt/futarchy/agents

# Install and start rollover timer if not already installed
if ! sudo systemctl is-active --quiet futarchy-rollover.timer 2>/dev/null; then
    echo "Installing futarchy-rollover timer..."
    sudo cp deploy/futarchy-rollover.service /etc/systemd/system/
    sudo cp deploy/futarchy-rollover.timer /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now futarchy-rollover.timer
fi

# Always update the service/timer files in case they changed
sudo cp deploy/futarchy-rollover.service /etc/systemd/system/
sudo cp deploy/futarchy-rollover.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Run one immediate rollover after deploy (background, non-blocking)
echo "Running immediate rollover..."
sudo systemctl start futarchy-rollover.service &

echo "Deploy complete at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
