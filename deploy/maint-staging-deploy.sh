#!/usr/bin/env bash
# Run on the staging server after code is at origin/staging (TheNetworkerMaint repo).
set -euo pipefail

APP_DIR="${TNW_STAGING_APP_DIR:-/home/ubuntu/PythonRoot/TheNetworkerDevMaint}"
SERVICE="${TNW_MAINT_SERVICE:-the-networker-maint}"

cd "$APP_DIR"

if grep -q $'\r' deploy/maint-staging-deploy.sh 2>/dev/null; then
  sed -i 's/\r$//' deploy/*.sh 2>/dev/null || true
fi

if [ "${TNW_DEPLOY_REEXEC:-}" != "1" ]; then
  export TNW_DEPLOY_REEXEC=1
  if [ -d .git ]; then
    echo "==> git fetch + reset (refresh code and deploy script)"
    GIT_TERMINAL_PROMPT=0 git fetch origin staging
    git reset --hard origin/staging
  else
    echo "==> no .git (archive deploy from PC); continuing"
  fi
  exec bash deploy/maint-staging-deploy.sh
fi

echo "==> pip install (--user)"
python3 -m pip install --user --break-system-packages -r requirements.txt
python3 -m pip install --user --break-system-packages gunicorn

echo "==> restart $SERVICE"
if ! sudo -n systemctl restart "$SERVICE"; then
  echo "ERROR: passwordless sudo required for: systemctl restart $SERVICE"
  exit 1
fi

if ! sudo -n systemctl is-active --quiet "$SERVICE"; then
  echo "ERROR: $SERVICE is not active after restart"
  sudo systemctl status "$SERVICE" --no-pager || true
  exit 1
fi

if [ -d .git ]; then
  echo "Deploy OK: $(git log -1 --oneline)"
else
  echo "Deploy OK (archive sync)"
fi
