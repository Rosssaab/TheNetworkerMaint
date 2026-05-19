"""Environment toggles read at request time (not only from Config at import)."""

from __future__ import annotations

import os
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

# Ensure .env is merged into os.environ even if this module is imported early.
load_dotenv()

_DEFAULT_STAGING = "https://staging.thenetworkerhub.com"
_STAGING_PROBE_TTL = float(
    (os.environ.get("TNW_STAGING_PROBE_TTL_SECONDS") or "30").strip() or "30"
)
_STAGING_PROBE_TIMEOUT = float(
    (os.environ.get("TNW_STAGING_PROBE_TIMEOUT_SECONDS") or "4").strip() or "4"
)
_probe_lock = threading.Lock()
_probe_cache: dict[str, object] = {
    "url": "",
    "at": 0.0,
    "available": True,
}


def tnw_migration_notice_on() -> bool:
    """True when this host should show only the staging migration page (no app DB)."""
    v = (os.environ.get("TNW_MIGRATION_NOTICE") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def tnw_staging_url() -> str:
    return (os.environ.get("TNW_STAGING_URL") or _DEFAULT_STAGING).strip()


def _staging_http_status(url: str) -> int | None:
    """Return HTTP status from a lightweight probe, or None if the request failed."""
    headers = {"User-Agent": "TheNetworker-MigrationProbe/1.0"}

    def _head() -> int | None:
        try:
            req = Request(url, method="HEAD", headers=headers)
            with urlopen(req, timeout=_STAGING_PROBE_TIMEOUT) as resp:
                return int(resp.status)
        except HTTPError as e:
            return int(e.code)
        except (URLError, TimeoutError, OSError):
            return None

    def _get_minimal() -> int | None:
        try:
            req = Request(url, method="GET", headers=headers)
            with urlopen(req, timeout=_STAGING_PROBE_TIMEOUT) as resp:
                try:
                    resp.read(1)
                except OSError:
                    pass
                return int(resp.status)
        except HTTPError as e:
            return int(e.code)
        except (URLError, TimeoutError, OSError):
            return None

    status = _head()
    if status in (405, 501):
        status = _get_minimal()
    return status


def tnw_staging_available_for_redirect(url: str) -> bool:
    """
    True when staging responds with something other than "gone" / server error.
    Used to avoid auto-redirect and primary CTA when staging returns 404 or is unreachable.
    """
    if not url:
        return False
    status = _staging_http_status(url)
    if status is None:
        return False
    if status == 404:
        return False
    if status >= 500:
        return False
    return 200 <= status < 500


def _cached_staging_available(url: str) -> bool:
    now = time.monotonic()
    with _probe_lock:
        if (
            _probe_cache["url"] == url
            and now - float(_probe_cache["at"]) < _STAGING_PROBE_TTL
        ):
            return bool(_probe_cache["available"])
        available = tnw_staging_available_for_redirect(url)
        _probe_cache["url"] = url
        _probe_cache["at"] = now
        _probe_cache["available"] = available
        return available


def tnw_migration_notice_response_or_none():
    """If migration tombstone is on, return that HTML response; otherwise None."""
    if not tnw_migration_notice_on():
        return None
    from flask import render_template, request

    ep = request.endpoint
    if ep in ("static", "main.tnw_service_worker"):
        return None
    staging_url = tnw_staging_url()
    staging_available = _cached_staging_available(staging_url)
    return render_template(
        "migration_notice.html",
        staging_url=staging_url,
        staging_available=staging_available,
    )
