import mimetypes
import os

from flask import Flask
from jinja2 import FileSystemBytecodeCache

from .models import db


def create_app():
    mimetypes.add_type("application/manifest+json", ".webmanifest")
    app = Flask(__name__)
    app.config.from_object("config.Config")

    from .maint_gate import register_maint_gate

    register_maint_gate(app)

    @app.before_request
    def _tnw_migration_notice_gate():
        from .tnw_feature_flags import tnw_migration_notice_response_or_none

        return tnw_migration_notice_response_or_none()

    db.init_app(app)

    with app.app_context():
        try:
            from .db_schema_patches import apply_startup_schema_patches

            apply_startup_schema_patches()
        except Exception as exc:
            app.logger.warning("Startup schema patches skipped: %s", exc)

    from .routes import bp

    app.register_blueprint(bp)

    with app.app_context():
        try:
            from .routes import ensure_event_image_upload_dir

            created = ensure_event_image_upload_dir()
            app.logger.info("Upload directories ready: %s", created)
        except Exception as exc:
            app.logger.warning("Upload directory setup skipped: %s", exc)

    try:
        os.makedirs(app.instance_path, exist_ok=True)
        _jinja_bc = os.path.join(app.instance_path, "jinja_bytecode")
        os.makedirs(_jinja_bc, exist_ok=True)
        app.jinja_env.bytecode_cache = FileSystemBytecodeCache(_jinja_bc)
    except OSError:
        pass

    return app
