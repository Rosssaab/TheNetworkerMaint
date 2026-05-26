import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Admin maint console (separate deploy from public site).
    TNW_MAINT_APP = True
    TNW_MAINT_LOGIN_USER_1 = (os.getenv("TNW_MAINT_LOGIN_USER_1") or "").strip()
    TNW_MAINT_LOGIN_PASSWORD_1 = os.getenv("TNW_MAINT_LOGIN_PASSWORD_1") or ""
    TNW_MAINT_LOGIN_USER_2 = (os.getenv("TNW_MAINT_LOGIN_USER_2") or "").strip()
    TNW_MAINT_LOGIN_PASSWORD_2 = os.getenv("TNW_MAINT_LOGIN_PASSWORD_2") or ""
    # Semver shown in site footer. Patch: PushToMaint.bat | minor: PushToMaintStaging.bat
    APP_VERSION = "1.38.0-maint"
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        "mysql+pymysql://root:@localhost:3306/mynetworkermdb?charset=utf8mb4",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Hard cap on any single upload (profile picture, etc). Rejected with 413
    # before the view even runs.
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10 MB
    IDEAL_POSTCODES_API_KEY = os.getenv("IDEAL_POSTCODES_API_KEY", "")
    IDEAL_POSTCODES_API_ENDPOINT = os.getenv(
        "IDEAL_POSTCODES_API_ENDPOINT",
        "https://api.ideal-postcodes.co.uk/v1",
    )
    # UK /api/address-lookup: "ideal" (Ideal Postcodes, needs IDEAL_POSTCODES_API_KEY) or "postcodesio" (free OSM stack).
    ADDRESS_LOOKUP_PROVIDER = (
        os.getenv("TNW_ADDRESS_LOOKUP_PROVIDER", "ideal") or "ideal"
    ).strip().lower()
    # Postcodes.io public API (no key). See https://postcodes.io/
    POSTCODES_IO_API_BASE = (
        os.getenv("TNW_POSTCODES_IO_BASE_URL", "https://api.postcodes.io") or "https://api.postcodes.io"
    ).strip().rstrip("/")
    # Overpass API for premise-style rows (addr:postcode). Default public instance.
    OVERPASS_INTERPRETER_URL = (
        os.getenv("TNW_OVERPASS_INTERPRETER_URL", "https://overpass-api.de/api/interpreter")
        or "https://overpass-api.de/api/interpreter"
    ).strip()
    # OSM Nominatim requires a descriptive User-Agent; set explicitly in production.
    NOMINATIM_USER_AGENT = (os.getenv("TNW_NOMINATIM_USER_AGENT") or "").strip()
    # When true, signed-in users can open /postcode-lookup-test (dev tooling).
    ENABLE_POSTCODE_LOOKUP_TEST_PAGE = os.getenv(
        "TNW_ENABLE_POSTCODE_LOOKUP_TEST_PAGE", ""
    ).strip().lower() in ("1", "true", "yes", "on")
    # Log /dashboard phase timings to stderr when TNW_DASHBOARD_TIMING=1 (even without Flask debug).
    # TNW_DASHBOARD_TIMING=0 (or false/no/off) disables timings even when Flask debug is on.
    DASHBOARD_REQUEST_TIMING = os.getenv("TNW_DASHBOARD_TIMING", "").lower() in (
        "1",
        "true",
        "yes",
    )
    # Tombstone: full-site notice + redirect target. Enable with TNW_MIGRATION_NOTICE=1 (or true/yes/on).
    # Any other value (including 0, empty, false) leaves the notice OFF so the app works normally.
    TNW_MIGRATION_NOTICE = os.getenv("TNW_MIGRATION_NOTICE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    TNW_STAGING_URL = (os.getenv("TNW_STAGING_URL") or "https://staging.thenetworkerhub.com").strip()
    # Relative folder under app/static/ for per-event images (e.g. event_images/1).
    TNW_EVENT_IMAGE_LOCATION = (
        os.getenv("TNW_EVENT_IMAGE_LOCATION", "event_images/1") or "event_images/1"
    ).strip().replace("\\", "/").strip("/")
