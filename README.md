# The Networker — Admin maint console

Separate Flask app for `/admin` tools. Uses the **same database** as `TheNetworkerDev` (copy `.env` or share `DATABASE_URL`).

## Local run

```powershell
cd C:\PythonRoot\TheNetworkerDevMaint
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy ..\\TheNetworkerDev\\.env .env   # or copy .env.example and set DATABASE_URL
.\scripts\link-upload-static.ps1       # junctions upload folders to main site static
python app.py
```

Open http://127.0.0.1:8104/admin/events (default port **8104**).

Sign in with a user that has `admin_user` set in the database.

## Shared uploads

Group/event/user images are **not in git** and are **not deployed** by `PushToMaint*.bat`. On your PC, junction them to the main site static tree (run once):

```powershell
.\scripts\link-upload-static.ps1
```

This creates directory junctions:

- `app\static\meeting_group_images` → `TheNetworkerDev\app\static\meeting_group_images`
- `app\static\event_images` → …
- `app\static\user_images` → …

On the VPS, keep upload folders on disk only (create empty dirs or sync media separately); `git pull` / staging deploy must not overwrite them.

## Production (same VPS, different URL)

- Deploy this folder to e.g. `admin.thenetworkerhub.com`
- Use the same `DATABASE_URL` / `SECRET_KEY` as production (or maint-specific `SECRET_KEY` only if sessions must not cross hosts)
- Set `TNW_MIGRATION_NOTICE=0` in `.env` so the maint host is not blocked by the public migration gate
- Point nginx at port `8104` (or gunicorn/waitress)

## What was copied

- `app/routes.py` — full routes module (admin handlers share helpers with the public app); non-admin URLs return **404** via `maint_gate.py`
- Models, DB patches, promotion/boosts, invoicing modules used by admin
- Admin templates + login/2FA + modal partials
- Admin CSS/JS and logo assets

Public site (`TheNetworkerDev`) is unchanged; remove admin templates there after you switch operators to this URL.

## Env

| Variable | Notes |
|----------|--------|
| `DATABASE_URL` | Same as main site |
| `SECRET_KEY` | Same as main site if you need shared sessions across subdomains; otherwise separate |
| `GEMINI_API_KEY` | Optional — AI tools in admin (test events, keyword suggest, polish) |
| `PORT` | Default `8104` |

## Git push ([TheNetworkerMaint](https://github.com/Rosssaab/TheNetworkerMaint))

On branch **main**:

```bat
PushToMaint.bat "Describe your changes"
```

- Bumps patch version in `config.py`, commits, pushes to `origin/main`
- Ensures `origin` is `https://github.com/Rosssaab/TheNetworkerMaint.git`
- Skips `.env`, PEM keys, and local SSH env files

Push-only (commits already made):

```bat
PushToMaint.bat
```

Staging merge + server deploy (after one-time copy of `deploy\maint-staging-ssh.local.env.example`):

```bat
PushToMaintStaging.bat "optional commit message"
```

See `deploy\MAINT_STAGING_SERVER_SETUP.md` for VPS one-time setup (`.env`, systemd, nginx).

`PushToMain.bat` is a wrapper that calls `PushToMaint.bat`.
