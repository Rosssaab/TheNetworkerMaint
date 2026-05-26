#!/usr/bin/env bash
# Run on the staging server after code is at origin/staging (TheNetworkerMaint repo).
set -euo pipefail

APP_DIR="${TNW_STAGING_APP_DIR:-/home/ubuntu/PythonRoot/maint}"
SERVICE="${TNW_MAINT_SERVICE:-the-networker-maint}"

cd "$APP_DIR"

mkdir -p app/static/meeting_group_images app/static/event_images app/static/user_images

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

if [ ! -f "$APP_DIR/wsgi.py" ]; then
  echo "ERROR: missing wsgi.py in $APP_DIR"
  exit 1
fi

if ! systemctl cat "$SERVICE" &>/dev/null; then
  echo "==> $SERVICE not installed; running deploy/install-systemd-service.sh"
  TNW_STAGING_APP_DIR="$APP_DIR" bash deploy/install-systemd-service.sh
fi

echo "==> restart $SERVICE"
if ! sudo -n systemctl stop "$SERVICE" 2>/dev/null; then
  echo "WARN: could not stop $SERVICE (may not be running yet)"
fi
# Orphan gunicorn/python processes can keep :8104 after a failed restart, so systemd
# reports success while the site still runs stale code.
if command -v fuser >/dev/null 2>&1; then
  sudo -n fuser -k 8104/tcp >/dev/null 2>&1 || true
elif command -v ss >/dev/null 2>&1; then
  orphan_pids=$(ss -tlnp 2>/dev/null | awk '/127.0.0.1:8104/ { while (match($0, /pid=[0-9]+/)) { print substr($0, RSTART+4, RLENGTH-4); $0=substr($0, RSTART+RLENGTH) } }' | sort -u)
  if [ -n "${orphan_pids:-}" ]; then
    echo "WARN: killing orphan listener(s) on :8104: $orphan_pids"
    kill $orphan_pids 2>/dev/null || sudo -n kill $orphan_pids 2>/dev/null || true
  fi
fi
sleep 1
if ! sudo -n systemctl start "$SERVICE"; then
  echo "ERROR: passwordless sudo required for: systemctl start $SERVICE"
  echo "  One-time on VPS: cd $APP_DIR && bash deploy/install-systemd-service.sh"
  echo "  Or add sudoers, e.g.:"
  echo "  ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart the-networker-maint, /bin/systemctl start the-networker-maint, /bin/systemctl stop the-networker-maint, /bin/systemctl is-active the-networker-maint, /usr/bin/fuser"
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
