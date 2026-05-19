# Admin maint app deploy notes

See `../README.md` for local setup.

- **Code root:** `C:\PythonRoot\TheNetworkerDevMaint`
- **Public site:** `C:\PythonRoot\TheNetworkerDev` (unchanged)
- **Shared DB:** same `DATABASE_URL` in `.env`
- **Shared uploads:** run `scripts\link-upload-static.ps1` on the server so both apps read/write the same image folders

Nginx example: `admin.example.com` → `127.0.0.1:8104`
