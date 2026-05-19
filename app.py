from app import create_app
import os

app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8104"))
    _reloader_excludes = ("*site-packages*",)
    _reloader_type = (os.getenv("TNW_RELOADER_TYPE") or "").strip().lower() or (
        "stat" if os.name == "nt" else "auto"
    )
    app.run(
        debug=True,
        port=port,
        exclude_patterns=_reloader_excludes,
        reloader_type=_reloader_type,
    )
