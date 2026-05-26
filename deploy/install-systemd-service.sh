#!/usr/bin/env bash
# Install and start the-networker-maint.service (run from repo root on the VPS).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
APP_DIR="${TNW_STAGING_APP_DIR:-/home/ubuntu/PythonRoot/maint}"
UNIT=the-networker-maint.service
DEST="/etc/systemd/system/$UNIT"

echo "Installing $UNIT (WorkingDirectory=$APP_DIR)"
sudo cp "$ROOT/deploy/$UNIT" "$DEST"
sudo sed -i "s|^WorkingDirectory=.*|WorkingDirectory=$APP_DIR|" "$DEST"
sudo systemctl daemon-reload
sudo systemctl enable --now "$UNIT"
sudo systemctl status "$UNIT" --no-pager || true
echo "Done. Logs: journalctl -u $UNIT -f"
