# Maint staging server setup (VPS)

`PushToMaintStaging.bat` pushes `origin/staging` to GitHub, then deploys over SSH to `STAGING_APP_DIR` (default `/home/ubuntu/PythonRoot/maint`).

## First deploy (no git on server)

If the app dir is not a git clone, deploy uses `git archive` from your PC. Preserves on the server if they already exist:

- `STAGING_APP_DIR/.env`
- `app/static/meeting_group_images/`, `event_images/`, `user_images/` (uploads are not in Git)

**nginx + static (avoid mixing paths):** point `/static/` at `maint/app/static/` (not the main site’s static tree). To share upload images with the public site, symlink the three upload folders inside `maint/app/static/` to the main app’s copies — see `deploy/nginx-maint.example.conf`. Pointing nginx only at the main site’s image folders makes images work but maint CSS/JS (e.g. `admin_console.css`) 404.

## Still required on a new box (one-time)

1. **`.env`** in the maint app dir (`DATABASE_URL`, `TNW_MAINT_LOGIN_*`, `SECRET_KEY`, etc.)
2. **Passwordless sudo** for `ubuntu` (cloud-init often grants `NOPASSWD:ALL`). Minimum for deploy:

   ```text
   ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart the-networker-maint, /bin/systemctl is-active the-networker-maint, /bin/systemctl enable the-networker-maint, /bin/systemctl daemon-reload
   ```

   First deploy also runs `deploy/install-systemd-service.sh`, which needs `sudo cp` to `/etc/systemd/system/`.

3. **Optional:** nginx vhost for admin subdomain → `127.0.0.1:8104`. If nginx serves `/static/` from the **main** site tree, maint-only files such as `admin_console.css` will 404 — point `/static/` at `maint/app/static/` instead (see `deploy/nginx-maint.example.conf`). The maint app also serves those files at `/admin/_static/...` as a fallback.

## Manual service install

If auto-install during deploy fails:

```bash
cd /home/ubuntu/PythonRoot/maint
bash deploy/install-systemd-service.sh
```

Logs: `journalctl -u the-networker-maint -f`
