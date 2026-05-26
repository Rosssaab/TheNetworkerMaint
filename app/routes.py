"""Application routes.

All database-backed routes have been reduced to render-only stubs while the
app is being rebuilt against the new MyNetworkerDev schema. Each view simply
renders its template (or redirects) with no DB access.

Kept intact (no DB needed):
  - Contact form + email send
  - FAQ chatbot (in-memory knowledge)
  - Admin preview (reads a file off disk)
  - Networking discovery stub API

Removed entirely:
  - Meeting create/edit POST handlers
  - Meeting-group create/edit POST handlers
  - Attendance POST
  - Networking event create/checkout POST handlers
  - Email verification endpoints
  - All queries against old models (Meeting, Topic, MeetingGroup, etc.)
"""

from datetime import date, datetime, time, timedelta, timezone
from functools import cmp_to_key, wraps
from html import escape, unescape
from html.parser import HTMLParser
import calendar
import io
import json
import logging
import math
import os
import re
import secrets
import shutil
import smtplib
import sys
import tempfile
import time as time_mod
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
import ipaddress

import base64
import hashlib
import hmac
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, case, delete, func, inspect, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import joinedload, load_only, noload, selectinload

import qrcode
from PIL import Image, UnidentifiedImageError
from markupsafe import Markup
from werkzeug.exceptions import RequestEntityTooLarge, abort
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    g,
    has_app_context,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)

from .env_auth import (
    SESSION_MAINT_ENV_IMPERSONATOR,
    SESSION_MAINT_ENV_USER,
    clear_maint_env_session_keys,
    get_maint_env_impersonator_user,
    get_maint_env_session_user,
    maint_env_login_configured,
    session_has_maint_env_auth,
    verify_maint_env_login,
)
from .bootstrap_themes import (
    TNW_ADMIN_BOOTSTRAP_THEME_COOKIE,
    TNW_ADMIN_BOOTSTRAP_THEME_SESSION,
    TNW_BOOTSTRAP_THEMES,
    admin_bootstrap_uses_light_shell,
    bootstrap_theme_stylesheet_url,
    normalize_bootstrap_theme_slug,
    resolve_admin_bootstrap_theme_slug,
    resolve_site_bootstrap_theme_slug,
    resolve_bootstrap_theme_slug,
    bootstrap_theme_is_bootswatch,
    TNW_SITE_BOOTSTRAP_THEME_COOKIE,
    TNW_SITE_BOOTSTRAP_THEME_SESSION,
)
from .tnw_feature_flags import tnw_migration_notice_response_or_none
from .models import (
    Country,
    Industry,
    Meeting,
    MeetingAttendee,
    MeetingGroup,
    MeetingTicketEntry,
    MeetingTicketType,
    Tag,
    User,
    UserSavedMeeting,
    db,
    meeting_group_tags,
    user_attendee_tags,
    user_industries,
)
from .schema_labels import table_label
from .ticket_vat import (
    buyer_unit_price_for_ticket,
    display_price_from_stored,
    infer_vat_mode,
    normalize_vat_from_form,
)


def _tnw_commit_event_listing_record(
    user_id: int,
    meeting_id: int,
    previous_status: str | None,
) -> None:
    from .user_account_tx import commit_event_listing_record

    commit_event_listing_record(user_id, meeting_id, previous_status)


def _session_is_signed_in():
    return bool(session.get("user_id") or session_has_maint_env_auth())


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _session_is_signed_in():
            flash("Please sign in to continue.", "info")
            path = request.path or ""
            if path in ("/login", "/register", "/login/twofa"):
                return redirect(url_for("main.login"))
            safe = _safe_post_login_next(path)
            if safe:
                return redirect(url_for("main.login", next=safe))
            return redirect(url_for("main.login"))
        return view(*args, **kwargs)

    return wrapped


def site_admin_required(view):
    """Require the acting site admin (supports impersonation: checks real admin, not fake user)."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _session_is_signed_in():
            flash("Please sign in to continue.", "info")
            path = request.path or ""
            if path in ("/login", "/register", "/login/twofa"):
                return redirect(url_for("main.login"))
            safe = _safe_post_login_next(path)
            if safe:
                return redirect(url_for("main.login", next=safe))
            return redirect(url_for("main.login"))
        admin = _session_site_admin_user()
        if not admin:
            flash("That area is only for site administrators.", "danger")
            return redirect(url_for("main.home"))
        return view(*args, **kwargs)

    return wrapped


bp = Blueprint("main", __name__)


def tnw_url_for(endpoint: str, **values) -> str:
    """Like Flask url_for, but ``_anchor`` becomes a URL fragment (#pane-id)."""
    anchor = values.pop("_anchor", None)
    url = url_for(endpoint, **values)
    if anchor:
        frag = anchor if str(anchor).startswith("#") else f"#{anchor}"
        base = url.split("#", 1)[0]
        return base + frag
    return url


@bp.app_context_processor
def _inject_tnw_url_for():
    return {"url_for": tnw_url_for}


# When set, ``session["user_id"]`` is the impersonated account; this holds the admin's id.
SESSION_IMPERSONATOR_ADMIN_ID = "impersonator_admin_id"
# After password OK, 2FA challenge completes — redirect here if set (same-origin path only).
SESSION_POST_LOGIN_NEXT = "post_login_next"


def _safe_post_login_next(raw):
    """Return a same-origin path (with optional query/fragment) for post-login redirect, or None."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or "\n" in s or "\r" in s or "\\" in s:
        return None
    join_base = request.url_root
    try:
        joined = urljoin(join_base, s)
    except Exception:
        return None
    test = urlsplit(joined)
    root = urlsplit(join_base)
    if test.scheme not in ("http", "https") or (test.netloc or "").lower() != (root.netloc or "").lower():
        return None
    path = test.path or "/"
    if not path.startswith("/") or ".." in path:
        return None
    out = path
    if test.query:
        out += "?" + test.query
    if test.fragment:
        out += "#" + test.fragment
    return out


def _login_next_from_request():
    raw = (request.form.get("next") or request.args.get("next") or "").strip()
    return _safe_post_login_next(raw)


def _append_signed_in_query(url: str) -> str:
    """Add signed_in=1 so the client may offer an occasional PWA install prompt."""
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q["signed_in"] = "1"
    query = urlencode(q)
    out = parts.path or "/"
    if query:
        out += "?" + query
    if parts.fragment:
        out += "#" + parts.fragment
    return out


def _redirect_after_sign_in(target=None):
    """Redirect after login / 2FA; target is a safe relative path or None for home."""
    if target:
        return redirect(_append_signed_in_query(target))
    return redirect(url_for("main.home", signed_in=1))


# Default values the registration form doesn't collect yet.
DEFAULT_COUNTRY_ID = 1  # United Kingdom (only row in countries)
DEFAULT_IMAGE_NAME = ""


def _admin_session_user_id():
    """The site admin's user id for permission checks (not the impersonated user)."""
    return session.get(SESSION_IMPERSONATOR_ADMIN_ID) or session.get("user_id")


def _session_site_admin_user():
    """Return the acting site admin (DB ``User`` or env maint account), or None."""
    imp = get_maint_env_impersonator_user()
    if imp:
        return imp
    env_user = get_maint_env_session_user()
    if env_user:
        return env_user
    uid = _admin_session_user_id()
    if not uid:
        return None
    u = User.query.get(uid)
    if not u or not u.admin_user:
        return None
    return u


def _session_clear_login():
    session.pop("user_id", None)
    session.pop(SESSION_IMPERSONATOR_ADMIN_ID, None)
    clear_maint_env_session_keys()
    session.pop("pending_twofa_user_id", None)
    session.pop(SESSION_POST_LOGIN_NEXT, None)


def _logout_redirect_response():
    """End the server session and clear the session cookie so revisits require sign-in."""
    _session_clear_login()
    session.clear()
    flash("You have been signed out.", "info")
    resp = make_response(redirect(url_for("main.home", signed_out=1)))
    iface = current_app.session_interface
    cookie_name = iface.get_cookie_name(current_app)
    resp.delete_cookie(
        cookie_name,
        domain=iface.get_cookie_domain(current_app),
        path=iface.get_cookie_path(current_app),
        secure=iface.get_cookie_secure(current_app),
        httponly=iface.get_cookie_httponly(current_app),
        samesite=iface.get_cookie_samesite(current_app),
    )
    return resp


class _RichTextSanitizer(HTMLParser):
    _allowed_tags = {"b", "strong", "i", "em", "u", "ul", "ol", "li", "p", "div", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self._allowed_tags:
            self.parts.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag == "br":
            self.parts.append("<br>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._allowed_tags and tag != "br":
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(escape(data, quote=False))


def _sanitize_rich_text_html(value):
    raw = (value or "").strip()
    if not raw:
        return ""
    if not re.search(r"<[a-zA-Z][^>]*>", raw):
        return escape(raw, quote=False).replace("\r\n", "\n").replace("\n", "<br>")
    parser = _RichTextSanitizer()
    parser.feed(raw)
    parser.close()
    return "".join(parser.parts).strip()


def _rich_text_plain_text(value):
    text = re.sub(r"<[^>]*>", " ", value or "")
    return unescape(text).replace("\xa0", " ").strip()


def _numeric_or_none(val):
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _meeting_has_displayable_location(m):
    if not m:
        return False
    la, lo = _numeric_or_none(m.latitude), _numeric_or_none(m.longitude)
    if la is not None and lo is not None:
        return True
    return bool(
        (m.venue_name or "").strip()
        or (m.address_line1 or "").strip()
        or (m.address_town or "").strip()
        or (m.address_postcode or "").strip()
    )


def _format_meeting_location_lines(m):
    lines = []
    if not m:
        return lines
    vn = (m.venue_name or "").strip()
    if vn:
        lines.append(vn)
    street = ", ".join(
        p
        for p in [
            (m.address_line1 or "").strip(),
            (m.address_line2 or "").strip(),
        ]
        if p
    )
    if street:
        lines.append(street)
    town = ", ".join(
        p
        for p in [
            (m.address_town or "").strip(),
            (m.address_county or "").strip(),
            (m.address_postcode or "").strip(),
        ]
        if p
    )
    if town:
        lines.append(town)
    country = (m.address_country or m.location_country or "").strip()
    if country:
        lines.append(country)
    return lines


def _pick_preview_location_meeting(meetings):
    live = [x for x in (meetings or []) if (x.status or "").strip() == "Live"]
    candidates = [x for x in live if _meeting_has_displayable_location(x)]
    if not candidates:
        return None
    now = datetime.utcnow()
    candidates.sort(key=lambda x: (x.starts_at is None, x.starts_at or datetime.min))
    upcoming = [x for x in candidates if x.starts_at and x.starts_at >= now]
    if upcoming:
        return min(upcoming, key=lambda x: x.starts_at)
    past = [x for x in candidates if x.starts_at and x.starts_at < now]
    if past:
        return max(past, key=lambda x: x.starts_at)
    return candidates[0]


def _google_maps_search_url(m):
    if not m:
        return None
    la, lo = _numeric_or_none(m.latitude), _numeric_or_none(m.longitude)
    if la is not None and lo is not None:
        return f"https://www.google.com/maps/search/?api=1&query={la},{lo}"
    lines = _format_meeting_location_lines(m)
    if not lines:
        return None
    return "https://www.google.com/maps/search/?api=1&query=" + quote(", ".join(lines))


def _google_maps_embed_url(m):
    if not m:
        return None
    la, lo = _numeric_or_none(m.latitude), _numeric_or_none(m.longitude)
    if la is None or lo is None:
        return None
    return f"https://maps.google.com/maps?q={la},{lo}&z=15&output=embed"


def _meeting_starts_at_naive_utc(meeting: Meeting | None):
    """Naive UTC instant for ``meeting.starts_at``, or None if unknown."""
    if not meeting or not meeting.starts_at:
        return None
    st = meeting.starts_at
    if st.tzinfo is not None:
        return st.astimezone(timezone.utc).replace(tzinfo=None)
    return st


def _meeting_calendar_time_bounds_naive_utc(meeting: Meeting):
    """Return (start, end) naive datetimes; stored times are treated as UTC (see datetime.utcnow())."""
    start = meeting.starts_at
    if not start:
        return None, None
    if start.tzinfo is not None:
        start = start.astimezone(timezone.utc).replace(tzinfo=None)
    try:
        dm = int(meeting.duration_minutes or 60) or 60
    except (TypeError, ValueError):
        dm = 60
    end = start + timedelta(minutes=dm)
    return start, end


def _meeting_calendar_location_line(meeting: Meeting) -> str:
    fmt = (meeting.meeting_format or "").strip()
    if fmt == "Virtual":
        plat = (meeting.virtual_platform or "").strip()
        return f"Online ({plat})" if plat else "Online"
    lines = _format_meeting_location_lines(meeting)
    if lines:
        return ", ".join(lines)
    bits = [
        (meeting.venue_name or "").strip(),
        (meeting.address_town or "").strip(),
        (meeting.address_postcode or "").strip(),
        (meeting.location_postcode or "").strip(),
    ]
    bits = [b for b in bits if b]
    return ", ".join(bits) if bits else "Location to be confirmed"


def _meeting_calendar_body_text(
    meeting: Meeting, detail_url: str, subject_plain: str, max_len: int = 3500
) -> str:
    parts = []
    vl = (meeting.virtual_link or "").strip()
    if vl:
        parts.append(vl)
    sp = (subject_plain or "").strip()
    if sp:
        remain = max_len - len(detail_url) - 50
        if remain > 80:
            parts.append(sp[:remain])
    parts.append(detail_url)
    body = "\n\n".join(parts)
    if len(body) > max_len:
        body = body[: max_len - 3].rstrip() + "..."
    return body


def _google_calendar_add_url(
    meeting: Meeting, detail_url: str, title: str, subject_plain: str
) -> str | None:
    start, end = _meeting_calendar_time_bounds_naive_utc(meeting)
    if not start:
        return None
    title_q = (title or "Event")[:500]
    loc = _meeting_calendar_location_line(meeting)
    details = _meeting_calendar_body_text(meeting, detail_url, subject_plain, max_len=1800)
    fmt = "%Y%m%dT%H%M%SZ"
    dates = f"{start.strftime(fmt)}/{end.strftime(fmt)}"
    base = "https://calendar.google.com/calendar/render"
    return (
        f"{base}?action=TEMPLATE"
        f"&text={quote(title_q)}"
        f"&dates={quote(dates)}"
        f"&details={quote(details)}"
        f"&location={quote(loc)}"
    )


def _outlook_calendar_add_url(
    meeting: Meeting, detail_url: str, title: str, subject_plain: str
) -> str | None:
    start, end = _meeting_calendar_time_bounds_naive_utc(meeting)
    if not start:
        return None
    loc = _meeting_calendar_location_line(meeting)
    body = _meeting_calendar_body_text(meeting, detail_url, subject_plain, max_len=4500)
    title_q = (title or "Event")[:500]
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    end_iso = end.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    base = "https://outlook.live.com/calendar/0/action/compose"
    return (
        f"{base}?subject={quote(title_q)}"
        f"&body={quote(body)}"
        f"&startdt={quote(start_iso)}"
        f"&enddt={quote(end_iso)}"
        f"&location={quote(loc)}"
    )


def _ics_escape_text_field(value: str) -> str:
    t = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    return (
        t.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def _meeting_calendar_ics_text(
    meeting: Meeting, detail_url: str, title: str, subject_plain: str
) -> str | None:
    start, end = _meeting_calendar_time_bounds_naive_utc(meeting)
    if not start:
        return None
    fmt = "%Y%m%dT%H%M%SZ"
    dtstamp = datetime.utcnow().strftime(fmt)
    loc = _meeting_calendar_location_line(meeting)
    desc = _meeting_calendar_body_text(meeting, detail_url, subject_plain, max_len=8000)
    uid = f"tnw-meeting-{meeting.meeting_id}@thenetworker"
    sum_raw = (title or "Event")[:900]
    loc_raw = loc[:900]
    desc_raw = desc[:9000]
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//The Networker//Event//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{start.strftime(fmt)}",
        f"DTEND:{end.strftime(fmt)}",
        f"SUMMARY:{_ics_escape_text_field(sum_raw)}",
        f"DESCRIPTION:{_ics_escape_text_field(desc_raw)}",
        f"LOCATION:{_ics_escape_text_field(loc_raw)}",
        f"URL:{detail_url}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def _user_display_name(user):
    if not user:
        return ""
    parts = [(user.first_name or "").strip(), (user.second_name or "").strip()]
    name = " ".join(p for p in parts if p).strip()
    return name or (user.email or "").strip() or (user.username or "").strip()


def _live_meeting_has_paid_options(meeting):
    if not meeting:
        return False
    if meeting.is_paid_and_published:
        return True
    for tt in meeting.ticket_types or []:
        try:
            if tt.price_amount is not None and float(tt.price_amount) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _meeting_status_counts(meetings):
    counts = {}
    for m in meetings or []:
        st = ((m.status or "") or "Unknown").strip() or "Unknown"
        counts[st] = counts.get(st, 0) + 1
    return counts


def _fix_utf8_mojibake_from_cp1252(s: str | None) -> str:
    """Undo common WordPress/SQL import glitch: UTF-8 stored as if it were Windows-1252.

    A UTF-8 sequence such as E2 80 99 (Unicode RIGHT SINGLE QUOTATION MARK) mis-read as
    three CP-1252 code units becomes the characters U+00E2, U+20AC, U+2122 (often shown
    as ``Itâ€™s`` instead of ``It's``). Re-encoding as CP-1252 and decoding as UTF-8
    recovers the intended text. Strings that do not contain the marker pair ``â€`` are
    returned unchanged so normal English and real euro signs stay safe.
    """
    if not s:
        return ""
    if "\u00e2\u20ac" not in s:
        return s
    try:
        fixed = s.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    if "\ufffd" in fixed:
        return s
    return fixed


# ---------------------------------------------------------------------------
# Context processor
# ---------------------------------------------------------------------------
# Templates reference `current_user` (e.g. `{% if current_user %}` in base.html).
# Maint app: env-configured operators; otherwise loads from ``users`` when signed in.


@bp.app_context_processor
def inject_user():
    uid = session.get("user_id")
    user = User.query.get(uid) if uid else None
    if not user:
        user = get_maint_env_session_user()
    imp_id = session.get(SESSION_IMPERSONATOR_ADMIN_ID)
    imp_env = session.get(SESSION_MAINT_ENV_IMPERSONATOR)
    if imp_id:
        real_admin_user = User.query.get(imp_id)
    elif imp_env:
        real_admin_user = get_maint_env_impersonator_user()
    else:
        real_admin_user = user if (user and getattr(user, "admin_user", False)) else None
    nav_has_saved_meetings = False
    if uid:
        nav_has_saved_meetings = (
            UserSavedMeeting.query.filter_by(user_id=int(uid)).first() is not None
        )
    return {
        "current_user": user,
        "impersonating": bool(imp_id or imp_env),
        "real_admin_user": real_admin_user,
        "nav_has_saved_meetings": nav_has_saved_meetings,
        "tnw_maint_app": bool(current_app.config.get("TNW_MAINT_APP")),
    }


def _persist_bootstrap_theme(slug: str, response):
    """Store one theme choice for the whole app (session + cookies)."""
    session[TNW_SITE_BOOTSTRAP_THEME_SESSION] = slug
    session[TNW_ADMIN_BOOTSTRAP_THEME_SESSION] = slug
    for cookie_name in (
        TNW_SITE_BOOTSTRAP_THEME_COOKIE,
        TNW_ADMIN_BOOTSTRAP_THEME_COOKIE,
    ):
        response.set_cookie(
            cookie_name,
            slug,
            max_age=60 * 60 * 24 * 400,
            samesite="Lax",
            secure=bool(getattr(request, "is_secure", False)),
            path="/",
        )
    return response


@bp.app_context_processor
def inject_admin_bootstrap_theme():
    theme_choices = [{"id": sid, "label": lab} for sid, lab in TNW_BOOTSTRAP_THEMES]
    slug = resolve_bootstrap_theme_slug(
        session.get(TNW_SITE_BOOTSTRAP_THEME_SESSION),
        request.cookies.get(TNW_SITE_BOOTSTRAP_THEME_COOKIE),
        session.get(TNW_ADMIN_BOOTSTRAP_THEME_SESSION),
        request.cookies.get(TNW_ADMIN_BOOTSTRAP_THEME_COOKIE),
    )
    bootswatch = bootstrap_theme_is_bootswatch(slug)
    return {
        "tnw_site_bootstrap_theme": slug,
        "tnw_site_bootstrap_theme_href": bootstrap_theme_stylesheet_url(slug),
        "tnw_site_bootstrap_theme_choices": theme_choices,
        "tnw_admin_bootstrap_theme": slug,
        "tnw_admin_bootstrap_theme_href": bootstrap_theme_stylesheet_url(slug),
        "tnw_admin_bootstrap_theme_choices": theme_choices,
        "tnw_bootswatch_theme_active": bootswatch,
        "tnw_admin_bootstrap_light_shell": admin_bootstrap_uses_light_shell(slug),
        "tnw_bootstrap_theme_choices": theme_choices,
        "tnw_bootstrap_theme": slug,
    }


@bp.app_context_processor
def inject_schema_labels():
    """Expose table comment metadata to templates via ``table_label('events', 'Events')``."""
    return {"table_label": table_label}


@bp.app_context_processor
def inject_app_version():
    return {"app_version": current_app.config.get("APP_VERSION", "")}


@bp.app_context_processor
def inject_register_modal_state():
    """Populated on ``/register`` for the global create-account modal."""
    return {
        "register_modal_form_data": getattr(g, "register_modal_form_data", None) or {},
        "register_modal_errors": getattr(g, "register_modal_errors", None) or {},
        "open_register_modal": bool(getattr(g, "open_register_modal", False)),
    }


@bp.app_template_global()
def meeting_group_image_url(mg=None):
    """URL for a meeting group's banner image, or the shared placeholder when none is set."""
    if mg is not None:
        fn = (getattr(mg, "image_filename", None) or "").strip()
        if fn:
            return url_for("static", filename=f"meeting_group_images/{fn}")
    return url_for("static", filename=TNW_NO_GROUP_IMAGE_STATIC)


def _safe_static_image_rel(folder: str | None, filename: str | None) -> str | None:
    """``static/<folder>/<filename>`` when folder segments and filename are safe."""
    fn = os.path.basename((filename or "").strip())
    sub = (folder or "").strip().replace("\\", "/").strip("/")
    if not fn or not sub or ".." in fn or ".." in sub:
        return None
    parts = [p for p in sub.split("/") if p]
    if not parts:
        return None
    for part in parts:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", part):
            return None
    return "/".join(parts) + "/" + fn


def _safe_meeting_static_image_rel(folder: str | None, filename: str | None) -> str | None:
    return _safe_static_image_rel(folder, filename)


@bp.app_template_global()
def meeting_image_url(meeting=None, mg=None):
    """Event card image: per-event file when set, otherwise the parent group image."""
    if meeting is not None:
        fn = (getattr(meeting, "image_name", None) or "").strip()
        if fn:
            loc = (getattr(meeting, "image_location", None) or "").strip() or "event_images"
            rel = _safe_meeting_static_image_rel(loc, fn)
            if rel:
                return url_for("static", filename=rel)
    group = mg
    if group is None and meeting is not None:
        group = getattr(meeting, "meeting_group", None)
    return meeting_group_image_url(group)


@bp.app_template_filter("fix_utf8_mojibake")
def fix_utf8_mojibake_filter(value):
    return _fix_utf8_mojibake_from_cp1252(value if value is not None else "")


@bp.app_template_filter("decode_html_entities")
def decode_html_entities_filter(value):
    return unescape(value if value is not None else "")


@bp.app_template_filter("first_sentences")
def first_sentences_filter(text, max_sentences=3, max_chars=360):
    """Plain text: first few sentences for short tooltips (sentence split on . ! ?)."""
    raw = (text or "").strip()
    if not raw:
        return ""
    try:
        max_sentences = int(max_sentences)
    except (TypeError, ValueError):
        max_sentences = 3
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        max_chars = 360
    bits = re.split(r"(?<=[.!?])\s+", raw)
    bits = [b.strip() for b in bits if b.strip()]
    if not bits:
        return (raw[:max_chars] + "…") if len(raw) > max_chars else raw
    if len(bits) == 1 and len(bits[0]) > max_chars:
        chunk = bits[0][: max_chars - 1]
        sp = chunk.rsplit(" ", 1)[0]
        return sp.rstrip(",;:") + "…"
    chosen = bits[:max_sentences]
    out = " ".join(chosen).strip()
    if len(out) > max_chars:
        out = out[: max_chars - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    elif len(bits) > max_sentences:
        out = out + " …"
    return out


# ---------------------------------------------------------------------------
# Profile-completion gate (server-side defence in depth)
# ---------------------------------------------------------------------------
# If a logged-in user hasn't completed their profile yet, redirect every
# non-essential request back to /profile so they cannot bypass the nav gate
# by typing URLs directly.
_PROFILE_GATE_ALLOWED_ENDPOINTS = {
    "main.profile",
    "main.profile_setup",
    "main.profile_image",
    "main.profile_details",
    "main.profile_password",
    "main.profile_twofa_setup",
    "main.profile_twofa_verify",
    "main.profile_twofa_disable",
    "main.profile_location",
    "main.profile_industries",
    "main.profile_tags_search",
    "main.profile_tags_all",
    "main.profile_tags_add",
    "main.profile_tags_remove",
    "main.logout",
    "main.verify_email",
    "main.site_search",
    "main.meeting_group_public",
    "main.meeting_group_contact_organiser",
    "main.admin_preview",
    "main.admin_events",
    "main.admin_keywords",
    "main.admin_users",
    "main.admin_move_events",
    "main.admin_delete_group_events",
    "main.admin_meeting_group_image_replace",
    "main.admin_meeting_groups_bulk_delete",
    "main.admin_meeting_groups_bulk_transfer",
    "main.admin_meeting_groups_bulk_image",
    "main.admin_meeting_groups_bulk_details",
    "main.admin_meeting_groups_bulk_website",
    "main.admin_meeting_group_cascade_delete",
    "main.admin_meetings_move_to_group",
    "main.admin_meeting_groups_lookup_for_move",
    "main.set_admin_bootstrap_theme",
    "main.admin_review_about_v3_draft",
    "main.keyword_maintenance",
    "main.topic_add",
    "main.topic_update",
    "main.topic_delete",
    "main.keyword_add",
    "main.keyword_update",
    "main.keyword_delete",
    "main.admin_keywords_suggest",
    "main.admin_keywords_apply_suggestions",
    "main.admin_meeting_groups_suggest_keywords",
    "main.admin_meeting_groups_apply_tag_suggestions",
    "main.polish_meeting_group_description",
    "main.polish_meeting_description",
    "main.api_meeting_group_suggest_keywords",
    "main.admin_user_edit",
    "main.admin_user_edit_data",
    "main.admin_user_delete",
    "main.impersonate_users_json",
    "main.impersonate_start",
    "main.impersonate_stop",
    "main.tnw_service_worker",
    "static",
}


@bp.before_app_request
def _enforce_profile_completion():
    # Admin maint app: no profile-setup redirect (operators use /admin only).
    if current_app.config.get("TNW_MAINT_APP"):
        return None
    uid = session.get("user_id")
    if not uid:
        return None
    if session.get(SESSION_IMPERSONATOR_ADMIN_ID):
        return None
    if request.endpoint in _PROFILE_GATE_ALLOWED_ENDPOINTS:
        return None
    user = User.query.get(uid)
    if not user:
        _session_clear_login()
        return None
    if user.is_profile_complete:
        return None
    flash("Please complete your profile before using the rest of the site.", "info")
    return redirect(url_for("main.profile"))


# ---------------------------------------------------------------------------
# FAQ chatbot (no DB) — keep in sync with app/templates/faq.html accordion.
# ---------------------------------------------------------------------------
def _faq_keyword_hit(prompt: str, kw: str) -> bool:
    """Match multi-word phrases as substrings; match tokens with word boundaries."""
    key = (kw or "").strip().lower()
    if not key:
        return False
    if " " in key or "-" in key:
        return key in prompt
    if not re.search(r"[a-z0-9]", key, re.I):
        return key in prompt
    if re.fullmatch(r"[a-z0-9]+", key, re.I):
        return re.search(r"\b" + re.escape(key) + r"\b", prompt, re.I) is not None
    return key in prompt


FAQ_BOT_KNOWLEDGE = [
    {
        "keywords": [
            "my tickets",
            "my ticket",
            "ticket qr",
            "qr code",
            "admission",
            "check in",
            "check-in",
            "attendee dashboard",
        ],
        "answer": (
            "Use My Tickets in the top menu (sign in required). There you can see events you have booked "
            "or registered for. For in-person ticketed events you may have a QR or check-in details from the organiser."
        ),
    },
    {
        "keywords": [
            "my events",
            "organiser",
            "organizer",
            "list your event",
            "create event",
            "create meeting",
            "new event",
            "platform dashboard",
            "dashboard",
            "draft",
            "publish",
        ],
        "answer": (
            "Organisers use My Events in the top menu to manage event groups and individual events, "
            "set formats (in-person or virtual), ticketing, and publishing. Creating a new event starts from there once you are signed in."
        ),
    },
    {
        "keywords": [
            "saved",
            "favourites",
            "favorites",
            "bookmark",
            "save event",
        ],
        "answer": (
            "When you are logged in, you can save events you like to Favourites from search or event pages, "
            "then open Favourites from the top menu to revisit them."
        ),
    },
    {
        "keywords": [
            "search",
            "find events",
            "find event",
            "browse events",
            "filter",
            "distance",
            "near me",
            "postcode",
            "in-person",
            "face to face",
        ],
        "answer": (
            "Open Search in the top menu. You can switch between in-person and virtual listings, sort results, "
            "and for in-person events narrow by distance when you have a location set."
        ),
    },
    {
        "keywords": [
            "location",
            "profile complete",
            "complete profile",
            "unlock",
            "cannot access",
            "nav disabled",
            "set location",
        ],
        "answer": (
            "New accounts may need to complete profile basics, including location, before every main menu link unlocks. "
            "Use Profile to finish setup; this helps show relevant nearby in-person events."
        ),
    },
    {
        "keywords": [
            "networking directory",
            "networking-directory",
            "directory listing",
        ],
        "answer": (
            "Most public discovery happens under Search. A separate Networking directory exists for certain "
            "networking-style listings; your organiser dashboard may link there when that workflow is enabled."
        ),
    },
    {
        "keywords": [
            "address lookup",
            "postcode lookup",
            "venue address",
            "in-person event",
            "physical venue",
        ],
        "answer": (
            "In-person events need a clear venue and address. When you enter a UK postcode, the form can suggest "
            "addresses to speed up entry so attendees know exactly where to go."
        ),
    },
    {
        "keywords": ["what is", "the networker", "this platform", "tnw"],
        "answer": (
            "The Networker is a UK event management and ticketing platform for professional networking events. "
            "It helps organisers create polished listings, sell tickets, promote events, and bring the right people together."
        ),
    },
    {
        "keywords": ["who is it for", "who is this for", "who for", "audience", "who should"],
        "answer": (
            "It is built for event organisers and hosts, networking groups, entrepreneurs, SMEs, freelancers, "
            "consultants, and professionals looking for useful business events to attend."
        ),
    },
    {
        "keywords": ["free event", "free listing", "cost", "pricing", "platform fee", "2%", "ticket sales", "how much"],
        "answer": (
            "The Networker charges 2% on ticket sales taken through the platform. Free listings and free-to-attend "
            "events are supported; the percentage fee applies when paid tickets are sold here. Organisers can also use paid promotion for extra reach."
        ),
    },
    {
        "keywords": [
            "virtual event",
            "zoom",
            "teams",
            "online event",
            "joining link",
            "meeting link",
        ],
        "answer": (
            "Virtual events should name the platform (for example Zoom or Microsoft Teams) and include stable joining "
            "instructions or links so attendees can connect on the day."
        ),
    },
    {
        "keywords": [
            "event group",
            "event groups",
            "meeting group",
            "mixed group",
            "face2face",
            "face to face group",
        ],
        "answer": (
            "An event group is a named home for a series or brand of events. The group has an overall format style, "
            "and each scheduled event is set to in-person or virtual when you create or edit it under My Events."
        ),
    },
    {
        "keywords": ["ticketing", "sell tickets", "buy tickets", "book ticket", "paid ticket", "refund"],
        "answer": (
            "Organisers configure ticket types for an event; attendees purchase or register through the public event page. "
            "Payment handling and refunds follow the organiser’s policy and the flow shown at checkout."
        ),
    },
    {
        "keywords": ["ai", "polish description", "improve description", "rewrite listing"],
        "answer": (
            "Organisers can use built-in AI assistance to polish event and event group descriptions before they go live, "
            "so listings read clearly and professionally."
        ),
    },
    {
        "keywords": ["promote", "promotion", "visibility", "advertise", "reach more"],
        "answer": (
            "Yes. Organisers can choose paid promotion so events reach more of the right people in a cost-effective way."
        ),
    },
    {
        "keywords": ["register", "sign up", "create account", "new account", "login", "sign in", "password"],
        "answer": (
            "Use Register or Login in the top-right area of the site. An account lets you manage your profile, "
            "save events, buy tickets, and use organiser tools where you have permission."
        ),
    },
    {
        "keywords": ["profile", "keywords", "tags", "interests", "industries", "skills"],
        "answer": (
            "Your profile can include keywords and industries so other members and discovery features understand "
            "your background and networking goals."
        ),
    },
    {
        "keywords": ["contact", "support", "email", "phone", "speak to", "get in touch", "help desk"],
        "answer": (
            "Open Contact in the top menu for the contact form and published contact options."
        ),
    },
    {
        "keywords": ["faq", "frequently asked", "this page", "accordion"],
        "answer": (
            "Scroll this FAQ page for full answers, or ask me here in short natural language—I match your question "
            "to the same topics covered in the FAQ."
        ),
    },
]


def get_faq_bot_answer(user_message):
    prompt = (user_message or "").strip().lower()
    if not prompt:
        return "Please type a question and I will help with FAQ answers."

    best_score = 0
    best_answer = None
    for item in FAQ_BOT_KNOWLEDGE:
        score = sum(1 for kw in item["keywords"] if _faq_keyword_hit(prompt, kw))
        if score > best_score:
            best_score = score
            best_answer = item["answer"]

    if best_answer:
        return best_answer

    return (
        "I could not match that to a FAQ topic yet. Try asking about Search, My Events, My Tickets, Favourites, "
        "ticketing and the 2% fee, in-person versus virtual events, profiles and location, event groups, or use "
        "the Contact page in the top menu."
    )


_UK_POSTCODE_RE = re.compile(
    r"^([A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}|GIR\s?0A{2})$", re.IGNORECASE
)


def _postcodes_io_api_base():
    """Base URL for https://postcodes.io/ (no API key)."""
    if has_app_context():
        base = current_app.config.get("POSTCODES_IO_API_BASE")
    else:
        base = None
    if not base:
        base = os.getenv("TNW_POSTCODES_IO_BASE_URL", "https://api.postcodes.io")
    base = str(base).strip().rstrip("/")
    return base or "https://api.postcodes.io"


def _overpass_interpreter_url():
    if has_app_context():
        u = current_app.config.get("OVERPASS_INTERPRETER_URL")
    else:
        u = None
    if not u:
        u = os.getenv(
            "TNW_OVERPASS_INTERPRETER_URL", "https://overpass-api.de/api/interpreter"
        )
    u = str(u).strip()
    return u or "https://overpass-api.de/api/interpreter"


def _ideal_postcodes_endpoint(path):
    endpoint = (
        current_app.config.get("IDEAL_POSTCODES_API_ENDPOINT")
        or os.getenv("IDEAL_POSTCODES_API_ENDPOINT", "")
        or "https://api.ideal-postcodes.co.uk/v1"
    ).rstrip("/")
    if "?" in endpoint:
        parts = urlsplit(endpoint)
        endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    if endpoint.endswith(path):
        return endpoint
    return endpoint + "/" + path


def _debug_address_lookup(message):
    print(f"[address-lookup] {message}", flush=True)
    try:
        current_app.logger.info("[address-lookup] %s", message)
    except Exception:
        pass


def _event_countdown_label(starts_at, now=None):
    if not starts_at:
        return "Date not set"

    now = now or datetime.utcnow()
    delta = starts_at - now
    seconds = int(delta.total_seconds())
    past = seconds < 0
    seconds = abs(seconds)

    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60

    if days:
        value = f"{days} day{'s' if days != 1 else ''}"
        if hours:
            value += f", {hours} hr{'s' if hours != 1 else ''}"
    elif hours:
        value = f"{hours} hr{'s' if hours != 1 else ''}"
        if minutes:
            value += f", {minutes} min{'s' if minutes != 1 else ''}"
    elif minutes:
        value = f"{minutes} min{'s' if minutes != 1 else ''}"
    else:
        value = "less than 1 min"

    return f"{value} ago" if past else f"In {value}"


def _meeting_end_datetime(meeting) -> datetime | None:
    starts = getattr(meeting, "starts_at", None)
    if not starts:
        return None
    try:
        duration = int(getattr(meeting, "duration_minutes", None) or 60)
    except (TypeError, ValueError):
        duration = 60
    duration = max(duration, 1)
    return starts + timedelta(minutes=duration)


def _partition_directory_group_meetings(
    meetings: list, now: datetime | None = None
):
    """Upcoming first (soonest next), then ended (most recently ended first)."""
    now = now or datetime.utcnow()
    upcoming: list = []
    finished: list = []
    for meeting in meetings:
        ends = _meeting_end_datetime(meeting)
        if ends is not None and ends < now:
            finished.append(meeting)
        else:
            upcoming.append(meeting)

    def _upcoming_sort_key(m):
        st = getattr(m, "starts_at", None)
        return (st is None, st or datetime.max)

    def _finished_sort_key(m):
        st = getattr(m, "starts_at", None)
        return st or datetime.min

    upcoming.sort(key=_upcoming_sort_key)
    finished.sort(key=_finished_sort_key, reverse=True)
    return upcoming, finished


def _mask_secret(value):
    value = str(value or "")
    if not value:
        return "<missing>"
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "..." + value[-4:] + f" ({len(value)} chars)"


def _redact_query_secret(url):
    url = re.sub(r"([?&]key=)[^&]+", r"\1<redacted>", url)
    return re.sub(r"([?&]api_key=)[^&]+", r"\1<redacted>", url)


def _parse_ideal_postcodes_address(item):
    line1 = item.get("line_1") or ""
    line2 = item.get("line_2") or ""
    line3 = item.get("line_3") or ""
    town = item.get("post_town") or item.get("district") or ""
    county = item.get("county") or ""
    postcode = item.get("postcode") or ""
    label = ", ".join(
        part
        for part in [line1, line2, line3, town, county, postcode]
        if part
    )
    return {
        "label": label,
        "venue_name": item.get("organisation_name") or "",
        "address_line1": line1,
        "address_line2": ", ".join(part for part in [line2, line3] if part),
        "address_town": town,
        "address_county": county,
        "address_postcode": postcode,
        "latitude": item.get("latitude"),
        "longitude": item.get("longitude"),
    }


def _address_lookup_provider_slug():
    v = (
        current_app.config.get("ADDRESS_LOOKUP_PROVIDER")
        or os.getenv("TNW_ADDRESS_LOOKUP_PROVIDER", "ideal")
        or "ideal"
    )
    v = str(v).strip().lower()
    if v in ("ideal", "ideal-postcodes", "idealpostcodes"):
        return "ideal"
    return "postcodesio"


def _postcode_compact(value):
    return re.sub(r"\s+", "", (value or "").upper())


def _float_or_none(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in kilometres (WGS84 spherical approximation)."""
    try:
        lat1 = float(lat1)
        lon1 = float(lon1)
        lat2 = float(lat2)
        lon2 = float(lon2)
    except (TypeError, ValueError):
        return float("inf")
    rlat1, rlon1, rlat2, rlon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return 6371.0 * c


def _urllib_json_get(url, headers, timeout=8):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw)


def _row_from_postcodes_io_result(r, postcode_query):
    """Single summary row from postcodes.io /postcodes/{pc} (centroid + locality)."""
    if not isinstance(r, dict):
        return None
    pc = (r.get("postcode") or postcode_query or "").strip()
    ward = (r.get("admin_ward") or "").strip()
    district = (r.get("admin_district") or "").strip()
    region = (r.get("region") or "").strip()
    line1 = ward or district or "Postcode area"
    town = district or ward or (r.get("parish") or "").strip() or "United Kingdom"
    label = ", ".join(p for p in [line1, town, region, pc] if p)
    return {
        "label": label,
        "venue_name": "",
        "address_line1": line1,
        "address_line2": "",
        "address_town": town,
        "address_county": region,
        "address_postcode": pc,
        "latitude": _float_or_none(r.get("latitude")),
        "longitude": _float_or_none(r.get("longitude")),
    }


def _row_from_overpass_element(elem, target_pc, fallback_postcode):
    """Map an Overpass node/way (with center) to the same shape as Ideal Postcodes rows."""
    if not isinstance(elem, dict):
        return None
    if elem.get("type") not in ("node", "way"):
        return None
    tags = elem.get("tags") if isinstance(elem.get("tags"), dict) else {}
    lat = _float_or_none(elem.get("lat"))
    lon = _float_or_none(elem.get("lon"))
    if lat is None or lon is None:
        c = elem.get("center") if isinstance(elem.get("center"), dict) else {}
        lat = _float_or_none(c.get("lat"))
        lon = _float_or_none(c.get("lon"))
    if lat is None or lon is None:
        return None
    hn = (tags.get("addr:housenumber") or "").strip()
    st = (
        (tags.get("addr:street") or tags.get("addr:place") or tags.get("addr:hamlet") or "")
        .strip()
    )
    line1 = f"{hn} {st}".strip()
    name = (tags.get("name") or "").strip()
    if not line1:
        line1 = name or (tags.get("addr:interpolation") or "").strip()
    if not line1:
        return None
    town = (
        (tags.get("addr:city") or tags.get("addr:town") or tags.get("addr:village") or tags.get("addr:suburb") or "")
        .strip()
    )
    county = (tags.get("addr:county") or "").strip()
    pc = (tags.get("addr:postcode") or "").strip() or fallback_postcode
    if _postcode_compact(pc) != target_pc:
        return None
    amenity = (
        (tags.get("amenity") or tags.get("shop") or tags.get("leisure") or tags.get("office") or "")
        .strip()
    )
    venue_name = ""
    if name and name.lower() != line1.lower():
        venue_name = name
    elif amenity:
        venue_name = amenity
    label = ", ".join(p for p in [line1, town, county, pc] if p)
    return {
        "label": label,
        "venue_name": venue_name,
        "address_line1": line1,
        "address_line2": "",
        "address_town": town,
        "address_county": county,
        "address_postcode": pc,
        "latitude": lat,
        "longitude": lon,
    }


def _overpass_postcode_variants(postcode_spaced, pio_result):
    """OSM uses both spaced and compact postcodes in addr:postcode."""
    out = []
    base = re.sub(r"\s+", " ", (postcode_spaced or "").strip().upper())
    if base:
        out.append(base)
    if isinstance(pio_result, dict):
        rpc = (pio_result.get("postcode") or "").strip().upper()
        if rpc:
            out.append(re.sub(r"\s+", " ", rpc))
            out.append(rpc.replace(" ", ""))
    if base:
        out.append(base.replace(" ", ""))
    seen = set()
    uniq = []
    for v in out:
        v = v.strip()
        if not v or v in seen:
            continue
        seen.add(v)
        uniq.append(v)
    return uniq


def _fetch_overpass_addresses_for_postcode(lat, lon, pc_variants, radius_m, user_agent):
    """POST to Overpass: buildings tagged addr:postcode within radius of Postcodes.io centroid."""
    if not pc_variants or lat is None or lon is None:
        return []
    parts = []
    r = int(radius_m)
    for pv in pc_variants:
        esc = pv.replace("\\", "").replace('"', "")
        parts.append(f'  node["addr:postcode"="{esc}"](around:{r},{lat},{lon});')
        parts.append(f'  way["addr:postcode"="{esc}"](around:{r},{lat},{lon});')
    q = "[out:json][timeout:25];\n(\n" + "\n".join(parts) + "\n);\nout center tags;\n"
    body = urlencode({"data": q}).encode("utf-8")
    req = urllib.request.Request(
        _overpass_interpreter_url(),
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json",
            "User-Agent": user_agent,
        },
    )
    with urllib.request.urlopen(req, timeout=28) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        return []
    return data.get("elements") or []


def _dedup_address_rows(rows):
    seen = set()
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        lat = r.get("latitude")
        lon = r.get("longitude")
        lk = (
            (r.get("address_line1") or "").strip().lower(),
            (r.get("address_town") or "").strip().lower(),
            _postcode_compact(r.get("address_postcode")),
            round(float(lat), 5) if lat is not None else None,
            round(float(lon), 5) if lon is not None else None,
        )
        if lk in seen:
            continue
        seen.add(lk)
        out.append(r)
    return out


def _nominatim_user_agent():
    """Descriptive User-Agent for public OSM services (Overpass, Nominatim, etc.)."""
    ua = current_app.config.get("NOMINATIM_USER_AGENT") or os.getenv(
        "TNW_NOMINATIM_USER_AGENT", ""
    )
    ua = str(ua).strip()
    if ua:
        return ua
    site = (os.getenv("SITE_URL") or "https://the-networker.co.uk").strip()
    return f"TheNetworkerDev/1.0 (UK postcode lookup; {site})"


def _address_lookup_ideal(postcode):
    api_key = current_app.config.get("IDEAL_POSTCODES_API_KEY") or os.getenv(
        "IDEAL_POSTCODES_API_KEY", ""
    )
    endpoint = current_app.config.get("IDEAL_POSTCODES_API_ENDPOINT") or os.getenv(
        "IDEAL_POSTCODES_API_ENDPOINT", ""
    )
    _debug_address_lookup(
        "config endpoint="
        + (endpoint or "<default>")
        + " key="
        + _mask_secret(api_key)
    )
    if not api_key:
        _debug_address_lookup("missing IDEAL_POSTCODES_API_KEY")
        return jsonify(
            ok=False,
            needs_config=True,
            error=(
                "Address lookup needs IDEAL_POSTCODES_API_KEY in .env. "
                "Create an Ideal Postcodes key and restart the app."
            ),
        ), 503

    try:
        url = (
            _ideal_postcodes_endpoint("postcodes/" + quote(postcode))
            + "?api_key="
            + quote(api_key)
        )
        _debug_address_lookup("calling " + _redact_query_secret(url))
        data = _urllib_json_get(url, {"Accept": "application/json"}, timeout=8)
        _debug_address_lookup(
            f"Ideal Postcodes response parsed keys={list(data.keys())[:8]!r}"
        )
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        _debug_address_lookup(
            f"Ideal Postcodes HTTPError status={exc.code} body={body[:500]!r}"
        )
        if exc.code == 404:
            return jsonify(ok=False, error="Postcode not found."), 404
        return jsonify(ok=False, error=f"Address lookup failed ({exc.code})."), 502
    except Exception as exc:
        _debug_address_lookup(
            f"Ideal Postcodes request exception={type(exc).__name__}: {exc}"
        )
        return jsonify(ok=False, error="Address lookup failed."), 502

    addresses = [
        _parse_ideal_postcodes_address(item)
        for item in data.get("result", [])
        if isinstance(item, dict)
    ]
    _debug_address_lookup(f"parsed {len(addresses)} address result(s)")
    return jsonify(
        ok=True,
        postcode=postcode,
        latitude=addresses[0].get("latitude") if addresses else None,
        longitude=addresses[0].get("longitude") if addresses else None,
        addresses=addresses,
    )


def _address_lookup_postcodesio_nominatim(postcode):
    """postcodes.io validates the postcode; Overpass finds OSM features with addr:postcode nearby.

    Postcodes.io returns one centroid/admin record per postcode (not a Royal-Mail-style premise list).
    OpenStreetMap Overpass yields multiple street-level candidates when mappers tagged addr:postcode.
    """
    target_pc = _postcode_compact(postcode)
    pio_url = _postcodes_io_api_base() + "/postcodes/" + quote(postcode)
    _debug_address_lookup("postcodes.io calling " + pio_url)
    try:
        pio = _urllib_json_get(pio_url, {"Accept": "application/json"}, timeout=8)
    except urllib.error.HTTPError as exc:
        _debug_address_lookup(
            f"postcodes.io HTTPError status={exc.code} {type(exc).__name__}"
        )
        if exc.code == 404:
            return jsonify(ok=False, error="Postcode not found."), 404
        return jsonify(ok=False, error=f"Postcode lookup failed ({exc.code})."), 502
    except Exception as exc:
        _debug_address_lookup(
            f"postcodes.io exception={type(exc).__name__}: {exc}"
        )
        return jsonify(ok=False, error="Postcode lookup failed."), 502

    if not isinstance(pio, dict) or int(pio.get("status") or 0) != 200:
        _debug_address_lookup(
            f"postcodes.io non-200 body status={pio.get('status')!r}"
        )
        return jsonify(ok=False, error="Postcode not found."), 404

    result = pio.get("result")
    centroid_row = _row_from_postcodes_io_result(result, postcode)
    if not centroid_row:
        return jsonify(ok=False, error="Postcode not found."), 404

    cent_lat = centroid_row.get("latitude")
    cent_lon = centroid_row.get("longitude")
    ua = _nominatim_user_agent()
    pc_variants = _overpass_postcode_variants(postcode, result if isinstance(result, dict) else {})
    rows = []
    if cent_lat is not None and cent_lon is not None and pc_variants:
        for radius_m in (2200, 4800):
            try:
                _debug_address_lookup(f"overpass addr:postcode around={radius_m}m variants={pc_variants!r}")
                elements = _fetch_overpass_addresses_for_postcode(
                    cent_lat, cent_lon, pc_variants, radius_m, ua
                )
            except Exception as exc:
                _debug_address_lookup(
                    f"overpass exception={type(exc).__name__}: {exc}"
                )
                elements = []
            for elem in elements:
                row = _row_from_overpass_element(elem, target_pc, centroid_row.get("address_postcode") or postcode)
                if row:
                    rows.append(row)
            rows = _dedup_address_rows(rows)
            if rows:
                break
            if radius_m == 2200:
                try:
                    time_mod.sleep(1.05)
                except Exception:
                    pass

    rows = _dedup_address_rows(rows)
    rows.sort(
        key=lambda r: _haversine_km(
            cent_lat, cent_lon, r.get("latitude"), r.get("longitude")
        )
    )
    addresses = rows[:25] if rows else [centroid_row]

    first = addresses[0]
    return jsonify(
        ok=True,
        postcode=postcode,
        latitude=first.get("latitude"),
        longitude=first.get("longitude"),
        addresses=addresses,
    )


def _row_from_nominatim_forward_item(item):
    """Map a Nominatim /search jsonv2 hit to the same shape as postcode lookup rows."""
    if not isinstance(item, dict):
        return None
    lat = _float_or_none(item.get("lat"))
    lon = _float_or_none(item.get("lon"))
    if lat is None or lon is None:
        return None
    addr = item.get("address") if isinstance(item.get("address"), dict) else {}
    pc = (addr.get("postcode") or "").strip()
    road = (addr.get("road") or "").strip()
    house = (addr.get("house_number") or "").strip()
    line1 = f"{house} {road}".strip()
    if not line1:
        line1 = (
            (addr.get("neighbourhood") or addr.get("suburb") or addr.get("quarter") or "").strip()
        )
    if not line1:
        nm = (item.get("name") or "").strip()
        if nm:
            line1 = nm
    if not line1:
        disp = (item.get("display_name") or "").strip()
        line1 = disp.split(",")[0].strip() if disp else ""
    if not line1:
        return None
    town = (
        (addr.get("city") or addr.get("town") or addr.get("village") or addr.get("hamlet") or "")
        .strip()
    )
    county = (addr.get("county") or addr.get("state_district") or "").strip()
    label = (item.get("display_name") or "").strip()
    if not label:
        label = ", ".join(p for p in [line1, town, county, pc] if p)
    venue_name = (
        (addr.get("amenity") or addr.get("shop") or addr.get("office") or addr.get("building") or "")
        .strip()
    )
    if venue_name and venue_name.lower() == line1.lower():
        venue_name = ""
    return {
        "label": label[:400],
        "venue_name": venue_name[:200],
        "address_line1": line1[:250],
        "address_line2": "",
        "address_town": town[:120],
        "address_county": county[:120],
        "address_postcode": pc[:16],
        "latitude": lat,
        "longitude": lon,
    }


def _address_lookup_nominatim_forward(query: str):
    """Free-text UK search via Nominatim (used when input is not a full postcode)."""
    ua = _nominatim_user_agent()
    params = {
        "q": query,
        "format": "jsonv2",
        "addressdetails": "1",
        "limit": "18",
        "countrycodes": "gb",
    }
    url = "https://nominatim.openstreetmap.org/search?" + urlencode(params)
    _debug_address_lookup("nominatim forward " + _redact_query_secret(url[:180]))
    try:
        items = _urllib_json_get(
            url,
            {"Accept": "application/json", "User-Agent": ua},
            timeout=12,
        )
    except urllib.error.HTTPError as exc:
        _debug_address_lookup(
            f"nominatim forward HTTPError status={exc.code} {type(exc).__name__}"
        )
        return jsonify(ok=False, error="Address search failed. Try again in a moment."), 502
    except Exception as exc:
        _debug_address_lookup(
            f"nominatim forward exception={type(exc).__name__}: {exc}"
        )
        return jsonify(ok=False, error="Address search failed."), 502

    if not isinstance(items, list):
        return jsonify(ok=False, error="Unexpected response from address search."), 502

    rows = []
    for it in items:
        row = _row_from_nominatim_forward_item(it)
        if row:
            rows.append(row)
    rows = _dedup_address_rows(rows)
    if not rows:
        return jsonify(
            ok=False,
            error="No UK matches found. Try a postcode, street with town, or a shorter phrase.",
        ), 404

    first = rows[0]
    return jsonify(
        ok=True,
        postcode=(first.get("address_postcode") or "").strip(),
        latitude=first.get("latitude"),
        longitude=first.get("longitude"),
        addresses=rows[:25],
    )


@bp.route("/api/address-lookup")
def address_lookup():
    query_raw = (request.args.get("q") or request.args.get("query") or "").strip()
    postcode = (request.args.get("postcode") or "").strip().upper()
    postcode = re.sub(r"\s+", " ", postcode)
    provider = _address_lookup_provider_slug()
    _debug_address_lookup(
        f"request postcode={postcode!r} q_len={len(query_raw)} provider={provider!r}"
    )

    if postcode and _UK_POSTCODE_RE.match(postcode):
        if provider == "ideal":
            return _address_lookup_ideal(postcode)
        return _address_lookup_postcodesio_nominatim(postcode)

    if query_raw:
        if len(query_raw) < 3:
            return jsonify(ok=False, error="Enter at least 3 characters to search."), 400
        if len(query_raw) > 200:
            query_raw = query_raw[:200]
        return _address_lookup_nominatim_forward(query_raw)

    _debug_address_lookup("rejected: no valid postcode or query")
    return jsonify(
        ok=False,
        error="Enter a UK postcode or an address, street, or place name to search.",
    ), 400


@bp.route("/postcode-lookup-test")
@login_required
def postcode_lookup_test():
    """Dev page: compare Postcodes.io and /api/address-lookup. Gated by TNW_ENABLE_POSTCODE_LOOKUP_TEST_PAGE."""
    if not current_app.config.get("ENABLE_POSTCODE_LOOKUP_TEST_PAGE"):
        abort(404)
    return render_template(
        "postcode_lookup_test.html",
        postcodes_io_base=_postcodes_io_api_base(),
        address_lookup_url=url_for("main.address_lookup"),
        address_lookup_provider=_address_lookup_provider_slug(),
    )


# ---------------------------------------------------------------------------
# Home / discovery
# ---------------------------------------------------------------------------
def _home_featured_meeting_price_label(meeting: Meeting, now_utc: datetime | None = None) -> str:
    """Short GBP / Free label from active ticket types currently on sale."""
    now_utc = now_utc or datetime.utcnow()
    amounts: list[Decimal] = []
    for tt in sorted(
        list(meeting.ticket_types or []),
        key=lambda t: (t.sort_order or 0, t.ticket_type_id or 0),
    ):
        if (tt.status or "").strip().lower() != "active":
            continue
        if tt.sales_open_at and tt.sales_open_at > now_utc:
            continue
        if tt.sales_close_at and tt.sales_close_at < now_utc:
            continue
        try:
            amounts.append(Decimal(tt.price_amount or 0))
        except (InvalidOperation, TypeError):
            amounts.append(Decimal("0"))
    if not amounts:
        return ""
    lo = min(amounts)
    hi = max(amounts)
    if lo <= 0 and hi <= 0:
        return "Free"
    if lo == hi:
        return f"GBP {lo:.2f}"
    return f"From GBP {lo:.2f}"


def _home_meeting_short_location(m: Meeting) -> str:
    """One-line locality for featured cards (virtual platform or town/city)."""
    fmt = (m.meeting_format or "").strip()
    if fmt == "Virtual":
        plat = (m.virtual_platform or "").strip()
        return plat if plat else "Online"
    for attr in ("address_town", "location_city", "venue_name"):
        val = getattr(m, attr, None)
        if val and str(val).strip():
            return str(val).strip()[:80]
    return "Location TBC"


def _hydrate_home_meeting_cards(meetings: list[Meeting], now_utc: datetime | None = None):
    """Attach price label, booking count, industry, and short location for home carousels."""
    now_utc = now_utc or datetime.utcnow()
    mids = [int(m.meeting_id) for m in meetings if m]
    booking_by_mid: dict[int, int] = {}
    if mids:
        for mid, n in (
            db.session.query(
                MeetingAttendee.meeting_id,
                func.count(MeetingAttendee.meeting_attendee_id),
            )
            .filter(MeetingAttendee.meeting_id.in_(mids))
            .group_by(MeetingAttendee.meeting_id)
            .all()
        ):
            booking_by_mid[int(mid)] = int(n)
    for m in meetings:
        setattr(m, "_home_price_label", _home_featured_meeting_price_label(m, now_utc))
        setattr(m, "_home_booking_count", booking_by_mid.get(int(m.meeting_id), 0))
        mg = m.meeting_group
        ind = ""
        if mg and mg.industry and (mg.industry.industry or "").strip():
            ind = (mg.industry.industry or "").strip()
        setattr(m, "_home_industry_label", ind or "General Business")
        setattr(m, "_home_location_short", _home_meeting_short_location(m))


def _similar_meetings_for_detail(
    meeting: Meeting, now_utc: datetime | None = None, limit: int = 20
) -> list[Meeting]:
    """Other live public-style meetings: same series first, then same industry, then the rest."""
    now_utc = now_utc or datetime.utcnow()
    mid = int(meeting.meeting_id)
    my_gid = int(meeting.meeting_group_id)
    mg = meeting.meeting_group
    my_iid = int(mg.industry_id) if mg and mg.industry_id is not None else None

    rows = (
        Meeting.query.join(
            MeetingGroup, Meeting.meeting_group_id == MeetingGroup.meeting_group_id
        )
        .filter(Meeting.meeting_id != mid)
        .filter(Meeting.status == "Live")
        .filter(
            MeetingGroup.image_filename.isnot(None),
            MeetingGroup.image_filename != "",
        )
        .options(
            selectinload(Meeting.meeting_group).selectinload(MeetingGroup.industry),
            selectinload(Meeting.ticket_types),
        )
        .limit(120)
        .all()
    )

    def _sort_key(m: Meeting) -> tuple:
        tier = 2
        if int(m.meeting_group_id) == my_gid:
            tier = 0
        elif my_iid is not None and m.meeting_group and m.meeting_group.industry_id == my_iid:
            tier = 1
        st = m.starts_at
        if st is None:
            return (tier, 2, 0.0)
        try:
            ts = st.timestamp()
        except (OSError, OverflowError, ValueError):
            return (tier, 2, 0.0)
        if st >= now_utc:
            return (tier, 0, ts)
        return (tier, 1, -ts)

    rows.sort(key=_sort_key)
    return rows[:limit]


_TNW_SERVICE_WORKER_JS = """self.addEventListener('install', function (event) {
  self.skipWaiting();
});
self.addEventListener('activate', function (event) {
  event.waitUntil(self.clients.claim());
});
self.addEventListener('fetch', function (event) {
  event.respondWith(fetch(event.request));
});
"""


@bp.route("/sw.js")
def tnw_service_worker():
    """Minimal pass-through service worker so the app meets PWA install criteria (Chrome/Edge)."""
    return Response(
        _TNW_SERVICE_WORKER_JS,
        mimetype="application/javascript",
        headers={"Cache-Control": "public, max-age=0"},
    )


def _home_events_near_and_geo_for_carousels() -> tuple[str, float | None, float | None, set[int], bool]:
    """Label for home headings plus geo/tags used to sort featured meeting carousels (home, FAQ rails)."""
    home_events_near = "your area"
    ref_lat: float | None = None
    ref_lng: float | None = None
    user_tag_ids: set[int] = set()
    logged_in = False
    uid = session.get("user_id")
    if uid:
        user = User.query.get(uid)
        if user:
            logged_in = True
            for t in user.attendee_tags or []:
                if t and t.tag_id is not None:
                    user_tag_ids.add(int(t.tag_id))
            if user.latitude is not None and user.longitude is not None:
                ref_lat = float(user.latitude)
                ref_lng = float(user.longitude)
                lat, lng = ref_lat, ref_lng
                cache_key = "home_events_near_location"
                cache_lat = session.get("home_events_near_lat")
                cache_lng = session.get("home_events_near_lng")
                if cache_key and cache_lat == lat and cache_lng == lng:
                    cached_value = session.get(cache_key)
                    if cached_value:
                        home_events_near = cached_value
                else:
                    try:
                        req = urllib.request.Request(
                            f"{_postcodes_io_api_base()}/postcodes?lon={lng}&lat={lat}&limit=1&radius=3000",
                            headers={"Accept": "application/json"},
                            method="GET",
                        )
                        with urllib.request.urlopen(req, timeout=3) as resp:
                            data = json.loads(resp.read().decode("utf-8"))
                        if data and data.get("status") == 200 and data.get("result"):
                            nearest = data["result"][0] or {}
                            local_name = (
                                nearest.get("admin_district")
                                or nearest.get("parliamentary_constituency")
                                or nearest.get("admin_county")
                            )
                            if local_name:
                                home_events_near = str(local_name).strip()
                                session[cache_key] = home_events_near
                                session["home_events_near_lat"] = lat
                                session["home_events_near_lng"] = lng
                    except Exception:
                        pass

            if home_events_near == "your area" and user.country and user.country.country:
                home_events_near = user.country.country
    else:
        guest_area = session.get("home_visitor_area")
        if guest_area:
            home_events_near = str(guest_area).strip() or home_events_near
        glat = session.get("home_visitor_lat")
        glng = session.get("home_visitor_lng")
        if glat is not None and glng is not None:
            try:
                ref_lat = float(glat)
                ref_lng = float(glng)
            except (TypeError, ValueError):
                ref_lat, ref_lng = None, None
        forwarded_for = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        client_ip = forwarded_for or (request.remote_addr or "").strip()
        try:
            ip_obj = ipaddress.ip_address(client_ip) if client_ip else None
        except ValueError:
            ip_obj = None
        if ref_lat is None and ip_obj and not (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_link_local
        ):
            try:
                req = urllib.request.Request(
                    f"https://ipapi.co/{client_ip}/json/",
                    headers={"Accept": "application/json"},
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                ip_area = (data.get("city") or data.get("region") or data.get("country_name") or "").strip()
                if ip_area:
                    home_events_near = ip_area
                    session["home_visitor_area"] = ip_area
                lat_raw = data.get("latitude")
                lng_raw = data.get("longitude")
                if lat_raw is not None and lng_raw is not None:
                    ref_lat = float(lat_raw)
                    ref_lng = float(lng_raw)
                    session["home_visitor_lat"] = ref_lat
                    session["home_visitor_lng"] = ref_lng
            except Exception:
                pass

    return home_events_near, ref_lat, ref_lng, user_tag_ids, logged_in


@bp.route("/")
def home():
    # Backup if before_request order ever skips the gate: never touch DB when migration is on.
    r = tnw_migration_notice_response_or_none()
    if r is not None:
        return r
    return home_legacy()


@bp.route("/home")
def home_legacy():
    r = tnw_migration_notice_response_or_none()
    if r is not None:
        return r
    now_utc = datetime.utcnow()
    home_events_near, ref_lat, ref_lng, user_tag_ids, logged_in = _home_events_near_and_geo_for_carousels()

    home_featured_meetings, home_more_meetings = _home_carousel_meeting_lists(
        now_utc,
        ref_lat=ref_lat,
        ref_lng=ref_lng,
        user_tag_ids=user_tag_ids if logged_in else None,
        logged_in=logged_in,
    )
    _hydrate_home_meeting_cards(
        list(home_featured_meetings) + list(home_more_meetings), now_utc
    )
    promote_hero = {}
    home_hide_create_strip = False
    if logged_in:
        uid = session.get("user_id")
        if uid:
            promote_hero = _home_promote_hero_context(int(uid))
            home_hide_create_strip = _home_user_has_events(int(uid))
    return render_template(
        "home.html",
        home_featured_meetings=home_featured_meetings,
        home_more_meetings=home_more_meetings,
        home_events_near=home_events_near,
        home_hide_create_strip=home_hide_create_strip,
        **promote_hero,
    )


@bp.route("/networking")
def networking_discovery():
    return render_template("pips2.html")


# ---------------------------------------------------------------------------
# Event wizard (first-time organiser flow)
# ---------------------------------------------------------------------------
WIZARD_GROUP_IMAGE_SESSION_KEY = "wizard_group_image_staging"


def _wizard_group_image_staging_abs_dir() -> str:
    base = os.path.join(current_app.root_path, "instance", "wizard_group_image_staging")
    os.makedirs(base, exist_ok=True)
    return base


def _wizard_staging_basename_valid(staged_fn: str, uid: int) -> bool:
    if not staged_fn or uid <= 0:
        return False
    prefix = f"ws_{uid}_"
    if not staged_fn.startswith(prefix) or not staged_fn.endswith(".png"):
        return False
    mid = staged_fn[len(prefix) : -len(".png")]
    if len(mid) != 16:
        return False
    return all(c in "0123456789abcdef" for c in mid.lower())


def _wizard_delete_staged_file_if_owned(staged_fn: str | None, uid: int) -> None:
    if not staged_fn or not _wizard_staging_basename_valid(staged_fn, uid):
        return
    staging_root = os.path.realpath(_wizard_group_image_staging_abs_dir())
    path = os.path.realpath(os.path.join(staging_root, staged_fn))
    if path != staging_root and not path.startswith(staging_root + os.sep):
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _wizard_discard_session_staged_group_image(uid: int) -> None:
    fn = session.pop(WIZARD_GROUP_IMAGE_SESSION_KEY, None)
    session.modified = True
    _wizard_delete_staged_file_if_owned(fn, uid)


@bp.route("/event-wizard/abandon-staged-group-image", methods=["POST"])
def event_wizard_abandon_staged_group_image():
    """Drop wizard staging when the user leaves the page without completing create."""
    uid = session.get("user_id")
    if uid:
        _wizard_discard_session_staged_group_image(int(uid))
    return ("", 204)


@bp.route("/event-wizard/stage-group-image", methods=["POST"])
@login_required
def event_wizard_stage_group_image():
    """Resize and store a meeting-group image in a temp folder; consumed on successful create."""
    uid = int(session["user_id"])
    user = User.query.get(uid)
    if not user:
        session.pop("user_id", None)
        return jsonify(ok=False, error="Please sign in again."), 401

    image_file = request.files.get("group_image")
    if not image_file or not (image_file.filename or "").strip():
        return jsonify(ok=False, error="Choose an image file."), 400

    prev_fn = session.get(WIZARD_GROUP_IMAGE_SESSION_KEY)
    _wizard_delete_staged_file_if_owned(prev_fn, uid)
    session.pop(WIZARD_GROUP_IMAGE_SESSION_KEY, None)

    staged = f"ws_{uid}_{secrets.token_hex(8)}.png"
    staging_root = _wizard_group_image_staging_abs_dir()
    target_path = os.path.join(staging_root, staged)
    try:
        _resize_meeting_group_image(image_file, target_path)
    except UnidentifiedImageError:
        return jsonify(ok=False, error="That file is not a valid image."), 400
    except Exception:
        current_app.logger.exception("event_wizard_stage_group_image: resize failed")
        try:
            os.remove(target_path)
        except OSError:
            pass
        return jsonify(ok=False, error="Could not process that image."), 500

    session[WIZARD_GROUP_IMAGE_SESSION_KEY] = staged
    session.modified = True
    return jsonify(ok=True, staged_filename=staged)


@bp.route("/event-wizard", methods=["GET"])
@login_required
def event_wizard():
    """Simple first-time event wizard (optional AI draft)."""
    uid = int(session.get("user_id") or 0)
    industries = Industry.query.order_by(Industry.industry.asc()).all()
    user_meeting_groups = (
        MeetingGroup.query.filter_by(user_id=uid)
        .order_by(MeetingGroup.meeting_group_name.asc())
        .all()
        if uid
        else []
    )
    mg_ids = [int(mg.meeting_group_id) for mg in user_meeting_groups]
    meetings_by_mg: dict[int, list[Meeting]] = {mid: [] for mid in mg_ids}
    if mg_ids:
        for m in (
            Meeting.query.filter(Meeting.meeting_group_id.in_(mg_ids))
            .order_by(Meeting.starts_at.desc(), Meeting.meeting_id.desc())
            .all()
        ):
            meetings_by_mg.setdefault(int(m.meeting_group_id), []).append(m)
    ew_user_meeting_groups_json = [
        _meeting_group_wizard_json_item(mg, meetings_by_mg.get(int(mg.meeting_group_id), []))
        for mg in user_meeting_groups
    ]
    return render_template(
        "event_wizard.html",
        industries=industries,
        user_meeting_groups=user_meeting_groups,
        ew_user_meeting_groups_json=ew_user_meeting_groups_json,
        ew_meeting_group_update_url=url_for("main.meeting_group_update"),
        ew_meeting_wizard_update_url=url_for("main.meeting_wizard_update"),
    )


def _word_count_plain(s: str) -> int:
    t = (s or "").strip()
    if not t:
        return 0
    return len(t.split())


@bp.route("/api/event-wizard/ai-draft", methods=["POST"])
@login_required
def api_event_wizard_ai_draft():
    """Gemini: draft meeting group + first event fields from a short prompt."""
    uid = session.get("user_id")
    if not uid or not User.query.get(uid):
        session.pop("user_id", None)
        return jsonify(ok=False, error="Please sign in again."), 401

    payload = request.get_json(silent=True) or {}
    prompt = (payload.get("prompt") or "").strip()
    meeting_format = (payload.get("meeting_format") or "").strip()
    if meeting_format not in ("Face2Face", "Virtual"):
        return jsonify(ok=False, error="Choose in person or virtual first."), 400
    if _word_count_plain(prompt) < 3:
        return jsonify(ok=False, error="Add at least 3 words to your description first."), 400
    if len(prompt) > 1200:
        prompt = prompt[:1200]

    raw_iid = str(payload.get("industry_id") or "").strip()
    try:
        locked_iid = int(raw_iid) if raw_iid else None
    except (TypeError, ValueError):
        locked_iid = None
    if locked_iid is not None and not Industry.query.get(int(locked_iid)):
        locked_iid = None

    topics_for_prompt = _admin_test_events_topics_tags_for_prompt(locked_iid)
    if not topics_for_prompt:
        topics_for_prompt = _admin_test_events_topics_tags_for_prompt(None)
    if not topics_for_prompt:
        return jsonify(ok=False, error="Topics are not configured."), 500

    allowed_ids = [t["industry_id"] for t in topics_for_prompt]
    system = (
        "You help a new UK business networking organiser draft their first event. "
        "Reply with a single JSON object only (no markdown fences). Keys: "
        '"group_name" (string, max 120), '
        '"group_description" (string, plain text, up to 3 short paragraphs; no HTML), '
        '"industry_id" (integer; MUST be one of allowed_industry_ids), '
        '"event_title" (string, max 120), '
        '"event_description" (string, plain text, 2–5 short paragraphs; no HTML). '
        "Do not invent prices or promises. Keep copy practical and specific."
    )
    user_msg = (
        f"Meeting format: {meeting_format} (Face2Face = in-person; Virtual = online).\n"
        f"allowed_industry_ids (JSON): {json.dumps(allowed_ids)}\n"
        f"topics (each has industry_id, industry label, tags): {json.dumps(topics_for_prompt, ensure_ascii=False)}\n"
        f"Organiser description:\n---\n{prompt}\n---\n"
        "Return JSON as specified."
    )
    ok_ai, result = _gemini_generate_json_object(
        system=system,
        user_msg=user_msg,
        temperature=0.5,
        max_output_tokens=1600,
        timeout_s=45,
        use_json_mime=True,
    )
    if not ok_ai:
        msg = str(result or "")[:800]
        status = 503 if "not configured" in msg.lower() else 502
        return jsonify(ok=False, error=msg), status
    if not isinstance(result, dict):
        return jsonify(ok=False, error="Unexpected AI response shape."), 502

    try:
        ai_iid = (
            int(result.get("industry_id"))
            if result.get("industry_id") is not None
            else None
        )
    except (TypeError, ValueError):
        ai_iid = None
    if locked_iid is not None:
        resolved_iid = locked_iid
    else:
        id_set = set(allowed_ids)
        resolved_iid = ai_iid if ai_iid in id_set else (min(id_set) if id_set else None)
    if resolved_iid is None:
        return jsonify(ok=False, error="Could not resolve topic from AI response."), 502

    def _clip(s: str, n: int) -> str:
        s = (s or "").strip()
        return (s[: n - 1] + "…") if len(s) > n else s

    return jsonify(
        ok=True,
        industry_id=int(resolved_iid),
        group_name=_clip(str(result.get("group_name") or ""), 180),
        group_description=_clip(str(result.get("group_description") or ""), 8000),
        event_title=_clip(str(result.get("event_title") or ""), 180),
        event_description=_clip(str(result.get("event_description") or ""), 12000),
    )


@bp.route("/api/event-wizard/ai-rewrite-group-description", methods=["POST"])
@login_required
def api_event_wizard_ai_rewrite_group_description():
    """Gemini: polish the organiser's group description text in place (wizard step 1)."""
    uid = session.get("user_id")
    if not uid or not User.query.get(uid):
        session.pop("user_id", None)
        return jsonify(ok=False, error="Please sign in again."), 401

    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if len(text) > 8000:
        text = text[:8000]
    if _word_count_plain(text) < 3:
        return jsonify(ok=False, error="Add at least 3 words to your group description first."), 400

    ok_ai, result = _gemini_polish_meeting_description(text, description_kind="meeting-group")
    if not ok_ai:
        msg = str(result or "")[:800]
        status = 503 if "not configured" in msg.lower() else 502
        return jsonify(ok=False, error=msg), status

    polished = (result or "").strip()
    if not polished:
        return jsonify(ok=False, error="AI returned an empty result. Try again."), 502

    return jsonify(ok=True, group_description=polished[:8000])


_EVENT_WIZARD_REFUND_POLICIES = frozenset(
    {
        "No refunds",
        "Refund up to 7 days before",
        "Refund up to 24 hours before",
        "Manual review",
    }
)


def _event_wizard_ticket_form_value(key: str, index: int) -> str:
    lst = request.form.getlist(key)
    if index >= len(lst):
        return ""
    raw = lst[index]
    return raw if raw is not None else ""


def _event_wizard_parse_ticket_payload_at_index(
    starts_at: datetime, duration_minutes: int, index: int
) -> tuple[dict | None, str | None]:
    """Validate one wizard ticket row. Blank name → (None, None); invalid → (None, err)."""
    ticket_name = _event_wizard_ticket_form_value("ticket_name", index).strip()
    if not ticket_name:
        return None, None

    currency_code = (_event_wizard_ticket_form_value("currency_code", index) or "GBP").strip().upper()
    try:
        max_quantity = _parse_optional_int(_event_wizard_ticket_form_value("max_quantity", index))
        vat_mode, vat_rate_percent, price_amount = normalize_vat_from_form(
            request.form,
            _event_wizard_ticket_form_value("price_amount", index),
            index=index,
        )
    except ValueError:
        return None, "Ticket price and capacity values are not valid."

    try:
        sales_open_at = _parse_optional_date_as_datetime(
            _event_wizard_ticket_form_value("sales_open_at", index)
        )
        sales_close_at = _parse_optional_date_as_datetime(
            _event_wizard_ticket_form_value("sales_close_at", index), end_of_day=True
        )
    except ValueError:
        return None, "The ticket sales dates are not valid."

    refund_raw = _event_wizard_ticket_form_value("refund_policy", index).strip()
    if refund_raw not in _EVENT_WIZARD_REFUND_POLICIES:
        refund_raw = "No refunds"

    ticket_notes = _event_wizard_ticket_form_value("ticket_notes", index).strip() or None
    if ticket_notes and len(ticket_notes) > 1000:
        ticket_notes = ticket_notes[:1000]

    status_raw = _event_wizard_ticket_form_value("ticket_status", index).strip()
    ticket_status = "Active" if status_raw == "Active" else "Draft"

    errors: list[str] = []
    if currency_code != "GBP":
        errors.append("Only GBP is currently supported.")
    if price_amount is None or price_amount < 0:
        errors.append("Ticket price must be zero or more.")
    if max_quantity is None or max_quantity <= 0:
        errors.append("Maximum attendees must be greater than zero.")
    meeting_ends_at: datetime | None = None
    if starts_at and duration_minutes:
        meeting_ends_at = starts_at + timedelta(minutes=int(duration_minutes))
        if sales_close_at and meeting_ends_at and sales_close_at > meeting_ends_at:
            # Date-only "sales close" uses end-of-day; same-day events end earlier — cap at event end.
            sales_close_at = meeting_ends_at
    if sales_open_at and sales_close_at and sales_close_at <= sales_open_at:
        errors.append(
            "Sales close must be after sales open (check dates relative to when the event ends)."
        )

    if errors:
        return None, " ".join(errors)

    mq = max(1, int(max_quantity))
    price_dec = price_amount if price_amount is not None else Decimal("0")
    vat_dec = vat_rate_percent if vat_rate_percent is not None else Decimal("0")
    now = datetime.utcnow()
    return (
        {
            "ticket_name": ticket_name[:100],
            "ticket_description": None,
            "currency_code": currency_code[:3],
            "price_amount": price_dec,
            "max_quantity": mq,
            "max_tickets_per_user": mq,
            "sales_open_at": sales_open_at,
            "sales_close_at": sales_close_at,
            "vat_rate_percent": vat_dec,
            "vat_treatment": vat_mode,
            "refund_policy": refund_raw[:200],
            "ticket_notes": ticket_notes,
            "status": ticket_status,
            "sort_order": 0,
            "created_at": now,
            "updated_at": now,
        },
        None,
    )


def _event_wizard_parse_all_ticket_payloads(
    starts_at: datetime, duration_minutes: int
) -> tuple[list[dict] | None, str | None]:
    """Parse every posted ticket row (empty names skipped). At least one ticket required."""
    names = request.form.getlist("ticket_name")
    if not names:
        return None, "Add at least one ticket type."
    payloads: list[dict] = []
    for i in range(len(names)):
        payload, err = _event_wizard_parse_ticket_payload_at_index(
            starts_at, duration_minutes, i
        )
        if err:
            return None, err
        if payload:
            payloads.append(payload)
    if not payloads:
        return None, "Add at least one ticket type with a name."
    for sort_order, p in enumerate(payloads):
        p["sort_order"] = sort_order
    return payloads, None


class _TnwSqlalchemyEcho:
    """Temporarily echo SQLAlchemy SQL to stderr for one request-scoped block."""

    def __init__(self, log_tag: str) -> None:
        self._log_tag = log_tag
        self._states: list[tuple[object, object]] = []
        self._sa_logs: list[logging.Logger] = []
        self._sa_levels: dict[str, int] = {}
        self._added_handlers: list[logging.Handler] = []

    def __enter__(self) -> "_TnwSqlalchemyEcho":
        tag = self._log_tag
        try:
            engines_map = getattr(db, "engines", None)
            if isinstance(engines_map, dict) and engines_map:
                for eng in engines_map.values():
                    prev = getattr(eng, "echo", False)
                    self._states.append((eng, prev))
                    eng.echo = "debug"
            else:
                eng = db.engine
                prev = getattr(eng, "echo", False)
                self._states.append((eng, prev))
                eng.echo = "debug"
        except Exception as exc:
            print(f"[{tag}] WARN could not enable SQL echo: {exc}", flush=True)

        # SQLAlchemy logs SQL on `sqlalchemy.engine.Engine` (and sometimes `sqlalchemy.engine`).
        for name in ("sqlalchemy.engine.Engine", "sqlalchemy.engine"):
            lg = logging.getLogger(name)
            self._sa_logs.append(lg)
            self._sa_levels[name] = lg.level
            lg.setLevel(logging.INFO)
            h = logging.StreamHandler(stream=sys.stderr)
            h.setLevel(logging.INFO)
            h.setFormatter(logging.Formatter(f"[{tag}] %(message)s"))
            lg.addHandler(h)
            self._added_handlers.append(h)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for lg, h in zip(self._sa_logs, self._added_handlers):
            try:
                lg.removeHandler(h)
            except ValueError:
                pass
        for name, lvl in self._sa_levels.items():
            try:
                logging.getLogger(name).setLevel(lvl)
            except Exception:
                pass
        self._added_handlers.clear()
        self._sa_logs.clear()
        self._sa_levels.clear()
        for eng, prev in self._states:
            try:
                eng.echo = prev
            except Exception:
                pass
        self._states.clear()
        return False


def _event_wizard_debug_log(msg: str) -> None:
    line = f"[event_wizard_create] {msg}"
    print(line, flush=True)
    try:
        current_app.logger.info("%s", line)
    except RuntimeError:
        pass


def _wizard_min_repeat_until_date(starts_at: datetime, pattern: str) -> date:
    """Earliest allowed repeat-until date: start + 7d (weekly) or +1 calendar month (monthly)."""
    d0 = starts_at.date()
    if pattern == "weekly":
        return d0 + timedelta(days=7)
    if pattern in ("monthly_dom", "monthly_nth"):
        y, m, day = d0.year, d0.month, d0.day
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
        last_dom = calendar.monthrange(y, m)[1]
        return date(y, m, min(day, last_dom))
    return d0


_EW_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def _event_wizard_repeat_email_label(
    repeat_pattern_norm: str | None,
    repeat_until_date: date | None,
    repeat_nth_week: int | None,
    repeat_weekday: int | None,
) -> str:
    if not repeat_pattern_norm:
        return "Does not repeat"
    until = f" — until {repeat_until_date.isoformat()}" if repeat_until_date else ""
    if repeat_pattern_norm == "weekly":
        return "Weekly" + until
    if repeat_pattern_norm == "monthly_dom":
        return "Monthly (same calendar date)" + until
    if repeat_pattern_norm == "monthly_nth":
        nth_txt = ""
        if repeat_nth_week == -1:
            nth_txt = "last"
        elif repeat_nth_week is not None:
            ordmap = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"}
            nth_txt = ordmap.get(int(repeat_nth_week), str(repeat_nth_week))
        wd_txt = ""
        if repeat_weekday is not None and 0 <= int(repeat_weekday) <= 6:
            wd_txt = _EW_WEEKDAY_NAMES[int(repeat_weekday)]
        human = (nth_txt + " " + wd_txt).strip()
        return "Monthly — " + human + until
    return "Repeating" + until


def _send_event_wizard_completion_email(
    *,
    user: User,
    mg: MeetingGroup,
    master_meeting: Meeting,
    repeat_meetings: list[Meeting],
    publish_wizard: bool,
    meeting_status: str,
    meeting_format: str,
    industry_label: str,
    website_group: str | None,
    event_website_url: str | None,
    group_desc_html: str,
    event_title: str,
    event_desc_html: str,
    duration_minutes: int,
    repeat_label: str,
    wizard_ticket_payloads: list[dict],
    venue_postcode: str | None,
    venue_name: str | None,
    address_line1: str | None,
    address_town: str | None,
) -> None:
    """Post-success summary email (SMTP_* env). Never raises — caller wraps in try/except for logging."""
    to_addr = (getattr(user, "email", None) or "").strip()
    if not to_addr or not EMAIL_RE.match(to_addr):
        return

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    site_name = os.getenv("SITE_NAME", "The Networker")
    support_email = os.getenv("SUPPORT_EMAIL", smtp_user)
    if not smtp_user or not smtp_password:
        return

    lbl_mg = table_label("event_groups", "Event groups")
    lbl_mt = table_label("events", "Events")
    mg_name = (mg.meeting_group_name or "").strip() or lbl_mg
    live = str(meeting_status or "").strip().lower() == "live"
    status_headline = (
        f"Your scheduled {lbl_mt.lower()} are live on {site_name}."
        if live
        else f"Your listing is saved as a draft on {site_name}."
    )
    status_detail_plain = (
        f"Status: Live — your {lbl_mt.lower()} are visible according to how you published them."
        if live
        else f"Status: Draft — your {lbl_mt.lower()} are not public until you publish from My {lbl_mg}."
    )
    status_detail_html = (
        f"<strong style=\"color:#1a6b3a;\">Status: Live</strong> — your {lbl_mt.lower()} are visible "
        f"according to how you published them."
        if live
        else f"<strong style=\"color:#6b5c00;\">Status: Draft</strong> — your {lbl_mt.lower()} are not "
        f"public until you publish from My {escape(lbl_mg)}."
    )

    if not publish_wizard or not live:
        edit_note_plain = (
            f"While saved as a draft, you can edit all details from My {lbl_mg} before you publish."
        )
        edit_note_html = (
            f"<p>While saved as a draft, you can edit all details from <strong>My {escape(lbl_mg)}</strong> "
            "before you publish.</p>"
        )
    elif meeting_format == "Face2Face" and wizard_ticket_payloads:
        edit_note_plain = (
            "You can still edit event and ticket details until the first ticket is sold."
        )
        edit_note_html = (
            "<p>You can still edit event and ticket details <strong>until the first ticket is sold</strong>.</p>"
        )
    else:
        edit_note_plain = "You can still edit your event details from My events in your dashboard."
        edit_note_html = (
            "<p>You can still edit your event details from <strong>My events</strong> in your dashboard.</p>"
        )

    my_events_url = url_for(
        "main.platform_dashboard",
        meeting_groups_page=1,
        meeting_group_id=int(mg.meeting_group_id),
        _anchor="meetings-pane",
        _external=True,
    )

    group_plain = _rich_text_plain_text(group_desc_html) or "—"
    event_plain = _rich_text_plain_text(event_desc_html) or "—"
    fmt_line = "In-person venue" if meeting_format == "Face2Face" else "Online"

    venue_lines_plain: list[str] = []
    if meeting_format == "Face2Face":
        if (venue_postcode or "").strip():
            venue_lines_plain.append(f"Postcode: {venue_postcode.strip()}")
        if (venue_name or "").strip():
            venue_lines_plain.append(f"Venue: {venue_name.strip()}")
        if (address_line1 or "").strip():
            venue_lines_plain.append(f"Address: {address_line1.strip()}")
        if (address_town or "").strip():
            venue_lines_plain.append(f"City / town: {address_town.strip()}")

    ticket_lines_plain: list[str] = []
    if meeting_format == "Face2Face" and wizard_ticket_payloads:
        for p in wizard_ticket_payloads:
            nm = (p.get("ticket_name") or "").strip() or "Ticket"
            try:
                amt = p.get("price_amount")
                price_dec = Decimal(str(amt)) if amt is not None else Decimal("0")
            except (InvalidOperation, TypeError, ValueError):
                price_dec = Decimal("0")
            cur = (p.get("currency_code") or "GBP").strip().upper()
            ticket_lines_plain.append(
                f"  - {nm}: {'Free' if price_dec <= 0 else f'{cur} {price_dec:.2f}'}"
            )
    elif meeting_format == "Virtual":
        ticket_lines_plain.append("  (Online listing — no in-wizard ticket tiers.)")

    meetings_all = [master_meeting] + list(repeat_meetings or [])
    event_rows_plain: list[str] = []
    for m in meetings_all:
        st = (m.status or meeting_status or "").strip() or "—"
        sa = m.starts_at
        if sa and isinstance(sa, datetime):
            ts = sa.strftime("%a %d %b %Y, %H:%M UTC")
        else:
            ts = "—"
        event_rows_plain.append(f"  - {(m.title or '').strip() or '—'}  |  starts {ts}  |  {st}")

    fees_plain = (
        "There is no charge for free events. We only take 2% of any ticket sales (paid tickets only)."
    )

    promote_plain = (
        "Tip: plan to promote your listing once sharing tools are available on The Networker "
        "(coming soon — not in the dashboard yet)."
    )

    subject = f"[{site_name}] Your new {lbl_mg.lower()}: {mg_name[:120]}"

    plain_parts = [
        f"Hello{((' ' + (user.first_name or '').strip()) if (user.first_name or '').strip() else '')},",
        "",
        status_headline,
        "",
        status_detail_plain,
        "",
        f"--- {lbl_mg} ---",
        f"Name: {mg_name}",
        f"Topic: {(industry_label or '').strip() or '—'}",
        f"Group website: {(website_group or '').strip() or '—'}",
        f"Description: {group_plain}",
        "",
        f"--- {lbl_mt} ---",
        f"Title: {event_title.strip() or '—'}",
        f"Format: {fmt_line}",
        f"Duration: {int(duration_minutes)} minutes",
        f"Repeat schedule: {repeat_label}",
        f"Event description: {event_plain}",
        f"Event website / link: {(event_website_url or '').strip() or '—'}",
        "",
    ]
    if venue_lines_plain:
        plain_parts.append("Venue")
        plain_parts.extend(venue_lines_plain)
        plain_parts.append("")
    plain_parts.append(f"Scheduled {lbl_mt.lower()} ({len(meetings_all)})")
    plain_parts.extend(event_rows_plain)
    plain_parts.append("")
    if ticket_lines_plain:
        plain_parts.append("Tickets")
        plain_parts.extend(ticket_lines_plain)
        plain_parts.append("")
    plain_parts.extend(
        [
            edit_note_plain,
            "",
            fees_plain,
            "",
            promote_plain,
            "",
            f"Open this group in My {lbl_mg}:",
            my_events_url,
            "",
            f"Questions? {support_email}",
            "",
            f"— {site_name}",
        ]
    )
    plain_body = "\n".join(plain_parts)

    rows_html = []
    for m in meetings_all:
        st = escape((m.status or meeting_status or "").strip() or "—")
        sa = m.starts_at
        if sa and isinstance(sa, datetime):
            ts = escape(sa.strftime("%a %d %b %Y, %H:%M UTC"))
        else:
            ts = "—"
        ttl = escape((m.title or "").strip() or "—")
        st_style = "#1a6b3a" if str(m.status or meeting_status or "").strip().lower() == "live" else "#6b5c00"
        rows_html.append(
            f"<tr><td style=\"padding:6px 8px;border-bottom:1px solid #e6dcec;\">{ttl}</td>"
            f"<td style=\"padding:6px 8px;border-bottom:1px solid #e6dcec;\">{ts}</td>"
            f"<td style=\"padding:6px 8px;border-bottom:1px solid #e6dcec;color:{st_style};font-weight:600;\">{st}</td></tr>"
        )
    tickets_html = ""
    if meeting_format == "Face2Face" and wizard_ticket_payloads:
        trows = []
        for p in wizard_ticket_payloads:
            nm = escape((p.get("ticket_name") or "").strip() or "Ticket")
            try:
                amt = p.get("price_amount")
                price_dec = Decimal(str(amt)) if amt is not None else Decimal("0")
            except (InvalidOperation, TypeError, ValueError):
                price_dec = Decimal("0")
            cur = escape((p.get("currency_code") or "GBP").strip().upper())
            price_txt = "Free" if price_dec <= 0 else f"{cur} {price_dec:.2f}"
            trows.append(
                f"<tr><td style=\"padding:6px 8px;border-bottom:1px solid #e6dcec;\">{nm}</td>"
                f"<td style=\"padding:6px 8px;border-bottom:1px solid #e6dcec;\">{price_txt}</td></tr>"
            )
        tickets_html = (
            "<h3 style=\"color:#5b2d73;margin-top:18px;\">Tickets</h3>"
            "<table style=\"border-collapse:collapse;width:100%;max-width:520px;\">"
            "<thead><tr><th align=\"left\" style=\"padding:6px 8px;border-bottom:2px solid #5b2d73;\">Type</th>"
            "<th align=\"left\" style=\"padding:6px 8px;border-bottom:2px solid #5b2d73;\">Price</th></tr></thead>"
            f"<tbody>{''.join(trows)}</tbody></table>"
        )
    elif meeting_format == "Virtual":
        tickets_html = (
            "<p style=\"color:#5c4a66;\"><em>Online listing — no in-wizard ticket tiers.</em></p>"
        )

    venue_html = ""
    if meeting_format == "Face2Face" and venue_lines_plain:
        venue_html = "<h3 style=\"color:#5b2d73;margin-top:18px;\">Venue</h3><ul>" + "".join(
            f"<li>{escape(line)}</li>" for line in venue_lines_plain
        ) + "</ul>"

    html_body = (
        "<html><body style=\"font-family:Arial,Helvetica,sans-serif;color:#2f1f3a;line-height:1.5;\">"
        f"<h2 style=\"color:#5b2d73;\">Your listing summary</h2>"
        f"<p style=\"font-size:16px;\">{escape(status_headline)}</p>"
        f"<p>{status_detail_html}</p>"
        f"{edit_note_html}"
        "<hr style=\"border:none;border-top:1px solid #e6dcec;margin:20px 0;\">"
        f"<h3 style=\"color:#5b2d73;\">{escape(lbl_mg)}</h3>"
        "<table style=\"border-collapse:collapse;\">"
        f"<tr><td style=\"padding:4px 12px 4px 0;color:#6b5c75;\">Name</td><td>{escape(mg_name)}</td></tr>"
        f"<tr><td style=\"padding:4px 12px 4px 0;color:#6b5c75;\">Topic</td>"
        f"<td>{escape((industry_label or '').strip() or '—')}</td></tr>"
        f"<tr><td style=\"padding:4px 12px 4px 0;color:#6b5c75;\">Group website</td>"
        f"<td>{escape((website_group or '').strip() or '—')}</td></tr>"
        f"<tr><td style=\"padding:4px 12px 4px 0;vertical-align:top;color:#6b5c75;\">Description</td>"
        f"<td style=\"white-space:pre-wrap;\">{escape(group_plain)}</td></tr>"
        "</table>"
        f"<h3 style=\"color:#5b2d73;margin-top:18px;\">{escape(lbl_mt)}</h3>"
        "<table style=\"border-collapse:collapse;\">"
        f"<tr><td style=\"padding:4px 12px 4px 0;color:#6b5c75;\">Title</td><td>{escape(event_title.strip() or '—')}</td></tr>"
        f"<tr><td style=\"padding:4px 12px 4px 0;color:#6b5c75;\">Format</td><td>{escape(fmt_line)}</td></tr>"
        f"<tr><td style=\"padding:4px 12px 4px 0;color:#6b5c75;\">Duration</td>"
        f"<td>{int(duration_minutes)} minutes</td></tr>"
        f"<tr><td style=\"padding:4px 12px 4px 0;color:#6b5c75;\">Repeat</td><td>{escape(repeat_label)}</td></tr>"
        f"<tr><td style=\"padding:4px 12px 4px 0;vertical-align:top;color:#6b5c75;\">Event description</td>"
        f"<td style=\"white-space:pre-wrap;\">{escape(event_plain)}</td></tr>"
        f"<tr><td style=\"padding:4px 12px 4px 0;color:#6b5c75;\">Event website</td>"
        f"<td>{escape((event_website_url or '').strip() or '—')}</td></tr>"
        "</table>"
        f"{venue_html}"
        f"<h3 style=\"color:#5b2d73;margin-top:18px;\">Scheduled {escape(lbl_mt.lower())} ({len(meetings_all)})</h3>"
        "<table style=\"border-collapse:collapse;width:100%;max-width:640px;\">"
        "<thead><tr>"
        "<th align=\"left\" style=\"padding:6px 8px;border-bottom:2px solid #5b2d73;\">Title</th>"
        "<th align=\"left\" style=\"padding:6px 8px;border-bottom:2px solid #5b2d73;\">Starts (UTC)</th>"
        "<th align=\"left\" style=\"padding:6px 8px;border-bottom:2px solid #5b2d73;\">Status</th>"
        "</tr></thead><tbody>"
        f"{''.join(rows_html)}"
        "</tbody></table>"
        f"{tickets_html}"
        "<hr style=\"border:none;border-top:1px solid #e6dcec;margin:20px 0;\">"
        f"<p style=\"font-size:14px;\">{escape(fees_plain)}</p>"
        f"<p style=\"font-size:14px;color:#5c4a66;\">{escape(promote_plain)}</p>"
        f"<p><a href=\"{escape(my_events_url)}\" style=\"color:#5b2d73;font-weight:600;\">"
        f"Open this {escape(lbl_mg.lower())} in My {escape(lbl_mg)}</a></p>"
        f"<p style=\"font-size:12px;color:#6b5c75;\">Support: {escape(support_email)}</p>"
        "</body></html>"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=smtp_user.split("@")[-1])
    msg["X-Auto-Response-Suppress"] = "All"
    msg["X-Mailer"] = f"{site_name} event wizard"
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(smtp_host, smtp_port, timeout=25) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)


def _event_wizard_try_finish_existing_meeting_update(
    *,
    user: User,
    publish_wizard: bool,
    meeting_format: str,
    existing_mg_id: int | None,
    existing_mg: MeetingGroup | None,
    repeat_pattern_norm: str | None,
    title: str,
    subject: str,
    starts_at: datetime,
    duration_minutes: int,
    event_meeting_url: str | None,
    location_city_v: str | None,
    location_postcode_v: str | None,
    location_country_v: str | None,
    venue_name_v: str | None,
    address_line1_v: str | None,
    address_line2_v: str | None,
    address_town_v: str | None,
    address_county_v: str | None,
    address_postcode_v: str | None,
    address_country_v: str | None,
    latitude_v,
    longitude_v,
    virtual_platform_v: str | None,
    virtual_link_v: str | None,
    wizard_ticket_payloads: list[dict],
    has_paid_ticket: bool,
):
    """When the wizard targets an existing event, update it instead of inserting a duplicate."""
    existing_meeting_id = request.form.get("existing_meeting_id", type=int)
    if not existing_meeting_id:
        return None

    meeting = Meeting.query.get(int(existing_meeting_id))
    if not meeting or int(meeting.creator_user_id) != int(user.user_id):
        flash("Choose a valid event you own before saving.", "warning")
        return redirect(url_for("main.event_wizard"))

    mg = MeetingGroup.query.get(int(meeting.meeting_group_id))
    if not mg or int(mg.user_id) != int(user.user_id):
        flash("That event group was not found.", "warning")
        return redirect(url_for("main.event_wizard"))

    if existing_mg_id and int(existing_mg_id) != int(mg.meeting_group_id):
        flash("The selected event does not belong to that event group.", "warning")
        return redirect(url_for("main.event_wizard"))

    if existing_mg is not None and int(existing_mg.meeting_group_id) != int(mg.meeting_group_id):
        flash("The selected event does not belong to that event group.", "warning")
        return redirect(url_for("main.event_wizard"))

    if _sold_qty_by_meeting_ids([int(meeting.meeting_id)]).get(int(meeting.meeting_id), 0) > 0:
        flash(MEETING_LOCKED_AFTER_TICKET_SALES_TEXT, "warning")
        return redirect(url_for("main.event_wizard"))

    if repeat_pattern_norm:
        flash(
            "Repeating schedules apply only when you create a new event. "
            "Change this event only, or pick “Create a new event” for a series.",
            "warning",
        )
        return redirect(url_for("main.event_wizard"))

    meeting_status = "Live" if publish_wizard else "Draft"
    is_paid_and_published_flag = bool(publish_wizard and has_paid_ticket)
    previous_meeting_status = (meeting.status or "").strip()

    if publish_wizard:
        for payload in wizard_ticket_payloads:
            payload["status"] = "Active"

    repeat_title_base = title[:180]

    _event_wizard_debug_log(
        f"update existing meeting_id={meeting.meeting_id} publish={publish_wizard} "
        f"format={meeting_format} ticket_rows={len(wizard_ticket_payloads)}"
    )

    with _TnwSqlalchemyEcho("event_wizard SQL (update existing)"):
        mg.meeting_format = meeting_format
        meeting.meeting_format = meeting_format
        meeting.title = repeat_title_base
        meeting.subject = subject
        meeting.starts_at = starts_at
        meeting.duration_minutes = int(duration_minutes)
        meeting.website_url = event_meeting_url
        meeting.location_city = location_city_v
        meeting.location_postcode = location_postcode_v
        meeting.location_country = location_country_v
        meeting.venue_name = venue_name_v
        meeting.address_line1 = address_line1_v
        meeting.address_line2 = address_line2_v
        meeting.address_town = address_town_v or location_city_v
        meeting.address_county = address_county_v
        meeting.address_postcode = address_postcode_v or location_postcode_v
        meeting.address_country = address_country_v
        meeting.latitude = latitude_v
        meeting.longitude = longitude_v
        meeting.virtual_platform = virtual_platform_v
        meeting.virtual_link = virtual_link_v
        meeting.status = meeting_status
        meeting.is_paid_and_published = is_paid_and_published_flag

        MeetingTicketType.query.filter_by(meeting_id=int(meeting.meeting_id)).delete(
            synchronize_session=False
        )
        if meeting_format == "Face2Face" and wizard_ticket_payloads:
            mid = int(meeting.meeting_id)
            for payload in wizard_ticket_payloads:
                db.session.add(MeetingTicketType(meeting_id=mid, **payload))

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            current_app.logger.exception(
                "event_wizard_create: integrity error updating existing meeting"
            )
            flash(
                "Could not save your event because of a data conflict. Please review your entries and try again.",
                "danger",
            )
            return redirect(url_for("main.event_wizard"))
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "event_wizard_create: database commit failed updating existing meeting"
            )
            flash("We could not save your event. Please try again.", "danger")
            return redirect(url_for("main.event_wizard"))

    _event_wizard_debug_log(
        f"COMMIT OK (update existing) meeting_group_id={mg.meeting_group_id} "
        f"meeting_id={meeting.meeting_id}"
    )

    if publish_wizard:
        _tnw_commit_event_listing_record(
            int(user.user_id), int(meeting.meeting_id), previous_meeting_status
        )

    ntix = len(wizard_ticket_payloads)
    if publish_wizard:
        if meeting_format == "Face2Face" and wizard_ticket_payloads:
            tmsg = (
                "Your event is live and tickets are on sale."
                if ntix == 1
                else f"Your event is live with {ntix} ticket types on sale."
            )
            flash(
                tmsg
                + " You can still edit event and ticket details until the first ticket is sold.",
                "success",
            )
        else:
            flash(
                "Your online event is live."
                + " You can still edit event details from your dashboard.",
                "success",
            )
    else:
        flash(
            (
                (
                    "Event updated with 1 ticket type saved as draft."
                    if ntix == 1
                    else f"Event updated with {ntix} ticket types saved as draft."
                )
                if meeting_format == "Face2Face" and wizard_ticket_payloads
                else "Event updated and saved as draft."
            ),
            "success",
        )

    return redirect(
        url_for(
            "main.platform_dashboard",
            meeting_group_id=int(mg.meeting_group_id),
            _anchor="directory-pane",
        )
    )


@bp.route("/event-wizard/create", methods=["POST"])
@login_required
def event_wizard_create():
    """Create or update event group + event from the wizard (draft or published)."""
    uid = int(session.get("user_id") or 0)
    user = User.query.get(uid) if uid else None
    if not user:
        session.pop("user_id", None)
        flash("Please sign in again.", "warning")
        return redirect(url_for("main.login"))

    finalize_raw = (request.form.get("wizard_finalize_action") or "draft").strip().lower()
    publish_wizard = finalize_raw == "publish"

    _event_wizard_debug_log(
        f"POST received form_fields={len(request.form)} files={list(request.files.keys())} "
        f"finalize={finalize_raw!r}"
    )

    meeting_format = (request.form.get("meeting_format") or "").strip()
    if meeting_format not in ("Face2Face", "Virtual"):
        flash("Choose in person or virtual.", "warning")
        return redirect(url_for("main.event_wizard"))

    existing_mg_id = request.form.get("existing_meeting_group_id", type=int)
    existing_mg = None
    if existing_mg_id:
        existing_mg = MeetingGroup.query.filter_by(
            meeting_group_id=int(existing_mg_id),
            user_id=user.user_id,
        ).first()
        if not existing_mg:
            flash("Choose a valid event group you own.", "warning")
            return redirect(url_for("main.event_wizard"))
        if not existing_mg.industry_id:
            flash(
                "That event group has no topic set. Edit it from My Events before adding events.",
                "warning",
            )
            return redirect(url_for("main.event_wizard"))
        industry_id = int(existing_mg.industry_id)
        group_name = (existing_mg.meeting_group_name or "").strip()
        group_desc = existing_mg.description or ""
        website_url = existing_mg.website_url
        website_err = None
    else:
        industry_id = request.form.get("industry_id", type=int)
        if not industry_id or not Industry.query.get(int(industry_id)):
            flash("Choose a valid topic.", "warning")
            return redirect(url_for("main.event_wizard"))

        group_name = (request.form.get("meeting_group_name") or "").strip()
        group_desc = _sanitize_rich_text_html(request.form.get("meeting_group_description"))
        website_url, website_err = _normalize_optional_http_url(request.form.get("website_url"))
        if not group_name:
            flash("Group name is required.", "warning")
            return redirect(url_for("main.event_wizard"))
        if not _rich_text_plain_text(group_desc):
            flash("Add an event group description before continuing.", "warning")
            return redirect(url_for("main.event_wizard"))
        if website_err:
            flash(website_err, "warning")
            return redirect(url_for("main.event_wizard"))

    title = (request.form.get("title") or "").strip()
    subject = _sanitize_rich_text_html(request.form.get("subject"))
    if not title:
        flash("Event title is required.", "warning")
        return redirect(url_for("main.event_wizard"))
    if not _rich_text_plain_text(subject):
        flash("Event description is required.", "warning")
        return redirect(url_for("main.event_wizard"))

    try:
        starts_at = _parse_optional_datetime(request.form.get("starts_at"))
    except ValueError:
        starts_at = None
    if not starts_at:
        flash("Start date/time is not valid.", "warning")
        return redirect(url_for("main.event_wizard"))
    try:
        duration_minutes = _parse_optional_int(request.form.get("duration_minutes"))
    except ValueError:
        duration_minutes = None
    if not duration_minutes or duration_minutes < 15:
        flash("Duration must be at least 15 minutes.", "warning")
        return redirect(url_for("main.event_wizard"))

    event_meeting_url, event_meeting_url_err = _normalize_optional_http_url(
        request.form.get("event_website_url")
    )
    if event_meeting_url_err:
        flash(event_meeting_url_err, "warning")
        return redirect(url_for("main.event_wizard"))

    if publish_wizard and meeting_format == "Virtual" and not event_meeting_url:
        flash(
            "Add an event website URL (your join or landing link) before publishing an online event.",
            "warning",
        )
        return redirect(url_for("main.event_wizard"))

    repeat_pattern_norm: str | None = None
    repeat_until_date: date | None = None
    repeat_nth_week: int | None = None
    repeat_weekday: int | None = None

    raw_repeat = (request.form.get("repeat_pattern") or "").strip().lower()
    if raw_repeat in {"weekly", "monthly_dom", "monthly_nth"}:
        repeat_pattern_norm = raw_repeat

    if repeat_pattern_norm:
        rut = (request.form.get("repeat_until") or "").strip()
        if not rut:
            flash("Choose a repeat-until date for the repeating schedule.", "warning")
            return redirect(url_for("main.event_wizard"))
        try:
            repeat_until_date = date.fromisoformat(rut)
        except ValueError:
            flash("Repeat-until date is not valid.", "warning")
            return redirect(url_for("main.event_wizard"))

        if repeat_pattern_norm == "monthly_nth":
            nw_raw = (request.form.get("repeat_nth") or "1").strip().lower()
            if nw_raw == "last":
                repeat_nth_week = -1
            else:
                try:
                    repeat_nth_week = int(nw_raw)
                except (TypeError, ValueError):
                    repeat_nth_week = None
            if repeat_nth_week is None or repeat_nth_week not in {-1, 1, 2, 3, 4, 5}:
                flash(
                    "Choose “first … last weekday” correctly for the monthly repeat (or pick “Monthly same date”).",
                    "warning",
                )
                return redirect(url_for("main.event_wizard"))
            wd_raw = (request.form.get("repeat_weekday") or "").strip()
            try:
                repeat_weekday = int(wd_raw)
            except (TypeError, ValueError):
                repeat_weekday = None
            if repeat_weekday is None or repeat_weekday < 0 or repeat_weekday > 6:
                flash("Choose a weekday for the monthly repeating pattern.", "warning")
                return redirect(url_for("main.event_wizard"))

        min_until = _wizard_min_repeat_until_date(starts_at, repeat_pattern_norm)
        if repeat_until_date < min_until:
            flash(
                "Repeat-until must be at least one week after the first event for weekly repeats, "
                "or one calendar month after for monthly repeats.",
                "warning",
            )
            return redirect(url_for("main.event_wizard"))

    repeat_series_plan: list[datetime] = []
    if repeat_pattern_norm and repeat_until_date is not None:
        repeat_series_plan = _organiser_repeat_meeting_series(
            first=starts_at,
            pattern=repeat_pattern_norm,
            until=repeat_until_date,
            nth_week=repeat_nth_week,
            weekday=repeat_weekday,
        )

    location_city_v = (request.form.get("location_city") or "").strip() or None
    location_postcode_v = (request.form.get("location_postcode") or "").strip() or None
    venue_name_v = (request.form.get("venue_name") or "").strip() or None
    address_line1_v = (request.form.get("address_line1") or "").strip() or None
    address_line2_v = (request.form.get("address_line2") or "").strip() or None
    address_town_v = (request.form.get("address_town") or "").strip() or None
    address_county_v = (request.form.get("address_county") or "").strip() or None
    address_postcode_v = (request.form.get("address_postcode") or "").strip() or None
    address_country_v = (request.form.get("address_country") or "").strip() or None
    latitude_v = _parse_optional_decimal(request.form.get("latitude"))
    longitude_v = _parse_optional_decimal(request.form.get("longitude"))

    if meeting_format == "Face2Face":
        if not location_postcode_v:
            flash("Face-to-face events need a venue postcode.", "warning")
            return redirect(url_for("main.event_wizard"))

    wizard_ticket_payloads: list[dict] = []
    if meeting_format == "Face2Face":
        _ticket_names_raw = request.form.getlist("ticket_name")
        _form_keys_sample = sorted(request.form.keys())[:40]
        _event_wizard_debug_log(
            f"ticket POST snapshot n_names={len(_ticket_names_raw)} "
            f"form_keys_sample={_form_keys_sample!r}"
        )
        wizard_ticket_payloads, ticket_payload_err = _event_wizard_parse_all_ticket_payloads(
            starts_at, int(duration_minutes)
        )
        if ticket_payload_err:
            _event_wizard_debug_log(f"BAIL ticket_payload_err={ticket_payload_err!r}")
            flash(ticket_payload_err, "danger")
            return redirect(url_for("main.event_wizard"))

    has_paid_ticket = False
    for payload in wizard_ticket_payloads:
        try:
            pa = payload.get("price_amount")
            if pa is not None and float(pa) > 0:
                has_paid_ticket = True
                break
        except (TypeError, ValueError):
            continue

    if publish_wizard:
        for payload in wizard_ticket_payloads:
            payload["status"] = "Active"

    meeting_status = "Live" if publish_wizard else "Draft"
    is_paid_and_published_flag = bool(publish_wizard and has_paid_ticket)

    virtual_platform_v = None
    virtual_link_v = None
    if meeting_format == "Virtual" and publish_wizard:
        virtual_platform_v = "Online"
        virtual_link_v = event_meeting_url

    clone_ticket_status = "Active" if publish_wizard else "Draft"

    existing_update_redirect = _event_wizard_try_finish_existing_meeting_update(
        user=user,
        publish_wizard=publish_wizard,
        meeting_format=meeting_format,
        existing_mg_id=existing_mg_id,
        existing_mg=existing_mg,
        repeat_pattern_norm=repeat_pattern_norm,
        title=title,
        subject=subject,
        starts_at=starts_at,
        duration_minutes=int(duration_minutes),
        event_meeting_url=event_meeting_url,
        location_city_v=location_city_v,
        location_postcode_v=location_postcode_v,
        location_country_v=(request.form.get("location_country") or "").strip() or None,
        venue_name_v=venue_name_v,
        address_line1_v=address_line1_v,
        address_line2_v=address_line2_v,
        address_town_v=address_town_v,
        address_county_v=address_county_v,
        address_postcode_v=address_postcode_v,
        address_country_v=address_country_v,
        latitude_v=latitude_v,
        longitude_v=longitude_v,
        virtual_platform_v=virtual_platform_v,
        virtual_link_v=virtual_link_v,
        wizard_ticket_payloads=wizard_ticket_payloads,
        has_paid_ticket=has_paid_ticket,
    )
    if existing_update_redirect is not None:
        return existing_update_redirect

    image_filename = None
    uid_i = int(user.user_id)
    if not existing_mg:
        staged_fn = session.get(WIZARD_GROUP_IMAGE_SESSION_KEY)
        staged_path = None
        if staged_fn and _wizard_staging_basename_valid(staged_fn, uid_i):
            cand = os.path.join(_wizard_group_image_staging_abs_dir(), staged_fn)
            if os.path.isfile(cand):
                staged_path = cand

        image_file = request.files.get("group_image")
        had_upload = image_file and (image_file.filename or "").strip()

        if staged_path:
            image_filename = f"mg_{uid_i}_{int(datetime.utcnow().timestamp())}.png"
            os.makedirs(MEETING_GROUP_IMAGE_DIR, exist_ok=True)
            final_path = os.path.join(MEETING_GROUP_IMAGE_DIR, image_filename)
            try:
                shutil.move(staged_path, final_path)
            except OSError:
                current_app.logger.exception("event_wizard_create: move staged group image failed")
                flash("Something went wrong saving the image. Please try again.", "danger")
                return redirect(url_for("main.event_wizard"))
            session.pop(WIZARD_GROUP_IMAGE_SESSION_KEY, None)
            session.modified = True
        elif had_upload:
            os.makedirs(MEETING_GROUP_IMAGE_DIR, exist_ok=True)
            image_filename = f"mg_{uid_i}_{int(datetime.utcnow().timestamp())}.png"
            target_path = os.path.join(MEETING_GROUP_IMAGE_DIR, image_filename)
            try:
                _resize_meeting_group_image(image_file, target_path)
            except UnidentifiedImageError:
                flash(
                    "That file doesn't look like a valid image. Try a JPG, PNG, or WEBP.",
                    "warning",
                )
                return redirect(url_for("main.event_wizard"))
            except Exception:
                current_app.logger.exception("event_wizard_create: group image upload failed")
                flash("Something went wrong saving the image. Please try again.", "danger")
                return redirect(url_for("main.event_wizard"))
        else:
            try:
                image_filename = _test_events_stage_default_group_png()
            except Exception:
                current_app.logger.exception("event_wizard_create: staging default group image failed")
                flash("Could not prepare a default group image.", "danger")
                return redirect(url_for("main.event_wizard"))

    _event_wizard_debug_log(
        "persist begin "
        f"user_id={user.user_id} publish={publish_wizard} format={meeting_format} "
        f"industry_id={industry_id} repeat_dates={len(repeat_series_plan)} "
        f"ticket_rows={len(wizard_ticket_payloads)} meeting_status={meeting_status!r} "
        f"image={image_filename!r}"
    )

    repeat_extra_n = max(0, len(repeat_series_plan) - 1)
    repeat_title_base = title[:180]
    meeting_title_first = (
        _repeat_series_numbered_title(repeat_title_base, 1)
        if repeat_extra_n > 0
        else repeat_title_base
    )

    # Persist order (FKs): event_groups → events → event_ticket_types; repeats then clone tickets.
    with _TnwSqlalchemyEcho("event_wizard SQL"):
        if existing_mg:
            mg = existing_mg
            mg.meeting_format = meeting_format
            _event_wizard_debug_log(
                f"reuse MeetingGroup meeting_group_id={mg.meeting_group_id}"
            )
        else:
            mg = MeetingGroup(
                user_id=user.user_id,
                meeting_group_name=group_name[:180],
                description=group_desc or None,
                website_url=website_url,
                created_at=datetime.utcnow(),
                image_filename=image_filename,
                meeting_format=meeting_format,
                industry_id=int(industry_id),
            )
            db.session.add(mg)
            db.session.flush()
            _event_wizard_debug_log(f"after MeetingGroup flush meeting_group_id={mg.meeting_group_id}")

        meeting = Meeting(
            meeting_group_id=mg.meeting_group_id,
            creator_user_id=user.user_id,
            created_at=datetime.utcnow(),
            status=meeting_status,
            meeting_format=meeting_format,
            title=meeting_title_first,
            subject=subject,
            starts_at=starts_at,
            duration_minutes=int(duration_minutes),
            website_url=event_meeting_url,
            location_city=location_city_v,
            location_postcode=location_postcode_v,
            location_country=(request.form.get("location_country") or "").strip() or None,
            venue_name=venue_name_v,
            address_line1=address_line1_v,
            address_line2=address_line2_v,
            address_town=address_town_v or location_city_v,
            address_county=address_county_v,
            address_postcode=address_postcode_v or location_postcode_v,
            address_country=address_country_v,
            latitude=latitude_v,
            longitude=longitude_v,
            virtual_platform=virtual_platform_v,
            virtual_link=virtual_link_v,
            is_paid_and_published=is_paid_and_published_flag,
        )
        db.session.add(meeting)
        db.session.flush()
        _event_wizard_debug_log(f"after Meeting flush meeting_id={meeting.meeting_id}")

        if meeting_format == "Face2Face" and wizard_ticket_payloads:
            mid = int(meeting.meeting_id)
            for payload in wizard_ticket_payloads:
                db.session.add(MeetingTicketType(meeting_id=mid, **payload))
            db.session.flush()
            _event_wizard_debug_log(
                f"after MeetingTicketType flush count={len(wizard_ticket_payloads)} meeting_id={mid}"
            )

        bulk_copies: list[Meeting] = []
        if repeat_extra_n > 0:
            for idx, occ in enumerate(repeat_series_plan[1:], start=2):
                dup = Meeting(
                    meeting_group_id=mg.meeting_group_id,
                    creator_user_id=user.user_id,
                    created_at=datetime.utcnow(),
                    title=_repeat_series_numbered_title(repeat_title_base, idx),
                    subject=meeting.subject,
                    starts_at=occ,
                    meeting_format=meeting.meeting_format,
                    duration_minutes=meeting.duration_minutes,
                    website_url=meeting.website_url,
                    location_city=meeting.location_city,
                    location_postcode=meeting.location_postcode,
                    location_country=meeting.location_country,
                    venue_name=meeting.venue_name,
                    address_line1=meeting.address_line1,
                    address_line2=meeting.address_line2,
                    address_town=meeting.address_town,
                    address_county=meeting.address_county,
                    address_postcode=meeting.address_postcode,
                    address_country=meeting.address_country,
                    latitude=meeting.latitude,
                    longitude=meeting.longitude,
                    virtual_platform=meeting.virtual_platform,
                    virtual_link=meeting.virtual_link,
                    is_paid_and_published=meeting.is_paid_and_published,
                    status=meeting.status or "Draft",
                )
                db.session.add(dup)
                bulk_copies.append(dup)
            db.session.flush()
            master_id = int(meeting.meeting_id)
            for dup in bulk_copies:
                _clone_meeting_ticket_types_organiser(
                    master_id, int(dup.meeting_id), ticket_status=clone_ticket_status
                )
            _event_wizard_debug_log(
                f"after repeat meetings flush extra={repeat_extra_n} dup_ids="
                f"{[int(d.meeting_id) for d in bulk_copies]}"
            )

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            current_app.logger.exception("event_wizard_create: integrity error on commit")
            _event_wizard_debug_log("COMMIT FAILED IntegrityError — rolled back")
            flash(
                "Could not save your event because of a data conflict. Please review your entries and try again.",
                "danger",
            )
            return redirect(url_for("main.event_wizard"))
        except Exception:
            db.session.rollback()
            current_app.logger.exception("event_wizard_create: database commit failed")
            _event_wizard_debug_log("COMMIT FAILED — rolled back")
            flash(
                "We could not save your event group and events to the database. Please try again.",
                "danger",
            )
            return redirect(url_for("main.event_wizard"))

        _event_wizard_debug_log(
            "COMMIT OK "
            f"meeting_group_id={mg.meeting_group_id} master_meeting_id={meeting.meeting_id}"
        )

        if publish_wizard:
            _tnw_commit_event_listing_record(int(user.user_id), int(meeting.meeting_id), None)
            for dup in bulk_copies:
                _tnw_commit_event_listing_record(int(user.user_id), int(dup.meeting_id), None)

    industry_row = Industry.query.get(int(industry_id))
    industry_label = (industry_row.industry or "").strip() if industry_row else ""
    repeat_email_lbl = _event_wizard_repeat_email_label(
        repeat_pattern_norm, repeat_until_date, repeat_nth_week, repeat_weekday
    )
    try:
        _send_event_wizard_completion_email(
            user=user,
            mg=mg,
            master_meeting=meeting,
            repeat_meetings=bulk_copies,
            publish_wizard=publish_wizard,
            meeting_status=meeting_status,
            meeting_format=meeting_format,
            industry_label=industry_label,
            website_group=website_url,
            event_website_url=event_meeting_url,
            group_desc_html=group_desc,
            event_title=title,
            event_desc_html=subject,
            duration_minutes=int(duration_minutes),
            repeat_label=repeat_email_lbl,
            wizard_ticket_payloads=wizard_ticket_payloads,
            venue_postcode=(address_postcode_v or location_postcode_v or "").strip() or None,
            venue_name=venue_name_v,
            address_line1=address_line1_v,
            address_town=address_town_v,
        )
    except Exception:
        current_app.logger.exception("event_wizard_create: completion summary email failed")

    extra_note = ""
    if repeat_extra_n:
        plural = "event" if repeat_extra_n == 1 else "events"
        verb = "were saved" if not publish_wizard else "were published"
        extra_note = (
            f" Another {repeat_extra_n} repeating {plural} {verb} with the same details."
        )

    ntix = len(wizard_ticket_payloads)
    if publish_wizard:
        if meeting_format == "Face2Face" and wizard_ticket_payloads:
            tmsg = (
                "Your event is live and tickets are on sale."
                if ntix == 1
                else f"Your event is live with {ntix} ticket types on sale."
            )
            flash(
                tmsg
                + " You can still edit event and ticket details until the first ticket is sold."
                + extra_note,
                "success",
            )
        else:
            flash(
                "Your online event is live."
                + " You can still edit event details from your dashboard."
                + extra_note,
                "success",
            )
    else:
        flash(
            (
                (
                    "Draft saved with 1 ticket type."
                    if ntix == 1
                    else f"Draft saved with {ntix} ticket types."
                )
                if meeting_format == "Face2Face" and wizard_ticket_payloads
                else "Draft saved. You can edit and publish from your dashboard."
            )
            + extra_note,
            "success",
        )
    return redirect(
        url_for(
            "main.platform_dashboard",
            meeting_groups_page=1,
            meeting_group_id=int(mg.meeting_group_id),
            _anchor="meetings-pane",
        )
    )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
# Cap organiser attendee accordion: loading every past event + every row is O(events × bookings).
DASHBOARD_ORG_ATTENDEE_MEETING_CAP = 200


def _meeting_where_line_owner_summary(m: Meeting) -> str:
    """Short venue / online / group context for organiser→attendee emails and dashboard UI."""
    gn = ""
    if getattr(m, "meeting_group", None):
        gn = (m.meeting_group.meeting_group_name or "").strip()
    fmt = (m.meeting_format or "Face2Face") or "Face2Face"
    if fmt == "Virtual":
        parts = []
        vp = (m.virtual_platform or "").strip()
        if vp:
            parts.append(vp)
        if gn:
            parts.append("Group: " + gn)
        return " · ".join(parts) if parts else "Online event"
    parts = []
    vn = (m.venue_name or "").strip()
    if vn:
        parts.append(vn)
    town = (m.address_town or m.location_city or "").strip()
    pc = (m.address_postcode or m.location_postcode or "").strip()
    loc = ", ".join(x for x in [town, pc] if x)
    if loc:
        parts.append(loc)
    if not parts and gn:
        return gn
    return " · ".join(parts) if parts else "Venue to be confirmed"


def _send_dashboard_organiser_to_attendee_email(
    *,
    to_email: str,
    to_name: str,
    organiser_name: str,
    organiser_email: str,
    event_title: str,
    event_when: str,
    event_where: str,
    group_name: str,
    message_text: str,
) -> None:
    """Email a ticket holder from the organiser dashboard; delivered via SMTP_* (From platform, Reply-To organiser)."""
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    site_name = os.getenv("SITE_NAME", "The Networker")
    site_url = os.getenv("SITE_URL", "https://the-networker.co.uk")
    support_email = os.getenv("SUPPORT_EMAIL", smtp_user)
    if not smtp_user or not smtp_password or not to_email.strip():
        raise ValueError("mail_not_configured")

    safe_title = (event_title or "Event").strip() or "Event"
    oname = (organiser_name or organiser_email or "Organiser").strip() or "Organiser"
    subj_sender = oname[:120]
    email_message = EmailMessage()
    email_message["Subject"] = f'[{site_name}] Message from {subj_sender} about "{safe_title}"'
    email_message["From"] = smtp_user
    email_message["To"] = to_email.strip()
    email_message["Reply-To"] = organiser_email.strip() or smtp_user
    if (organiser_email.strip() or "").lower() != to_email.strip().lower():
        email_message["Bcc"] = organiser_email.strip() or smtp_user
    email_message["Date"] = formatdate(localtime=True)
    email_message["Message-ID"] = make_msgid(domain=smtp_user.split("@")[-1])
    email_message["X-Auto-Response-Suppress"] = "All"
    email_message["X-Mailer"] = f"{site_name} organiser message"

    gn = (group_name or "").strip()
    ctx_lines = [
        f"Event: {safe_title}",
        f"When: {event_when or '—'}",
        f"Where / details: {event_where or '—'}",
    ]
    if gn:
        ctx_lines.append(f"Event group: {gn}")
    ctx_block = "\n".join(ctx_lines)

    plain = (
        f"You have a message from {oname} (organiser) via {site_name}.\n\n"
        f"{ctx_block}\n\n"
        f"Message:\n{message_text}\n\n"
        f"---\nReply directly to this email to reach the organiser at {organiser_email.strip() or 'their address on file'}.\n"
        f"Questions about the platform: {support_email}\n{site_url}\n"
    )
    email_message.set_content(plain)

    et_e = escape(safe_title)
    when_e = escape(event_when or "—")
    where_e = escape(event_where or "—")
    gn_e = escape(gn) if gn else ""
    msg_html = escape(message_text).replace("\n", "<br>")
    on_e = escape(oname)
    oe_e = escape(organiser_email.strip() or "")
    tn_e = escape(to_name or "there")

    body_html = (
        "<html><body style=\"font-family:Arial,Helvetica,sans-serif;color:#2f1f3a;\">"
        f"<h2 style=\"color:#5b2d73;\">Message from your event organiser</h2>"
        f"<p>Hi {tn_e},</p>"
        f"<p><strong>{on_e}</strong> sent you this message about an event you booked on {escape(site_name)}.</p>"
        "<table style=\"border-collapse:collapse;margin:12px 0;font-size:14px;\">"
        f"<tr><td style=\"padding:4px 12px 4px 0;color:#5b3d72;\"><strong>Event</strong></td><td>{et_e}</td></tr>"
        f"<tr><td style=\"padding:4px 12px 4px 0;color:#5b3d72;\"><strong>When</strong></td><td>{when_e}</td></tr>"
        f"<tr><td style=\"padding:4px 12px 4px 0;color:#5b3d72;vertical-align:top;\"><strong>Where / details</strong></td><td>{where_e}</td></tr>"
    )
    if gn_e:
        body_html += (
            f"<tr><td style=\"padding:4px 12px 4px 0;color:#5b3d72;\"><strong>Group</strong></td><td>{gn_e}</td></tr>"
        )
    body_html += (
        "</table>"
        f"<p style=\"margin-top:16px;\">{msg_html}</p>"
        "<hr style=\"border:none;border-top:1px solid #e6dcec;\">"
        f"<p style=\"font-size:12px;color:#6b5c75;\">Reply to this email to contact the organiser ({oe_e}). "
        f"Support: {escape(support_email)}</p>"
        "</body></html>"
    )
    email_message.add_alternative(body_html, subtype="html")

    with smtplib.SMTP(smtp_host, smtp_port, timeout=25) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(email_message)


def _organiser_event_attendees_for_owner(uid):
    """Meetings owned via event groups plus ticket buyers (MeetingAttendee rows).

    Capped at ``DASHBOARD_ORG_ATTENDEE_MEETING_CAP`` (most recent by ``starts_at``)
    so the accordion stays fast for organisers with long history.
    """
    meetings_owned = (
        Meeting.query.join(MeetingGroup, Meeting.meeting_group_id == MeetingGroup.meeting_group_id)
        .filter(MeetingGroup.user_id == uid)
        .options(
            load_only(
                Meeting.meeting_id,
                Meeting.meeting_group_id,
                Meeting.title,
                Meeting.starts_at,
                Meeting.status,
                Meeting.meeting_format,
                Meeting.venue_name,
                Meeting.location_city,
                Meeting.location_postcode,
                Meeting.address_town,
                Meeting.address_postcode,
                Meeting.virtual_platform,
            ),
            selectinload(Meeting.meeting_group).load_only(
                MeetingGroup.meeting_group_id,
                MeetingGroup.meeting_group_name,
            ),
        )
        .order_by(Meeting.starts_at.desc(), Meeting.meeting_id.desc())
        .limit(DASHBOARD_ORG_ATTENDEE_MEETING_CAP)
        .all()
    )
    att_by_meeting_id = {}
    if meetings_owned:
        mids = [m.meeting_id for m in meetings_owned]
        batch_size = 1800
        for i in range(0, len(mids), batch_size):
            mids_batch = mids[i : i + batch_size]
            org_att_rows = (
                MeetingAttendee.query.options(
                    load_only(
                        MeetingAttendee.meeting_attendee_id,
                        MeetingAttendee.meeting_id,
                        MeetingAttendee.quantity,
                        MeetingAttendee.amount_paid,
                        MeetingAttendee.status,
                        MeetingAttendee.booked_at,
                    ),
                    selectinload(MeetingAttendee.user).load_only(
                        User.user_id,
                        User.first_name,
                        User.second_name,
                        User.username,
                        User.email,
                    ),
                    selectinload(MeetingAttendee.ticket_type).load_only(
                        MeetingTicketType.ticket_type_id,
                        MeetingTicketType.ticket_name,
                    ),
                )
                .filter(MeetingAttendee.meeting_id.in_(mids_batch))
                .order_by(MeetingAttendee.booked_at.desc())
                .all()
            )
            for att in org_att_rows:
                att_by_meeting_id.setdefault(att.meeting_id, []).append(att)
    return [
        {
            "meeting": m,
            "attendees": att_by_meeting_id.get(m.meeting_id, []),
            "where_line": _meeting_where_line_owner_summary(m),
        }
        for m in meetings_owned
    ]


def _home_user_has_events(uid: int | None) -> bool:
    """True if the organiser already has at least one event in any of their groups."""
    if not uid:
        return False
    row = (
        db.session.query(Meeting.meeting_id)
        .join(MeetingGroup, Meeting.meeting_group_id == MeetingGroup.meeting_group_id)
        .filter(MeetingGroup.user_id == int(uid))
        .limit(1)
        .first()
    )
    return row is not None


def _home_promote_hero_context(uid: int | None) -> dict:
    """Home hero when the signed-in organiser has upcoming live events to promote."""
    base = {
        "show_home_promote_hero": False,
        "home_promote_upcoming_count": 0,
        "home_promote_group_count": 0,
        "home_promote_url": tnw_url_for("main.buy_boosts"),
        "home_promote_dashboard_url": tnw_url_for(
            "main.platform_dashboard", _anchor="promote-events-pane"
        ),
        "home_promote_boost_balance": 0,
        "home_promote_boost_label_plural": "Boosts",
    }
    if not uid:
        return base

    from .promotion_boosts import BOOST_LABEL_PLURAL, get_boost_balance

    now_utc = datetime.utcnow()
    mg_rows = (
        db.session.query(MeetingGroup.meeting_group_id)
        .filter(MeetingGroup.user_id == int(uid))
        .all()
    )
    mg_ids = [int(r[0]) for r in mg_rows]
    if not mg_ids:
        return base

    upcoming_meetings = (
        Meeting.query.filter(
            Meeting.meeting_group_id.in_(mg_ids),
            Meeting.status == "Live",
            Meeting.starts_at.isnot(None),
            Meeting.starts_at >= now_utc,
        )
        .all()
    )
    upcoming_count = len(upcoming_meetings)
    if upcoming_count <= 0:
        return base

    groups_with_upcoming: set[int] = set()
    for m in upcoming_meetings:
        if m.meeting_group_id:
            groups_with_upcoming.add(int(m.meeting_group_id))

    base.update(
        {
            "show_home_promote_hero": True,
            "home_promote_upcoming_count": upcoming_count,
            "home_promote_group_count": len(groups_with_upcoming),
            "home_promote_boost_balance": get_boost_balance(int(uid)),
            "home_promote_boost_label_plural": BOOST_LABEL_PLURAL,
        }
    )
    return base


def _dashboard_promote_catalog(
    uid: int | None, event_counts_by_group: dict[int, dict[str, int]]
) -> dict:
    """JSON-safe promote targets: groups, live events, and tier pricing for event vs group scope."""
    from .promotion_boosts import promote_wallet_json, promotion_tiers_for_json, tier_costs_for_json

    tiers = promotion_tiers_for_json()
    tier_prices_event = [t["boost_event"] for t in tiers]
    tier_prices_group = [t["boost_group"] for t in tiers]

    if not uid:
        wallet = promote_wallet_json(None)
        return {
            "groups": [],
            "events": [],
            "tier_prices_event": tier_prices_event,
            "tier_prices_group": tier_prices_group,
            "boost_balance": 0,
            "boost_label": wallet.get("boost_label", "Boost"),
            "boost_label_plural": wallet.get("boost_label_plural", "Boosts"),
            "bundles": wallet.get("bundles", []),
            "tier_boost_costs": tier_costs_for_json(),
            "tiers": tiers,
        }

    groups_q = (
        MeetingGroup.query.filter_by(user_id=uid)
        .order_by(MeetingGroup.meeting_group_name.asc())
        .all()
    )
    mg_ids = [g.meeting_group_id for g in groups_q]

    events_out: list[dict] = []
    if mg_ids:
        now_utc = datetime.utcnow()
        live_meetings = (
            Meeting.query.filter(
                Meeting.meeting_group_id.in_(mg_ids),
                Meeting.status == "Live",
            )
            .order_by(Meeting.meeting_group_id.asc(), Meeting.starts_at.asc())
            .all()
        )
        for m in live_meetings:
            if m.starts_at is not None and m.starts_at < now_utc:
                continue
            fmt = (m.meeting_format or "Face2Face").strip()
            events_out.append(
                {
                    "id": m.meeting_id,
                    "group_id": m.meeting_group_id,
                    "title": (m.title or "Event").strip() or "Event",
                    "starts_at": m.starts_at.isoformat() if m.starts_at else None,
                    "meeting_format": fmt,
                    "is_virtual": fmt.lower() == "virtual",
                }
            )

    upcoming_by_group: dict[int, int] = {}
    for ev in events_out:
        gid = int(ev["group_id"])
        upcoming_by_group[gid] = upcoming_by_group.get(gid, 0) + 1

    groups_out: list[dict] = []
    for g in groups_q:
        counts = event_counts_by_group.get(g.meeting_group_id, {})
        total = int(counts.get("total") or 0)
        live_n = int(counts.get("live") or 0)
        draft_n = int(counts.get("draft") or 0)
        upcoming_n = int(upcoming_by_group.get(g.meeting_group_id, 0))
        can_promote = upcoming_n > 0
        is_draft_only = live_n == 0 and total > 0
        if not can_promote and not is_draft_only:
            continue
        groups_out.append(
            {
                "id": g.meeting_group_id,
                "name": (g.meeting_group_name or "Event group").strip() or "Event group",
                "event_count": total,
                "live_count": live_n,
                "draft_count": draft_n,
                "upcoming_count": upcoming_n,
                "can_promote": can_promote,
                "is_draft_only": is_draft_only,
                "group_promo_available": upcoming_n > 1,
                "image_url": meeting_group_image_url(g),
                "has_image": bool((g.image_filename or "").strip()),
            }
        )

    wallet = promote_wallet_json(uid)
    return {
        "groups": groups_out,
        "events": events_out,
        "tier_prices_event": tier_prices_event,
        "tier_prices_group": tier_prices_group,
        "boost_balance": wallet.get("boost_balance", 0),
        "boost_label": wallet.get("boost_label", "Boost"),
        "boost_label_plural": wallet.get("boost_label_plural", "Boosts"),
        "bundles": wallet.get("bundles", []),
        "tier_boost_costs": tier_costs_for_json(),
        "tiers": tiers,
    }


def _dashboard_load_ticket_meetings_batch(meeting_groups: list) -> list:
    """Load all meetings (and ticket types) for the organiser's groups — used by Event Tickets pane."""
    if not meeting_groups:
        return []
    out: list[Meeting] = []
    _mg_ids_all = [mg.meeting_group_id for mg in meeting_groups]
    _mg_order = {gid: pos for pos, gid in enumerate(_mg_ids_all)}
    for _i in range(0, len(_mg_ids_all), 1800):
        _batch = _mg_ids_all[_i : _i + 1800]
        out.extend(
            Meeting.query.filter(Meeting.meeting_group_id.in_(_batch))
            .options(
                selectinload(Meeting.ticket_types),
                joinedload(Meeting.meeting_group).load_only(
                    MeetingGroup.meeting_group_id,
                    MeetingGroup.meeting_group_name,
                ),
            )
            .order_by(Meeting.meeting_group_id.asc(), Meeting.starts_at.asc())
            .all()
        )
    out.sort(
        key=lambda m: (
            _mg_order.get(m.meeting_group_id, 10**9),
            m.starts_at or datetime.min,
            m.meeting_id,
        )
    )
    return out


def _dashboard_template_label_vars() -> dict:
    lbl_mg = table_label("event_groups", "Event groups")
    mg_l = (lbl_mg or "").lower()
    lbl_mg_single = (
        "Event Group"
        if mg_l == "event groups"
        else "Meeting Group"
        if mg_l == "meeting groups"
        else lbl_mg
    )
    lbl_mt = table_label("events", "Events")
    return {"lbl_mg": lbl_mg, "lbl_mg_single": lbl_mg_single, "lbl_mt": lbl_mt, "lbl_ind": "Topic"}


def _dashboard_meeting_group_event_counts(meeting_groups: list) -> dict[int, dict]:
    """Per-group event totals for dashboard pickers (total / draft / live)."""
    meeting_group_event_counts: dict[int, dict] = {}
    if not meeting_groups:
        return meeting_group_event_counts
    mg_ids = [mg.meeting_group_id for mg in meeting_groups]
    batch_size = 1800
    for i in range(0, len(mg_ids), batch_size):
        mg_ids_batch = mg_ids[i : i + batch_size]
        count_rows = (
            db.session.query(
                Meeting.meeting_group_id,
                func.count(Meeting.meeting_id).label("total_n"),
                func.sum(case((Meeting.status == "Draft", 1), else_=0)).label("draft_n"),
                func.sum(case((Meeting.status == "Live", 1), else_=0)).label("live_n"),
            )
            .filter(Meeting.meeting_group_id.in_(mg_ids_batch))
            .group_by(Meeting.meeting_group_id)
            .all()
        )
        for row in count_rows:
            meeting_group_event_counts[row.meeting_group_id] = {
                "total": int(row.total_n or 0),
                "draft": int(row.draft_n or 0),
                "live": int(row.live_n or 0),
            }
    for gid in mg_ids:
        meeting_group_event_counts.setdefault(gid, {"total": 0, "draft": 0, "live": 0})
    return meeting_group_event_counts


def _dashboard_resolve_ticket_heading(
    uid: int,
    meeting_groups: list,
    selected_meeting_group,
    selected_group_meetings: list,
    ticket_meeting_id: int | None,
    meetings_for_scan: list[Meeting],
) -> tuple[Meeting | None, str | None]:
    """Pick ticket panel heading meeting (Face2Face) without loading the full meeting batch when possible."""
    if not meeting_groups:
        return None, None
    matched_face = None
    matched_group_name = None
    first_face = None
    first_group_name = None
    if ticket_meeting_id is not None:
        m_ticket = (
            Meeting.query.options(
                selectinload(Meeting.ticket_types),
                selectinload(Meeting.meeting_group).joinedload(MeetingGroup.industry),
                selectinload(Meeting.meeting_group)
                .selectinload(MeetingGroup.owner)
                .options(noload(User.industries), noload(User.attendee_tags)),
            )
            .join(MeetingGroup, Meeting.meeting_group_id == MeetingGroup.meeting_group_id)
            .filter(
                MeetingGroup.user_id == uid,
                Meeting.meeting_id == ticket_meeting_id,
            )
            .first()
        )
        if m_ticket and (m_ticket.meeting_format or "") == "Face2Face":
            matched_face = m_ticket
            mg_h = next(
                (g for g in meeting_groups if g.meeting_group_id == m_ticket.meeting_group_id),
                None,
            )
            matched_group_name = (
                mg_h.meeting_group_name
                if mg_h
                else (
                    (m_ticket.meeting_group.meeting_group_name or "")
                    if m_ticket.meeting_group
                    else None
                )
            )
    if matched_face is None and selected_meeting_group and selected_group_meetings:
        for meeting in selected_group_meetings:
            if (meeting.meeting_format or "") != "Face2Face":
                continue
            first_face = meeting
            first_group_name = selected_meeting_group.meeting_group_name
            break
    if matched_face is None and first_face is None:
        scan = list(meetings_for_scan)
        if not scan:
            mg_ids = [mg.meeting_group_id for mg in meeting_groups]
            _order = {gid: i for i, gid in enumerate(mg_ids)}
            for _i in range(0, len(mg_ids), 1800):
                _batch = mg_ids[_i : _i + 1800]
                scan.extend(
                    Meeting.query.join(
                        MeetingGroup, Meeting.meeting_group_id == MeetingGroup.meeting_group_id
                    )
                    .filter(
                        MeetingGroup.user_id == uid,
                        Meeting.meeting_format == "Face2Face",
                        Meeting.meeting_group_id.in_(_batch),
                    )
                    .options(
                        selectinload(Meeting.ticket_types),
                        joinedload(Meeting.meeting_group).load_only(
                            MeetingGroup.meeting_group_id,
                            MeetingGroup.meeting_group_name,
                        ),
                    )
                    .all()
                )
            scan.sort(
                key=lambda m: (
                    _order.get(m.meeting_group_id, 10**9),
                    m.starts_at or datetime.min,
                    m.meeting_id,
                )
            )
        for meeting in scan:
            if (meeting.meeting_format or "") != "Face2Face":
                continue
            mg_row = meeting.meeting_group
            gnm = mg_row.meeting_group_name if mg_row else None
            if first_face is None:
                first_face = meeting
                first_group_name = gnm
            if ticket_meeting_id and meeting.meeting_id == ticket_meeting_id:
                matched_face = meeting
                matched_group_name = gnm
                break
    if matched_face is not None:
        return matched_face, matched_group_name or None
    if first_face is not None:
        return first_face, first_group_name
    return None, None


def _dashboard_fill_meeting_ticket_meta_maps(
    dashboard_ticket_meetings: list,
    selected_meeting_group,
    selected_group_meetings: list,
    ticket_heading_meeting: Meeting | None,
    edit_meeting: Meeting | None,
) -> tuple[dict[int, dict], dict[int, list[dict]], dict]:
    meeting_row_meta: dict[int, dict] = {}
    meeting_ticket_form_meta: dict[int, list[dict]] = {}

    def _fill(mrow: Meeting) -> None:
        if mrow.meeting_id in meeting_ticket_form_meta:
            return
        ordered_ticket_types = sorted(
            list(mrow.ticket_types or []),
            key=lambda tt: (tt.sort_order or 0, tt.ticket_type_id or 0),
        )
        meeting_ticket_form_meta[mrow.meeting_id] = [
            {
                "ticket_type_id": tt.ticket_type_id,
                "ticket_name": tt.ticket_name or "General admission",
                "ticket_description": tt.ticket_description or "",
                "currency_code": (tt.currency_code or "GBP"),
                "price_amount": str(
                    display_price_from_stored(
                        tt.price_amount,
                        tt.vat_rate_percent,
                        getattr(tt, "vat_treatment", None),
                    )
                ),
                "max_quantity": int(tt.max_quantity or 20),
                "vat_rate_percent": str(
                    tt.vat_rate_percent if tt.vat_rate_percent is not None else "0.00"
                ),
                "vat_treatment": infer_vat_mode(
                    tt.vat_rate_percent, getattr(tt, "vat_treatment", None)
                ),
                "sales_open_at": tt.sales_open_at.strftime("%Y-%m-%d")
                if tt.sales_open_at
                else "",
                "sales_close_at": tt.sales_close_at.strftime("%Y-%m-%d")
                if tt.sales_close_at
                else "",
                "refund_policy": tt.refund_policy or "No refunds",
                "ticket_notes": tt.ticket_notes or "",
                "ticket_status": tt.status or "Draft",
            }
            for tt in ordered_ticket_types
        ]
        meeting_row_meta[mrow.meeting_id] = {
            "sold_qty": 0,
            "ticket_type_count": len(mrow.ticket_types or []),
        }

    if selected_meeting_group is not None:
        for mrow in selected_group_meetings:
            _fill(mrow)
        for _extra in (ticket_heading_meeting, edit_meeting):
            if _extra is not None:
                _fill(_extra)
    else:
        for mrow in dashboard_ticket_meetings:
            _fill(mrow)
        for _extra in (ticket_heading_meeting, edit_meeting):
            if _extra is not None:
                _fill(_extra)

    if meeting_row_meta:
        sold_all = _sold_qty_by_meeting_ids(list(meeting_row_meta.keys()))
        for mid, row in meeting_row_meta.items():
            row["sold_qty"] = sold_all.get(mid, 0)

    meetings_for_stats: dict[int, Meeting] = {}
    if selected_meeting_group is not None:
        for mrow in selected_group_meetings:
            meetings_for_stats[int(mrow.meeting_id)] = mrow
    else:
        for mrow in dashboard_ticket_meetings:
            meetings_for_stats[int(mrow.meeting_id)] = mrow
    for _extra in (ticket_heading_meeting, edit_meeting):
        if _extra is not None:
            meetings_for_stats[int(_extra.meeting_id)] = _extra

    all_ticket_type_ids: list[int] = []
    for _rows in meeting_ticket_form_meta.values():
        for _meta in _rows:
            _tid = _meta.get("ticket_type_id")
            if _tid is not None:
                all_ticket_type_ids.append(int(_tid))
    ticket_type_sold_qty = _sold_qty_by_ticket_type_ids(all_ticket_type_ids)
    _apply_meeting_row_ticket_stats(
        meeting_row_meta, meetings_for_stats, ticket_type_sold_qty
    )
    return meeting_row_meta, meeting_ticket_form_meta, ticket_type_sold_qty


def _apply_meeting_row_ticket_stats(
    meeting_row_meta: dict[int, dict],
    meetings_by_id: dict[int, Meeting],
    ticket_type_sold_qty: dict[int, int],
) -> None:
    mids = list(meeting_row_meta.keys())
    if not mids:
        return
    sales_rows = (
        db.session.query(
            MeetingAttendee.meeting_id,
            func.coalesce(func.sum(MeetingAttendee.amount_paid), 0),
        )
        .filter(
            MeetingAttendee.meeting_id.in_(mids),
            MeetingAttendee.status.in_(_ATTENDEE_COUNTABLE_STATUSES),
        )
        .group_by(MeetingAttendee.meeting_id)
        .all()
    )
    sales_map = {int(m): float(amt or 0) for m, amt in sales_rows}
    for mid, row in meeting_row_meta.items():
        meeting = meetings_by_id.get(mid)
        row["total_sales_gbp"] = sales_map.get(mid, 0.0)
        row.setdefault("tickets_available", None)
        row.setdefault("tickets_capacity", 0)
        fmt = (meeting.meeting_format if meeting else "Face2Face") or "Face2Face"
        if fmt != "Face2Face":
            continue
        capacity = 0
        available = 0
        for tt in (meeting.ticket_types if meeting else []) or []:
            if (tt.status or "").strip() != "Live":
                continue
            try:
                max_q = int(tt.max_quantity or 0)
            except (TypeError, ValueError):
                max_q = 0
            sold_tt = ticket_type_sold_qty.get(int(tt.ticket_type_id), 0)
            capacity += max_q
            available += max(0, max_q - sold_tt)
        row["tickets_capacity"] = capacity
        row["tickets_available"] = available


def _directory_group_events_context(uid: int, meeting_group_id: int) -> dict | None:
    """Template context for directory event cards, or None if group not owned."""
    mg = MeetingGroup.query.get(meeting_group_id)
    if not mg or int(mg.user_id) != int(uid):
        return None
    meetings = (
        Meeting.query.filter_by(meeting_group_id=meeting_group_id)
        .order_by(Meeting.starts_at.asc())
        .options(selectinload(Meeting.ticket_types))
        .all()
    )
    upcoming, finished = _partition_directory_group_meetings(meetings)
    meetings_ordered = upcoming + finished
    countdown_now = datetime.utcnow()
    countdowns = {
        m.meeting_id: _event_countdown_label(m.starts_at, countdown_now)
        for m in meetings_ordered
    }
    meeting_row_meta, _, _ = _dashboard_fill_meeting_ticket_meta_maps(
        [],
        mg,
        meetings_ordered,
        None,
        None,
    )
    labels = _dashboard_template_label_vars()
    return {
        **labels,
        "selected_meeting_group": mg,
        "selected_meeting_group_id": meeting_group_id,
        "selected_group_meetings_upcoming": upcoming,
        "selected_group_meetings_finished": finished,
        "selected_group_meeting_countdowns": countdowns,
        "meeting_row_meta": meeting_row_meta,
        "is_editing_meeting": False,
        "meeting_form": None,
        "timedelta": timedelta,
    }


def _render_directory_events_sections_html(uid: int, meeting_group_id: int) -> str:
    ctx = _directory_group_events_context(uid, meeting_group_id)
    if ctx is None:
        return ""
    return render_template(
        "partials/dashboard_directory_events_sections.html", **ctx
    )


@bp.route("/api/dashboard/promotion/buy-bundle", methods=["POST"])
@login_required
def api_dashboard_promotion_buy_bundle():
    from .promotion_boosts import purchase_boost_bundle

    uid = session.get("user_id")
    if not uid:
        return jsonify(ok=False, error="Sign in required."), 401
    data = request.get_json(silent=True) or {}
    bundle_key = (data.get("bundle_key") or request.form.get("bundle_key") or "").strip()
    if not bundle_key:
        return jsonify(ok=False, error="Choose a bundle."), 400
    try:
        result = purchase_boost_bundle(
            int(uid),
            bundle_key,
            payment_method=(data.get("payment_method") or "").strip(),
            payment_reference=(data.get("payment_reference") or "").strip(),
            paypal_email=(data.get("paypal_email") or "").strip(),
            card_last4=(data.get("card_last4") or "").strip(),
        )
        status = 200 if result.get("ok") else 400
        return jsonify(result), status
    except Exception:
        current_app.logger.exception("api_dashboard_promotion_buy_bundle")
        db.session.rollback()
        return jsonify(ok=False, error="Could not complete the purchase. Try again."), 500


@bp.route("/api/dashboard/promotion/target-seo", methods=["GET"])
@login_required
def api_dashboard_promotion_target_seo():
    """SEO context for promote confirmation (description preview)."""
    from .search_index import promotion_target_seo_payload

    uid = session.get("user_id")
    if not uid:
        return jsonify(ok=False, error="Sign in required."), 401
    scope = (request.args.get("scope") or "").strip().lower()
    try:
        meeting_group_id = int(request.args.get("meeting_group_id") or 0)
    except (TypeError, ValueError):
        meeting_group_id = 0
    meeting_id = request.args.get("meeting_id")
    try:
        meeting_id = int(meeting_id) if meeting_id not in (None, "") else None
    except (TypeError, ValueError):
        meeting_id = None

    mg = MeetingGroup.query.get(meeting_group_id)
    if not mg or mg.user_id != uid:
        return jsonify(ok=False, error="Invalid event group."), 400

    meeting = None
    if scope == "event":
        if not meeting_id:
            return jsonify(ok=False, error="Invalid event."), 400
        meeting = Meeting.query.get(meeting_id)
        if (
            not meeting
            or meeting.meeting_group_id != meeting_group_id
            or meeting.creator_user_id != uid
        ):
            return jsonify(ok=False, error="Invalid event."), 400
    elif scope != "group":
        return jsonify(ok=False, error="Invalid promotion scope."), 400

    try:
        payload = promotion_target_seo_payload(
            scope=scope, meeting_group=mg, meeting=meeting
        )
        payload["ok"] = True
        return jsonify(payload)
    except Exception:
        current_app.logger.exception("api_dashboard_promotion_target_seo")
        return jsonify(ok=False, error="Could not load description."), 500


@bp.route("/api/dashboard/promotion/activate", methods=["POST"])
@login_required
def api_dashboard_promotion_activate():
    from .promotion_boosts import spend_boosts_on_promotion
    from .search_index import save_promotion_target_description

    uid = session.get("user_id")
    if not uid:
        return jsonify(ok=False, error="Sign in required."), 401
    data = request.get_json(silent=True) or {}
    scope = (data.get("scope") or "").strip().lower()
    try:
        meeting_group_id = int(data.get("meeting_group_id") or 0)
    except (TypeError, ValueError):
        meeting_group_id = 0
    meeting_id = data.get("meeting_id")
    try:
        meeting_id = int(meeting_id) if meeting_id not in (None, "") else None
    except (TypeError, ValueError):
        meeting_id = None
    tier_key = (data.get("tier_key") or "").strip().lower()
    target_label = (data.get("target_label") or "").strip()

    mg = MeetingGroup.query.get(meeting_group_id)
    if not mg or mg.user_id != uid:
        return jsonify(ok=False, error="Invalid event group."), 400

    if scope == "event" and meeting_id:
        meeting = Meeting.query.get(meeting_id)
        if (
            not meeting
            or meeting.meeting_group_id != meeting_group_id
            or meeting.creator_user_id != uid
        ):
            return jsonify(ok=False, error="Invalid event."), 400

    if "description_html" in data:
        saved, save_err = save_promotion_target_description(
            int(uid),
            scope=scope,
            meeting_group_id=meeting_group_id,
            meeting_id=meeting_id,
            description_html=data.get("description_html") or "",
        )
        if not saved:
            return jsonify(ok=False, error=save_err or "Could not save description."), 400

    try:
        result = spend_boosts_on_promotion(
            int(uid),
            scope=scope,
            meeting_group_id=meeting_group_id,
            meeting_id=meeting_id,
            tier_key=tier_key,
            target_label=target_label,
        )
        status = 200 if result.get("ok") else 400
        return jsonify(result), status
    except Exception:
        current_app.logger.exception("api_dashboard_promotion_activate")
        db.session.rollback()
        return jsonify(ok=False, error="Could not activate promotion. Try again."), 500


@bp.route("/api/dashboard/meeting-groups/<int:meeting_group_id>/events", methods=["GET"])
@login_required
def api_dashboard_meeting_group_events(meeting_group_id: int):
    uid = session.get("user_id")
    if not uid:
        return Response("Unauthorized", status=401, mimetype="text/plain; charset=utf-8")
    html = _render_directory_events_sections_html(int(uid), meeting_group_id)
    if html == "":
        return Response("Not found", status=404, mimetype="text/plain; charset=utf-8")
    return Response(html, mimetype="text/html; charset=utf-8")


@bp.route("/api/dashboard/pane/event-tickets", methods=["GET"])
def api_dashboard_pane_event_tickets():
    """HTML for Event Tickets tab (lazy-loaded)."""
    uid = session.get("user_id")
    if not uid:
        return Response("Unauthorized", status=401, mimetype="text/plain; charset=utf-8")
    meeting_groups = (
        MeetingGroup.query.filter_by(user_id=uid)
        .order_by(MeetingGroup.created_at.desc())
        .options(
            joinedload(MeetingGroup.industry),
            noload(MeetingGroup.meetings),
            selectinload(MeetingGroup.owner).options(
                noload(User.industries),
                noload(User.attendee_tags),
            ),
        )
        .all()
    )
    lbl = _dashboard_template_label_vars()
    if not meeting_groups:
        html = render_template(
            "partials/dashboard_event_tickets_inner.html",
            meeting_groups=[],
            dashboard_ticket_meetings=[],
            meeting_row_meta={},
            meeting_ticket_form_meta={},
            ticket_type_sold_qty={},
            ticket_heading_meeting=None,
            ticket_heading_group_name=None,
            ticket_type_id=request.args.get("ticket_type_id", type=int),
            selected_meeting_group_id=request.args.get("meeting_group_id", type=int),
            selected_meeting_group=None,
            meeting_group_event_counts={},
            ticket_picker_meetings=[],
            timedelta=timedelta,
            **lbl,
        )
        out = Response(html, mimetype="text/html; charset=utf-8")
        out.headers["Cache-Control"] = "private, no-store"
        return out

    dashboard_ticket_meetings = _dashboard_load_ticket_meetings_batch(meeting_groups)
    selected_meeting_group_id = request.args.get("meeting_group_id", type=int)
    selected_meeting_group = None
    selected_group_meetings: list = []
    edit_meeting = None
    if selected_meeting_group_id is not None:
        selected_meeting_group = next(
            (mg for mg in meeting_groups if mg.meeting_group_id == selected_meeting_group_id),
            None,
        )
        if selected_meeting_group is not None:
            selected_group_meetings = (
                Meeting.query.filter_by(meeting_group_id=selected_meeting_group_id)
                .order_by(Meeting.starts_at.asc())
                .options(
                    selectinload(Meeting.ticket_types),
                    selectinload(Meeting.meeting_group).joinedload(MeetingGroup.industry),
                    selectinload(Meeting.meeting_group)
                    .selectinload(MeetingGroup.owner)
                    .options(noload(User.industries), noload(User.attendee_tags)),
                )
                .all()
            )
    edit_meeting_id = request.args.get("edit_meeting_id", type=int)
    if edit_meeting_id is not None and selected_group_meetings:
        edit_meeting = next(
            (m for m in selected_group_meetings if m.meeting_id == edit_meeting_id),
            None,
        )
    ticket_meeting_id = request.args.get("ticket_meeting_id", type=int)
    ticket_heading_meeting, ticket_heading_group_name = _dashboard_resolve_ticket_heading(
        uid,
        meeting_groups,
        selected_meeting_group,
        selected_group_meetings,
        ticket_meeting_id,
        dashboard_ticket_meetings,
    )
    if selected_meeting_group is None and ticket_heading_meeting is not None:
        selected_meeting_group = next(
            (mg for mg in meeting_groups if mg.meeting_group_id == ticket_heading_meeting.meeting_group_id),
            None,
        )
        if selected_meeting_group is not None:
            selected_meeting_group_id = selected_meeting_group.meeting_group_id
            if not selected_group_meetings:
                selected_group_meetings = (
                    Meeting.query.filter_by(meeting_group_id=selected_meeting_group_id)
                    .order_by(Meeting.starts_at.asc())
                    .options(selectinload(Meeting.ticket_types))
                    .all()
                )
    meeting_group_event_counts = _dashboard_meeting_group_event_counts(meeting_groups)
    ticket_picker_meetings = [
        m
        for m in selected_group_meetings
        if (m.meeting_format or "") == "Face2Face"
    ] if selected_meeting_group else []
    meeting_row_meta, meeting_ticket_form_meta, ticket_type_sold_qty = (
        _dashboard_fill_meeting_ticket_meta_maps(
            dashboard_ticket_meetings,
            selected_meeting_group,
            selected_group_meetings,
            ticket_heading_meeting,
            edit_meeting,
        )
    )
    ticket_buy_availability = (
        _meeting_ticket_buy_availability(ticket_heading_meeting, uid)
        if ticket_heading_meeting is not None
        else None
    )
    html = render_template(
        "partials/dashboard_event_tickets_inner.html",
        meeting_groups=meeting_groups,
        dashboard_ticket_meetings=dashboard_ticket_meetings,
        meeting_row_meta=meeting_row_meta,
        meeting_ticket_form_meta=meeting_ticket_form_meta,
        ticket_type_sold_qty=ticket_type_sold_qty,
        ticket_heading_meeting=ticket_heading_meeting,
        ticket_heading_group_name=ticket_heading_group_name,
        ticket_type_id=request.args.get("ticket_type_id", type=int),
        selected_meeting_group_id=selected_meeting_group_id,
        selected_meeting_group=selected_meeting_group,
        meeting_group_event_counts=meeting_group_event_counts,
        ticket_picker_meetings=ticket_picker_meetings,
        ticket_buy_availability=ticket_buy_availability,
        timedelta=timedelta,
        **lbl,
    )
    out = Response(html, mimetype="text/html; charset=utf-8")
    out.headers["Cache-Control"] = "private, no-store"
    return out


@bp.route("/api/dashboard/organiser-attendees", methods=["GET"])
def api_dashboard_organiser_attendees():
    """HTML fragment for the organiser ticket-buyers accordion (lazy-loaded from /dashboard)."""
    uid = session.get("user_id")
    if not uid:
        return Response("Unauthorized", status=401, mimetype="text/plain; charset=utf-8")
    html = render_template(
        "partials/organiser_event_attendees_accordion.html",
        organiser_event_attendees=_organiser_event_attendees_for_owner(uid),
    )
    out = Response(html, mimetype="text/html; charset=utf-8")
    out.headers["Cache-Control"] = "private, no-store"
    return out


@bp.route("/api/dashboard/organiser-email-attendee", methods=["POST"])
def api_dashboard_organiser_email_attendee():
    """Send an email from the signed-in organiser to one ticket holder (Attendee Dashboard tab)."""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"ok": False, "error": "Please sign in again."}), 401
    try:
        ma_id = int(request.form.get("meeting_attendee_id") or 0)
    except (TypeError, ValueError):
        ma_id = 0
    message = (request.form.get("message") or "").strip()
    if not ma_id:
        return jsonify({"ok": False, "error": "Missing booking reference."}), 400
    if len(message) < 2:
        return jsonify({"ok": False, "error": "Please enter a message (at least a couple of characters)."}), 400
    if len(message) > 12000:
        return jsonify({"ok": False, "error": "Message is too long. Please keep it under about 12,000 characters."}), 400

    organiser = db.session.get(User, int(uid))
    if not organiser or not (organiser.email or "").strip():
        return jsonify(
            {"ok": False, "error": "Your profile does not have an email address; add one before messaging attendees."}
        ), 400

    ma = (
        MeetingAttendee.query.options(
            selectinload(MeetingAttendee.user).load_only(
                User.user_id,
                User.email,
                User.first_name,
                User.second_name,
                User.username,
            ),
            selectinload(MeetingAttendee.meeting).selectinload(Meeting.meeting_group),
        )
        .filter(MeetingAttendee.meeting_attendee_id == ma_id)
        .first()
    )
    if not ma or not ma.meeting:
        return jsonify({"ok": False, "error": "Booking not found."}), 404
    mg = ma.meeting.meeting_group
    if not mg or int(mg.user_id) != int(uid):
        return jsonify({"ok": False, "error": "You can only email attendees for your own events."}), 403
    att_user = ma.user
    if not att_user or not (att_user.email or "").strip():
        return jsonify({"ok": False, "error": "This attendee has no email on file."}), 400

    m = ma.meeting
    et = (m.title or "Event").strip()
    when_s = m.starts_at.strftime("%a %d %b %Y, %H:%M") if m.starts_at else "Date/time TBC"
    where_line = _meeting_where_line_owner_summary(m)
    gn = (mg.meeting_group_name or "").strip()

    nm = " ".join(
        p
        for p in [(att_user.first_name or "").strip(), (att_user.second_name or "").strip()]
        if p
    ).strip()
    to_name = nm or (att_user.username or "Attendee")

    organiser_name = (
        " ".join(
            p
            for p in [(organiser.first_name or "").strip(), (organiser.second_name or "").strip()]
            if p
        ).strip()
        or (organiser.username or "").strip()
    )

    try:
        _send_dashboard_organiser_to_attendee_email(
            to_email=att_user.email.strip(),
            to_name=to_name,
            organiser_name=organiser_name,
            organiser_email=organiser.email.strip(),
            event_title=et,
            event_when=when_s,
            event_where=where_line,
            group_name=gn,
            message_text=message,
        )
    except ValueError:
        return jsonify(
            {"ok": False, "error": "Email is not configured on the server. Please try again later."}
        ), 503
    except Exception:
        current_app.logger.exception("organiser_email_attendee failed ma_id=%s", ma_id)
        return jsonify({"ok": False, "error": "The message could not be sent. Try again in a moment."}), 500

    return jsonify({"ok": True})


@bp.route("/dashboard", methods=["GET", "POST"])
def platform_dashboard():
    if not session.get("user_id"):
        flash("You must be logged in to access these pages.", "info")
        return redirect(url_for("main.register"))

    meeting_group_errors = {}
    meeting_group_form_data = {
        "meeting_group_name": "",
        "description": "",
        "website_url": "",
        "industry_id": "",
    }
    show_meeting_group_modal = request.args.get("open") == "meeting-group"

    if request.method == "POST":
        uid = session.get("user_id")
        if not uid:
            flash("Please sign in to create a meeting group.", "info")
            return redirect(url_for("main.login"))

        user = User.query.get(uid)
        if not user:
            session.pop("user_id", None)
            flash("Please sign in again.", "warning")
            return redirect(url_for("main.login"))

        meeting_group_form_data = {
            "meeting_group_name": request.form.get("meeting_group_name", "").strip(),
            "description": _sanitize_rich_text_html(request.form.get("description")),
            "website_url": (request.form.get("website_url") or "").strip(),
            "industry_id": request.form.get("industry_id", "").strip(),
        }
        show_meeting_group_modal = True

        if not meeting_group_form_data["meeting_group_name"]:
            meeting_group_errors["meeting_group_name"] = (
                "Please enter a meeting group name."
            )
        elif len(meeting_group_form_data["meeting_group_name"]) > 180:
            meeting_group_errors["meeting_group_name"] = (
                "Meeting group name must be 180 characters or fewer."
            )

        selected_industry_id = None
        if not meeting_group_form_data["industry_id"]:
            meeting_group_errors["industry_id"] = "Please choose a topic."
        else:
            try:
                selected_industry_id = int(meeting_group_form_data["industry_id"])
            except ValueError:
                selected_industry_id = None
            if selected_industry_id is None or not Industry.query.get(selected_industry_id):
                meeting_group_errors["industry_id"] = "Please choose a valid topic."
        website_url, website_err = _normalize_optional_http_url(
            meeting_group_form_data["website_url"]
        )
        if website_err:
            meeting_group_errors["website_url"] = website_err

        image_filename = None
        if not meeting_group_errors:
            image_file = request.files.get("group_image")
            if not image_file or not (image_file.filename or "").strip():
                meeting_group_errors["group_image"] = (
                    "Please choose an image for this meeting group (JPG, PNG, or WEBP)."
                )
            else:
                os.makedirs(MEETING_GROUP_IMAGE_DIR, exist_ok=True)
                image_filename = (
                    f"mg_{user.user_id}_{int(datetime.utcnow().timestamp())}.png"
                )
                target_path = os.path.join(MEETING_GROUP_IMAGE_DIR, image_filename)
                try:
                    _resize_meeting_group_image(image_file, target_path)
                except UnidentifiedImageError:
                    image_filename = None
                    meeting_group_errors["group_image"] = (
                        "That file doesn't look like a valid image. Try a JPG, PNG, or WEBP."
                    )
                except Exception:
                    image_filename = None
                    meeting_group_errors["group_image"] = (
                        "Something went wrong saving the image. Please try again."
                    )

        if not meeting_group_errors:
            meeting_group = MeetingGroup(
                user_id=user.user_id,
                meeting_group_name=meeting_group_form_data["meeting_group_name"],
                description=meeting_group_form_data["description"] or None,
                website_url=website_url,
                created_at=datetime.utcnow(),
                image_filename=image_filename,
                meeting_format="Face2Face",
                industry_id=selected_industry_id,
            )
            db.session.add(meeting_group)
            db.session.commit()
            flash("Meeting group created successfully.", "success")
            return redirect(url_for("main.platform_dashboard", _anchor="meetings-pane"))

    def _platform_dashboard_after_post():
        _env_timing = (os.getenv("TNW_DASHBOARD_TIMING") or "").strip().lower()
        _env_timing_off = _env_timing in ("0", "false", "no", "off")
        _timing = current_app.config.get("DASHBOARD_REQUEST_TIMING", False)
        if current_app.debug and not _env_timing_off:
            _timing = True
        _t0 = time_mod.perf_counter()
        _last = [_t0]

        def _dash_lap(label: str) -> None:
            if not _timing:
                return
            now = time_mod.perf_counter()
            step_ms = (now - _last[0]) * 1000
            tot_ms = (now - _t0) * 1000
            _last[0] = now
            sys.stderr.write(
                f"[dashboard timing] {label}: +{step_ms:.1f}ms (cumulative {tot_ms:.1f}ms)\n"
            )
            sys.stderr.flush()

        _dash_lap("start")
        uid = session.get("user_id")
        meeting_groups = (
            MeetingGroup.query.filter_by(user_id=uid)
            .order_by(MeetingGroup.created_at.desc())
            .options(
                joinedload(MeetingGroup.industry),
                noload(MeetingGroup.meetings),
                selectinload(MeetingGroup.owner).options(
                    noload(User.industries),
                    noload(User.attendee_tags),
                ),
            )
            .all()
            if uid
            else []
        )
        _dash_lap("meeting_groups_query")
        # Event Tickets tab loads meetings + ticket meta via
        # GET /api/dashboard/pane/event-tickets (see platform_dashboard lazy mount).
        dashboard_ticket_meetings: list[Meeting] = []
        _dash_lap("dashboard_ticket_meetings_batch_skipped")
        meeting_group_event_counts = {}
        if meeting_groups:
            mg_ids = [mg.meeting_group_id for mg in meeting_groups]
            # Batch large IN() lists (SQL Server had a ~2100 parameter cap; batching is harmless on MariaDB).
            batch_size = 1800
            for i in range(0, len(mg_ids), batch_size):
                mg_ids_batch = mg_ids[i : i + batch_size]
                count_rows = (
                    db.session.query(
                        Meeting.meeting_group_id,
                        func.count(Meeting.meeting_id).label("total_n"),
                        func.sum(case((Meeting.status == "Draft", 1), else_=0)).label(
                            "draft_n"
                        ),
                        func.sum(case((Meeting.status == "Live", 1), else_=0)).label("live_n"),
                    )
                    .filter(Meeting.meeting_group_id.in_(mg_ids_batch))
                    .group_by(Meeting.meeting_group_id)
                    .all()
                )
                for row in count_rows:
                    meeting_group_event_counts[row.meeting_group_id] = {
                        "total": int(row.total_n or 0),
                        "draft": int(row.draft_n or 0),
                        "live": int(row.live_n or 0),
                    }
            for i in range(0, len(mg_ids), batch_size):
                mg_ids_batch = mg_ids[i : i + batch_size]
                draft_rows = (
                    db.session.query(
                        Meeting.meeting_group_id,
                        func.min(Meeting.meeting_id).label("first_draft_id"),
                    )
                    .filter(
                        Meeting.meeting_group_id.in_(mg_ids_batch),
                        Meeting.status == "Draft",
                    )
                    .group_by(Meeting.meeting_group_id)
                    .all()
                )
                for row in draft_rows:
                    gid = row.meeting_group_id
                    if gid in meeting_group_event_counts and row.first_draft_id:
                        meeting_group_event_counts[gid]["first_draft_id"] = int(
                            row.first_draft_id
                        )
            for gid in mg_ids:
                meeting_group_event_counts.setdefault(
                    gid, {"total": 0, "draft": 0, "live": 0}
                )
        _dash_lap("meeting_group_event_counts")
        meeting_groups_page = request.args.get("meeting_groups_page", type=int) or 1
        if meeting_groups_page < 1:
            meeting_groups_page = 1
        meeting_groups_pagination = _ListPagination(meeting_groups, meeting_groups_page, 12)
        meeting_groups_page_items = list(meeting_groups_pagination.items or [])
        meeting_groups_nav_kwargs = {}
        for k in request.args.keys():
            if k in ("meeting_groups_page", "mg_saved"):
                continue
            vals = request.args.getlist(k)
            if not vals:
                continue
            meeting_groups_nav_kwargs[k] = vals[0] if len(vals) == 1 else vals
        industries = Industry.query.order_by(Industry.industry).all()
        _dash_lap("industries_query")
        selected_meeting_group_id = request.args.get("meeting_group_id", type=int)
        if selected_meeting_group_id is not None:
            owned_ids = {mg.meeting_group_id for mg in meeting_groups}
            if selected_meeting_group_id not in owned_ids:
                foreign = MeetingGroup.query.get(selected_meeting_group_id)
                if foreign:
                    flash(
                        "Only the organiser can open this group in the dashboard. "
                        "Here is a read-only preview.",
                        "info",
                    )
                    return redirect(
                        url_for(
                            "main.meeting_group_public",
                            meeting_group_id=selected_meeting_group_id,
                        )
                    )
                flash("That event group was not found.", "warning")
                return redirect(url_for("main.platform_dashboard", _anchor="meetings-pane"))
        
        edit_meeting_id = request.args.get("edit_meeting_id", type=int)
        selected_meeting_group = None
        selected_group_meetings = []
        selected_group_meetings_upcoming = []
        selected_group_meetings_finished = []
        selected_group_meeting_countdowns = {}
        edit_meeting = None
        if meeting_groups:
            if selected_meeting_group_id is not None:
                selected_meeting_group = next(
                    (
                        mg
                        for mg in meeting_groups
                        if mg.meeting_group_id == selected_meeting_group_id
                    ),
                    None,
                )
        
            if selected_meeting_group is not None:
                # Default ORM loaders join Meeting→MeetingGroup→User and then join *all* organiser
                # industries and attendee tags in one statement (row multiplication + heavy sort).
                # The directory list only needs group + owner basics; defer tag/industry collections.
                _group_meetings_q = (
                    Meeting.query.filter_by(meeting_group_id=selected_meeting_group_id)
                    .order_by(Meeting.starts_at.asc())
                    .options(
                        selectinload(Meeting.ticket_types),
                        selectinload(Meeting.meeting_group).joinedload(MeetingGroup.industry),
                        selectinload(Meeting.meeting_group)
                        .selectinload(MeetingGroup.owner)
                        .options(
                            noload(User.industries),
                            noload(User.attendee_tags),
                        ),
                    )
                )
                selected_group_meetings = _group_meetings_q.all()
                (
                    selected_group_meetings_upcoming,
                    selected_group_meetings_finished,
                ) = _partition_directory_group_meetings(selected_group_meetings)
                selected_group_meetings = (
                    selected_group_meetings_upcoming
                    + selected_group_meetings_finished
                )
                countdown_now = datetime.utcnow()
                selected_group_meeting_countdowns = {
                    meeting.meeting_id: _event_countdown_label(
                        meeting.starts_at, countdown_now
                    )
                    for meeting in selected_group_meetings
                }
                _dash_lap("selected_group_meetings+tickets+countdowns")
                if edit_meeting_id is not None:
                    edit_meeting = next(
                        (
                            meeting
                            for meeting in selected_group_meetings
                            if meeting.meeting_id == edit_meeting_id
                        ),
                        None,
                    )
                    if edit_meeting is not None and (
                        _sold_qty_by_meeting_ids([edit_meeting.meeting_id]).get(
                            edit_meeting.meeting_id, 0
                        )
                        > 0
                    ):
                        flash(MEETING_LOCKED_AFTER_TICKET_SALES_TEXT, "warning")
                        return redirect(
                            url_for(
                                "main.platform_dashboard",
                                meeting_group_id=selected_meeting_group_id,
                                _anchor="directory-pane",
                            )
                        )
            else:
                selected_meeting_group_id = None
                selected_group_meetings_upcoming = []
                selected_group_meetings_finished = []
                _dash_lap("selected_group_meetings(skipped-no-open-group)")
        
        _dash_lap("after_meeting_directory_branch")
        edit_meeting_group = None
        if request.method == "GET":
            edit_meeting_group = session.pop("meeting_group_edit", None)
        show_edit_meeting_group_modal = bool(
            edit_meeting_group and edit_meeting_group.get("meeting_group_id")
        )
        open_meeting_group_id = request.args.get("open_meeting_group_id", type=int)
        open_meeting_group_edit = request.args.get("open_meeting_group_edit") == "1"
        mg_saved = request.args.get("mg_saved") == "1"
        
        # Ticket panel heading is rendered only in GET /api/dashboard/pane/event-tickets
        # (lazy tab). Resolving it here used to scan all Face2Face meetings (~seconds) for
        # every /dashboard load even when the Event Tickets tab was never opened.
        _dash_lap("ticket_heading_scan_skipped_shell")

        meeting_create_min_date = date.today().isoformat()
        default_meeting_title_face = ""
        default_meeting_title_virtual = ""
        virtual_platform_choices = [
            "Microsoft Teams",
            "Zoom",
            "Google Meet",
            "Cisco Webex",
            "GoTo Meeting",
            "Skype",
            "Discord",
            "Other",
        ]
        if selected_meeting_group:
            gn = (selected_meeting_group.meeting_group_name or "").strip() or "Event"
            face_count = sum(
                1
                for m in selected_group_meetings
                if (m.meeting_format or "Face2Face") != "Virtual"
            )
            virt_count = sum(
                1 for m in selected_group_meetings if (m.meeting_format or "") == "Virtual"
            )
            default_meeting_title_face = f"{gn} Meeting {face_count + 1}"[:180]
            default_meeting_title_virtual = f"{gn} Virtual Meeting {virt_count + 1}"[:180]
        
        if selected_meeting_group is not None:
            meeting_row_meta, _meeting_ticket_form_meta_unused, _ticket_type_sold_qty_unused = (
                _dashboard_fill_meeting_ticket_meta_maps(
                    dashboard_ticket_meetings,
                    selected_meeting_group,
                    selected_group_meetings,
                    None,
                    edit_meeting,
                )
            )
        else:
            meeting_row_meta = {}
        _dash_lap("meeting_ticket_meta+sold_qty_by_meeting")
        _dash_lap("sold_qty_by_ticket_type_ids")

        promote_catalog = _dashboard_promote_catalog(uid, meeting_group_event_counts)
        _dash_lap("promote_catalog")

        _dash_lap("before_render_template")
        _resp = render_template(
            "platform_dashboard.html",
            promote_catalog=promote_catalog,
            meeting_group_errors=meeting_group_errors,
            meeting_group_form_data=meeting_group_form_data,
            show_meeting_group_modal=show_meeting_group_modal,
            meeting_groups=meeting_groups,
            meeting_groups_page_items=meeting_groups_page_items,
            meeting_groups_pagination=meeting_groups_pagination,
            meeting_groups_nav_kwargs=meeting_groups_nav_kwargs,
            meeting_group_event_counts=meeting_group_event_counts,
            industries=industries,
            selected_meeting_group_id=selected_meeting_group_id,
            selected_meeting_group=selected_meeting_group,
            selected_group_meetings=selected_group_meetings,
            selected_group_meetings_upcoming=selected_group_meetings_upcoming,
            selected_group_meetings_finished=selected_group_meetings_finished,
            selected_group_meeting_countdowns=selected_group_meeting_countdowns,
            edit_meeting=edit_meeting,
            timedelta=timedelta,
            edit_meeting_group=edit_meeting_group,
            show_edit_meeting_group_modal=show_edit_meeting_group_modal,
            open_meeting_group_id=open_meeting_group_id,
            open_meeting_group_edit=open_meeting_group_edit,
            mg_saved=mg_saved,
            meeting_create_min_date=meeting_create_min_date,
            default_meeting_title_face=default_meeting_title_face,
            default_meeting_title_virtual=default_meeting_title_virtual,
            virtual_platform_choices=virtual_platform_choices,
            meeting_row_meta=meeting_row_meta,
            meeting_locked_after_sales_message=MEETING_LOCKED_AFTER_TICKET_SALES_TEXT,
        )
        _dash_lap("render_template")
        return _resp

    return _platform_dashboard_after_post()


def _request_wants_json_response() -> bool:
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    return request.form.get("ajax") == "1"


def _meeting_wizard_json_item(m: Meeting) -> dict:
    mid = int(m.meeting_id)
    has_sales = _sold_qty_by_meeting_ids([mid]).get(mid, 0) > 0
    starts_iso = ""
    if m.starts_at:
        starts_iso = m.starts_at.strftime("%Y-%m-%dT%H:%M")
    return {
        "id": mid,
        "title": (m.title or "").strip(),
        "subject_html": m.subject or "",
        "meeting_format": (m.meeting_format or "").strip(),
        "starts_at": starts_iso,
        "duration_minutes": int(m.duration_minutes or 60),
        "website_url": (m.website_url or "").strip(),
        "status": (m.status or "Draft").strip(),
        "has_tickets_sold": has_sales,
    }


def _meeting_group_wizard_json_item(
    mg: MeetingGroup, meetings: list[Meeting] | None = None
) -> dict:
    industry_name = ""
    if mg.industry_id:
        ind = Industry.query.get(int(mg.industry_id))
        industry_name = (ind.industry or "").strip() if ind else ""
    mg_id = int(mg.meeting_group_id)
    has_sales = mg_id in _meeting_group_ids_with_ticket_sales([mg_id])
    meeting_rows = meetings if meetings is not None else list(mg.meetings or [])
    return {
        "id": mg_id,
        "name": (mg.meeting_group_name or "").strip(),
        "industry_id": int(mg.industry_id) if mg.industry_id else None,
        "industry_name": industry_name,
        "website_url": (mg.website_url or "").strip(),
        "description_html": mg.description or "",
        "image_url": meeting_group_image_url(mg),
        "has_tickets_sold": has_sales,
        "meetings": [_meeting_wizard_json_item(m) for m in meeting_rows],
        "edit_url": url_for(
            "main.platform_dashboard",
            open_meeting_group_id=mg_id,
            open_meeting_group_edit=1,
            _anchor="meetings-pane",
        ),
    }


def _validate_wizard_meeting_step_fields(form) -> tuple[dict, list[str]]:
    """Validate event-wizard step 2 fields for create or inline update."""
    errors: list[str] = []
    data: dict = {}

    meeting_format = (form.get("meeting_format") or "").strip()
    if meeting_format not in ("Face2Face", "Virtual"):
        errors.append("Choose in person or virtual.")
    else:
        data["meeting_format"] = meeting_format

    title = (form.get("title") or "").strip()
    if not title:
        errors.append("Event title is required.")
    elif len(title) > 180:
        errors.append("Title must be 180 characters or fewer.")
    else:
        data["title"] = title

    subject = _sanitize_rich_text_html(form.get("subject"))
    if not _rich_text_plain_text(subject):
        errors.append("Event description is required.")
    else:
        data["subject"] = subject

    try:
        starts_at = _parse_optional_datetime(form.get("starts_at"))
    except ValueError:
        starts_at = None
        errors.append("Start date/time is not valid.")
    if not starts_at:
        if "Start date/time is not valid." not in errors:
            errors.append("Start date/time is required.")
    else:
        data["starts_at"] = starts_at

    try:
        duration_minutes = _parse_optional_int(form.get("duration_minutes"))
    except ValueError:
        duration_minutes = None
        errors.append("Duration must be a whole number of minutes.")
    if not duration_minutes or duration_minutes < 15:
        errors.append("Duration must be at least 15 minutes.")
    else:
        data["duration_minutes"] = int(duration_minutes)

    website_url, website_err = _normalize_optional_http_url(form.get("event_website_url"))
    if website_err:
        errors.append(website_err)
    else:
        data["website_url"] = website_url

    return data, errors


def _prune_meeting_group_tags_to_topic(mg: MeetingGroup) -> None:
    """Keep only keywords whose topic matches the event group's industry_id."""
    if mg.industry_id is None:
        if mg.tags:
            mg.tags = []
        return
    iid = int(mg.industry_id)
    cur = list(mg.tags or [])
    pruned = [t for t in cur if int(t.industry_id) == iid]
    if len(pruned) != len(cur):
        mg.tags = pruned


def _meeting_nonempty_field(val: str | None) -> str:
    text = (val or "").strip()
    if text.lower() == "none":
        return ""
    return text


def _meeting_meets_live_format_constraint(meeting: Meeting) -> bool:
    """Match dbo.events CK_meetings_format_required_fields for status Live."""
    fmt = (meeting.meeting_format or "").strip()
    if fmt == "Face2Face":
        return bool(
            _meeting_nonempty_field(meeting.location_city)
            and _meeting_nonempty_field(meeting.location_postcode)
            and _meeting_nonempty_field(meeting.location_country)
        )
    if fmt == "Virtual":
        return bool(
            _meeting_nonempty_field(meeting.virtual_platform)
            and _meeting_nonempty_field(meeting.virtual_link)
        )
    return False


def _meeting_live_format_warning(meeting: Meeting) -> str:
    title = (meeting.title or "This event").strip()
    fmt = (meeting.meeting_format or "").strip()
    if fmt == "Virtual":
        return (
            f"“{title}” stayed draft — add a platform and join link on the Events tab "
            "before marking the group live."
        )
    return (
        f"“{title}” stayed draft — add city, postcode, and country on the Events tab "
        "before marking the group live."
    )


def _sync_meeting_group_events_status(
    mg: MeetingGroup, target_status: str
) -> tuple[list[str], list[tuple[int, str]]]:
    """Apply Live/Draft to all events in a group; return warnings and (meeting_id, previous_status) newly live."""
    target = (target_status or "").strip()
    if target not in {"Live", "Draft"}:
        return [], []
    meetings = Meeting.query.filter_by(meeting_group_id=mg.meeting_group_id).all()
    if not meetings:
        if target == "Live":
            return ["Add an event to this group before you can mark it live."], []
        return [], []
    warnings: list[str] = []
    newly_live: list[tuple[int, str]] = []
    sold_map = _sold_qty_by_meeting_ids([int(m.meeting_id) for m in meetings])
    if target == "Draft":
        for meeting in meetings:
            if (meeting.status or "").strip() != "Live":
                continue
            if sold_map.get(int(meeting.meeting_id), 0) > 0:
                warnings.append(
                    f"“{meeting.title}” stayed live because tickets have already been sold."
                )
                continue
            meeting.status = "Draft"
    else:
        for meeting in meetings:
            if (meeting.status or "").strip() != "Draft":
                continue
            if not _meeting_meets_live_format_constraint(meeting):
                warnings.append(_meeting_live_format_warning(meeting))
                continue
            previous_status = (meeting.status or "").strip()
            meeting.status = "Live"
            newly_live.append((int(meeting.meeting_id), previous_status))
    return warnings, newly_live


@bp.route("/meeting-groups/update", methods=["POST"])
@login_required
def meeting_group_update():
    uid = session["user_id"]
    mid = request.form.get("meeting_group_id", type=int)
    wants_json = _request_wants_json_response()
    current_app.logger.info(
        "[meeting-group-update] start user_id=%s meeting_group_id=%s industry_id=%r website_url=%r",
        uid,
        mid,
        request.form.get("industry_id"),
        request.form.get("website_url"),
    )
    if not mid:
        if wants_json:
            return jsonify(ok=False, error="Invalid meeting group."), 400
        flash("Invalid meeting group.", "danger")
        return redirect(url_for("main.platform_dashboard", _anchor="meetings-pane"))

    user = User.query.get(uid)
    mg = MeetingGroup.query.get(mid)
    if not user or not mg or mg.user_id != user.user_id:
        if wants_json:
            return jsonify(ok=False, error="Meeting group not found."), 404
        flash("Meeting group not found.", "danger")
        return redirect(url_for("main.platform_dashboard", _anchor="meetings-pane"))

    name = request.form.get("meeting_group_name", "").strip()
    description = _sanitize_rich_text_html(request.form.get("description"))
    raw_website_url = (request.form.get("website_url") or "").strip()
    raw_industry_id = request.form.get("industry_id", "").strip()
    sync_tags = request.form.get("meeting_group_tags_sync") == "1"
    raw_group_status = (request.form.get("meeting_group_status") or "").strip()
    if raw_group_status not in {"Live", "Draft"}:
        raw_group_status = ""
    errors = {}

    if not name:
        errors["meeting_group_name"] = "Please enter a meeting group name."
    elif len(name) > 180:
        errors["meeting_group_name"] = (
            "Meeting group name must be 180 characters or fewer."
        )

    industry_id = None
    if not raw_industry_id:
        errors["industry_id"] = "Please choose a topic."
    else:
        try:
            industry_id = int(raw_industry_id)
        except ValueError:
            industry_id = None
        if industry_id is None or not Industry.query.get(industry_id):
            errors["industry_id"] = "Please choose a valid topic."
    website_url, website_err = _normalize_optional_http_url(raw_website_url)
    if website_err:
        errors["website_url"] = website_err

    new_image_filename = None
    image_file = request.files.get("group_image")
    if image_file and (image_file.filename or "").strip():
        os.makedirs(MEETING_GROUP_IMAGE_DIR, exist_ok=True)
        new_image_filename = f"mg_{user.user_id}_{int(datetime.utcnow().timestamp())}.png"
        target_path = os.path.join(MEETING_GROUP_IMAGE_DIR, new_image_filename)
        try:
            _resize_meeting_group_image(image_file, target_path)
        except UnidentifiedImageError:
            new_image_filename = None
            errors["group_image"] = (
                "That file doesn't look like a valid image. Try a JPG, PNG, or WEBP."
            )
        except Exception:
            new_image_filename = None
            errors["group_image"] = (
                "Something went wrong saving the image. Please try again."
            )

    if errors:
        current_app.logger.info(
            "[meeting-group-update] validation_failed meeting_group_id=%s errors=%s",
            mid,
            errors,
        )
        if wants_json:
            return jsonify(ok=False, errors=errors), 400
        session["meeting_group_edit"] = {
            "meeting_group_id": mid,
            "reopen_edit": True,
            "data": {
                "meeting_group_name": name,
                "description": description,
                "website_url": raw_website_url,
                "image_filename": mg.image_filename,
                "industry_id": raw_industry_id,
                "meeting_group_status": raw_group_status or "Draft",
            },
            "errors": errors,
        }
        return redirect(url_for("main.platform_dashboard", _anchor="meetings-pane"))

    old_image = (mg.image_filename or "").strip()
    mg.meeting_group_name = name
    mg.description = description or None
    mg.website_url = website_url
    mg.industry_id = industry_id

    orphan_tag_count = 0
    if not sync_tags and industry_id is not None:
        cur_tags = list(mg.tags or [])
        orphan_tag_count = sum(
            1 for t in cur_tags if int(t.industry_id) != int(industry_id)
        )

    if sync_tags:
        submitted_tag_ids = {
            int(v)
            for v in request.form.getlist("tag_ids")
            if str(v).strip().isdigit()
        }
        mg.tags = (
            Tag.query.filter(
                Tag.tag_id.in_(submitted_tag_ids),
                Tag.industry_id == industry_id,
            )
            .order_by(Tag.tag)
            .all()
            if submitted_tag_ids
            else []
        )
    _prune_meeting_group_tags_to_topic(mg)
    if orphan_tag_count:
        flash(
            f"Removed {orphan_tag_count} keyword(s) that belonged to a different topic than this group.",
            "info",
        )

    if new_image_filename:
        if old_image:
            old_path = os.path.join(MEETING_GROUP_IMAGE_DIR, old_image)
            if os.path.isfile(old_path):
                try:
                    os.remove(old_path)
                except OSError:
                    pass
        mg.image_filename = new_image_filename

    status_warnings: list[str] = []
    newly_live_events: list[tuple[int, str]] = []
    if raw_group_status:
        status_warnings, newly_live_events = _sync_meeting_group_events_status(
            mg, raw_group_status
        )

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        current_app.logger.exception(
            "[meeting-group-update] integrity error meeting_group_id=%s",
            mid,
        )
        err_msg = (
            "Your group was not saved because one or more events are missing details "
            "required to go live (venue city, postcode, and country for in-person events; "
            "platform and join link for online events). Update those events on the Events tab, "
            "then try again."
        )
        if wants_json:
            return jsonify(ok=False, error=err_msg), 400
        flash(err_msg, "danger")
        session["meeting_group_edit"] = {
            "meeting_group_id": mid,
            "reopen_edit": True,
            "data": {
                "meeting_group_name": name,
                "description": description,
                "website_url": raw_website_url,
                "image_filename": mg.image_filename,
                "industry_id": raw_industry_id,
                "meeting_group_status": raw_group_status or "Draft",
            },
            "errors": {},
        }
        return redirect(url_for("main.platform_dashboard", _anchor="meetings-pane"))

    if mg.user_id and newly_live_events:
        for event_id, prev_status in newly_live_events:
            _tnw_commit_event_listing_record(int(mg.user_id), event_id, prev_status)

    current_app.logger.info(
        "[meeting-group-update] success meeting_group_id=%s industry_id=%s website_url=%r",
        mid,
        industry_id,
        website_url,
    )
    if wants_json:
        info_msg = None
        if orphan_tag_count:
            info_msg = (
                f"Removed {orphan_tag_count} keyword(s) that belonged to a "
                "different topic than this group."
            )
        return jsonify(
            ok=True,
            group=_meeting_group_wizard_json_item(mg),
            info=info_msg,
        )
    session.pop("meeting_group_edit", None)
    flash("Meeting group updated.", "success")
    if status_warnings:
        current_app.logger.info(
            "[meeting-group-update] partial event status sync meeting_group_id=%s warnings=%r",
            mid,
            status_warnings,
        )
    return redirect(
        url_for("main.platform_dashboard", _anchor="meetings-pane", mg_saved=1)
    )


@bp.route("/meetings/wizard-update", methods=["POST"])
@login_required
def meeting_wizard_update():
    """Update an existing event from the event wizard (step 2 inline save)."""
    uid = int(session.get("user_id") or 0)
    user = User.query.get(uid) if uid else None
    if not user:
        session.pop("user_id", None)
        return jsonify(ok=False, error="Please sign in again."), 401

    meeting_id = request.form.get("meeting_id", type=int)
    meeting_group_id = request.form.get("meeting_group_id", type=int)
    if not meeting_id or not meeting_group_id:
        return jsonify(ok=False, error="Invalid event."), 400

    mg = MeetingGroup.query.get(meeting_group_id)
    meeting = Meeting.query.get(meeting_id)
    if (
        not mg
        or not meeting
        or mg.user_id != user.user_id
        or meeting.meeting_group_id != mg.meeting_group_id
        or meeting.creator_user_id != user.user_id
    ):
        return jsonify(ok=False, error="Event not found."), 404

    if _sold_qty_by_meeting_ids([meeting_id]).get(meeting_id, 0) > 0:
        return jsonify(
            ok=False,
            error=MEETING_LOCKED_AFTER_TICKET_SALES_TEXT,
        ), 400

    data, errors = _validate_wizard_meeting_step_fields(request.form)
    if errors:
        return jsonify(ok=False, errors={"_form": " ".join(errors)}), 400

    meeting.meeting_format = data["meeting_format"]
    meeting.title = data["title"][:180]
    meeting.subject = data["subject"]
    meeting.starts_at = data["starts_at"]
    meeting.duration_minutes = data["duration_minutes"]
    meeting.website_url = data["website_url"]
    mg.meeting_format = data["meeting_format"]

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        current_app.logger.exception("meeting_wizard_update: integrity error")
        return jsonify(ok=False, error="Could not save this event."), 500

    meetings = (
        Meeting.query.filter_by(meeting_group_id=mg.meeting_group_id)
        .order_by(Meeting.starts_at.desc(), Meeting.meeting_id.desc())
        .all()
    )
    return jsonify(
        ok=True,
        meeting=_meeting_wizard_json_item(meeting),
        group=_meeting_group_wizard_json_item(mg, meetings),
    )


@bp.route("/meeting-groups/delete", methods=["POST"])
@login_required
def meeting_group_delete():
    uid = session["user_id"]
    mid = request.form.get("meeting_group_id", type=int)
    if not mid:
        flash("Invalid meeting group.", "danger")
        return redirect(url_for("main.platform_dashboard", _anchor="meetings-pane"))

    user = User.query.get(uid)
    mg = MeetingGroup.query.get(mid)
    if not user or not mg or mg.user_id != user.user_id:
        flash("Meeting group not found.", "danger")
        return redirect(url_for("main.platform_dashboard", _anchor="meetings-pane"))

    meeting_count = Meeting.query.filter_by(meeting_group_id=mid).count()
    if meeting_count:
        flash(
            "This event group cannot be deleted because it still has "
            f"{meeting_count} event{'s' if meeting_count != 1 else ''}. "
            "Delete or move those events first.",
            "warning",
        )
        return redirect(
            url_for(
                "main.platform_dashboard",
                meeting_group_id=mid,
                _anchor="meetings-pane",
            )
        )

    old_image = (mg.image_filename or "").strip()
    db.session.delete(mg)
    db.session.commit()

    if old_image:
        old_path = os.path.join(MEETING_GROUP_IMAGE_DIR, old_image)
        if os.path.isfile(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass

    flash("Meeting group deleted.", "success")
    return redirect(url_for("main.platform_dashboard", _anchor="meetings-pane"))


@bp.route("/meeting-groups/tags/update", methods=["POST"])
@login_required
def meeting_group_tags_update():
    uid = session["user_id"]
    mid = request.form.get("meeting_group_id", type=int)
    if not mid:
        flash("Invalid meeting group.", "danger")
        return redirect(url_for("main.platform_dashboard", _anchor="meetings-pane"))

    mg = MeetingGroup.query.get(mid)
    if not mg or mg.user_id != uid:
        flash("Meeting group not found.", "danger")
        return redirect(url_for("main.platform_dashboard", _anchor="meetings-pane"))

    if not mg.industry_id:
        flash("Choose a topic before selecting keywords.", "warning")
        return redirect(url_for("main.platform_dashboard", _anchor="meetings-pane"))

    submitted_tag_ids = {
        int(v)
        for v in request.form.getlist("tag_ids")
        if str(v).strip().isdigit()
    }
    selected_tags = (
        Tag.query.filter(
            Tag.tag_id.in_(submitted_tag_ids),
            Tag.industry_id == mg.industry_id,
        )
        .order_by(Tag.tag)
        .all()
        if submitted_tag_ids
        else []
    )

    mg.tags = selected_tags
    db.session.commit()
    flash("Updated event group keywords.", "success")
    return redirect(url_for("main.platform_dashboard", _anchor="meetings-pane"))


@bp.route("/meeting-groups/admin-transfer", methods=["POST"])
@login_required
def meeting_group_admin_transfer():
    """Admin-only: transfer an event group and its events to another user."""
    return_page = request.form.get("meeting_groups_page", type=int) or 1
    if return_page < 1:
        return_page = 1
    from_admin_console = request.form.get("return_to") == "admin_functions"

    def _transfer_redirect(**dashboard_kwargs):
        if from_admin_console:
            return redirect(url_for("main.admin_events"))
        return redirect(
            url_for(
                "main.platform_dashboard",
                meeting_groups_page=return_page,
                **dashboard_kwargs,
            )
        )

    if not _session_site_admin_user():
        flash("Only site administrators can transfer event groups.", "danger")
        return _transfer_redirect(_anchor="meetings-pane")
    mid = request.form.get("meeting_group_id", type=int)
    target_uid = request.form.get("target_user_id", type=int)
    if not mid or not target_uid:
        flash("Choose an event group and a destination account.", "warning")
        return _transfer_redirect(_anchor="meetings-pane")

    mg = MeetingGroup.query.get(mid)
    target = User.query.get(target_uid)
    if not mg or not target:
        flash("The selected event group or user was not found.", "danger")
        return _transfer_redirect(_anchor="meetings-pane")

    if mg.user_id == target.user_id:
        flash("That event group is already owned by this user.", "info")
        if from_admin_console:
            return _transfer_redirect()
        return _transfer_redirect(_anchor="meetings-pane")

    moved_events_n = Meeting.query.filter_by(meeting_group_id=mid).count()
    mg.user_id = target.user_id
    Meeting.query.filter_by(meeting_group_id=mid).update(
        {"creator_user_id": target.user_id}, synchronize_session=False
    )
    db.session.commit()
    flash(
        f"Transferred '{mg.meeting_group_name}' to {target.email}. "
        f"{moved_events_n} event{'s' if moved_events_n != 1 else ''} and related tickets now follow the new owner.",
        "success",
    )
    if from_admin_console:
        return _transfer_redirect()
    return _transfer_redirect(_anchor="meetings-pane")


@bp.route("/admin/meeting-groups/bulk-delete", methods=["POST"])
@site_admin_required
def admin_meeting_groups_bulk_delete():
    ids = _admin_bulk_meeting_group_ids_from_form()
    if not ids:
        flash("Select at least one event group on this page.", "warning")
        return _admin_bulk_redirect_from_form()
    try:
        n = _admin_cascade_delete_meeting_groups(ids)
    except Exception:
        db.session.rollback()
        current_app.logger.exception("admin_meeting_groups_bulk_delete failed")
        flash("Could not delete the selected groups. No changes were saved.", "danger")
        return _admin_bulk_redirect_from_form()
    flash(
        f"Deleted {n} event group(s) and all related meetings, tickets, and attendees.",
        "success",
    )
    return _admin_bulk_redirect_from_form()


@bp.route("/admin/meeting-groups/cascade-delete", methods=["POST"])
@site_admin_required
def admin_meeting_group_cascade_delete():
    """Delete one event group with the same cascade as bulk delete (meetings, tickets, etc.)."""
    mid = request.form.get("meeting_group_id", type=int)
    if not mid:
        flash("No event group was specified.", "warning")
        return _admin_bulk_redirect_from_form()
    try:
        n = _admin_cascade_delete_meeting_groups([mid])
    except Exception:
        db.session.rollback()
        current_app.logger.exception("admin_meeting_group_cascade_delete failed")
        flash("Could not delete that event group. No changes were saved.", "danger")
        return _admin_bulk_redirect_from_form()
    if n:
        flash(
            "Deleted the event group and all related meetings, tickets, attendees, and images.",
            "success",
        )
    else:
        flash("That event group was not found or was already removed.", "warning")
    return _admin_bulk_redirect_from_form()


@bp.route("/admin/meeting-groups/bulk-transfer", methods=["POST"])
@site_admin_required
def admin_meeting_groups_bulk_transfer():
    ids = _admin_bulk_meeting_group_ids_from_form()
    target_uid = request.form.get("target_user_id", type=int)
    if not ids:
        flash("Select at least one event group on this page.", "warning")
        return _admin_bulk_redirect_from_form()
    if not target_uid:
        flash("Choose a destination user.", "warning")
        return _admin_bulk_redirect_from_form()
    target = User.query.get(target_uid)
    if not target:
        flash("Destination user was not found.", "danger")
        return _admin_bulk_redirect_from_form()

    groups = MeetingGroup.query.filter(MeetingGroup.meeting_group_id.in_(ids)).all()
    if not groups:
        flash("No matching event groups were found.", "warning")
        return _admin_bulk_redirect_from_form()

    moved_groups = 0
    skipped_already = 0
    meetings_touch = 0
    for mg in groups:
        if mg.user_id == target.user_id:
            skipped_already += 1
            continue
        meetings_touch += Meeting.query.filter_by(meeting_group_id=mg.meeting_group_id).update(
            {"creator_user_id": target.user_id}, synchronize_session=False
        )
        mg.user_id = target.user_id
        moved_groups += 1
    db.session.commit()

    parts = [
        f"Transferred {moved_groups} event group(s) to {target.email}.",
        f"Updated {meetings_touch} event(s) to the new organiser.",
    ]
    if skipped_already:
        parts.append(
            f"{skipped_already} group(s) were already owned by that user and were skipped."
        )
    msg = " ".join(parts)
    # Success only in the centered modal (admin_events); avoid duplicate flash banner.
    session["admin_bulk_transfer_notice"] = {
        "title": "Transfer complete",
        "body": msg,
    }
    return _admin_bulk_redirect_from_form()


def _admin_move_events_redirect_clean():
    """After moving events (or cancel path): open Move events page with an empty form."""
    q: dict = {}
    if not _admin_should_hide_test_users():
        q["ignore_test_users"] = "0"
    return redirect(url_for("main.admin_move_events", **q))


@bp.route("/admin/meeting-groups/lookup-for-move", methods=["GET"])
def admin_meeting_groups_lookup_for_move():
    """JSON: event groups whose name contains ``q`` (admin move-events flow)."""
    uid = session.get("user_id")
    if not uid:
        return jsonify(ok=False, error="Please sign in."), 401
    if not _session_site_admin_user():
        return jsonify(ok=False, error="Only site administrators can use this."), 403

    q = (request.args.get("q") or "").strip()
    if len(q) < 1:
        return jsonify(
            ok=False,
            error="Enter at least one character of the event group name.",
        ), 400
    if len(q) > 120:
        q = q[:120]

    dq = (
        MeetingGroup.query.options(selectinload(MeetingGroup.owner))
        .join(User, MeetingGroup.user_id == User.user_id)
        .filter(MeetingGroup.meeting_group_name.contains(q))
        .order_by(
            MeetingGroup.meeting_group_name.asc(),
            MeetingGroup.meeting_group_id.asc(),
        )
    )
    if _admin_should_hide_test_users():
        dq = dq.filter(~User.username.like("tnw_tu%"))

    rows = dq.limit(51).all()
    truncated = len(rows) > 50
    rows = rows[:50]
    groups = []
    for mg in rows:
        owner = mg.owner
        groups.append(
            {
                "id": int(mg.meeting_group_id),
                "name": mg.meeting_group_name or "",
                "owner_email": (owner.email if owner else "") or "",
            }
        )
    return jsonify(ok=True, groups=groups, truncated=truncated)


@bp.route("/admin/meetings/move-to-group", methods=["POST"])
@site_admin_required
def admin_meetings_move_to_group():
    """Reassign selected meetings to another event group (meeting_group_id only)."""
    meeting_ids: list[int] = []
    for raw in request.form.getlist("meeting_ids"):
        try:
            i = int(str(raw).strip())
            if i > 0:
                meeting_ids.append(i)
        except (TypeError, ValueError):
            continue
    meeting_ids = sorted(set(meeting_ids))[:500]
    target_gid = request.form.get("target_meeting_group_id", type=int)
    if not meeting_ids:
        flash("Select at least one event.", "warning")
        return _admin_move_events_redirect_clean()
    if not target_gid:
        flash("Choose a destination event group.", "warning")
        return _admin_move_events_redirect_clean()
    tgt = MeetingGroup.query.get(target_gid)
    if not tgt:
        flash("Destination event group was not found.", "danger")
        return _admin_move_events_redirect_clean()
    meetings = Meeting.query.filter(Meeting.meeting_id.in_(meeting_ids)).all()
    if not meetings:
        flash("No matching events were found.", "warning")
        return _admin_move_events_redirect_clean()
    moved = 0
    skipped_same = 0
    for m in meetings:
        if int(m.meeting_group_id) == int(target_gid):
            skipped_same += 1
            continue
        m.meeting_group_id = int(target_gid)
        moved += 1
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("admin_meetings_move_to_group failed")
        flash("Could not move the selected events. No changes were saved.", "danger")
        return _admin_move_events_redirect_clean()
    parts = [
        f"Moved {moved} event(s) to “{tgt.meeting_group_name}” (group ID {target_gid}).",
    ]
    if skipped_same:
        parts.append(
            f"{skipped_same} event(s) were already in that group and were skipped."
        )
    session["admin_move_events_notice"] = {
        "title": "Move complete",
        "body": " ".join(parts),
    }
    return _admin_move_events_redirect_clean()


@bp.route("/admin/meeting-groups/provision-transfer-user", methods=["POST"])
def admin_meeting_groups_provision_transfer_user():
    """Create an unverified organiser account for bulk transfer (site admins, JSON)."""
    uid = session.get("user_id")
    if not uid:
        return jsonify(ok=False, error="Please sign in."), 401
    if not User.query.get(uid):
        session.pop("user_id", None)
        return jsonify(ok=False, error="Please sign in again."), 401
    if not _session_site_admin_user():
        return jsonify(ok=False, error="Only site administrators can use this."), 403
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    email = (payload.get("email") or "").strip().lower()

    if not username or not USERNAME_RE.match(username):
        return jsonify(
            ok=False,
            error="Username must be 5–50 characters with no spaces.",
            field="username",
        ), 400
    if not email or not EMAIL_RE.match(email):
        return jsonify(
            ok=False, error="Please enter a valid email address.", field="email"
        ), 400
    if len(email) > 50:
        return jsonify(
            ok=False,
            error="Email is too long for the database (maximum 50 characters).",
            field="email",
        ), 400

    if User.query.filter(db.func.lower(User.username) == username.lower()).first():
        return jsonify(
            ok=False,
            error="That username is already taken.",
            field="username",
        ), 409
    if User.query.filter_by(email=email).first():
        return jsonify(
            ok=False,
            error="An account already exists with this email.",
            field="email",
        ), 409

    new_user = User(
        username=username,
        email=email,
        created_date=datetime.utcnow(),
        image_name=DEFAULT_IMAGE_NAME,
        country_id=DEFAULT_COUNTRY_ID,
        password_hash=None,
        verification_send=None,
        verification_code=None,
        verification_confirmed=None,
    )
    db.session.add(new_user)
    try:
        db.session.commit()
    except OperationalError as e:
        db.session.rollback()
        current_app.logger.exception(
            "admin_meeting_groups_provision_transfer_user commit failed"
        )
        orig = getattr(e, "orig", None)
        errno = orig.args[0] if orig and getattr(orig, "args", None) else None
        msg = str(orig) if orig else str(e)
        if errno == 1364 and "user_id" in msg:
            return jsonify(
                ok=False,
                error=(
                    "Database error: new users cannot be inserted because users.user_id is not "
                    "set up as an auto-incrementing primary key. Apply "
                    "scripts/mariadb_align_primary_keys_mynetworkermdb.sql "
                    "(adds PRIMARY KEY on user_id, then AUTO_INCREMENT). "
                    "If MariaDB returns error 1075, see "
                    "scripts/mariadb_fix_users_user_id_autoincrement_after_1075.sql."
                ),
            ), 500
        return jsonify(
            ok=False,
            error="Could not create the user. Please try again or contact support.",
        ), 500

    return jsonify(
        ok=True,
        user_id=new_user.user_id,
        email=new_user.email,
        username=new_user.username,
    )


def _gemini_polish_meeting_description(text: str, description_kind: str = "meeting-group"):
    """Call Google Gemini generateContent. Returns (True, polished_text) or (False, error_message)."""
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return (
            False,
            "AI is not configured on the server yet. Ask your administrator to set "
            "the GEMINI_API_KEY environment variable (Google AI Studio API key).",
        )

    # Model IDs change; if this 404s, set GEMINI_MODEL in .env (e.g. gemini-2.5-flash) or list models via REST.
    model = (os.environ.get("GEMINI_MODEL") or "gemini-2.0-flash").strip()
    system = (
        "You rewrite informal business copy into clear, professional UK English. "
        "Keep the same meaning and factual content. Use short paragraphs where it helps readability. "
        "Do not invent facts, prices, dates, or legal promises. "
        "Return only the improved description text, with no preamble or closing remarks."
    )
    user_msg = (
        f"Improve this {description_kind} description for a UK business networking platform.\n\n"
        "---\n"
        + text.strip()
        + "\n---"
    )
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_msg}],
            }
        ],
        "generationConfig": {
            "temperature": 0.35,
            "maxOutputTokens": 2048,
        },
    }
    body = json.dumps(payload).encode("utf-8")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + quote(model, safe="")
        + ":generateContent"
    )
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
            err_json = json.loads(err_body)
            err_obj = err_json.get("error") or {}
            msg = err_obj.get("message") or err_obj.get("status") or str(e)
        except Exception:
            msg = str(e)
        return False, msg or "Google Gemini returned an error."
    except urllib.error.URLError:
        return False, "Could not reach Google Gemini. Check the server network connection."
    except Exception:
        return False, "Something went wrong calling Google Gemini."

    try:
        data = json.loads(raw)
        candidates = data.get("candidates") or []
        if not candidates:
            fb = data.get("promptFeedback") or {}
            br = fb.get("blockReason")
            if br:
                return False, f"The prompt was blocked ({br}). Try shortening or rephrasing your text."
            return False, "Gemini returned no suggestions. Try again or edit manually."
        parts = (candidates[0].get("content") or {}).get("parts") or []
        if not parts or "text" not in parts[0]:
            return False, "Unexpected response shape from Gemini."
        polished = (parts[0].get("text") or "").strip()
        if not polished:
            return False, "Gemini returned an empty response. Try again or edit manually."
        return True, polished
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return False, "Unexpected response from Google Gemini."


def _parse_loose_json_from_gemini_text(text: str) -> dict | None:
    """Parse a JSON **object** from Gemini output.

    Handles: markdown ``` fences, preamble/postamble prose, root JSON array of objects
    (merged), single-element [ {...} ] wrapper, and first balanced {...} slice.
    """
    t = (text or "").strip()
    if not t:
        return None
    if t.startswith("```"):
        lines = t.split("\n")
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        while lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()

    def _coerce_to_dict(out: object) -> dict | None:
        if isinstance(out, dict):
            return out
        if isinstance(out, str):
            inner = out.strip()
            if inner.startswith("{") or inner.startswith("["):
                try:
                    nested = json.loads(inner)
                except json.JSONDecodeError:
                    return None
                return _coerce_to_dict(nested)
            return None
        if isinstance(out, list):
            merged: dict = {}
            for el in out:
                if isinstance(el, dict):
                    merged.update(el)
            return merged if merged else None
        return None

    def _loads(s: str) -> dict | None:
        s = (s or "").strip()
        if not s:
            return None
        try:
            return _coerce_to_dict(json.loads(s))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    got = _loads(t)
    if got is not None:
        return got

    # Model added text before/after JSON — take first balanced {...} block.
    i = t.find("{")
    while i != -1:
        depth = 0
        for j in range(i, len(t)):
            if t[j] == "{":
                depth += 1
            elif t[j] == "}":
                depth -= 1
                if depth == 0:
                    got = _loads(t[i : j + 1])
                    if got is not None:
                        return got
                    break
        i = t.find("{", i + 1)
    return None


def _gemini_generate_json_object(
    *,
    system: str,
    user_msg: str,
    temperature: float = 0.25,
    max_output_tokens: int = 8192,
    timeout_s: int = 120,
    use_json_mime: bool = True,
    terminal_progress_label: str | None = None,
) -> tuple[bool, dict | str]:
    """Call Gemini generateContent; prefer JSON MIME type, fall back on API error."""
    def _tp(msg: str) -> None:
        if terminal_progress_label:
            print(f"[{terminal_progress_label}] {msg}", flush=True)

    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        _tp("Aborted: GEMINI_API_KEY is not set.")
        return (
            False,
            "AI is not configured on the server yet. Ask your administrator to set "
            "the GEMINI_API_KEY environment variable (Google AI Studio API key).",
        )

    model = (os.environ.get("GEMINI_MODEL") or "gemini-2.0-flash").strip()
    gen_cfg: dict = {
        "temperature": temperature,
        "maxOutputTokens": max_output_tokens,
    }
    if use_json_mime:
        gen_cfg["responseMimeType"] = "application/json"

    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_msg}],
            }
        ],
        "generationConfig": gen_cfg,
    }
    body = json.dumps(payload).encode("utf-8")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + quote(model, safe="")
        + ":generateContent"
    )
    _tp(
        f"POST Gemini generateContent model={model!r} timeout_s={timeout_s} "
        f"json_mime={use_json_mime} max_out_tok={max_output_tokens} "
        f"payload_bytes={len(body)} (system_chars={len(system)} user_chars={len(user_msg)})"
    )

    def _do_request() -> tuple[bool, str]:
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return True, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8")
                err_json = json.loads(err_body)
                err_obj = err_json.get("error") or {}
                msg = err_obj.get("message") or err_obj.get("status") or str(e)
            except Exception:
                msg = str(e)
            return False, msg or "Google Gemini returned an error."
        except urllib.error.URLError:
            return False, "Could not reach Google Gemini. Check the server network connection."
        except Exception:
            return False, "Something went wrong calling Google Gemini."

    ok_http, raw = _do_request()
    if not ok_http and use_json_mime and (
        "responsemimetype" in (raw or "").lower()
        or "response_mime_type" in (raw or "").lower()
        or "Unknown name" in (raw or "")
    ):
        _tp("Retrying without responseMimeType=application/json (API rejected JSON mode).")
        return _gemini_generate_json_object(
            system=system,
            user_msg=user_msg,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            timeout_s=timeout_s,
            use_json_mime=False,
            terminal_progress_label=terminal_progress_label,
        )
    if not ok_http:
        _tp(f"Gemini request failed: {(raw or '')[:400]!r}")
        return False, raw

    try:
        data = json.loads(raw)
        candidates = data.get("candidates") or []
        if not candidates:
            fb = data.get("promptFeedback") or {}
            br = fb.get("blockReason")
            if br:
                _tp(f"No candidates; prompt blocked: {br!r}")
                return False, f"The prompt was blocked ({br}). Try again with a smaller dataset."
            _tp("No candidates in Gemini response (empty finish).")
            return False, "Gemini returned no output. Try again."
        parts = (candidates[0].get("content") or {}).get("parts") or []
        if not parts or "text" not in parts[0]:
            _tp("Unexpected response: missing content.parts[0].text")
            return False, "Unexpected response shape from Gemini."
        blob = (parts[0].get("text") or "").strip()
        if not blob:
            _tp("Gemini returned empty text blob.")
            return False, "Gemini returned an empty response."
        _tp(f"HTTP OK; model text length={len(blob)} chars, parsing JSON…")
        parsed = _parse_loose_json_from_gemini_text(blob)
        if not isinstance(parsed, dict):
            if not use_json_mime:
                _tp(
                    "Could not parse a JSON object from model text. "
                    f"Preview: {blob[:480]!r}"
                )
                return False, "Gemini did not return valid JSON. Try again."
            _tp(
                "Parsed value was not a usable JSON object (e.g. array-only or prose); "
                "retrying without responseMimeType=application/json …"
            )
            return _gemini_generate_json_object(
                system=system,
                user_msg=user_msg,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                timeout_s=timeout_s,
                use_json_mime=False,
                terminal_progress_label=terminal_progress_label,
            )
        _tp(f"Parsed JSON object with keys: {list(parsed.keys())[:12]!r}")
        return True, parsed
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as ex:
        _tp(f"Exception while handling Gemini response: {type(ex).__name__}: {ex!r}")
        return False, "Unexpected response from Google Gemini."


def _admin_event_group_keyword_corpus() -> tuple[str, dict]:
    """Plain-text corpus of all meeting group titles + descriptions (bounded size)."""
    budget = 120_000
    used = 0
    chunks: list[str] = []
    included_ids: list[int] = []
    n_in = 0
    n_total = db.session.query(func.count(MeetingGroup.meeting_group_id)).scalar() or 0
    n_total = int(n_total)
    # Column-only select: ORM ``yield_per`` on ``MeetingGroup`` conflicts with joined/selectin
    # loaders on ``owner`` / ``industry`` / ``tags`` / ``meetings``.
    dialect = (db.session.get_bind().dialect.name or "").lower()
    if dialect == "mssql":
        order_cols = (func.newid(),)
    elif dialect in ("mysql", "mariadb"):
        order_cols = (func.rand(),)
    else:
        order_cols = (MeetingGroup.meeting_group_id.asc(),)
    stmt = (
        select(
            MeetingGroup.meeting_group_id,
            MeetingGroup.meeting_group_name,
            MeetingGroup.description,
        )
        .order_by(*order_cols)
        .execution_options(yield_per=120)
    )
    for mg_id, meeting_group_name, description in db.session.execute(stmt):
        title = (meeting_group_name or "").strip()
        desc = _rich_text_plain_text(description or "")
        if len(desc) > 3200:
            desc = desc[:3200] + "…"
        block = (
            f"--- meeting_group id={mg_id}\n"
            f"meeting_group_name (title): {title}\n"
            f"description:\n{desc}\n"
        )
        if used + len(block) > budget:
            break
        chunks.append(block)
        used += len(block)
        n_in += 1
        try:
            included_ids.append(int(mg_id))
        except (TypeError, ValueError):
            pass
    corpus = "\n".join(chunks)
    meta = {
        "groups_included": n_in,
        "groups_total": n_total,
        "truncated": n_in < n_total,
        "included_group_ids": included_ids,
    }
    return corpus, meta


def _admin_existing_tags_for_keyword_ai() -> list[dict]:
    rows = Tag.query.order_by(Tag.industry_id.asc(), Tag.tag.asc()).all()
    out: list[dict] = []
    for t in rows:
        topic = ""
        if t.industry:
            topic = (t.industry.industry or "").strip()
        out.append(
            {
                "tag_id": t.tag_id,
                "industry_id": t.industry_id,
                "topic": topic,
                "tag": (t.tag or "").strip(),
            }
        )
    return out


def _admin_industry_lines_for_keyword_prompt() -> str:
    """All industries (topics), even if no tags exist yet — required for per-topic suggestions."""
    rows = Industry.query.order_by(Industry.industry_id.asc()).all()
    if not rows:
        return "(no industries/topics in database)"
    return "\n".join(
        f"- industry_id={ind.industry_id}: {(ind.industry or '').strip()}" for ind in rows
    )


def _admin_topic_name_lower_to_industry_id() -> dict[str, int]:
    out: dict[str, int] = {}
    for ind in Industry.query.all():
        nm = (ind.industry or "").strip().lower()
        if nm:
            out[nm] = int(ind.industry_id)
    return out


def _admin_tags_compact_json_for_prompt(existing_tags: list[dict], max_chars: int = 130_000) -> str:
    """Compact tag list for the model prompt (saves tokens). Each row: i=industry_id, id=tag_id, t=tag text."""
    if not existing_tags:
        return "[]"
    rows: list[dict] = []
    for e in existing_tags:
        rows.append(
            {
                "i": int(e["industry_id"]),
                "id": int(e["tag_id"]),
                "t": (e.get("tag") or "")[:80],
            }
        )
    s = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    while len(s) > max_chars and len(rows) > 80:
        rows = rows[: max(80, len(rows) * 4 // 5)]
        payload = {"tags_truncated": True, "count_sent": len(rows), "rows": rows}
        s = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return s


def _flatten_gemini_suggested_add(data: dict) -> list[dict]:
    """Merge flat suggested_add with per-topic shapes the model may return."""
    out: list[dict] = []
    raw = data.get("suggested_add")
    if isinstance(raw, list):
        for x in raw:
            if isinstance(x, dict):
                out.append(dict(x))
            elif isinstance(x, str) and x.strip():
                out.append({"tag": x.strip(), "industry_id": None})
    for key in (
        "suggested_add_by_topic",
        "suggestions_by_topic",
        "by_topic",
        "topics",
    ):
        blocks = data.get(key)
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            iid_raw = block.get("industry_id")
            try:
                iid = int(iid_raw) if iid_raw is not None and iid_raw != "" else None
            except (TypeError, ValueError):
                iid = None
            tags = (
                block.get("tags")
                or block.get("suggested_tags")
                or block.get("keywords")
                or block.get("suggestions")
                or []
            )
            if not isinstance(tags, list):
                continue
            for tag_el in tags:
                if isinstance(tag_el, str) and tag_el.strip():
                    out.append(
                        {
                            "tag": tag_el.strip(),
                            "industry_id": iid,
                            "reason": "",
                            "example_group_ids": [],
                        }
                    )
                elif isinstance(tag_el, dict):
                    row = dict(tag_el)
                    if row.get("industry_id") is None and iid is not None:
                        row["industry_id"] = iid
                    out.append(row)
    # Dict keyed by numeric industry id string → list of tags
    for k, v in data.items():
        if not isinstance(v, list) or k in (
            "suggested_add",
            "suggested_remove",
            "suggested_add_by_topic",
            "suggestions_by_topic",
            "by_topic",
            "topics",
            "notes",
            "link_existing_tags",
            "link_existing",
            "suggested_group_tag_links",
            "attach_existing_tags",
        ):
            continue
        try:
            iid = int(k)
        except (TypeError, ValueError):
            continue
        for tag_el in v:
            if isinstance(tag_el, str) and tag_el.strip():
                out.append(
                    {
                        "tag": tag_el.strip(),
                        "industry_id": iid,
                        "reason": "",
                        "example_group_ids": [],
                    }
                )
            elif isinstance(tag_el, dict):
                row = dict(tag_el)
                if row.get("industry_id") is None:
                    row["industry_id"] = iid
                out.append(row)
    return out


def _admin_validate_gemini_group_tag_links(
    result: dict,
    *,
    included_group_ids: list[int],
    existing_by_tag_id: dict[int, dict],
) -> tuple[list[dict], dict[str, int]]:
    """Parse link_existing_tags (or aliases) from Gemini; validate tag_id, group topic, not already linked."""
    stats: dict[str, int] = {
        "raw_rows": 0,
        "kept": 0,
        "skipped_bad_row": 0,
        "skipped_unknown_group": 0,
        "skipped_unknown_tag": 0,
        "skipped_wrong_topic": 0,
        "skipped_already_linked": 0,
        "skipped_not_in_corpus": 0,
        "skipped_dup_row": 0,
    }
    keys = (
        "link_existing_tags",
        "link_existing",
        "suggested_group_tag_links",
        "attach_existing_tags",
    )
    raw_rows: list[dict] = []
    for key in keys:
        block = result.get(key)
        if isinstance(block, list):
            for x in block:
                if isinstance(x, dict):
                    raw_rows.append(x)
            break
    stats["raw_rows"] = len(raw_rows)
    if not raw_rows:
        return [], stats

    inc_set = {int(x) for x in (included_group_ids or []) if x is not None}
    mids_needed: set[int] = set(inc_set)
    for row in raw_rows[:500]:
        for k in ("meeting_group_id", "group_id"):
            v = row.get(k)
            if v is not None:
                try:
                    mids_needed.add(int(v))
                except (TypeError, ValueError):
                    pass
                break
    mids_needed = {m for m in mids_needed if m > 0}
    if not mids_needed:
        return [], stats

    id_list = list(mids_needed)[:2000]
    mg_rows = (
        db.session.query(
            MeetingGroup.meeting_group_id,
            MeetingGroup.industry_id,
            MeetingGroup.meeting_group_name,
        )
        .filter(MeetingGroup.meeting_group_id.in_(id_list))
        .all()
    )
    mg_topic: dict[int, int | None] = {}
    mg_name: dict[int, str] = {}
    for mid, iid, nm in mg_rows:
        mid_i = int(mid)
        mg_topic[mid_i] = int(iid) if iid is not None else None
        mg_name[mid_i] = (nm or "").strip()[:200]

    existing_pairs: set[tuple[int, int]] = set()
    for i in range(0, len(id_list), 1200):
        chunk = id_list[i : i + 1200]
        if not chunk:
            break
        pr = db.session.execute(
            select(meeting_group_tags.c.meeting_group_id, meeting_group_tags.c.tag_id).where(
                meeting_group_tags.c.meeting_group_id.in_(chunk)
            )
        ).all()
        for mid, tid in pr:
            existing_pairs.add((int(mid), int(tid)))

    out: list[dict] = []
    seen_pair: set[tuple[int, int]] = set()
    for row in raw_rows[:400]:
        mid_raw = row.get("meeting_group_id") or row.get("group_id")
        tid_raw = row.get("tag_id") or row.get("id")
        if mid_raw is None or tid_raw is None:
            stats["skipped_bad_row"] += 1
            continue
        try:
            mid = int(mid_raw)
            tid = int(tid_raw)
        except (TypeError, ValueError):
            stats["skipped_bad_row"] += 1
            continue
        if (mid, tid) in seen_pair:
            stats["skipped_dup_row"] += 1
            continue
        seen_pair.add((mid, tid))
        if inc_set and mid not in inc_set:
            stats["skipped_not_in_corpus"] += 1
            continue
        if mid not in mg_topic:
            stats["skipped_unknown_group"] += 1
            continue
        gtopic = mg_topic[mid]
        if gtopic is None:
            stats["skipped_unknown_group"] += 1
            continue
        tag_info = existing_by_tag_id.get(tid)
        if not tag_info:
            stats["skipped_unknown_tag"] += 1
            continue
        try:
            tag_iid = int(tag_info.get("industry_id"))
        except (TypeError, ValueError):
            stats["skipped_unknown_tag"] += 1
            continue
        if int(gtopic) != tag_iid:
            stats["skipped_wrong_topic"] += 1
            continue
        if (mid, tid) in existing_pairs:
            stats["skipped_already_linked"] += 1
            continue
        out.append(
            {
                "meeting_group_id": mid,
                "meeting_group_name": mg_name.get(mid, ""),
                "tag_id": tid,
                "tag": (tag_info.get("tag") or "").strip()[:50],
                "topic": (tag_info.get("topic") or "").strip(),
                "reason": (row.get("reason") or "").strip()[:500],
            }
        )
        stats["kept"] += 1
        existing_pairs.add((mid, tid))
        if len(out) >= 280:
            break

    return out, stats


def _admin_gemini_keyword_response_shape_summary(data: dict, max_len: int = 2000) -> str:
    """Compact description of model JSON for terminal debugging (no corpus text)."""
    slim: dict = {}
    for k, v in data.items():
        if isinstance(v, list):
            first_t = type(v[0]).__name__ if v else "empty"
            slim[k] = {"len": len(v), "first_item_type": first_t}
        elif isinstance(v, dict):
            slim[k] = {"dict_keys": list(v.keys())[:20]}
        elif isinstance(v, str):
            slim[k] = {"str_len": len(v)}
        else:
            slim[k] = type(v).__name__
    s = json.dumps(slim, ensure_ascii=False)
    if len(s) > max_len:
        return s[:max_len] + "…"
    return s


def _gemini_suggest_admin_keywords(
    corpus: str, corpus_meta: dict, existing_tags: list[dict]
) -> tuple[bool, dict | str]:
    print(
        "[admin_keywords_suggest] Step 2: building Gemini prompt "
        f"(topics lines + {len(existing_tags)} tag row(s) + corpus).",
        flush=True,
    )
    topics_blob = _admin_industry_lines_for_keyword_prompt()
    tags_json = _admin_tags_compact_json_for_prompt(existing_tags)
    print(
        "[admin_keywords_suggest] Prompt material sizes: "
        f"topics_blob_chars={len(topics_blob)} tags_json_chars={len(tags_json)} corpus_chars={len(corpus)}",
        flush=True,
    )
    system = (
        "You are curating searchable keywords (tags) for a UK business networking web app. "
        "Meeting organisers pick tags from fixed topics (industries). "
        "You are given: (1) ALL topics as industry_id + name; (2) current tags as compact JSON rows "
        '{ "i": industry_id, "id": tag_id, "t": "tag text" } (or wrapped with tags_truncated); '
        "(3) a corpus of many event_groups: each block has meeting_group id, meeting_group_name (title), "
        "and description (plain text). Read the titles and descriptions carefully.\n\n"
        "Your job:\n"
        "- IMPORTANT: Propose which EXISTING tags (from the compact JSON, using tag_id) should be "
        "attached to which event groups. For each link, the group's real-world topic in the database "
        "must match the tag's industry_id (same topic). Only use meeting_group ids that appear in the corpus "
        "and tag_id values that appear in the JSON. Prefer links clearly supported by that group's title "
        "and description. Omit links that already fit the group's story (we will skip duplicates server-side). "
        'Return these in "link_existing_tags": array of { "meeting_group_id": number, "tag_id": number, '
        '"reason": string } (up to 200 rows).\n'
        "- Propose NEW tags to ADD for each topic where the corpus clearly supports themes, sectors, "
        "locations, event formats, or audiences. Aim for several strong additions per topic that has "
        "relevant language in the corpus — not only one or two for the whole site.\n"
        "- Each new tag must be 1–4 words, max 50 characters, Title Case or sentence case, "
        "no hashtags, no pipe characters.\n"
        "- For every suggested tag you MUST set industry_id to a real topic id from the topic list "
        "(never null unless impossible).\n"
        "- Do not repeat a tag that already exists for that industry_id (same spelling, case-insensitive).\n"
        "- Optionally suggest tags to REMOVE only when clearly redundant, misleading, or unsupported; "
        "otherwise return an empty suggested_remove array.\n\n"
        "Return ONE JSON object only (no markdown). Use these keys:\n"
        '"link_existing_tags": array of { "meeting_group_id": number, "tag_id": number, "reason": string } '
        "(existing vocabulary only; see job above).\n"
        '"suggested_add": array of objects, each { "tag": string, "industry_id": number, '
        '"reason": string (one short sentence tied to the corpus), "example_group_ids": array of integers '
        "(meeting_group ids from the corpus that support the tag). Include up to 80 objects across all topics.\n"
        'ALSO you may use "suggested_add_by_topic": array of { "industry_id": number, "tags": array of '
        '{ "tag", "reason", "example_group_ids" } } instead of or in addition to suggested_add; '
        "we will merge both.\n"
        '"suggested_remove": array of { "tag_id": number, "reason": string } using tag ids from the input JSON.\n'
        '"notes": string (brief summary of coverage, may be empty).\n'
    )
    user_msg = (
        "Topics (every industry_id you may assign new tags to):\n"
        f"{topics_blob}\n\n"
        "Current tags (compact JSON; i=industry_id, id=tag_id, t=tag text):\n"
        f"{tags_json}\n\n"
        f"Corpus metadata: {json.dumps(corpus_meta, ensure_ascii=False)}\n\n"
        "Meeting group corpus (title = meeting_group_name, body = description):\n"
        f"{corpus}\n"
    )
    print("[admin_keywords_suggest] Step 3: calling Gemini (this may take up to ~3 minutes)…", flush=True)
    ok, result = _gemini_generate_json_object(
        system=system,
        user_msg=user_msg,
        temperature=0.42,
        max_output_tokens=16384,
        timeout_s=180,
        terminal_progress_label="admin_keywords_suggest",
    )
    return ok, result


@bp.route("/api/meeting-group/polish-description", methods=["POST"])
def polish_meeting_group_description():
    """Rewrite meeting-group description using Google Gemini (optional GEMINI_API_KEY)."""
    uid = session.get("user_id")
    if not uid:
        return jsonify(ok=False, error="Please sign in to use this feature."), 401
    if not User.query.get(uid):
        session.pop("user_id", None)
        return jsonify(ok=False, error="Please sign in again."), 401

    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify(
            ok=False,
            error="Add some description text first, then use AI polish.",
        ), 400
    if len(text) > 12000:
        return jsonify(
            ok=False,
            error="That description is too long for this tool (max 12,000 characters).",
        ), 400

    ok, result = _gemini_polish_meeting_description(text)
    if ok:
        return jsonify(ok=True, text=result)

    status = 503 if "not configured" in result.lower() else 502
    return jsonify(ok=False, error=result), status


@bp.route("/api/meeting/polish-description", methods=["POST"])
def polish_meeting_description():
    """Rewrite event/meeting description using Google Gemini (optional GEMINI_API_KEY)."""
    uid = session.get("user_id")
    if not uid:
        return jsonify(ok=False, error="Please sign in to use this feature."), 401
    if not User.query.get(uid):
        session.pop("user_id", None)
        return jsonify(ok=False, error="Please sign in again."), 401

    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify(
            ok=False,
            error="Add some description text first, then use AI polish.",
        ), 400
    if len(text) > 12000:
        return jsonify(
            ok=False,
            error="That description is too long for this tool (max 12,000 characters).",
        ), 400

    ok, result = _gemini_polish_meeting_description(text, "event")
    if ok:
        return jsonify(ok=True, text=result)

    status = 503 if "not configured" in result.lower() else 502
    return jsonify(ok=False, error=result), status


def _normalize_admin_keywords_gemini_shape(data: dict) -> dict:
    """Gemini 2.5 sometimes returns one link row or one add row as the entire JSON object."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    le = out.get("link_existing_tags")
    if isinstance(le, dict) and ("meeting_group_id" in le or "tag_id" in le):
        out["link_existing_tags"] = [le]
    has_schema = any(
        k in out
        for k in (
            "suggested_add",
            "suggested_add_by_topic",
            "suggestions_by_topic",
            "link_existing_tags",
            "suggested_remove",
        )
    )
    if not has_schema and "meeting_group_id" in out and "tag_id" in out:
        current_app.logger.info(
            "admin_keywords_suggest: normalizing root-level link row into link_existing_tags[]"
        )
        return {
            "link_existing_tags": [
                {
                    "meeting_group_id": out.get("meeting_group_id"),
                    "tag_id": out.get("tag_id"),
                    "reason": out.get("reason") if isinstance(out.get("reason"), str) else "",
                }
            ],
            "suggested_add": [],
            "suggested_remove": out.get("suggested_remove")
            if isinstance(out.get("suggested_remove"), list)
            else [],
            "notes": out.get("notes") if isinstance(out.get("notes"), str) else "",
        }
    if (
        "suggested_add" not in out
        and "suggested_add_by_topic" not in out
        and "tag" in out
        and "industry_id" in out
        and "tag_id" not in out
    ):
        current_app.logger.info(
            "admin_keywords_suggest: normalizing root-level add row into suggested_add[]"
        )
        return {
            "suggested_add": [
                {
                    "tag": out.get("tag"),
                    "industry_id": out.get("industry_id"),
                    "reason": out.get("reason") if isinstance(out.get("reason"), str) else "",
                    "example_group_ids": out.get("example_group_ids")
                    if isinstance(out.get("example_group_ids"), list)
                    else [],
                }
            ],
            "link_existing_tags": out.get("link_existing_tags")
            if isinstance(out.get("link_existing_tags"), list)
            else [],
            "suggested_remove": out.get("suggested_remove")
            if isinstance(out.get("suggested_remove"), list)
            else [],
            "notes": out.get("notes") if isinstance(out.get("notes"), str) else "",
        }
    return out


@bp.route("/admin/keywords/suggest", methods=["POST"])
def admin_keywords_suggest():
    """AI suggestions for new tags and tags to remove, from all meeting group copy (site admins).

    Uses all event groups in the DB for the corpus (admin Keywords topic checkboxes only filter
    the on-page keyword list, not this endpoint).
    """
    uid = session.get("user_id")
    if not uid:
        return jsonify(ok=False, error="Please sign in."), 401
    if not User.query.get(uid):
        session.pop("user_id", None)
        return jsonify(ok=False, error="Please sign in again."), 401
    if not _session_site_admin_user():
        return jsonify(ok=False, error="Only site administrators can use this."), 403

    log = current_app.logger
    t_req = time_mod.perf_counter()
    print(
        f"[admin_keywords_suggest] Step 1: start user_id={uid} — building corpus from all event groups…",
        flush=True,
    )
    log.info(
        "admin_keywords_suggest: start user_id=%s (building corpus and tag list)",
        uid,
    )

    corpus, meta = _admin_event_group_keyword_corpus()
    existing = _admin_existing_tags_for_keyword_ai()
    print(
        "[admin_keywords_suggest] Step 1 done: "
        f"groups_included={meta.get('groups_included')} groups_total={meta.get('groups_total')} "
        f"corpus_chars={len(corpus)} existing_tag_rows={len(existing)} truncated={meta.get('truncated')}",
        flush=True,
    )
    log.info(
        "admin_keywords_suggest: corpus ready groups_included=%s groups_total=%s "
        "corpus_chars=%s existing_tag_rows=%s truncated=%s",
        meta.get("groups_included"),
        meta.get("groups_total"),
        len(corpus),
        len(existing),
        meta.get("truncated"),
    )

    if meta["groups_total"] == 0:
        print("[admin_keywords_suggest] No event groups in DB — skipping AI.", flush=True)
        log.info("admin_keywords_suggest: no meeting groups in database; skipping AI.")
        return jsonify(
            ok=True,
            meta=meta,
            suggested_add=[],
            suggested_remove=[],
            notes="There are no event groups to analyse yet.",
        )

    def _dup_add(industry_id: int, tag_lc: str) -> bool:
        """True if this tag string already exists for the same industry (case-insensitive)."""
        for e in existing:
            if (e.get("tag") or "").strip().lower() != tag_lc:
                continue
            try:
                if int(e.get("industry_id")) == int(industry_id):
                    return True
            except (TypeError, ValueError):
                continue
        return False

    topic_lc_to_id = _admin_topic_name_lower_to_industry_id()

    log.info(
        "admin_keywords_suggest: calling Gemini (model=%s) …",
        (os.environ.get("GEMINI_MODEL") or "gemini-2.0-flash").strip(),
    )
    t_ai = time_mod.perf_counter()
    ok, result = _gemini_suggest_admin_keywords(corpus, meta, existing)
    ai_secs = time_mod.perf_counter() - t_ai
    if not ok:
        msg = result if isinstance(result, str) else "The AI request failed."
        print(
            f"[admin_keywords_suggest] Gemini failed after {ai_secs:.1f}s — {msg[:500]!r}",
            flush=True,
        )
        log.warning(
            "admin_keywords_suggest: Gemini failed after %.1fs — %s",
            ai_secs,
            msg[:500],
        )
        status = 503 if "not configured" in msg.lower() else 502
        return jsonify(ok=False, error=msg), status

    print(
        f"[admin_keywords_suggest] Step 3 done in {ai_secs:.1f}s; response shape: "
        f"{_admin_gemini_keyword_response_shape_summary(result)}",
        flush=True,
    )
    log.info(
        "admin_keywords_suggest: Gemini returned JSON in %.1fs — shape=%s",
        ai_secs,
        _admin_gemini_keyword_response_shape_summary(result),
    )

    assert isinstance(result, dict)
    result = _normalize_admin_keywords_gemini_shape(result)
    flat_add = _flatten_gemini_suggested_add(result)
    raw_rem_raw = result.get("suggested_remove") or result.get("suggest_remove") or []
    if raw_rem_raw and not isinstance(raw_rem_raw, list):
        log.warning(
            "admin_keywords_suggest: suggested_remove is not a list (type=%s); treating as empty",
            type(raw_rem_raw).__name__,
        )
        raw_rem: list = []
    else:
        raw_rem = raw_rem_raw if isinstance(raw_rem_raw, list) else []
    notes = (result.get("notes") or "").strip()

    raw_add_list = result.get("suggested_add")
    raw_add_n = len(raw_add_list) if isinstance(raw_add_list, list) else 0
    topic_blocks_n = 0
    for _topic_key in (
        "suggested_add_by_topic",
        "suggestions_by_topic",
        "by_topic",
        "topics",
    ):
        _blocks = result.get(_topic_key)
        if isinstance(_blocks, list):
            topic_blocks_n += len(_blocks)
    log.info(
        "admin_keywords_suggest: post-flatten raw_suggested_add_len=%s topic_block_rows=%s "
        "flat_add_len=%s raw_remove_len=%s",
        raw_add_n,
        topic_blocks_n,
        len(flat_add),
        len(raw_rem),
    )
    if flat_add:
        log.info(
            "admin_keywords_suggest: flat_add[0] preview=%s",
            repr(flat_add[0])[:600],
        )

    tag_by_id = {int(t["tag_id"]): t for t in existing if t.get("tag_id") is not None}

    suggested_group_tag_links, link_stats = _admin_validate_gemini_group_tag_links(
        result,
        included_group_ids=list(meta.get("included_group_ids") or []),
        existing_by_tag_id=tag_by_id,
    )
    print(
        "[admin_keywords_suggest] link_existing_tags: "
        f"raw={link_stats.get('raw_rows', 0)} kept={len(suggested_group_tag_links)} "
        f"skip_bad={link_stats.get('skipped_bad_row', 0)} skip_wrong_topic={link_stats.get('skipped_wrong_topic', 0)} "
        f"skip_already={link_stats.get('skipped_already_linked', 0)} skip_not_in_corpus={link_stats.get('skipped_not_in_corpus', 0)}",
        flush=True,
    )
    log.info(
        "admin_keywords_suggest: link_existing_tags raw=%s kept=%s stats=%s",
        link_stats.get("raw_rows"),
        len(suggested_group_tag_links),
        link_stats,
    )

    suggested_add: list[dict] = []
    seen_keys: set[tuple[int, str]] = set()
    skipped_no_topic = 0
    skipped_dup = 0
    skipped_add_bad = 0
    for item in flat_add[:120]:
        if not isinstance(item, dict):
            skipped_add_bad += 1
            continue
        tag = (item.get("tag") or "").strip()[:50]
        if not tag:
            continue
        i_raw = item.get("industry_id")
        nid: int | None
        if i_raw is None or i_raw == "":
            nid = None
        else:
            try:
                nid = int(i_raw)
            except (TypeError, ValueError):
                nid = None
        if nid is None:
            tnm = (item.get("topic") or "").strip().lower()
            if tnm in topic_lc_to_id:
                nid = topic_lc_to_id[tnm]
        if nid is None:
            skipped_no_topic += 1
            continue
        if Industry.query.get(nid) is None:
            skipped_no_topic += 1
            continue
        tlc = tag.lower()
        if _dup_add(nid, tlc):
            skipped_dup += 1
            continue
        dedupe_key = (nid, tlc)
        if dedupe_key in seen_keys:
            skipped_dup += 1
            continue
        seen_keys.add(dedupe_key)
        eg = item.get("example_group_ids") or item.get("example_group_id") or []
        if isinstance(eg, int):
            eg = [eg]
        ex_ids: list[int] = []
        if isinstance(eg, list):
            for x in eg[:12]:
                try:
                    ex_ids.append(int(x))
                except (TypeError, ValueError):
                    continue
        topic_name = ""
        ind = Industry.query.get(nid)
        if ind:
            topic_name = (ind.industry or "").strip()
        suggested_add.append(
            {
                "tag": tag,
                "industry_id": nid,
                "topic": topic_name,
                "reason": (item.get("reason") or "").strip()[:500],
                "example_group_ids": ex_ids,
            }
        )

    if not suggested_add and flat_add:
        extra = []
        if skipped_no_topic:
            extra.append(
                f"{skipped_no_topic} suggestion(s) had no valid topic (industry_id); assign a topic id from the list."
            )
        if skipped_dup:
            extra.append(
                f"{skipped_dup} suggestion(s) matched existing tags or duplicates and were omitted."
            )
        notes = (notes + " — " if notes else "") + " ".join(extra)

    suggested_remove: list[dict] = []
    skipped_remove_bad = 0
    skipped_remove_missing = 0
    for item in raw_rem[:40]:
        if not isinstance(item, dict):
            skipped_remove_bad += 1
            continue
        tid_raw = item.get("tag_id")
        try:
            tid = int(tid_raw)
        except (TypeError, ValueError):
            skipped_remove_bad += 1
            continue
        base = tag_by_id.get(tid)
        if not base:
            skipped_remove_missing += 1
            continue
        suggested_remove.append(
            {
                "tag_id": tid,
                "industry_id": base.get("industry_id"),
                "topic": (base.get("topic") or "").strip(),
                "tag": (base.get("tag") or "").strip(),
                "reason": (item.get("reason") or "").strip()[:500],
            }
        )

    if not suggested_add and not suggested_remove and not suggested_group_tag_links:
        empty_lines: list[str] = []
        if len(flat_add) == 0 and link_stats.get("raw_rows", 0) == 0:
            empty_lines.append(
                "No add rows or link_existing_tags rows reached validation: the model JSON had no "
                "flattenable suggested_add or per-topic blocks, and no link_existing_tags array "
                f"(raw suggested_add length {raw_add_n}, topic-style block rows {topic_blocks_n}). "
                'Check the server log "response_shape" for top-level keys.'
            )
        elif len(flat_add) == 0:
            empty_lines.append(
                "No new keyword rows: check suggested_add shape in logs. "
                f"link_existing_tags from model had {link_stats.get('raw_rows', 0)} row(s) but none passed validation."
            )
        elif link_stats.get("raw_rows", 0) == 0:
            empty_lines.append(
                "No link_existing_tags from the model: it should attach existing tag_id values to "
                "meeting_group_id values from the corpus when titles/descriptions support them."
            )
        if raw_rem and not suggested_remove:
            empty_lines.append(
                f"No removals shown: {skipped_remove_missing} row(s) referenced tag_id not in the "
                f"current tag list, {skipped_remove_bad} row(s) invalid (from {len(raw_rem[:40])} "
                "remove row(s) considered)."
            )
        if empty_lines:
            notes = ((notes + "\n\n").strip() + "\n\n" + "\n".join(empty_lines)).strip()[:2000]
        warn_detail = (
            " | ".join(empty_lines)
            if empty_lines
            else (
                f"flat_add={len(flat_add)} all filtered or none (no_topic={skipped_no_topic}, "
                f"dup={skipped_dup}, add_bad={skipped_add_bad}); "
                f"removes_kept={len(suggested_remove)}; links_kept={len(suggested_group_tag_links)}"
            )
        )
        log.warning("admin_keywords_suggest: empty UI result — %s", warn_detail)

    total_secs = time_mod.perf_counter() - t_req
    print(
        "[admin_keywords_suggest] Step 4: dedupe / validation — "
        f"flat_add={len(flat_add)} → suggested_add={len(suggested_add)}, "
        f"suggested_remove={len(suggested_remove)}, group_tag_links={len(suggested_group_tag_links)} "
        f"(skipped_no_topic={skipped_no_topic} skipped_dup={skipped_dup} "
        f"skipped_add_bad={skipped_add_bad})",
        flush=True,
    )
    log.info(
        "admin_keywords_suggest: complete in %.1fs (AI %.1fs) — flat_add=%s suggested_add=%s "
        "suggested_remove=%s group_links=%s skipped_no_topic=%s skipped_dup=%s skipped_add_bad=%s "
        "remove_missing_id=%s remove_bad_row=%s",
        total_secs,
        ai_secs,
        len(flat_add),
        len(suggested_add),
        len(suggested_remove),
        len(suggested_group_tag_links),
        skipped_no_topic,
        skipped_dup,
        skipped_add_bad,
        skipped_remove_missing,
        skipped_remove_bad,
    )
    print(
        f"[admin_keywords_suggest] Done in {total_secs:.1f}s (AI {ai_secs:.1f}s). "
        f"Returning {len(suggested_add)} add(s), {len(suggested_remove)} remove(s), "
        f"{len(suggested_group_tag_links)} group link(s).",
        flush=True,
    )

    return jsonify(
        ok=True,
        meta=meta,
        suggested_add=suggested_add,
        suggested_remove=suggested_remove,
        suggested_group_tag_links=suggested_group_tag_links,
        notes=notes[:2000],
    )


@bp.route("/admin/keywords/apply-suggestions", methods=["POST"])
@site_admin_required
def admin_keywords_apply_suggestions():
    """Apply selected AI rows: new tags, attach existing tags to event groups, then tag deletes."""
    payload = request.get_json(silent=True) or {}
    adds_raw = payload.get("adds") or []
    removes_raw = payload.get("removes") or []
    group_links_raw = payload.get("group_links") or []
    if not isinstance(adds_raw, list):
        adds_raw = []
    if not isinstance(removes_raw, list):
        removes_raw = []
    if not isinstance(group_links_raw, list):
        group_links_raw = []

    remove_messages: list[str] = []
    add_messages: list[str] = []
    link_messages: list[str] = []
    removed_n = 0
    added_n = 0
    linked_n = 0

    # 1) Create new keywords first (so nothing depends on delete order).
    seen_add: set[tuple[int, str]] = set()
    for item in adds_raw[:200]:
        if not isinstance(item, dict):
            continue
        try:
            iid = int(item.get("industry_id"))
        except (TypeError, ValueError):
            add_messages.append("Skipped an add: invalid industry_id.")
            continue
        tag = (item.get("tag") or "").strip()
        if not tag:
            add_messages.append("Skipped an add: empty tag name.")
            continue
        if len(tag) > 50:
            add_messages.append(f"Skipped an add: '{tag[:20]}…' is too long.")
            continue
        dedupe = (iid, tag.lower())
        if dedupe in seen_add:
            continue
        seen_add.add(dedupe)

        if Industry.query.get(iid) is None:
            add_messages.append(f"Skipped add '{tag}': topic (industry) {iid} not found.")
            continue
        existing = Tag.query.filter(
            Tag.industry_id == iid,
            db.func.lower(Tag.tag) == tag.lower(),
        ).first()
        if existing:
            add_messages.append(f"Skipped add '{tag}': already exists for that topic.")
            continue
        db.session.add(Tag(tag=tag, industry_id=iid))
        added_n += 1

    # 2) Attach existing tags to event groups (topic must match tag.industry_id).
    merged_links: dict[int, list[int]] = {}
    for item in group_links_raw[:400]:
        if not isinstance(item, dict):
            continue
        try:
            mid = int(item.get("meeting_group_id"))
            tid = int(item.get("tag_id"))
        except (TypeError, ValueError):
            link_messages.append("Skipped a link: invalid meeting_group_id or tag_id.")
            continue
        bucket = merged_links.setdefault(mid, [])
        if tid not in bucket:
            bucket.append(tid)

    for mid, add_ids in merged_links.items():
        if not add_ids:
            continue
        mg = MeetingGroup.query.options(selectinload(MeetingGroup.tags)).get(mid)
        if not mg:
            link_messages.append(f"Group {mid}: not found.")
            continue
        if not mg.industry_id:
            link_messages.append(f"Group {mid}: has no topic; cannot attach tags.")
            continue
        iid = int(mg.industry_id)
        valid_tags = (
            Tag.query.filter(
                Tag.tag_id.in_(add_ids),
                Tag.industry_id == iid,
            )
            .all()
        )
        valid_by_id = {int(t.tag_id): t for t in valid_tags}
        current = {int(t.tag_id) for t in (mg.tags or [])}
        merged = list(mg.tags or [])
        for tid in add_ids:
            if tid in current:
                continue
            t_obj = valid_by_id.get(tid)
            if not t_obj:
                link_messages.append(f"Group {mid}: tag_id {tid} is not valid for this topic.")
                continue
            merged.append(t_obj)
            current.add(tid)
            linked_n += 1
        mg.tags = merged

    # 3) Remove unused keywords last.
    seen_remove: set[int] = set()
    for item in removes_raw[:200]:
        if not isinstance(item, dict):
            continue
        try:
            tid = int(item.get("tag_id"))
        except (TypeError, ValueError):
            remove_messages.append("Skipped a remove: invalid tag_id.")
            continue
        if tid in seen_remove:
            continue
        seen_remove.add(tid)

        keyword = Tag.query.get(tid)
        if not keyword:
            remove_messages.append(f"Skipped remove (tag_id {tid}): not found.")
            continue
        counts = _keyword_reference_counts(tid)
        if any(counts.values()):
            refs = _format_reference_counts(counts)
            remove_messages.append(
                f"Cannot remove '{keyword.tag}' (id {tid}): still used by {refs}."
            )
            continue
        db.session.delete(keyword)
        removed_n += 1

    if removed_n == 0 and added_n == 0 and linked_n == 0:
        notes = " ".join(add_messages + link_messages + remove_messages).strip()
        if not notes:
            return jsonify(
                ok=False,
                error="Nothing to apply. Select at least one add, group link, or remove, then try again.",
            ), 400
        return jsonify(ok=True, added=0, removed=0, linked=0, notes=notes[:2000])

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("admin_keywords_apply_suggestions")
        return jsonify(
            ok=False,
            error="Could not save changes. The database was rolled back.",
        ), 500

    notes = " ".join(add_messages + link_messages + remove_messages).strip()[:2000] or None
    return jsonify(
        ok=True,
        added=added_n,
        removed=removed_n,
        linked=linked_n,
        notes=notes,
    )


_ADMIN_MG_KEYWORD_SUGGEST_MAX_BATCH = 5


def _admin_meeting_group_description_plain(mg: MeetingGroup) -> str:
    raw = _fix_utf8_mojibake_from_cp1252(mg.description or "")
    return _rich_text_plain_text(raw)[:12000]


def _gemini_suggest_meeting_group_existing_tag_ids(
    *,
    meeting_group_name: str,
    description_plain: str,
    topic_name: str,
    industry_id: int,
    candidates: list[tuple[int, str]],
    current_tag_ids: list[int],
) -> tuple[bool, dict | str]:
    """Ask Gemini for tag_id values; every id must appear in ``candidates``."""
    cand_payload = [{"id": int(tid), "tag": txt[:50]} for tid, txt in candidates]
    system = (
        "You help assign existing website keywords (tags) to one business networking event group. "
        "Each candidate has integer id and tag text. You MUST only include ids from candidate_tags. "
        "Never invent ids. Prefer tags clearly supported by the title and description. "
        "Return strict JSON with keys suggested_tag_ids (array of integers, max 15, no duplicates) "
        'and optional notes (short string). Example: {"suggested_tag_ids":[1,2],"notes":""}. '
        "If nothing fits, return an empty suggested_tag_ids array."
    )
    user_obj = {
        "meeting_group_name": meeting_group_name[:200],
        "description_plain": description_plain[:12000],
        "topic_name": topic_name[:80],
        "industry_id": industry_id,
        "current_tag_ids": current_tag_ids,
        "candidate_tags": cand_payload,
    }
    user_msg = json.dumps(user_obj, ensure_ascii=False)
    return _gemini_generate_json_object(
        system=system,
        user_msg=user_msg,
        temperature=0.2,
        max_output_tokens=2048,
        timeout_s=90,
    )


def _admin_suggest_keywords_one_meeting_group(
    mg: MeetingGroup,
    *,
    description_override_raw: str | None = None,
) -> dict:
    """Suggest existing tags from group copy (Gemini).

    ``description_override_raw`` may contain HTML-rich text from the inline editor so
    organiser suggestions reflect unsaved drafts; omitted or empty falls back to the
    saved database description."""
    gid = int(mg.meeting_group_id)
    name = (mg.meeting_group_name or "").strip()[:200]
    cur_ids = sorted(
        {int(t.tag_id) for t in (mg.tags or []) if t is not None and t.tag_id is not None}
    )
    cur_set = set(cur_ids)
    out: dict = {
        "meeting_group_id": gid,
        "meeting_group_name": name,
        "industry_id": mg.industry_id,
        "topic": "",
        "current_tag_ids": cur_ids,
        "suggested_add": [],
        "notes": "",
        "error": None,
    }
    if not mg.industry_id:
        out["error"] = (
            "This event group has no topic set. Set a topic on the platform first, "
            "then keyword suggestions can use that topic's tag list."
        )
        return out
    ind = mg.industry
    topic = (ind.industry or "").strip() if ind else ""
    out["topic"] = topic
    if description_override_raw is None:
        desc = _admin_meeting_group_description_plain(mg)
        empty_detail = (
            "This event group has no saved description, or it is empty after stripping formatting. "
            "Add description text on the platform first. AI only suggests keywords for groups that have a description."
        )
    else:
        desc = _rich_text_plain_text(
            _fix_utf8_mojibake_from_cp1252(description_override_raw)
        )[:12000]
        empty_detail = (
            "The description looks empty after stripping formatting. Add some description text "
            "above first, then try suggesting keywords."
        )
    if not desc.strip():
        out["error"] = empty_detail
        current_app.logger.info(
            "admin_mg_suggest_keywords: group_id=%s skipped (no plain-text description, override=%s)",
            gid,
            description_override_raw is not None,
        )
        return out

    cand_rows = (
        Tag.query.filter(Tag.industry_id == mg.industry_id)
        .order_by(Tag.tag.asc())
        .limit(400)
        .all()
    )
    candidates: list[tuple[int, str]] = []
    for t in cand_rows:
        tid = int(t.tag_id)
        if tid not in cur_set:
            tx = (t.tag or "").strip()[:50]
            if tx:
                candidates.append((tid, tx))
    if not candidates:
        out["notes"] = (
            "No unused keywords remain for this topic (every tag is already on the group, "
            "or no tags exist for the topic)."
        )
        return out
    use_cand = candidates[:280]
    allowed = {c[0] for c in use_cand}
    ok, payload = _gemini_suggest_meeting_group_existing_tag_ids(
        meeting_group_name=name,
        description_plain=desc,
        topic_name=topic,
        industry_id=int(mg.industry_id),
        candidates=use_cand,
        current_tag_ids=cur_ids,
    )
    if not ok:
        out["error"] = payload if isinstance(payload, str) else "The AI request failed."
        return out
    assert isinstance(payload, dict)
    raw_ids = payload.get("suggested_tag_ids")
    if raw_ids is None:
        raw_ids = payload.get("tag_ids") or []
    if not isinstance(raw_ids, list):
        raw_ids = []
    picked: list[int] = []
    for x in raw_ids:
        try:
            tid = int(x)
        except (TypeError, ValueError):
            continue
        if tid not in allowed or tid in cur_set or tid in picked:
            continue
        picked.append(tid)
        if len(picked) >= 15:
            break
    suggested: list[dict] = []
    for tid in picked:
        tag_row = Tag.query.get(tid)
        if not tag_row or int(tag_row.industry_id) != int(mg.industry_id):
            continue
        suggested.append(
            {
                "tag_id": int(tag_row.tag_id),
                "tag": (tag_row.tag or "").strip()[:50],
                "reason": "",
            }
        )
    reason_list = payload.get("reasons")
    if isinstance(reason_list, list):
        for item in reason_list:
            if not isinstance(item, dict):
                continue
            try:
                rtid = int(item.get("tag_id"))
            except (TypeError, ValueError):
                continue
            rs = (item.get("reason") or "").strip()[:400]
            for s in suggested:
                if s["tag_id"] == rtid and rs:
                    s["reason"] = rs
                    break
    notes = (payload.get("notes") or "").strip()
    if notes:
        out["notes"] = notes[:800]
    out["suggested_add"] = suggested
    current_app.logger.info(
        "admin_mg_suggest_keywords: group_id=%s industry_id=%s candidates=%s picked=%s desc_chars=%s",
        gid,
        mg.industry_id,
        len(candidates),
        len(suggested),
        len(desc),
    )
    return out


@bp.route("/admin/meeting-groups/suggest-keywords", methods=["POST"])
def admin_meeting_groups_suggest_keywords():
    """AI: suggest existing tags from each group's description (site admins; skips empty descriptions)."""
    uid = session.get("user_id")
    if not uid:
        return jsonify(ok=False, error="Please sign in."), 401
    if not User.query.get(uid):
        session.pop("user_id", None)
        return jsonify(ok=False, error="Please sign in again."), 401
    if not _session_site_admin_user():
        return jsonify(ok=False, error="Only site administrators can use this."), 403
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("meeting_group_ids")
    if raw_ids is None and payload.get("meeting_group_id") is not None:
        raw_ids = [payload.get("meeting_group_id")]
    if not isinstance(raw_ids, list):
        raw_ids = []
    ids: list[int] = []
    seen: set[int] = set()
    for x in raw_ids[:32]:
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v <= 0 or v in seen:
            continue
        seen.add(v)
        ids.append(v)
    if not ids:
        return jsonify(ok=False, error="No event group ids were sent."), 400

    truncated = False
    max_batch = _ADMIN_MG_KEYWORD_SUGGEST_MAX_BATCH
    if len(ids) > max_batch:
        ids = ids[:max_batch]
        truncated = True

    rows = (
        MeetingGroup.query.options(
            selectinload(MeetingGroup.tags),
            selectinload(MeetingGroup.industry),
        )
        .filter(MeetingGroup.meeting_group_id.in_(ids))
        .all()
    )
    by_id = {int(m.meeting_group_id): m for m in rows}
    groups_out: list[dict] = []
    for gid in ids:
        mg = by_id.get(gid)
        if not mg:
            groups_out.append(
                {
                    "meeting_group_id": gid,
                    "meeting_group_name": "",
                    "industry_id": None,
                    "topic": "",
                    "current_tag_ids": [],
                    "suggested_add": [],
                    "notes": "",
                    "error": "That event group was not found.",
                }
            )
            continue
        groups_out.append(_admin_suggest_keywords_one_meeting_group(mg))

    return jsonify(
        ok=True,
        meta={"truncated": truncated, "max_batch": max_batch},
        groups=groups_out,
    )


@bp.route("/api/meeting-group/suggest-keywords", methods=["POST"])
def api_meeting_group_suggest_keywords():
    """Gemini keyword suggestions from group description for the group owner."""
    uid = session.get("user_id")
    if not uid:
        return jsonify(ok=False, error="Please sign in to use this feature."), 401
    if not User.query.get(uid):
        session.pop("user_id", None)
        return jsonify(ok=False, error="Please sign in again."), 401

    payload = request.get_json(silent=True) or {}
    raw_mid = payload.get("meeting_group_id")
    try:
        mid = int(raw_mid)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Invalid event group."), 400

    ov = payload.get("description")
    description_override_raw: str | None = ov if isinstance(ov, str) else None

    mg = MeetingGroup.query.options(
        selectinload(MeetingGroup.tags),
        selectinload(MeetingGroup.industry),
    ).get(mid)
    if not mg or int(mg.user_id) != int(uid):
        return jsonify(ok=False, error="Event group not found."), 404

    block = _admin_suggest_keywords_one_meeting_group(
        mg,
        description_override_raw=description_override_raw,
    )
    err = block.get("error")
    if err:
        code = 503 if "not configured" in str(err).lower() else 400
        return jsonify(ok=False, error=err), code

    return jsonify(
        ok=True,
        meeting_group_id=block["meeting_group_id"],
        suggested_add=block.get("suggested_add") or [],
        notes=(block.get("notes") or "")[:800],
    )


@bp.route("/admin/meeting-groups/apply-tag-suggestions", methods=["POST"])
def admin_meeting_groups_apply_tag_suggestions():
    """Attach selected existing tags to event groups (JSON; site admins)."""
    uid = session.get("user_id")
    if not uid:
        return jsonify(ok=False, error="Please sign in."), 401
    if not User.query.get(uid):
        session.pop("user_id", None)
        return jsonify(ok=False, error="Please sign in again."), 401
    if not _session_site_admin_user():
        return jsonify(ok=False, error="Only site administrators can use this."), 403
    payload = request.get_json(silent=True) or {}
    updates = payload.get("updates")
    if not isinstance(updates, list):
        updates = []
    if not updates:
        return jsonify(ok=False, error="Nothing to apply."), 400

    merged_adds: dict[int, list[int]] = {}
    for block in updates[:40]:
        if not isinstance(block, dict):
            continue
        try:
            mid = int(block.get("meeting_group_id"))
        except (TypeError, ValueError):
            continue
        add_raw = block.get("add_tag_ids") or []
        if not isinstance(add_raw, list):
            add_raw = []
        bucket = merged_adds.setdefault(mid, [])
        for x in add_raw[:80]:
            try:
                tid = int(x)
            except (TypeError, ValueError):
                continue
            if tid not in bucket:
                bucket.append(tid)
    if not merged_adds:
        return jsonify(ok=False, error="Nothing to apply."), 400

    applied = 0
    skipped: list[str] = []
    for mid, add_ids in merged_adds.items():
        if not add_ids:
            continue
        mg = MeetingGroup.query.options(selectinload(MeetingGroup.tags)).get(mid)
        if not mg:
            skipped.append(f"Group {mid}: not found.")
            continue
        if not mg.industry_id:
            skipped.append(f"Group {mid}: has no topic; cannot attach tags.")
            continue
        iid = int(mg.industry_id)
        valid_tags = (
            Tag.query.filter(
                Tag.tag_id.in_(add_ids),
                Tag.industry_id == iid,
            )
            .all()
        )
        valid_by_id = {int(t.tag_id): t for t in valid_tags}
        current = {int(t.tag_id) for t in (mg.tags or [])}
        merged = list(mg.tags or [])
        for tid in add_ids:
            if tid in current:
                continue
            t_obj = valid_by_id.get(tid)
            if not t_obj:
                skipped.append(f"Group {mid}: tag_id {tid} is not valid for this topic.")
                continue
            merged.append(t_obj)
            current.add(tid)
            applied += 1
        mg.tags = merged

    if applied == 0:
        msg = " ".join(skipped).strip() if skipped else "No tags were applied."
        return jsonify(ok=False, error=msg[:2000]), 400

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("admin_meeting_groups_apply_tag_suggestions")
        return jsonify(
            ok=False,
            error="Could not save changes. The database was rolled back.",
        ), 500

    notes = " ".join(skipped).strip()[:2000] if skipped else None
    return jsonify(ok=True, applied=applied, notes=notes)


_ADMIN_FUNCTION_PANELS = frozenset(
    {"overview", "keywords", "users", "move_events", "delete_group_events"}
)
_ADMIN_PANEL_ENDPOINTS: dict[str, str] = {
    "overview": "admin_events",
    "keywords": "admin_keywords",
    "users": "admin_users",
    "move_events": "admin_move_events",
    "delete_group_events": "admin_delete_group_events",
}
_ADMIN_PANEL_TEMPLATES: dict[str, str] = {
    "overview": "admin/overview.html",
    "keywords": "admin/keywords.html",
    "users": "admin/users.html",
    "move_events": "admin/move_events.html",
    "delete_group_events": "admin/delete_group_events.html",
}
_ADMIN_FUNCTION_ENDPOINTS = frozenset(
    f"main.{ep}" for ep in _ADMIN_PANEL_ENDPOINTS.values()
)


def _admin_redirect_for_panel(panel: str, **kwargs):
    """Redirect legacy ``?panel=`` URLs to dedicated admin pages."""
    panel = (panel or "overview").strip().lower()
    if panel == "transfer":
        panel = "overview"
    if panel == "more":
        panel = "keywords"
    if panel not in _ADMIN_FUNCTION_PANELS:
        panel = "overview"
    clean = {k: v for k, v in kwargs.items() if k != "panel" and v is not None}
    endpoint = _ADMIN_PANEL_ENDPOINTS[panel]
    return redirect(url_for(f"main.{endpoint}", **clean))


def _admin_group_meeting_stats_by_ids(gids: list[int]) -> dict[int, dict[str, int]]:
    """Per–event-group counts (total / live / draft meetings) for admin UI; batched for very large IN lists."""
    out: dict[int, dict[str, int]] = {gid: {"total": 0, "live": 0, "draft": 0} for gid in gids}
    if not gids:
        return out
    batch_size = 1800
    for i in range(0, len(gids), batch_size):
        batch = gids[i : i + batch_size]
        rows = (
            db.session.query(
                Meeting.meeting_group_id,
                func.count(Meeting.meeting_id).label("total_n"),
                func.sum(case((Meeting.status == "Live", 1), else_=0)).label("live_n"),
                func.sum(case((Meeting.status == "Draft", 1), else_=0)).label("draft_n"),
            )
            .filter(Meeting.meeting_group_id.in_(batch))
            .group_by(Meeting.meeting_group_id)
            .all()
        )
        for row in rows:
            gid = row.meeting_group_id
            out[gid] = {
                "total": int(row.total_n or 0),
                "live": int(row.live_n or 0),
                "draft": int(row.draft_n or 0),
            }
    return out


def _admin_parse_optional_int(val: str | None) -> int | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        n = int(s)
    except ValueError:
        return None
    return n if n >= 0 else None


class _AdminEventsPagination:
    """Pager metadata for admin events table (no materialized item list)."""

    def __init__(self, total: int, page: int, per_page: int):
        self.total = int(total or 0)
        self.page = page
        self.per_page = per_page
        self.pages = max(1, math.ceil(self.total / per_page)) if self.total else 1
        self.has_prev = page > 1
        self.has_next = page < self.pages
        self.prev_num = page - 1
        self.next_num = page + 1

    def iter_pages(
        self, left_edge=1, left_current=2, right_current=2, right_edge=1
    ):
        last = 0
        for num in range(1, self.pages + 1):
            if (
                num <= left_edge
                or (self.page - left_current - 1 < num < self.page + right_current)
                or num > self.pages - right_edge
            ):
                if last + 1 != num:
                    yield None
                yield num
                last = num


def _admin_bulk_meeting_group_ids_from_form() -> list[int]:
    out: list[int] = []
    for raw in request.form.getlist("meeting_group_ids"):
        try:
            i = int(str(raw).strip())
            if i > 0:
                out.append(i)
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def _admin_bulk_meeting_group_ids_from_json(payload: dict | None) -> list[int]:
    out: list[int] = []
    if not payload:
        return out
    raw_ids = payload.get("meeting_group_ids")
    if not isinstance(raw_ids, list):
        return out
    for raw in raw_ids:
        try:
            i = int(str(raw).strip())
            if i > 0:
                out.append(i)
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def _admin_bulk_id_batches(ids: list[int], size: int = 1500):
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


def _admin_should_hide_test_users() -> bool:
    """Default True: exclude seeded test accounts (username ``tnw_tu*``). ``?ignore_test_users=0`` includes them."""
    v = (request.args.get("ignore_test_users") or "").strip().lower()
    if v in ("0", "false", "no"):
        return False
    return True


def _admin_bulk_redirect_from_form():
    """Rebuild admin events URL from hidden ``ret_*`` POST fields."""
    q: dict = {}
    for key in (
        "panel",
        "group_q",
        "email_q",
        "event_status",
        "sort",
        "dir",
        "ignore_test_users",
    ):
        raw = request.form.get(f"ret_{key}")
        if raw is None:
            continue
        s = str(raw).strip()
        if not s:
            continue
        q[key] = s
    for key in ("events_min", "events_max", "admin_ov_page"):
        raw = request.form.get(f"ret_{key}")
        if raw is None or str(raw).strip() == "":
            continue
        try:
            q[key] = int(str(raw).strip())
        except ValueError:
            continue
    panel = (q.pop("panel", None) or "overview").strip().lower()
    return _admin_redirect_for_panel(panel, **q)


def _admin_users_redirect_from_form():
    """Rebuild admin users page URL from hidden ``ret_*`` POST fields."""
    q: dict = {}
    for key in (
        "user_q",
        "user_email_q",
        "user_verified",
        "user_admin",
        "usort",
        "udir",
        "ignore_test_users",
    ):
        raw = request.form.get(f"ret_{key}")
        if raw is None:
            continue
        s = str(raw).strip()
        if not s:
            continue
        q[key] = s
    raw_page = request.form.get("ret_users_page")
    if raw_page is not None and str(raw_page).strip() != "":
        try:
            q["users_page"] = int(str(raw_page).strip())
        except ValueError:
            pass
    return redirect(url_for("main.admin_users", **q))


def _admin_users_nav_kwargs_from_request_args() -> dict:
    """Query args that round-trip the users grid (for edit back-links and redirects)."""
    out: dict = {}
    for key in (
        "user_q",
        "user_email_q",
        "user_verified",
        "user_admin",
        "usort",
        "udir",
        "ignore_test_users",
    ):
        raw = request.args.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        out[key] = str(raw).strip()
    up = request.args.get("users_page", type=int)
    if up is not None and up >= 1:
        out["users_page"] = up
    return out


def _admin_user_dependent_counts(user_id: int) -> dict[str, int]:
    """Counts shown in the users grid and delete confirmation."""
    n_groups = (
        MeetingGroup.query.filter(MeetingGroup.user_id == user_id)
        .with_entities(func.count(MeetingGroup.meeting_group_id))
        .scalar()
        or 0
    )
    att_row = (
        db.session.query(
            func.count(MeetingAttendee.meeting_attendee_id),
            func.coalesce(func.sum(MeetingAttendee.quantity), 0),
        )
        .filter(MeetingAttendee.user_id == user_id)
        .one()
    )
    n_attendee_rows = int(att_row[0] or 0)
    n_ticket_qty = int(att_row[1] or 0)
    n_creator_meetings = (
        Meeting.query.filter(Meeting.creator_user_id == user_id)
        .with_entities(func.count(Meeting.meeting_id))
        .scalar()
        or 0
    )
    n_industries = (
        db.session.query(func.count())
        .select_from(user_industries)
        .filter(user_industries.c.user_id == user_id)
        .scalar()
        or 0
    )
    n_attendee_tags = (
        db.session.query(func.count())
        .select_from(user_attendee_tags)
        .filter(user_attendee_tags.c.user_id == user_id)
        .scalar()
        or 0
    )
    return {
        "n_groups": int(n_groups),
        "n_attendee_rows": n_attendee_rows,
        "n_ticket_qty": n_ticket_qty,
        "n_creator_meetings": int(n_creator_meetings),
        "n_industries": int(n_industries),
        "n_attendee_tags": int(n_attendee_tags),
    }


def _admin_delete_user_cascade(user_id: int) -> None:
    """Delete a user after removing or reassigning dependent rows (caller commits)."""
    owned_ids = [
        gid
        for (gid,) in db.session.query(MeetingGroup.meeting_group_id)
        .filter(MeetingGroup.user_id == user_id)
        .all()
    ]
    if owned_ids:
        _admin_cascade_delete_meeting_groups(owned_ids)
    MeetingAttendee.query.filter(MeetingAttendee.user_id == user_id).delete(
        synchronize_session=False
    )
    db.session.execute(
        delete(user_industries).where(user_industries.c.user_id == user_id)
    )
    db.session.execute(
        delete(user_attendee_tags).where(user_attendee_tags.c.user_id == user_id)
    )
    new_owner = (
        select(MeetingGroup.user_id)
        .where(MeetingGroup.meeting_group_id == Meeting.meeting_group_id)
        .scalar_subquery()
    )
    db.session.execute(
        update(Meeting)
        .where(Meeting.creator_user_id == user_id)
        .values(creator_user_id=new_owner)
    )
    u = User.query.get(user_id)
    if u:
        db.session.delete(u)


def _admin_cascade_delete_meeting_groups(meeting_group_ids: list[int]) -> int:
    """FK-safe cascade: ticket entries, attendees, ticket types, bookmarks, meetings, tags, then groups.

    After a successful DB commit, removes each group's banner file from disk under
    ``static/meeting_group_images/`` (same tree Flask serves for ``/static/...``).

    Uses Core ``delete()`` so the ORM does not keep stale ``Meeting`` rows that conflict with
    ``db.session.delete(MeetingGroup)`` (which could otherwise emit failing UPDATEs on meetings).
    """
    if not meeting_group_ids:
        return 0
    meeting_group_ids = sorted({int(g) for g in meeting_group_ids if int(g) > 0})
    if not meeting_group_ids:
        return 0

    meeting_ids: list[int] = []
    for batch in _admin_bulk_id_batches(meeting_group_ids):
        meeting_ids.extend(
            mid
            for (mid,) in db.session.query(Meeting.meeting_id)
            .filter(Meeting.meeting_group_id.in_(batch))
            .all()
        )
    meeting_ids = sorted({int(mid) for mid in meeting_ids if int(mid) > 0})

    ticket_entries_table_present = inspect(db.engine).has_table(
        MeetingTicketEntry.__tablename__
    )

    for batch in _admin_bulk_id_batches(meeting_ids):
        if not batch:
            continue
        attendee_ids = [
            aid
            for (aid,) in db.session.query(MeetingAttendee.meeting_attendee_id)
            .filter(MeetingAttendee.meeting_id.in_(batch))
            .all()
        ]
        for att_batch in _admin_bulk_id_batches(attendee_ids):
            if att_batch and ticket_entries_table_present:
                db.session.execute(
                    delete(MeetingTicketEntry).where(
                        MeetingTicketEntry.meeting_attendee_id.in_(att_batch)
                    )
                )
        db.session.execute(
            delete(MeetingAttendee).where(MeetingAttendee.meeting_id.in_(batch))
        )
        db.session.execute(
            delete(MeetingTicketType).where(MeetingTicketType.meeting_id.in_(batch))
        )
        db.session.execute(
            delete(UserSavedMeeting).where(UserSavedMeeting.meeting_id.in_(batch))
        )
        db.session.execute(delete(Meeting).where(Meeting.meeting_id.in_(batch)))

    for batch in _admin_bulk_id_batches(meeting_group_ids):
        db.session.execute(
            delete(meeting_group_tags).where(
                meeting_group_tags.c.meeting_group_id.in_(batch)
            )
        )

    image_by_gid: dict[int, str] = {}
    for batch in _admin_bulk_id_batches(meeting_group_ids):
        for gid, img in db.session.execute(
            select(MeetingGroup.meeting_group_id, MeetingGroup.image_filename).where(
                MeetingGroup.meeting_group_id.in_(batch)
            )
        ).all():
            image_by_gid[int(gid)] = (img or "").strip()

    deleted = 0
    for batch in _admin_bulk_id_batches(meeting_group_ids):
        res = db.session.execute(
            delete(MeetingGroup).where(MeetingGroup.meeting_group_id.in_(batch))
        )
        deleted += int(res.rowcount or 0)

    db.session.commit()

    removed_paths: set[str] = set()
    for _gid, old_image in image_by_gid.items():
        old_path = _meeting_group_image_fs_path(old_image)
        if not old_path or old_path in removed_paths:
            continue
        if os.path.isfile(old_path):
            try:
                os.remove(old_path)
                removed_paths.add(old_path)
            except OSError:
                pass
    return deleted


def _admin_delete_meetings_by_ids(meeting_ids: list[int]) -> int:
    """Delete events (meetings) and dependent attendee/ticket rows for provided meeting IDs."""
    if not meeting_ids:
        return 0
    meeting_ids = sorted({int(mid) for mid in meeting_ids if int(mid) > 0})
    if not meeting_ids:
        return 0

    attendee_ids: list[int] = []
    for batch in _admin_bulk_id_batches(meeting_ids):
        attendee_ids.extend(
            aid
            for (aid,) in db.session.query(MeetingAttendee.meeting_attendee_id)
            .filter(MeetingAttendee.meeting_id.in_(batch))
            .all()
        )

    ticket_entries_table_present = inspect(db.engine).has_table(
        MeetingTicketEntry.__tablename__
    )
    if ticket_entries_table_present:
        for batch in _admin_bulk_id_batches(attendee_ids):
            if not batch:
                continue
            db.session.execute(
                delete(MeetingTicketEntry).where(
                    MeetingTicketEntry.meeting_attendee_id.in_(batch)
                )
            )

    for batch in _admin_bulk_id_batches(meeting_ids):
        if not batch:
            continue
        db.session.execute(
            delete(MeetingAttendee).where(MeetingAttendee.meeting_id.in_(batch))
        )
        db.session.execute(
            delete(MeetingTicketType).where(MeetingTicketType.meeting_id.in_(batch))
        )
        db.session.execute(
            delete(UserSavedMeeting).where(UserSavedMeeting.meeting_id.in_(batch))
        )
        db.session.execute(delete(Meeting).where(Meeting.meeting_id.in_(batch)))

    db.session.commit()
    return len(meeting_ids)


def _admin_delete_events_for_group(meeting_group_id: int) -> int:
    """Delete all events (and ticket/attendee rows) for one event group."""
    if not meeting_group_id:
        return 0
    meeting_ids = [
        mid
        for (mid,) in db.session.query(Meeting.meeting_id)
        .filter(Meeting.meeting_group_id == meeting_group_id)
        .all()
    ]
    return _admin_delete_meetings_by_ids(meeting_ids)


@bp.route("/preferences/bootstrap-theme", methods=["POST"])
def set_site_bootstrap_theme():
    """Persist Bootswatch/Bootstrap choice site-wide (session + cookies)."""
    slug = normalize_bootstrap_theme_slug(request.form.get("theme"))
    return_to = (request.form.get("return_to") or "").strip()
    if not return_to.startswith("/"):
        return_to = url_for("main.home")
    return _persist_bootstrap_theme(slug, redirect(return_to))


@bp.route("/admin/preferences/bootstrap-theme", methods=["POST"])
@site_admin_required
def set_admin_bootstrap_theme():
    """Same site-wide theme store (admin header posts here for compatibility)."""
    slug = normalize_bootstrap_theme_slug(request.form.get("theme"))
    return_to = (request.form.get("return_to") or "").strip()
    if not return_to.startswith("/"):
        return_to = url_for("main.admin_events")
    return _persist_bootstrap_theme(slug, redirect(return_to))


# ---------------------------------------------------------------------------
# Admin preview (reads a file off disk, no DB)
# ---------------------------------------------------------------------------
@bp.route("/admin/_static/<path:filename>")
def admin_maint_static(filename: str):
    """Serve maint-app static files via gunicorn.

    On the VPS, nginx often aliases ``/static/`` to the main site's static tree,
    so maint-only assets (e.g. admin_console.css) 404 there. This path is proxied
    to the maint app instead.
    """
    safe = filename.replace("\\", "/").lstrip("/")
    if not safe or ".." in safe.split("/"):
        abort(404)
    static_root = os.path.normpath(os.path.join(current_app.root_path, "static"))
    full = os.path.normpath(os.path.join(static_root, safe))
    if full != static_root and not full.startswith(static_root + os.sep):
        abort(404)
    return send_from_directory(static_root, safe, conditional=True)


@bp.route("/admin")
@site_admin_required
def admin_preview():
    source_path = r"C:\Users\IISUSER\Desktop\Membership_Improved.txt"
    preview_html = None
    if not os.path.exists(source_path):
        flash("Admin preview source file not found on desktop.", "warning")
    else:
        with open(source_path, "r", encoding="utf-8") as f:
            preview_html = f.read()

        preview_html = preview_html.replace("/my-account/?action=register", url_for("main.register"))
        preview_html = preview_html.replace("/my-account", url_for("main.login"))

    return render_template("admin_preview.html", preview_html=preview_html)


@bp.route("/admin/events")
@site_admin_required
def admin_events():
    """Event groups overview and bulk admin tools."""
    legacy_panel = (request.args.get("panel") or "").strip().lower()
    if legacy_panel and legacy_panel not in ("overview", ""):
        q = request.args.to_dict()
        q.pop("panel", None)
        return _admin_redirect_for_panel(legacy_panel, **q)
    return _admin_functions_render("overview")


@bp.route("/admin/keywords")
@site_admin_required
def admin_keywords():
    return _admin_functions_render("keywords")


@bp.route("/admin/users")
@site_admin_required
def admin_users():
    return _admin_functions_render("users")


@bp.route("/admin/move-events")
@site_admin_required
def admin_move_events():
    return _admin_functions_render("move_events")


@bp.route("/admin/delete-group-events")
@site_admin_required
def admin_delete_group_events():
    return _admin_functions_render("delete_group_events")


def _admin_functions_render(active_panel: str):
    """Shared context for admin console pages (one panel per URL)."""
    imp_id = session.get(SESSION_IMPERSONATOR_ADMIN_ID)
    if imp_id:
        viewed_uid = session.get("user_id")
        viewed = User.query.get(viewed_uid) if viewed_uid else None
        admin_actor = User.query.get(imp_id)
        current_app.logger.info(
            "[admin_functions] opened while impersonating: admin_user_id=%s admin_email=%s "
            "impersonated_user_id=%s impersonated_email=%s",
            imp_id,
            (admin_actor.email if admin_actor else ""),
            viewed_uid,
            (viewed.email if viewed else ""),
        )

    total_meetings = Meeting.query.count()
    total_meeting_groups = MeetingGroup.query.count()
    status_rows = (
        db.session.query(Meeting.status, func.count(Meeting.meeting_id))
        .group_by(Meeting.status)
        .all()
    )
    status_counts: dict[str, int] = {}
    for st, cnt in status_rows:
        key = (st or "Unknown").strip() or "Unknown"
        status_counts[key] = int(cnt or 0)

    other_status_totals = [
        (st, n)
        for st, n in sorted(status_counts.items())
        if st not in ("Draft", "Live")
    ]

    if active_panel not in _ADMIN_FUNCTION_PANELS:
        active_panel = "overview"

    admin_kw_topics = (
        Industry.query.order_by(Industry.industry.asc()).all()
    )
    admin_kw_tags_by_topic: dict[str, list[dict]] = {}
    for row in Tag.query.order_by(Tag.industry_id.asc(), Tag.tag.asc()).all():
        key = str(row.industry_id)
        admin_kw_tags_by_topic.setdefault(key, []).append(
            {"id": row.tag_id, "tag": row.tag}
        )

    admin_test_events_user_rows: list[dict] = []
    admin_test_events_default_user_id: int | None = None

    admin_transfer_users: list = []
    if not session.get(SESSION_IMPERSONATOR_ADMIN_ID):
        admin_transfer_users = User.query.order_by(
            db.func.lower(User.email), User.user_id
        ).all()

    g_stats_sq = (
        db.session.query(
            Meeting.meeting_group_id.label("gs_gid"),
            func.count(Meeting.meeting_id).label("gs_total"),
            func.sum(case((Meeting.status == "Live", 1), else_=0)).label("gs_live"),
            func.sum(case((Meeting.status == "Draft", 1), else_=0)).label("gs_draft"),
        )
        .group_by(Meeting.meeting_group_id)
        .subquery()
    )
    gq = (
        MeetingGroup.query.options(selectinload(MeetingGroup.owner))
        .join(User, MeetingGroup.user_id == User.user_id)
        .outerjoin(g_stats_sq, g_stats_sq.c.gs_gid == MeetingGroup.meeting_group_id)
    )

    group_q = (request.args.get("group_q") or "").strip()
    email_q = (request.args.get("email_q") or "").strip()
    event_status = (request.args.get("event_status") or "").strip()
    events_min = _admin_parse_optional_int(request.args.get("events_min"))
    events_max = _admin_parse_optional_int(request.args.get("events_max"))
    if group_q:
        gq = gq.filter(MeetingGroup.meeting_group_name.contains(group_q))
    if email_q:
        gq = gq.filter(User.email.contains(email_q))
    if event_status == "Live":
        gq = gq.filter(func.coalesce(g_stats_sq.c.gs_live, 0) > 0)
    elif event_status == "Draft":
        gq = gq.filter(func.coalesce(g_stats_sq.c.gs_draft, 0) > 0)
    if events_min is not None:
        gq = gq.filter(func.coalesce(g_stats_sq.c.gs_total, 0) >= events_min)
    if events_max is not None:
        gq = gq.filter(func.coalesce(g_stats_sq.c.gs_total, 0) <= events_max)
    if _admin_should_hide_test_users():
        gq = gq.filter(~User.username.like("tnw_tu%"))

    sort_key = (request.args.get("sort") or "created").strip().lower()
    sort_dir = (request.args.get("dir") or "desc").strip().lower()
    asc = sort_dir == "asc"
    sort_map = {
        "group_id": MeetingGroup.meeting_group_id,
        "group_name": MeetingGroup.meeting_group_name,
        "owner_email": User.email,
        "events": func.coalesce(g_stats_sq.c.gs_total, 0),
        "live": func.coalesce(g_stats_sq.c.gs_live, 0),
        "draft": func.coalesce(g_stats_sq.c.gs_draft, 0),
        "created": MeetingGroup.created_at,
    }
    order_col = sort_map.get(sort_key, MeetingGroup.created_at)
    primary = order_col.asc() if asc else order_col.desc()
    # Avoid redundant ORDER BY on the same column where the backend rejects it (e.g. SQL Server).
    if sort_key == "group_id":
        secondary = MeetingGroup.created_at.asc() if asc else MeetingGroup.created_at.desc()
    else:
        secondary = MeetingGroup.meeting_group_id.asc() if asc else MeetingGroup.meeting_group_id.desc()
    order_parts = (primary, secondary)

    total_filtered = gq.with_entities(func.count(MeetingGroup.meeting_group_id)).scalar() or 0
    per_page = 30
    ov_page = request.args.get("admin_ov_page", type=int) or 1
    if ov_page < 1:
        ov_page = 1
    ov_pages = max(1, math.ceil(total_filtered / per_page)) if total_filtered else 1
    if ov_page > ov_pages:
        ov_page = ov_pages
    offset = (ov_page - 1) * per_page

    gq_data = gq.add_columns(
        func.coalesce(g_stats_sq.c.gs_total, 0).label("ov_evt"),
        func.coalesce(g_stats_sq.c.gs_live, 0).label("ov_live"),
        func.coalesce(g_stats_sq.c.gs_draft, 0).label("ov_draft"),
    )
    raw_rows = gq_data.order_by(*order_parts).offset(offset).limit(per_page).all()
    admin_overview_rows = [
        {
            "group": r[0],
            "total": int(r[1] or 0),
            "live": int(r[2] or 0),
            "draft": int(r[3] or 0),
        }
        for r in raw_rows
    ]

    def _flip_dir(d: str) -> str:
        return "asc" if d == "desc" else "desc"

    admin_sort_next_dir: dict[str, str] = {}
    for col in sort_map:
        if col == sort_key:
            admin_sort_next_dir[col] = _flip_dir(sort_dir if sort_dir in ("asc", "desc") else "desc")
        else:
            admin_sort_next_dir[col] = "desc"

    admin_overview_nav_kwargs: dict = {}
    if group_q:
        admin_overview_nav_kwargs["group_q"] = group_q
    if email_q:
        admin_overview_nav_kwargs["email_q"] = email_q
    if event_status in ("Live", "Draft"):
        admin_overview_nav_kwargs["event_status"] = event_status
    em_raw = (request.args.get("events_min") or "").strip()
    if em_raw:
        admin_overview_nav_kwargs["events_min"] = em_raw
    ex_raw = (request.args.get("events_max") or "").strip()
    if ex_raw:
        admin_overview_nav_kwargs["events_max"] = ex_raw
    admin_overview_nav_kwargs["sort"] = sort_key
    admin_overview_nav_kwargs["dir"] = (
        sort_dir if sort_dir in ("asc", "desc") else "desc"
    )
    if not _admin_should_hide_test_users():
        admin_overview_nav_kwargs["ignore_test_users"] = "0"

    admin_ignore_test_users_nav: dict = {}
    if not _admin_should_hide_test_users():
        admin_ignore_test_users_nav["ignore_test_users"] = "0"

    admin_sort_urls = {
        col: url_for(
            "main.admin_events",
            **{
                **admin_overview_nav_kwargs,
                "admin_ov_page": 1,
                "sort": col,
                "dir": admin_sort_next_dir[col],
            },
        )
        for col in sort_map.keys()
    }

    admin_ov_pagination = _AdminEventsPagination(
        int(total_filtered), ov_page, per_page
    )

    admin_bulk_ret_fields: list[tuple[str, str]] = []
    for k, v in admin_overview_nav_kwargs.items():
        if v is None:
            continue
        vs = str(v).strip()
        if not vs:
            continue
        admin_bulk_ret_fields.append((k, vs))
    admin_bulk_ret_fields.append(("admin_ov_page", str(ov_page)))

    admin_bulk_transfer_notice = session.pop("admin_bulk_transfer_notice", None)
    admin_move_events_notice = session.pop("admin_move_events_notice", None)
    admin_delete_events_notice = session.pop("admin_delete_events_notice", None)

    # --- Users panel (maintain users): filters, sort, paging -----------------------------
    admin_users_rows: list[dict] = []
    admin_users_total = 0
    admin_users_page = 1
    admin_users_pages = 1
    admin_users_per_page = 30
    admin_users_nav_kwargs: dict = {}
    admin_users_nav_kwargs.update(admin_ignore_test_users_nav)
    admin_users_sort_key = "created"
    admin_users_sort_dir = "desc"
    admin_users_sort_urls: dict[str, str] = {}
    admin_users_ret_fields: list[tuple[str, str]] = []
    admin_users_pagination = _AdminEventsPagination(0, 1, admin_users_per_page)

    if active_panel == "users":
        user_q = (request.args.get("user_q") or "").strip()
        user_email_q = (request.args.get("user_email_q") or "").strip()
        user_verified = (request.args.get("user_verified") or "").strip().lower()
        user_admin_f = (request.args.get("user_admin") or "").strip().lower()
        if user_q:
            admin_users_nav_kwargs["user_q"] = user_q
        if user_email_q:
            admin_users_nav_kwargs["user_email_q"] = user_email_q
        if user_verified in ("yes", "no"):
            admin_users_nav_kwargs["user_verified"] = user_verified
        if user_admin_f in ("yes", "no"):
            admin_users_nav_kwargs["user_admin"] = user_admin_f

        ug_sq = (
            db.session.query(
                MeetingGroup.user_id.label("ug_uid"),
                func.count(MeetingGroup.meeting_group_id).label("ug_n"),
            )
            .group_by(MeetingGroup.user_id)
            .subquery()
        )
        ua_sq = (
            db.session.query(
                MeetingAttendee.user_id.label("ua_uid"),
                func.count(MeetingAttendee.meeting_attendee_id).label("ua_n"),
            )
            .group_by(MeetingAttendee.user_id)
            .subquery()
        )
        uc_sq = (
            db.session.query(
                Meeting.creator_user_id.label("uc_uid"),
                func.count(Meeting.meeting_id).label("uc_n"),
            )
            .group_by(Meeting.creator_user_id)
            .subquery()
        )
        ui_cnt_sq = (
            db.session.query(
                user_industries.c.user_id.label("ui_uid"),
                func.count(user_industries.c.industry_id).label("ui_n"),
            )
            .group_by(user_industries.c.user_id)
            .subquery()
        )
        uat_cnt_sq = (
            db.session.query(
                user_attendee_tags.c.user_id.label("uat_uid"),
                func.count(user_attendee_tags.c.tag_id).label("uat_n"),
            )
            .group_by(user_attendee_tags.c.user_id)
            .subquery()
        )
        uq = (
            User.query.outerjoin(ug_sq, ug_sq.c.ug_uid == User.user_id)
            .outerjoin(ua_sq, ua_sq.c.ua_uid == User.user_id)
            .outerjoin(uc_sq, uc_sq.c.uc_uid == User.user_id)
            .outerjoin(ui_cnt_sq, ui_cnt_sq.c.ui_uid == User.user_id)
            .outerjoin(uat_cnt_sq, uat_cnt_sq.c.uat_uid == User.user_id)
        )
        if user_q:
            uq = uq.filter(User.username.contains(user_q))
        if user_email_q:
            uq = uq.filter(User.email.contains(user_email_q))
        if user_verified == "yes":
            uq = uq.filter(User.verification_confirmed.isnot(None))
        elif user_verified == "no":
            uq = uq.filter(User.verification_confirmed.is_(None))
        if user_admin_f == "yes":
            uq = uq.filter(User.admin_user.is_(True))
        elif user_admin_f == "no":
            uq = uq.filter(User.admin_user.is_(False))
        if _admin_should_hide_test_users():
            uq = uq.filter(~User.username.like("tnw_tu%"))

        n_groups_col = func.coalesce(ug_sq.c.ug_n, 0)
        n_att_rows_col = func.coalesce(ua_sq.c.ua_n, 0)
        n_creator_col = func.coalesce(uc_sq.c.uc_n, 0)
        n_ui_col = func.coalesce(ui_cnt_sq.c.ui_n, 0)
        n_uat_col = func.coalesce(uat_cnt_sq.c.uat_n, 0)
        verified_ord = case((User.verification_confirmed.isnot(None), 1), else_=0)

        user_sort_key = (request.args.get("usort") or "created").strip().lower()
        user_sort_dir = (request.args.get("udir") or "desc").strip().lower()
        u_asc = user_sort_dir == "asc"
        user_sort_map = {
            "user_id": User.user_id,
            "username": User.username,
            "email": User.email,
            "created": User.created_date,
            "groups": n_groups_col,
            "attendees": n_att_rows_col,
            "creator": n_creator_col,
            "verified": verified_ord,
            "admin": User.admin_user,
        }
        order_u_col = user_sort_map.get(user_sort_key, User.created_date)
        primary_u = order_u_col.asc() if u_asc else order_u_col.desc()
        if user_sort_key == "user_id":
            secondary_u = User.created_date.asc() if u_asc else User.created_date.desc()
        else:
            secondary_u = User.user_id.asc() if u_asc else User.user_id.desc()
        order_u_parts = (primary_u, secondary_u)

        admin_users_sort_key = user_sort_key
        admin_users_sort_dir = (
            user_sort_dir if user_sort_dir in ("asc", "desc") else "desc"
        )
        admin_users_nav_kwargs["usort"] = admin_users_sort_key
        admin_users_nav_kwargs["udir"] = admin_users_sort_dir

        def _flip_udir(d: str) -> str:
            return "asc" if d == "desc" else "desc"

        admin_users_sort_next_dir: dict[str, str] = {}
        for col in user_sort_map:
            if col == admin_users_sort_key:
                admin_users_sort_next_dir[col] = _flip_udir(
                    admin_users_sort_dir
                    if admin_users_sort_dir in ("asc", "desc")
                    else "desc"
                )
            else:
                admin_users_sort_next_dir[col] = "desc"

        admin_users_sort_urls = {
            col: url_for(
                "main.admin_users",
                **{
                    **admin_users_nav_kwargs,
                    "users_page": 1,
                    "usort": col,
                    "udir": admin_users_sort_next_dir[col],
                },
            )
            for col in user_sort_map.keys()
        }

        total_users = uq.with_entities(func.count(User.user_id)).scalar() or 0
        admin_users_total = int(total_users)
        up_page = request.args.get("users_page", type=int) or 1
        if up_page < 1:
            up_page = 1
        admin_users_pages = (
            max(1, math.ceil(admin_users_total / admin_users_per_page))
            if admin_users_total
            else 1
        )
        if up_page > admin_users_pages:
            up_page = admin_users_pages
        admin_users_page = up_page
        offset_u = (admin_users_page - 1) * admin_users_per_page

        uq_data = uq.add_columns(
            n_groups_col.label("u_n_groups"),
            n_att_rows_col.label("u_n_att"),
            n_creator_col.label("u_n_creator"),
            n_ui_col.label("u_n_ui"),
            n_uat_col.label("u_n_uat"),
        )
        raw_u = (
            uq_data.order_by(*order_u_parts)
            .offset(offset_u)
            .limit(admin_users_per_page)
            .all()
        )
        for r in raw_u:
            u = r[0]
            admin_users_rows.append(
                {
                    "user": u,
                    "n_groups": int(r[1] or 0),
                    "n_attendee_rows": int(r[2] or 0),
                    "n_creator_meetings": int(r[3] or 0),
                    "n_industries": int(r[4] or 0),
                    "n_attendee_tags": int(r[5] or 0),
                }
            )

        admin_users_pagination = _AdminEventsPagination(
            admin_users_total, admin_users_page, admin_users_per_page
        )
        for k, v in admin_users_nav_kwargs.items():
            if v is None:
                continue
            vs = str(v).strip()
            if not vs:
                continue
            admin_users_ret_fields.append((k, vs))
        admin_users_ret_fields.append(("users_page", str(admin_users_page)))
    else:
        _uku: dict = {"usort": admin_users_sort_key, "udir": admin_users_sort_dir}
        if not _admin_should_hide_test_users():
            _uku["ignore_test_users"] = "0"

        def _flip_u2(d: str) -> str:
            return "asc" if d == "desc" else "desc"

        _snd_u: dict[str, str] = {}
        for _col in (
            "user_id",
            "username",
            "email",
            "created",
            "groups",
            "attendees",
            "creator",
            "verified",
            "admin",
        ):
            if _col == admin_users_sort_key:
                _snd_u[_col] = _flip_u2(
                    admin_users_sort_dir
                    if admin_users_sort_dir in ("asc", "desc")
                    else "desc"
                )
            else:
                _snd_u[_col] = "desc"
        admin_users_sort_urls = {
            _col: url_for(
                "main.admin_users",
                **{**_uku, "users_page": 1, "usort": _col, "udir": _snd_u[_col]},
            )
            for _col in (
                "user_id",
                "username",
                "email",
                "created",
                "groups",
                "attendees",
                "creator",
                "verified",
                "admin",
            )
        }

    admin_event_images_rows: list = []
    admin_event_images_total = 0
    admin_event_images_page = 1
    admin_event_images_pages = 1
    admin_event_images_per_page = 24
    admin_event_images_nav_kwargs: dict = {}
    admin_event_images_nav_kwargs.update(admin_ignore_test_users_nav)
    admin_event_images_pagination = _AdminEventsPagination(
        0, 1, admin_event_images_per_page
    )

    admin_move_events_rows: list[Meeting] = []
    admin_move_events_total = 0
    admin_move_events_page = 1
    admin_move_events_pages = 1
    admin_move_events_per_page = 40
    admin_move_events_nav_kwargs: dict = {}
    admin_move_events_nav_kwargs.update(admin_ignore_test_users_nav)
    admin_move_events_title_ok = False
    admin_move_events_pagination = _AdminEventsPagination(
        0, 1, admin_move_events_per_page
    )
    admin_delete_events_groups: list[dict] = []
    admin_delete_events_selected_group_id = request.args.get(
        "del_group_id", type=int
    )

    if active_panel == "delete_group_events":
        del_counts_sq = (
            db.session.query(
                Meeting.meeting_group_id.label("dg_gid"),
                func.count(Meeting.meeting_id).label("dg_n"),
            )
            .group_by(Meeting.meeting_group_id)
            .subquery()
        )
        del_q = (
            db.session.query(
                MeetingGroup.meeting_group_id,
                MeetingGroup.meeting_group_name,
                User.email,
                func.coalesce(del_counts_sq.c.dg_n, 0),
            )
            .join(User, MeetingGroup.user_id == User.user_id)
            .outerjoin(
                del_counts_sq, del_counts_sq.c.dg_gid == MeetingGroup.meeting_group_id
            )
            .order_by(
                MeetingGroup.meeting_group_name.asc(), MeetingGroup.meeting_group_id.asc()
            )
        )
        if _admin_should_hide_test_users():
            del_q = del_q.filter(~User.username.like("tnw_tu%"))
        admin_delete_events_groups = [
            {
                "meeting_group_id": int(row[0]),
                "meeting_group_name": row[1] or f"Group {int(row[0])}",
                "owner_email": row[2] or "",
                "meeting_count": int(row[3] or 0),
            }
            for row in del_q.all()
        ]

    if active_panel == "move_events":
        mv_title_q = (request.args.get("mv_title_q") or "").strip()

        admin_move_events_title_ok = len(mv_title_q) >= 2

        admin_move_events_nav_kwargs = {}
        if not _admin_should_hide_test_users():
            admin_move_events_nav_kwargs["ignore_test_users"] = "0"
        if mv_title_q:
            admin_move_events_nav_kwargs["mv_title_q"] = mv_title_q

        if admin_move_events_title_ok:
            mq = (
                Meeting.query.options(
                    selectinload(Meeting.meeting_group).selectinload(MeetingGroup.owner)
                )
                .join(MeetingGroup, Meeting.meeting_group_id == MeetingGroup.meeting_group_id)
                .join(User, MeetingGroup.user_id == User.user_id)
            )
            mq = mq.filter(Meeting.title.contains(mv_title_q))
            if _admin_should_hide_test_users():
                mq = mq.filter(~User.username.like("tnw_tu%"))

            admin_move_events_total = (
                int(mq.with_entities(func.count(Meeting.meeting_id)).scalar() or 0)
            )
            mv_page = request.args.get("mv_page", type=int) or 1
            if mv_page < 1:
                mv_page = 1
            admin_move_events_pages = (
                max(
                    1,
                    math.ceil(admin_move_events_total / admin_move_events_per_page),
                )
                if admin_move_events_total
                else 1
            )
            if mv_page > admin_move_events_pages:
                mv_page = admin_move_events_pages
            admin_move_events_page = mv_page
            offset_mv = (admin_move_events_page - 1) * admin_move_events_per_page
            admin_move_events_rows = (
                mq.order_by(
                    Meeting.starts_at.desc(),
                    Meeting.meeting_id.desc(),
                )
                .offset(offset_mv)
                .limit(admin_move_events_per_page)
                .all()
            )
            admin_move_events_pagination = _AdminEventsPagination(
                admin_move_events_total,
                admin_move_events_page,
                admin_move_events_per_page,
            )

    return render_template(
        _ADMIN_PANEL_TEMPLATES[active_panel],
        total_meetings=total_meetings,
        total_meeting_groups=total_meeting_groups,
        status_counts=status_counts,
        other_status_totals=other_status_totals,
        active_panel=active_panel,
        admin_nav_active=active_panel,
        admin_transfer_users=admin_transfer_users,
        admin_overview_rows=admin_overview_rows,
        admin_ov_total=total_filtered,
        admin_ov_page=ov_page,
        admin_ov_pages=ov_pages,
        admin_ov_per_page=per_page,
        admin_overview_nav_kwargs=admin_overview_nav_kwargs,
        admin_bulk_ret_fields=admin_bulk_ret_fields,
        admin_sort_key=sort_key,
        admin_sort_dir=sort_dir if sort_dir in ("asc", "desc") else "desc",
        admin_sort_urls=admin_sort_urls,
        admin_ov_pagination=admin_ov_pagination,
        admin_bulk_transfer_notice=admin_bulk_transfer_notice,
        admin_move_events_notice=admin_move_events_notice,
        admin_delete_events_notice=admin_delete_events_notice,
        admin_kw_topics=admin_kw_topics,
        admin_kw_tags_by_topic=admin_kw_tags_by_topic,
        admin_users_rows=admin_users_rows,
        admin_users_total=admin_users_total,
        admin_users_page=admin_users_page,
        admin_users_pages=admin_users_pages,
        admin_users_per_page=admin_users_per_page,
        admin_users_nav_kwargs=admin_users_nav_kwargs,
        admin_users_sort_key=admin_users_sort_key,
        admin_users_sort_dir=admin_users_sort_dir,
        admin_users_sort_urls=admin_users_sort_urls,
        admin_users_pagination=admin_users_pagination,
        admin_users_ret_fields=admin_users_ret_fields,
        admin_ignore_test_users_nav=admin_ignore_test_users_nav,
        admin_test_events_user_rows=admin_test_events_user_rows,
        admin_test_events_default_user_id=admin_test_events_default_user_id,
        admin_test_events_today=datetime.now(timezone.utc).date().isoformat(),
        admin_event_images_rows=admin_event_images_rows,
        admin_event_images_total=admin_event_images_total,
        admin_event_images_page=admin_event_images_page,
        admin_event_images_pages=admin_event_images_pages,
        admin_event_images_per_page=admin_event_images_per_page,
        admin_event_images_nav_kwargs=admin_event_images_nav_kwargs,
        admin_event_images_pagination=admin_event_images_pagination,
        admin_move_events_rows=admin_move_events_rows,
        admin_move_events_total=admin_move_events_total,
        admin_move_events_page=admin_move_events_page,
        admin_move_events_pages=admin_move_events_pages,
        admin_move_events_per_page=admin_move_events_per_page,
        admin_move_events_nav_kwargs=admin_move_events_nav_kwargs,
        admin_move_events_title_ok=admin_move_events_title_ok,
        admin_move_events_pagination=admin_move_events_pagination,
        admin_delete_events_groups=admin_delete_events_groups,
        admin_delete_events_selected_group_id=admin_delete_events_selected_group_id,
    )


@bp.route("/admin/meeting-groups/delete-events", methods=["POST"])
@site_admin_required
def admin_meeting_group_delete_events():
    meeting_group_id = request.form.get("meeting_group_id", type=int)
    if not meeting_group_id:
        flash("Choose a valid event group first.", "warning")
        return redirect(
            url_for("main.admin_delete_group_events")
        )
    mg = MeetingGroup.query.get(meeting_group_id)
    if not mg:
        flash("That event group was not found.", "warning")
        return redirect(
            url_for("main.admin_delete_group_events")
        )
    selected_meeting_ids = request.form.getlist("meeting_ids", type=int) or []
    selected_meeting_ids = [mid for mid in selected_meeting_ids if mid]
    if not selected_meeting_ids:
        flash("Select at least one event to delete.", "warning")
        return redirect(
            url_for(
                "main.admin_delete_group_events",
                del_group_id=meeting_group_id,
            )
        )
    allowed_ids = {
        mid
        for (mid,) in db.session.query(Meeting.meeting_id)
        .filter(Meeting.meeting_group_id == meeting_group_id)
        .all()
    }
    valid_ids = [mid for mid in selected_meeting_ids if mid in allowed_ids]
    if not valid_ids:
        flash("No valid events were selected for this group.", "warning")
        return redirect(
            url_for(
                "main.admin_delete_group_events",
                del_group_id=meeting_group_id,
            )
        )
    try:
        deleted_events = _admin_delete_meetings_by_ids(valid_ids)
    except Exception:
        db.session.rollback()
        current_app.logger.exception("admin_meeting_group_delete_events failed")
        flash(
            "Could not delete events for that group. No changes were saved.",
            "danger",
        )
        return redirect(
            url_for(
                "main.admin_delete_group_events",
                del_group_id=meeting_group_id,
            )
        )
    if deleted_events == 0:
        flash(
            f"No events were found in '{mg.meeting_group_name}'.",
            "info",
        )
    else:
        session["admin_delete_events_notice"] = {
            "title": "Delete completed",
            "body": (
                f"Deleted {deleted_events} selected event(s) from '{mg.meeting_group_name}', "
                "including related ticket types, attendee bookings, and sold ticket entries."
            ),
        }
    return redirect(
        url_for(
            "main.admin_delete_group_events",
            del_group_id=meeting_group_id,
        )
    )


@bp.route("/admin/meeting-groups/<int:meeting_group_id>/events-for-delete", methods=["GET"])
@site_admin_required
def admin_meeting_group_events_for_delete(meeting_group_id: int):
    mg = MeetingGroup.query.options(selectinload(MeetingGroup.owner)).get(meeting_group_id)
    if not mg:
        return jsonify(ok=False, error="Event group not found."), 404
    rows = (
        Meeting.query.filter(Meeting.meeting_group_id == meeting_group_id)
        .order_by(Meeting.starts_at.desc(), Meeting.meeting_id.desc())
        .all()
    )
    items = [
        {
            "meeting_id": int(m.meeting_id),
            "title": (m.title or "").strip() or f"Event {m.meeting_id}",
            "starts_at": m.starts_at.strftime("%Y-%m-%d %H:%M") if m.starts_at else "",
            "status": (m.status or "").strip() or "Unknown",
            "meeting_format": (m.meeting_format or "").strip() or "Face2Face",
        }
        for m in rows
    ]
    return jsonify(
        ok=True,
        meeting_group={
            "meeting_group_id": int(mg.meeting_group_id),
            "meeting_group_name": mg.meeting_group_name or f"Group {mg.meeting_group_id}",
            "owner_email": (mg.owner.email if mg.owner else ""),
        },
        events=items,
    )


def _meeting_group_image_dir_abs() -> str:
    return os.path.join(current_app.root_path, "static", "meeting_group_images")


def _stored_meeting_group_image_abs_path(filename: str) -> str | None:
    """Resolve a stored meeting_group_images filename to an absolute path under static, or None if unsafe."""
    fn = (filename or "").strip()
    if not fn or os.path.basename(fn) != fn:
        return None
    base = os.path.abspath(_meeting_group_image_dir_abs())
    target = os.path.abspath(os.path.join(base, fn))
    try:
        if os.path.commonpath([base, target]) != base:
            return None
    except ValueError:
        return None
    return target


@bp.route("/admin/meeting-groups/<int:meeting_group_id>/image", methods=["POST"])
@site_admin_required
def admin_meeting_group_image_replace(meeting_group_id: int):
    """Replace a meeting group's banner image (admin only). Accepts multipart field ``image``.

    If the group already has ``image_filename``, the uploaded image is written to that same path
    (overwriting the file) and the database row is unchanged. If there is no stored file yet, a
    new filename is created and ``image_filename`` is set.
    """
    mg = MeetingGroup.query.get(meeting_group_id)
    if not mg:
        return jsonify(ok=False, error="Event group not found."), 404
    img = request.files.get("image")
    if not img:
        return jsonify(ok=False, error="No image uploaded."), 400
    try:
        img.stream.seek(0)
    except Exception:
        pass

    os.makedirs(MEETING_GROUP_IMAGE_DIR, exist_ok=True)
    stored = (mg.image_filename or "").strip()
    in_place = bool(stored)
    if in_place:
        target_path = _stored_meeting_group_image_abs_path(stored)
        if not target_path:
            return jsonify(ok=False, error="Stored image filename is not valid."), 400
        out_fn = stored
    else:
        uid = int(mg.user_id)
        out_fn = f"mg_{uid}_{int(datetime.utcnow().timestamp())}.png"
        target_path = os.path.join(MEETING_GROUP_IMAGE_DIR, out_fn)

    try:
        _resize_meeting_group_image(img, target_path)
    except UnidentifiedImageError:
        return jsonify(ok=False, error="That file is not a valid image (JPG, PNG, or WEBP)."), 400
    except Exception:
        current_app.logger.exception("admin_meeting_group_image_replace")
        return jsonify(ok=False, error="Could not process the image."), 500

    if not in_place:
        mg.image_filename = out_fn
        db.session.commit()

    image_url = url_for("static", filename=f"meeting_group_images/{out_fn}")
    return jsonify(
        ok=True,
        image_filename=out_fn,
        image_url=image_url,
        in_place=in_place,
    )


@bp.route("/admin/meeting-groups/<int:meeting_group_id>/description", methods=["GET", "POST"])
@site_admin_required
def admin_meeting_group_description(meeting_group_id: int):
    """Admin: view/update an event group's name, website, and description (JSON)."""
    mg = MeetingGroup.query.options(selectinload(MeetingGroup.owner)).get(meeting_group_id)
    if not mg:
        return jsonify(ok=False, error="Event group not found."), 404

    if request.method == "GET":
        owner = mg.owner
        return jsonify(
            ok=True,
            meeting_group_id=int(mg.meeting_group_id),
            meeting_group_name=(mg.meeting_group_name or "")[:180],
            owner_email=(owner.email if owner else "") or "",
            website_url=(mg.website_url or "")[:500],
            description=(mg.description or "")[:12000],
        )

    payload = request.get_json(silent=True) or {}
    if "meeting_group_name" in payload:
        name_raw = payload.get("meeting_group_name")
        if name_raw is None:
            name = ""
        elif not isinstance(name_raw, str):
            name = str(name_raw).strip()
        else:
            name = name_raw.strip()
        if not name:
            return jsonify(ok=False, error="Please enter an event group name."), 400
        if len(name) > 180:
            return jsonify(
                ok=False, error="Event group name must be 180 characters or fewer."
            ), 400
        mg.meeting_group_name = name[:180]

    if "website_url" in payload:
        raw_website = payload.get("website_url")
        if raw_website is None:
            raw_website = ""
        elif not isinstance(raw_website, str):
            raw_website = str(raw_website).strip()
        else:
            raw_website = raw_website.strip()
        website_url, website_err = _normalize_optional_http_url(raw_website)
        if website_err:
            return jsonify(ok=False, error=website_err), 400
        mg.website_url = website_url

    raw = payload.get("description")
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raw = str(raw)
    raw = raw[:20000]
    mg.description = _sanitize_rich_text_html(raw) or None
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("admin_meeting_group_description update failed")
        return jsonify(ok=False, error="Could not save the event group."), 500

    return jsonify(
        ok=True,
        meeting_group_id=int(mg.meeting_group_id),
        meeting_group_name=(mg.meeting_group_name or "")[:180],
        website_url=(mg.website_url or "")[:500],
    )


@bp.route("/admin/meeting-groups/bulk-image", methods=["POST"])
@site_admin_required
def admin_meeting_groups_bulk_image():
    """Apply one uploaded banner image to every selected event group."""
    ids = _admin_bulk_meeting_group_ids_from_form()
    if not ids:
        return jsonify(ok=False, error="Select at least one event group."), 400
    img = request.files.get("image")
    if not img:
        return jsonify(ok=False, error="No image uploaded."), 400

    groups = MeetingGroup.query.filter(MeetingGroup.meeting_group_id.in_(ids)).all()
    if not groups:
        return jsonify(ok=False, error="No matching event groups were found."), 404

    os.makedirs(_meeting_group_image_dir_abs(), exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".upload") as tmp:
            img.save(tmp.name)
            tmp_path = tmp.name
        try:
            probe_path = tmp_path + ".probe.png"
            _resize_meeting_group_image_from_path(tmp_path, probe_path)
            if os.path.isfile(probe_path):
                os.remove(probe_path)
        except UnidentifiedImageError:
            return jsonify(
                ok=False, error="That file is not a valid image (JPG, PNG, or WEBP)."
            ), 400

        image_dir = _meeting_group_image_dir_abs()
        ts_base = int(datetime.utcnow().timestamp())
        results: list[dict] = []
        for mg in groups:
            stored = (mg.image_filename or "").strip()
            in_place = bool(stored and _stored_meeting_group_image_abs_path(stored))
            if in_place:
                target_path = _stored_meeting_group_image_abs_path(stored)
                out_fn = stored
            else:
                out_fn = f"mg_{int(mg.user_id)}_{ts_base}_{int(mg.meeting_group_id)}.png"
                target_path = os.path.join(image_dir, out_fn)
            if not target_path:
                return jsonify(
                    ok=False,
                    error=f"Could not resolve image path for group {mg.meeting_group_id}.",
                ), 400
            _resize_meeting_group_image_from_path(tmp_path, target_path)
            if not in_place:
                mg.image_filename = out_fn
            results.append(
                {
                    "meeting_group_id": int(mg.meeting_group_id),
                    "image_filename": out_fn,
                    "image_url": url_for(
                        "static", filename=f"meeting_group_images/{out_fn}"
                    ),
                }
            )
        db.session.commit()
        return jsonify(ok=True, updated=len(results), results=results)
    except Exception:
        db.session.rollback()
        current_app.logger.exception("admin_meeting_groups_bulk_image")
        return jsonify(ok=False, error="Could not apply the image."), 500
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@bp.route("/admin/meeting-groups/bulk-details", methods=["POST"])
@site_admin_required
def admin_meeting_groups_bulk_details():
    """Apply the same description and/or website URL to selected event groups."""
    payload = request.get_json(silent=True) or {}
    ids = _admin_bulk_meeting_group_ids_from_json(payload)
    if not ids:
        return jsonify(ok=False, error="Select at least one event group."), 400
    if "description" not in payload and "website_url" not in payload:
        return jsonify(ok=False, error="Nothing to update."), 400

    website_url = None
    if "website_url" in payload:
        raw_website = payload.get("website_url")
        if raw_website is None:
            raw_website = ""
        elif not isinstance(raw_website, str):
            raw_website = str(raw_website).strip()
        else:
            raw_website = raw_website.strip()
        website_url, website_err = _normalize_optional_http_url(raw_website)
        if website_err:
            return jsonify(ok=False, error=website_err), 400

    desc = None
    if "description" in payload:
        raw = payload.get("description")
        if raw is None:
            raw = ""
        elif not isinstance(raw, str):
            raw = str(raw)
        desc = _sanitize_rich_text_html(raw[:20000]) or None

    groups = MeetingGroup.query.filter(MeetingGroup.meeting_group_id.in_(ids)).all()
    if not groups:
        return jsonify(ok=False, error="No matching event groups were found."), 404

    for mg in groups:
        if "description" in payload:
            mg.description = desc
        if "website_url" in payload:
            mg.website_url = website_url
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("admin_meeting_groups_bulk_details")
        return jsonify(ok=False, error="Could not save changes."), 500

    return jsonify(ok=True, updated=len(groups))


@bp.route("/admin/meeting-groups/bulk-website", methods=["POST"])
@site_admin_required
def admin_meeting_groups_bulk_website():
    """Apply the same website URL to selected event groups."""
    payload = request.get_json(silent=True) or {}
    ids = _admin_bulk_meeting_group_ids_from_json(payload)
    if not ids:
        return jsonify(ok=False, error="Select at least one event group."), 400

    raw_website = payload.get("website_url")
    if raw_website is None:
        raw_website = ""
    elif not isinstance(raw_website, str):
        raw_website = str(raw_website).strip()
    else:
        raw_website = raw_website.strip()
    website_url, website_err = _normalize_optional_http_url(raw_website)
    if website_err:
        return jsonify(ok=False, error=website_err), 400

    groups = MeetingGroup.query.filter(MeetingGroup.meeting_group_id.in_(ids)).all()
    if not groups:
        return jsonify(ok=False, error="No matching event groups were found."), 404

    for mg in groups:
        mg.website_url = website_url
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("admin_meeting_groups_bulk_website")
        return jsonify(ok=False, error="Could not save website URLs."), 500

    return jsonify(ok=True, updated=len(groups))


def _admin_user_edit_prefers_json() -> bool:
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    return (
        request.accept_mimetypes.best_match(["application/json", "text/html"])
        == "application/json"
    )


@bp.route("/admin/users/<int:user_id>/edit-data")
@site_admin_required
def admin_user_edit_data(user_id: int):
    """JSON payload for the admin user edit modal."""
    user = User.query.get_or_404(user_id)
    countries = [
        {"id": c.country_id, "name": c.country}
        for c in Country.query.order_by(Country.country.asc()).all()
    ]
    return jsonify(
        ok=True,
        user={
            "user_id": user.user_id,
            "username": user.username,
            "email": user.email,
            "first_name": user.first_name or "",
            "second_name": user.second_name or "",
            "mobile": user.mobile or "",
            "country_id": user.country_id,
            "admin_user": bool(user.admin_user),
        },
        countries=countries,
    )


@bp.route("/admin/users/<int:user_id>/edit", methods=["GET", "POST"])
@site_admin_required
def admin_user_edit(user_id: int):
    user = User.query.get_or_404(user_id)
    nav_kwargs = _admin_users_nav_kwargs_from_request_args()
    back_url = url_for("main.admin_users", **nav_kwargs)

    if request.method == "GET":
        q = dict(nav_kwargs)
        q["edit_user"] = user_id
        return redirect(url_for("main.admin_users", **q))

    pj = _admin_user_edit_prefers_json()
    nav_kwargs = {}
    for key in ("user_q", "user_email_q", "user_verified", "user_admin", "usort", "udir"):
        raw = request.form.get(f"ret_{key}")
        if raw is None or str(raw).strip() == "":
            continue
        nav_kwargs[key] = str(raw).strip()
    raw_page = request.form.get("ret_users_page")
    if raw_page is not None and str(raw_page).strip() != "":
        try:
            nav_kwargs["users_page"] = int(str(raw_page).strip())
        except ValueError:
            pass
    back_url = url_for("main.admin_users", **nav_kwargs)

    username = (request.form.get("username") or "").strip()[:50]
    email = (request.form.get("email") or "").strip()[:50]
    first_name = (request.form.get("first_name") or "").strip()[:100] or None
    second_name = (request.form.get("second_name") or "").strip()[:100] or None
    mobile = (request.form.get("mobile") or "").strip()[:50] or None
    country_id = request.form.get("country_id", type=int)
    admin_user_flag = bool(request.form.get("admin_user"))

    def _fail(msg: str, status: int = 400):
        if pj:
            return jsonify(ok=False, error=msg), status
        flash(msg, "danger")
        return redirect(url_for("main.admin_users", **{**nav_kwargs, "edit_user": user_id}))

    if not username:
        return _fail("Username is required.")
    if not email:
        return _fail("Email is required.")
    if country_id is None or Country.query.get(country_id) is None:
        return _fail("Choose a valid country.")

    du = (
        User.query.filter(
            db.func.lower(User.username) == username.lower(),
            User.user_id != user.user_id,
        ).first()
    )
    if du:
        return _fail("Another account already uses that username.")
    de = (
        User.query.filter(
            db.func.lower(User.email) == email.lower(),
            User.user_id != user.user_id,
        ).first()
    )
    if de:
        return _fail("Another account already uses that email address.")

    user.username = username
    user.email = email
    user.first_name = first_name
    user.second_name = second_name
    user.mobile = mobile
    user.country_id = country_id
    user.admin_user = admin_user_flag
    db.session.commit()
    if pj:
        return jsonify(ok=True, message="User updated.")
    flash("User updated.", "success")
    return redirect(back_url)


@bp.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@site_admin_required
def admin_user_delete(user_id: int):
    actor_id = _admin_session_user_id()
    if actor_id and user_id == actor_id:
        flash("You cannot delete your own administrator account from here.", "danger")
        return _admin_users_redirect_from_form()
    if not User.query.get(user_id):
        flash("That user was not found.", "warning")
        return _admin_users_redirect_from_form()
    try:
        _admin_delete_user_cascade(user_id)
        db.session.commit()
        flash("User deleted.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("admin_user_delete failed")
        flash("Could not delete that user. No changes were saved.", "danger")
    return _admin_users_redirect_from_form()


# ---------------------------------------------------------------------------
# Admin-only static review (delete app/static/admin_review/ when no longer needed)
# ---------------------------------------------------------------------------
@bp.route("/admin/review/about-v3-draft")
@login_required
def admin_review_about_v3_draft():
    """Serve standalone About v3 HTML for site admins (new tab, no app JS on that document)."""
    if not _session_site_admin_user():
        abort(403)
    path = os.path.join(current_app.static_folder, "admin_review", "about_v3_draft.html")
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="text/html; charset=utf-8", max_age=0)


# ---------------------------------------------------------------------------
# Admin impersonation (view site as another user)
# ---------------------------------------------------------------------------
@bp.route("/admin/impersonate/users.json")
@site_admin_required
def impersonate_users_json():
    group_rows = (
        db.session.query(MeetingGroup.user_id, func.count(MeetingGroup.meeting_group_id))
        .group_by(MeetingGroup.user_id)
        .all()
    )
    group_count_by_user = {uid: int(cnt) for uid, cnt in group_rows}

    ticket_rows = (
        db.session.query(
            MeetingAttendee.user_id,
            func.coalesce(func.sum(MeetingAttendee.quantity), 0),
        )
        .group_by(MeetingAttendee.user_id)
        .all()
    )
    ticket_sum_by_user = {uid: int(qty or 0) for uid, qty in ticket_rows}

    users = User.query.order_by(db.func.lower(User.email), User.user_id).all()
    return jsonify(
        [
            {
                "user_id": u.user_id,
                "email": (u.email or "").strip(),
                "event_groups": group_count_by_user.get(u.user_id, 0),
                "tickets_bought": ticket_sum_by_user.get(u.user_id, 0),
            }
            for u in users
        ]
    )


@bp.route("/admin/impersonate/start", methods=["POST"])
@login_required
def impersonate_start():
    admin = _session_site_admin_user()
    if not admin:
        flash("Only site administrators can view as another user.", "danger")
        return redirect(url_for("main.home"))

    target_id = request.form.get("user_id", type=int)
    if not target_id:
        flash("Choose a user from the list.", "warning")
        return redirect(url_for("main.home"))

    target = User.query.get(target_id)
    if not target:
        flash("That user was not found.", "danger")
        return redirect(url_for("main.home"))

    if getattr(admin, "user_id", None):
        session[SESSION_IMPERSONATOR_ADMIN_ID] = admin.user_id
        session.pop(SESSION_MAINT_ENV_USER, None)
    else:
        maint_name = session.get(SESSION_MAINT_ENV_USER)
        if not maint_name:
            flash("Could not start impersonation.", "danger")
            return redirect(url_for("main.home"))
        session[SESSION_MAINT_ENV_IMPERSONATOR] = maint_name
        session.pop(SESSION_MAINT_ENV_USER, None)
        session.pop(SESSION_IMPERSONATOR_ADMIN_ID, None)
    session["user_id"] = target.user_id
    flash(f"You are now viewing the site as {target.email}.", "info")
    return redirect(url_for("main.home"))


@bp.route("/admin/impersonate/stop", methods=["POST"])
@login_required
def impersonate_stop():
    imp_id = session.get(SESSION_IMPERSONATOR_ADMIN_ID)
    imp_env = session.get(SESSION_MAINT_ENV_IMPERSONATOR)
    if not imp_id and not imp_env:
        return redirect(url_for("main.home"))
    if imp_id:
        session["user_id"] = imp_id
        session.pop(SESSION_IMPERSONATOR_ADMIN_ID, None)
    else:
        session.pop("user_id", None)
        session[SESSION_MAINT_ENV_USER] = imp_env
        session.pop(SESSION_MAINT_ENV_IMPERSONATOR, None)
    flash("You are back in your own administrator account.", "info")
    return redirect(url_for("main.home"))


# ---------------------------------------------------------------------------
# Profile (render only, no DB)
# ---------------------------------------------------------------------------
@bp.route("/buy-boosts", methods=["GET"])
@login_required
def buy_boosts():
    from .promotion_boosts import (
        BOOST_LABEL,
        BOOST_LABEL_PLURAL,
        bundle_catalog_for_json,
        get_boost_balance,
        promotion_tiers_for_json,
    )

    user = User.query.get(session["user_id"])
    if not user:
        _session_clear_login()
        flash("Your session has expired. Please sign in again.", "warning")
        return redirect(url_for("main.login"))
    uid = int(user.user_id)
    return render_template(
        "buy_boosts.html",
        user=user,
        boost_balance=get_boost_balance(uid),
        boost_label=BOOST_LABEL,
        boost_label_plural=BOOST_LABEL_PLURAL,
        boost_bundles=bundle_catalog_for_json(),
        tiers=promotion_tiers_for_json(),
        buy_bundle_url=url_for("main.api_dashboard_promotion_buy_bundle"),
        promote_url=tnw_url_for("main.platform_dashboard", _anchor="promote-events-pane"),
    )


def _my_account_tx_filters_from_request():
    return {
        "date_from": (request.args.get("date_from") or "").strip() or None,
        "date_to": (request.args.get("date_to") or "").strip() or None,
        "description_q": (request.args.get("description") or request.args.get("q") or "").strip() or None,
        "flow": (request.args.get("flow") or "all").strip().lower() or "all",
    }


@bp.route("/my-account/transactions/export")
@login_required
def my_account_transactions_export():
    from .account_tx_exports import build_transactions_csv, build_transactions_pdf
    from .user_account_tx import gbp_transactions_query

    user = User.query.get(session["user_id"])
    if not user:
        return jsonify({"ok": False, "error": "Session expired."}), 401
    uid = int(user.user_id)
    filters = _my_account_tx_filters_from_request()
    rows = gbp_transactions_query(uid, **filters, limit=5000)
    fmt = (request.args.get("format") or "csv").strip().lower()
    email = (user.email or "").strip()

    if fmt == "pdf":
        body = build_transactions_pdf(
            rows,
            user_email=email,
            title="My Account transactions",
        )
        return Response(
            body,
            mimetype="application/pdf",
            headers={
                "Content-Disposition": 'attachment; filename="the-networker-transactions.pdf"'
            },
        )

    body = build_transactions_csv(rows, user_email=email)
    return Response(
        body,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="the-networker-transactions.csv"'
        },
    )


@bp.route("/my-account/transactions/<int:tx_pk>")
@login_required
def my_account_transaction_detail(tx_pk: int):
    from .user_account_tx import get_user_transaction

    user = User.query.get(session["user_id"])
    if not user:
        return jsonify({"ok": False, "error": "Session expired."}), 401
    tx = get_user_transaction(int(user.user_id), int(tx_pk))
    if not tx:
        return jsonify({"ok": False, "error": "Transaction not found."}), 404
    return jsonify({"ok": True, "transaction": tx})


@bp.route("/my-account/transactions/<int:tx_pk>/pdf")
@login_required
def my_account_transaction_pdf(tx_pk: int):
    from .account_tx_exports import build_transaction_pdf
    from .user_account_tx import get_user_transaction

    user = User.query.get(session["user_id"])
    if not user:
        return jsonify({"ok": False, "error": "Session expired."}), 401
    tx = get_user_transaction(int(user.user_id), int(tx_pk))
    if not tx:
        return jsonify({"ok": False, "error": "Transaction not found."}), 404
    from .purchase_invoicing import bill_to_from_user, invoice_filename, should_issue_vat_invoice

    bill_to = bill_to_from_user(user)
    body = build_transaction_pdf(
        tx, user_email=(user.email or "").strip(), bill_to=bill_to
    )
    fname = (
        invoice_filename(tx)
        if should_issue_vat_invoice(tx)
        else f"transaction-{tx['user_tx_number']}.pdf"
    )
    return Response(
        body,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@bp.route("/my-account/transactions/<int:tx_pk>/email-pdf", methods=["POST"])
@login_required
def my_account_transaction_email_pdf(tx_pk: int):
    from .account_tx_exports import build_transaction_pdf
    from .purchase_invoicing import (
        bill_to_from_user,
        invoice_filename,
        send_account_transaction_pdf_email,
        should_issue_vat_invoice,
    )
    from .user_account_tx import get_user_transaction

    user = User.query.get(session["user_id"])
    if not user:
        return jsonify(ok=False, error="Session expired."), 401
    tx = get_user_transaction(int(user.user_id), int(tx_pk))
    if not tx:
        return jsonify(ok=False, error="Transaction not found."), 404
    email = (user.email or "").strip()
    if not email:
        return jsonify(ok=False, error="Add an email address on your profile first."), 400

    bill_to = bill_to_from_user(user)
    body = build_transaction_pdf(
        tx, user_email=email, bill_to=bill_to
    )
    fname = (
        invoice_filename(tx)
        if should_issue_vat_invoice(tx)
        else f"transaction-{tx['user_tx_number']}.pdf"
    )
    try:
        send_account_transaction_pdf_email(
            recipient_email=email,
            tx=tx,
            pdf_bytes=body,
            filename=fname,
            bill_to=bill_to,
        )
    except RuntimeError as exc:
        if "SMTP" in str(exc):
            return jsonify(
                ok=False,
                error="Email is not configured on this server. Download the PDF instead.",
            ), 503
        raise
    except Exception:
        current_app.logger.exception("my_account_transaction_email_pdf tx_pk=%s", tx_pk)
        return jsonify(ok=False, error="Could not send the email. Try again."), 500

    return jsonify(ok=True, message=f"Sent to {email}.")


@bp.route("/my-account/gbp-transactions")
@login_required
def my_account_gbp_transactions_json():
    from .user_account_tx import gbp_transactions_query

    user = User.query.get(session["user_id"])
    if not user:
        return jsonify(ok=False, error="Session expired."), 401
    uid = int(user.user_id)
    rows = gbp_transactions_query(
        uid,
        date_from=(request.args.get("date_from") or "").strip() or None,
        date_to=(request.args.get("date_to") or "").strip() or None,
        description_q=(request.args.get("description") or "").strip() or None,
        flow=(request.args.get("flow") or "").strip() or None,
        limit=500,
    )
    return jsonify(ok=True, transactions=rows)


@bp.route("/my-account/boost-transactions")
@login_required
def my_account_boost_transactions_json():
    from .promotion_boosts import BOOST_LABEL, BOOST_LABEL_PLURAL
    from .user_account_tx import boost_ledger_transactions_for_user

    user = User.query.get(session["user_id"])
    if not user:
        return jsonify(ok=False, error="Session expired."), 401
    uid = int(user.user_id)
    rows = boost_ledger_transactions_for_user(
        uid,
        date_from=(request.args.get("date_from") or "").strip() or None,
        date_to=(request.args.get("date_to") or "").strip() or None,
        description_q=(request.args.get("description") or "").strip() or None,
        flow=(request.args.get("flow") or "").strip() or None,
        limit=500,
        boost_label=BOOST_LABEL,
        boost_label_plural=BOOST_LABEL_PLURAL,
    )
    return jsonify(ok=True, transactions=rows)


@bp.route("/my-account", methods=["GET"])
@login_required
def my_account():
    from .promotion_boosts import (
        BOOST_LABEL,
        BOOST_LABEL_PLURAL,
        bundle_catalog_for_json,
        get_boost_balance,
    )
    from .user_account_tx import (
        boost_ledger_transactions_for_user,
        default_account_tx_filter_dates,
        gbp_transactions_query,
        organiser_account_summary,
        pending_ticket_sales_for_user,
        withdraw_funds_panel_for_user,
    )

    user = User.query.get(session["user_id"])
    if not user:
        _session_clear_login()
        flash("Your session has expired. Please sign in again.", "warning")
        return redirect(url_for("main.login"))
    uid = int(user.user_id)
    account_summary = organiser_account_summary(uid)
    tx_filter_date_from, tx_filter_date_to = default_account_tx_filter_dates()
    return render_template(
        "my_account.html",
        user=user,
        boost_balance=get_boost_balance(uid),
        boost_label=BOOST_LABEL,
        boost_label_plural=BOOST_LABEL_PLURAL,
        account_summary=account_summary,
        boost_bundles=bundle_catalog_for_json(),
        gbp_transactions=gbp_transactions_query(uid, limit=500),
        boost_transactions=boost_ledger_transactions_for_user(
            uid,
            limit=500,
            boost_label=BOOST_LABEL,
            boost_label_plural=BOOST_LABEL_PLURAL,
        ),
        pending_ticket_sales=pending_ticket_sales_for_user(uid),
        withdraw_funds=withdraw_funds_panel_for_user(uid),
        tx_filter_date_from=tx_filter_date_from,
        tx_filter_date_to=tx_filter_date_to,
        buy_bundle_url=url_for("main.api_dashboard_promotion_buy_bundle"),
        buy_boosts_url=url_for("main.buy_boosts"),
        promote_url=tnw_url_for("main.platform_dashboard", _anchor="promote-events-pane"),
    )


@bp.route("/profile/setup", methods=["GET", "POST"])
def profile_setup():
    return redirect(url_for("main.profile"))


@bp.route("/profile", methods=["GET"])
@login_required
def profile():
    user = User.query.get(session["user_id"])
    if not user:
        _session_clear_login()
        flash("Your session has expired. Please sign in again.", "warning")
        return redirect(url_for("main.login"))

    countries = Country.query.order_by(Country.country_id).all()
    industries = Industry.query.order_by(Industry.industry).all()
    selected_industry_ids = {i.industry_id for i in user.industries}
    user_tags = user.attendee_tags
    return render_template(
        "profile.html",
        countries=countries,
        industries=industries,
        selected_industry_ids=selected_industry_ids,
        user_tags=user_tags,
    )


@bp.route("/profile/details", methods=["POST"])
@login_required
def profile_details():
    user = User.query.get(session["user_id"])
    if not user:
        session.pop("user_id", None)
        return redirect(url_for("main.login"))

    new_username = request.form.get("username", "").strip()
    new_first_name = request.form.get("first_name", "").strip()
    new_last_name = request.form.get("last_name", "").strip()
    new_mobile_country_code = request.form.get("mobile_country_code", "+44").strip()
    new_mobile_local = request.form.get("mobile", "").strip()
    new_mobile = f"{new_mobile_country_code} {new_mobile_local}".strip() if new_mobile_local else ""
    new_email = request.form.get("email", "").strip().lower()

    if not new_username:
        flash("Please enter a username.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionDetails"))
    if len(new_username) > 50:
        flash("Username must be 50 characters or fewer.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionDetails"))

    if new_first_name and len(new_first_name) > 100:
        flash("First name must be 100 characters or fewer.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionDetails"))

    if new_last_name and len(new_last_name) > 100:
        flash("Last name must be 100 characters or fewer.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionDetails"))

    if new_mobile:
        if len(new_mobile) > 50:
            flash("Mobile must be 50 characters or fewer.", "danger")
            return redirect(url_for("main.profile", _anchor="sectionDetails"))
        if not MOBILE_RE.match(new_mobile):
            flash("Please enter a valid mobile number.", "danger")
            return redirect(url_for("main.profile", _anchor="sectionDetails"))

    if not new_email or not EMAIL_RE.match(new_email):
        flash("Please enter a valid email address.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionDetails"))
    if len(new_email) > 50:
        flash("Email must be 50 characters or fewer.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionDetails"))

    email_changed = new_email != (user.email or "").lower()
    if email_changed:
        clash = User.query.filter(
            User.email == new_email, User.user_id != user.user_id
        ).first()
        if clash:
            flash("That email is already in use by another account.", "danger")
            return redirect(url_for("main.profile", _anchor="sectionDetails"))

    user.username = new_username
    user.first_name = new_first_name or None
    user.second_name = new_last_name or None
    user.mobile = new_mobile or None

    if email_changed:
        user.email = new_email
        user.verification_confirmed = None
        user.verification_code = secrets.token_urlsafe(32)
        user.verification_send = datetime.utcnow()

    db.session.commit()
    profile_incomplete_message = (
        "Finish setting up your account.\n"
        "Drop a pin on the map to set your location and unlock the rest of the site."
    )

    if email_changed:
        try:
            _send_verification_email(user, user.verification_code)
            if user.is_profile_complete:
                flash(
                    "Details saved. We've sent a verification link to your new email address.",
                    "success",
                )
            else:
                flash(profile_incomplete_message, "info")
        except Exception:
            flash(
                "Details saved, but we couldn't send the verification email right now. "
                "Please try again later.",
                "warning",
            )
    else:
        if user.is_profile_complete:
            flash("Details saved.", "success")
        else:
            flash(profile_incomplete_message, "info")

    next_anchor = "sectionLocation" if not user.is_profile_complete else "sectionDetails"
    return redirect(url_for("main.profile", _anchor=next_anchor))


@bp.route("/profile/password", methods=["POST"])
@login_required
def profile_password():
    user = User.query.get(session["user_id"])
    if not user:
        session.pop("user_id", None)
        return redirect(url_for("main.login"))

    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not current_password or not new_password or not confirm_password:
        flash("Please fill in all password fields.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionSecurity"))

    if not user.check_password(current_password):
        flash("Your current password is incorrect.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionSecurity"))

    if new_password != confirm_password:
        flash("New passwords do not match.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionSecurity"))

    if not _password_is_strong(new_password):
        flash(
            "Password must be at least 8 characters and include both uppercase and lowercase letters.",
            "danger",
        )
        return redirect(url_for("main.profile", _anchor="sectionSecurity"))

    if current_password == new_password:
        flash("Your new password must be different from your current password.", "warning")
        return redirect(url_for("main.profile", _anchor="sectionSecurity"))

    user.set_password(new_password)
    db.session.commit()

    flash("Password updated.", "success")
    return redirect(url_for("main.profile", _anchor="sectionSecurity"))


def _qr_data_uri(text):
    """Build a small PNG QR code for the given text and return it as a
    base64 data: URI suitable for dropping into an <img src=...> tag."""
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


@bp.route("/profile/twofa/setup", methods=["GET"])
@login_required
def profile_twofa_setup():
    """Returns JSON with a freshly generated TOTP secret + QR image for
    the Security modal to render."""
    user = User.query.get(session["user_id"])
    if not user:
        return jsonify({"error": "not_logged_in"}), 401

    if user.twofa_enabled:
        return jsonify({"error": "already_enabled"}), 400

    # Regenerate a fresh secret every time the modal is opened so an
    # abandoned half-setup never leaves a stale secret paired with an
    # authenticator app that was never actually linked.
    user.generate_twofa_secret()
    db.session.commit()

    uri = user.get_twofa_uri()
    raw = user.twofa_secret or ""
    pretty_secret = " ".join(raw[i : i + 4] for i in range(0, len(raw), 4))

    return jsonify(
        {
            "qr_data_uri": _qr_data_uri(uri) if uri else None,
            "secret_pretty": pretty_secret,
            "account_name": user.email,
            "issuer": "The Networker",
        }
    )


@bp.route("/profile/twofa/verify", methods=["POST"])
@login_required
def profile_twofa_verify():
    user = User.query.get(session["user_id"])
    if not user:
        session.pop("user_id", None)
        return redirect(url_for("main.login"))

    code = (request.form.get("code") or "").strip().replace(" ", "")

    if not user.twofa_secret:
        flash("Start the setup again to get a fresh QR code.", "warning")
        return redirect(url_for("main.profile", _anchor="sectionSecurity"))

    if not user.verify_twofa_code(code):
        flash("That code didn't match. Try again with the latest code from your app.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionSecurity"))

    user.twofa_enabled = True
    db.session.commit()
    flash("Two-factor authentication is now on. Keep your authenticator app safe!", "success")
    return redirect(url_for("main.profile", _anchor="sectionSecurity"))


@bp.route("/profile/twofa/disable", methods=["POST"])
@login_required
def profile_twofa_disable():
    user = User.query.get(session["user_id"])
    if not user:
        session.pop("user_id", None)
        return redirect(url_for("main.login"))

    user.twofa_enabled = False
    user.twofa_secret = None
    db.session.commit()
    flash("Two-factor authentication disabled.", "info")
    return redirect(url_for("main.profile", _anchor="sectionSecurity"))


@bp.route("/profile/location", methods=["POST"])
@login_required
def profile_location():
    user = User.query.get(session["user_id"])
    if not user:
        session.pop("user_id", None)
        return redirect(url_for("main.login"))

    try:
        lat = float(request.form.get("latitude", ""))
        lng = float(request.form.get("longitude", ""))
    except (TypeError, ValueError):
        flash("Invalid location. Please drop a pin on the map and try again.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionLocation"))

    # Rough UK bounding box (includes Shetland + Channel Islands buffer).
    if not (49.5 <= lat <= 61.0) or not (-9.0 <= lng <= 2.5):
        flash("Please pick a location within the UK.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionLocation"))

    user.latitude = lat
    user.longitude = lng
    db.session.commit()

    flash("Location saved.", "success")
    return redirect(url_for("main.profile", _anchor="sectionLocation"))


@bp.route("/profile/industries", methods=["POST"])
@login_required
def profile_industries():
    user = User.query.get(session["user_id"])
    if not user:
        session.pop("user_id", None)
        return redirect(url_for("main.login"))

    raw_ids = request.form.getlist("industries")
    try:
        submitted_ids = {int(v) for v in raw_ids if v}
    except ValueError:
        flash("Invalid selection. Please try again.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionInterests"))

    # Only keep IDs that actually exist in the topics table.
    if submitted_ids:
        valid = {
            row.industry_id
            for row in Industry.query.filter(Industry.industry_id.in_(submitted_ids)).all()
        }
    else:
        valid = set()

    user.industries = Industry.query.filter(Industry.industry_id.in_(valid)).all() if valid else []
    db.session.commit()

    if valid:
        flash(f"Saved {len(valid)} industr{'y' if len(valid) == 1 else 'ies'}.", "success")
    else:
        flash("Cleared your industry selection.", "info")
    return redirect(url_for("main.profile", _anchor="sectionInterests"))


# ---------------------------------------------------------------------------
# Search tags (autocomplete + add/remove for user_attendee_tags)
# ---------------------------------------------------------------------------
@bp.route("/profile/tags/search")
@login_required
def profile_tags_search():
    """Live autocomplete endpoint. Returns up to 10 matching tags as JSON.
    Optional filter by industry_id. Selected tags are still returned so the UI
    can show and toggle them in place."""
    q = request.args.get("q", "").strip()
    industry_id_raw = request.args.get("industry_id", "").strip()

    query = Tag.query
    if industry_id_raw:
        try:
            query = query.filter(Tag.industry_id == int(industry_id_raw))
        except ValueError:
            pass
    if q:
        query = query.filter(Tag.tag.ilike(f"%{q}%"))

    results = query.order_by(Tag.tag).limit(10).all()
    return jsonify(
        [
            {
                "tag_id": t.tag_id,
                "tag": t.tag,
                "industry_id": t.industry_id,
                "industry": t.industry.industry if t.industry else "",
            }
            for t in results
        ]
    )


@bp.route("/profile/tags/all")
@login_required
def profile_tags_all():
    """Return all tags, optionally filtered by industry_id."""
    industry_id_raw = request.args.get("industry_id", "").strip()
    query = Tag.query
    if industry_id_raw:
        try:
            query = query.filter(Tag.industry_id == int(industry_id_raw))
        except ValueError:
            pass

    results = query.order_by(Tag.tag).all()
    return jsonify(
        [
            {
                "tag_id": t.tag_id,
                "tag": t.tag,
                "industry_id": t.industry_id,
                "industry": t.industry.industry if t.industry else "",
            }
            for t in results
        ]
    )


@bp.route("/profile/tags/add", methods=["POST"])
@login_required
def profile_tags_add():
    user = User.query.get(session["user_id"])
    if not user:
        session.pop("user_id", None)
        return redirect(url_for("main.login"))

    is_picker_sync = request.form.get("keyword_picker_sync") == "1"
    submitted_tag_ids = {
        int(v)
        for v in request.form.getlist("tag_ids")
        if str(v).strip().isdigit()
    }
    raw_tag = request.form.get("tag", "").strip()
    raw_industry = request.form.get("industry_id", "").strip()

    selected_tags = (
        Tag.query.filter(Tag.tag_id.in_(submitted_tag_ids)).order_by(Tag.tag).all()
        if submitted_tag_ids
        else []
    )

    if is_picker_sync and not raw_tag:
        user.attendee_tags = selected_tags
        db.session.commit()
        flash("Updated your keywords.", "success")
        return redirect(url_for("main.profile", _anchor="sectionSearchTags"))

    if not raw_tag:
        flash("Please type a tag before saving.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionSearchTags"))
    if len(raw_tag) > 50:
        flash("Tags must be 50 characters or fewer.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionSearchTags"))

    industry_id = None
    if raw_industry:
        try:
            industry_id = int(raw_industry)
        except ValueError:
            industry_id = None
        if industry_id is not None and not Industry.query.get(industry_id):
            industry_id = None

    # Try to find an existing tag (case-insensitive exact name match, optionally
    # constrained to the chosen industry).
    existing_q = Tag.query.filter(db.func.lower(Tag.tag) == raw_tag.lower())
    if industry_id is not None:
        existing_q = existing_q.filter(Tag.industry_id == industry_id)
    tag = existing_q.first()

    if not tag:
        # Creating a brand-new tag requires an industry (tags.industry_id NOT NULL).
        if industry_id is None:
            flash(
                "That tag doesn't exist yet. Pick an industry from the dropdown "
                "so we can create it under the right category.",
                "warning",
            )
            return redirect(url_for("main.profile", _anchor="sectionSearchTags"))
        tag = Tag(tag=raw_tag, industry_id=industry_id)
        db.session.add(tag)
        db.session.flush()

    if is_picker_sync:
        if tag not in selected_tags:
            selected_tags.append(tag)
        user.attendee_tags = selected_tags
        db.session.commit()
        flash("Updated your keywords.", "success")
    elif tag in user.attendee_tags:
        flash(f"'{tag.tag}' is already on your list.", "info")
    else:
        user.attendee_tags.append(tag)
        db.session.commit()
        flash(f"Added '{tag.tag}'.", "success")

    return redirect(url_for("main.profile", _anchor="sectionSearchTags"))


@bp.route("/profile/tags/remove", methods=["POST"])
@login_required
def profile_tags_remove():
    user = User.query.get(session["user_id"])
    if not user:
        session.pop("user_id", None)
        return redirect(url_for("main.login"))

    try:
        tag_id = int(request.form.get("tag_id", ""))
    except (TypeError, ValueError):
        flash("Invalid tag.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionSearchTags"))

    tag = Tag.query.get(tag_id)
    if tag and tag in user.attendee_tags:
        user.attendee_tags.remove(tag)
        db.session.commit()
        flash(f"Removed '{tag.tag}'.", "info")
    return redirect(url_for("main.profile", _anchor="sectionSearchTags"))


def _topic_reference_counts(industry_id):
    return {
        "keywords": Tag.query.filter_by(industry_id=industry_id).count(),
        "event groups": MeetingGroup.query.filter_by(industry_id=industry_id).count(),
        "organiser profiles": db.session.query(user_industries)
        .filter(user_industries.c.industry_id == industry_id)
        .count(),
    }


def _keyword_reference_counts(tag_id):
    return {
        "attendee profiles": db.session.query(user_attendee_tags)
        .filter(user_attendee_tags.c.tag_id == tag_id)
        .count(),
        "event groups": db.session.query(meeting_group_tags)
        .filter(meeting_group_tags.c.tag_id == tag_id)
        .count(),
    }


def _format_reference_counts(counts):
    return ", ".join(
        f"{count} {label}" for label, count in counts.items() if count
    )


@bp.route("/admin/keyword-maintenance")
@site_admin_required
def keyword_maintenance():
    topics = Industry.query.order_by(Industry.industry).all()
    selected_topic_id = request.args.get("topic_id", type=int)
    keyword_query = Tag.query
    if selected_topic_id:
        keyword_query = keyword_query.filter(Tag.industry_id == selected_topic_id)
    keywords = keyword_query.order_by(Tag.tag).all()

    topic_counts = {
        topic.industry_id: _topic_reference_counts(topic.industry_id)
        for topic in topics
    }
    keyword_counts = {
        keyword.tag_id: _keyword_reference_counts(keyword.tag_id)
        for keyword in keywords
    }

    return render_template(
        "keyword_maintenance.html",
        topics=topics,
        keywords=keywords,
        selected_topic_id=selected_topic_id,
        topic_counts=topic_counts,
        keyword_counts=keyword_counts,
    )


@bp.route("/admin/topics/add", methods=["POST"])
@site_admin_required
def topic_add():
    topic_name = (request.form.get("industry") or "").strip()
    if not topic_name:
        flash("Enter a topic name.", "danger")
        return redirect(url_for("main.keyword_maintenance"))
    if len(topic_name) > 50:
        flash("Topic names must be 50 characters or fewer.", "danger")
        return redirect(url_for("main.keyword_maintenance"))
    existing = Industry.query.filter(db.func.lower(Industry.industry) == topic_name.lower()).first()
    if existing:
        flash("That topic already exists.", "warning")
        return redirect(url_for("main.keyword_maintenance", topic_id=existing.industry_id, tab="topics"))

    topic = Industry(industry=topic_name)
    db.session.add(topic)
    db.session.commit()
    flash("Topic added.", "success")
    return redirect(url_for("main.keyword_maintenance", topic_id=topic.industry_id, tab="topics"))


@bp.route("/admin/topics/update", methods=["POST"])
@site_admin_required
def topic_update():
    topic_id = request.form.get("industry_id", type=int)
    topic = Industry.query.get(topic_id) if topic_id else None
    if not topic:
        flash("Topic not found.", "danger")
        return redirect(url_for("main.keyword_maintenance"))

    topic_name = (request.form.get("industry") or "").strip()
    if not topic_name:
        flash("Enter a topic name.", "danger")
        return redirect(url_for("main.keyword_maintenance", topic_id=topic.industry_id, tab="topics"))
    if len(topic_name) > 50:
        flash("Topic names must be 50 characters or fewer.", "danger")
        return redirect(url_for("main.keyword_maintenance", topic_id=topic.industry_id, tab="topics"))
    duplicate = (
        Industry.query.filter(db.func.lower(Industry.industry) == topic_name.lower())
        .filter(Industry.industry_id != topic.industry_id)
        .first()
    )
    if duplicate:
        flash("Another topic already uses that name.", "warning")
        return redirect(url_for("main.keyword_maintenance", topic_id=topic.industry_id, tab="topics"))

    topic.industry = topic_name
    db.session.commit()
    flash("Topic updated.", "success")
    return redirect(url_for("main.keyword_maintenance", topic_id=topic.industry_id, tab="topics"))


@bp.route("/admin/topics/delete", methods=["POST"])
@site_admin_required
def topic_delete():
    topic_id = request.form.get("industry_id", type=int)
    topic = Industry.query.get(topic_id) if topic_id else None
    if not topic:
        flash("Topic not found.", "danger")
        return redirect(url_for("main.keyword_maintenance"))

    counts = _topic_reference_counts(topic.industry_id)
    references = _format_reference_counts(counts)
    if references:
        flash(f"Cannot delete topic '{topic.industry}' because it is used by {references}.", "warning")
        return redirect(url_for("main.keyword_maintenance", topic_id=topic.industry_id, tab="topics"))

    db.session.delete(topic)
    db.session.commit()
    flash("Topic deleted.", "success")
    return redirect(url_for("main.keyword_maintenance"))


@bp.route("/admin/keywords/add", methods=["POST"])
@site_admin_required
def keyword_add():
    tag_name = (request.form.get("tag") or "").strip()
    industry_id = request.form.get("industry_id", type=int)
    topic = Industry.query.get(industry_id) if industry_id else None
    if not topic:
        flash("Choose a topic before adding a keyword.", "danger")
        return redirect(url_for("main.keyword_maintenance", tab="keywords"))
    if not tag_name:
        flash("Enter a keyword.", "danger")
        return redirect(url_for("main.keyword_maintenance", topic_id=industry_id, tab="keywords"))
    if len(tag_name) > 50:
        flash("Keywords must be 50 characters or fewer.", "danger")
        return redirect(url_for("main.keyword_maintenance", topic_id=industry_id, tab="keywords"))
    existing = Tag.query.filter(
        Tag.industry_id == industry_id,
        db.func.lower(Tag.tag) == tag_name.lower(),
    ).first()
    if existing:
        flash("That keyword already exists for this topic.", "warning")
        return redirect(url_for("main.keyword_maintenance", topic_id=industry_id, tab="keywords"))

    db.session.add(Tag(tag=tag_name, industry_id=industry_id))
    db.session.commit()
    flash("Keyword added.", "success")
    return redirect(url_for("main.keyword_maintenance", topic_id=industry_id, tab="keywords"))


@bp.route("/admin/keywords/update", methods=["POST"])
@site_admin_required
def keyword_update():
    tag_id = request.form.get("tag_id", type=int)
    keyword = Tag.query.get(tag_id) if tag_id else None
    if not keyword:
        flash("Keyword not found.", "danger")
        return redirect(url_for("main.keyword_maintenance", tab="keywords"))

    tag_name = (request.form.get("tag") or "").strip()
    industry_id = request.form.get("industry_id", type=int)
    topic = Industry.query.get(industry_id) if industry_id else None
    if not topic:
        flash("Choose a valid topic for the keyword.", "danger")
        return redirect(url_for("main.keyword_maintenance", topic_id=keyword.industry_id, tab="keywords"))
    if not tag_name:
        flash("Enter a keyword.", "danger")
        return redirect(url_for("main.keyword_maintenance", topic_id=keyword.industry_id, tab="keywords"))
    if len(tag_name) > 50:
        flash("Keywords must be 50 characters or fewer.", "danger")
        return redirect(url_for("main.keyword_maintenance", topic_id=keyword.industry_id, tab="keywords"))

    duplicate = (
        Tag.query.filter(
            Tag.industry_id == industry_id,
            db.func.lower(Tag.tag) == tag_name.lower(),
        )
        .filter(Tag.tag_id != keyword.tag_id)
        .first()
    )
    if duplicate:
        flash("Another keyword already uses that name for this topic.", "warning")
        return redirect(url_for("main.keyword_maintenance", topic_id=keyword.industry_id, tab="keywords"))

    keyword.tag = tag_name
    keyword.industry_id = industry_id
    db.session.commit()
    flash("Keyword updated.", "success")
    return redirect(url_for("main.keyword_maintenance", topic_id=industry_id, tab="keywords"))


@bp.route("/admin/keywords/delete", methods=["POST"])
@site_admin_required
def keyword_delete():
    tag_id = request.form.get("tag_id", type=int)
    keyword = Tag.query.get(tag_id) if tag_id else None
    if not keyword:
        flash("Keyword not found.", "danger")
        return redirect(url_for("main.keyword_maintenance", tab="keywords"))

    industry_id = keyword.industry_id
    counts = _keyword_reference_counts(keyword.tag_id)
    references = _format_reference_counts(counts)
    if references:
        flash(f"Cannot delete keyword '{keyword.tag}' because it is used by {references}.", "warning")
        return redirect(url_for("main.keyword_maintenance", topic_id=industry_id, tab="keywords"))

    db.session.delete(keyword)
    db.session.commit()
    flash("Keyword deleted.", "success")
    return redirect(url_for("main.keyword_maintenance", topic_id=industry_id, tab="keywords"))


# ---------------------------------------------------------------------------
# Profile picture upload
# ---------------------------------------------------------------------------
USER_IMAGE_DIR = os.path.join("app", "static", "user_images")
USER_IMAGE_MAX_DIM = 300  # longest edge in pixels after resize

MEETING_GROUP_IMAGE_DIR = os.path.join("app", "static", "meeting_group_images")
MEETING_GROUP_IMAGE_SIZE = 300  # square PNG output: width and height in pixels
TNW_NO_GROUP_IMAGE_STATIC = "images/NoGroupImage.jpg"


def _meeting_group_image_fs_path(image_filename: str | None) -> str:
    """Absolute path to a stored group banner (basename only; matches ``url_for('static', ...)`` layout)."""
    safe = os.path.basename((image_filename or "").strip())
    if not safe:
        return ""
    if has_app_context():
        return os.path.join(
            current_app.root_path, "static", "meeting_group_images", safe
        )
    return os.path.abspath(os.path.join(MEETING_GROUP_IMAGE_DIR, safe))


def _resize_profile_image(file_storage, target_path):
    """Open the uploaded image, convert to RGB PNG, and resize so the longest
    edge is at most USER_IMAGE_MAX_DIM. Preserves aspect ratio and does NOT
    upscale small images. Raises UnidentifiedImageError if the file isn't a
    valid image.
    """
    data = file_storage.read()
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        elif img.mode == "RGBA":
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[3])
            img = background
        img.thumbnail((USER_IMAGE_MAX_DIM, USER_IMAGE_MAX_DIM), Image.LANCZOS)
        img.save(target_path, format="PNG", optimize=True)


def _configured_event_image_location() -> str:
    raw = (
        current_app.config.get("TNW_EVENT_IMAGE_LOCATION") or "event_images/1"
    ).strip().replace("\\", "/").strip("/")
    parts = [p for p in raw.split("/") if p]
    if not parts:
        return "event_images/1"
    for part in parts:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", part):
            return "event_images/1"
    return "/".join(parts)


def _event_image_dir_abs(location: str | None = None) -> str:
    loc = (location or _configured_event_image_location()).strip().replace("\\", "/").strip("/")
    rel = _safe_static_image_rel(loc, "x.png")
    if not rel:
        loc = "event_images/1"
    return os.path.join(current_app.root_path, "static", loc.replace("/", os.sep))


def ensure_event_image_upload_dir(location: str | None = None) -> str:
    """Create ``static/<TNW_EVENT_IMAGE_LOCATION>/`` (and parents) if it does not exist."""
    path = _event_image_dir_abs(location)
    os.makedirs(path, exist_ok=True)
    return path


def _meeting_event_image_abs_path(image_location: str | None, image_name: str | None) -> str | None:
    rel = _safe_static_image_rel(image_location, image_name)
    if not rel:
        return None
    base = os.path.abspath(os.path.join(current_app.root_path, "static"))
    target = os.path.abspath(os.path.join(base, rel.replace("/", os.sep)))
    try:
        if os.path.commonpath([base, target]) != base:
            return None
    except ValueError:
        return None
    return target


def _delete_meeting_event_image_file(meeting: Meeting) -> None:
    path = _meeting_event_image_abs_path(meeting.image_location, meeting.image_name)
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _apply_meeting_event_image_upload(meeting: Meeting, file_storage, user_id: int) -> None:
    loc = _configured_event_image_location()
    upload_dir = ensure_event_image_upload_dir(loc)
    new_name = f"ev_{user_id}_{int(meeting.meeting_id)}_{int(datetime.utcnow().timestamp())}.png"
    target_path = os.path.join(upload_dir, new_name)
    _resize_meeting_group_image(file_storage, target_path)
    old_loc = (meeting.image_location or "").strip()
    old_name = (meeting.image_name or "").strip()
    if old_name and old_loc:
        _delete_meeting_event_image_file(meeting)
    meeting.image_location = loc
    meeting.image_name = new_name


def _resize_meeting_group_image(file_storage, target_path):
    """Convert to RGB PNG, center-crop to a square, resize to MEETING_GROUP_IMAGE_SIZE."""
    data = file_storage.read()
    size = MEETING_GROUP_IMAGE_SIZE
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        elif img.mode == "RGBA":
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[3])
            img = background
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
        if img.size != (size, size):
            img = img.resize((size, size), Image.LANCZOS)
        img.save(target_path, format="PNG", optimize=True)


def _resize_meeting_group_image_from_path(src_path: str, target_path: str) -> None:
    """Same output rules as _resize_meeting_group_image, reading from a file path."""
    size = MEETING_GROUP_IMAGE_SIZE
    with Image.open(src_path) as img:
        img.load()
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        elif img.mode == "RGBA":
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[3])
            img = background
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
        if img.size != (size, size):
            img = img.resize((size, size), Image.LANCZOS)
        img.save(target_path, format="PNG", optimize=True)


def _admin_test_events_first_user_id_without_groups() -> int | None:
    """First seeded test user (tnw_tu*, by user_id) who owns no meeting groups yet."""
    te_users = (
        User.query.filter(User.username.like("tnw_tu%"))
        .order_by(User.user_id.asc())
        .all()
    )
    uids = [int(u.user_id) for u in te_users if u.user_id is not None]
    if not uids:
        return None
    rows = (
        db.session.query(
            MeetingGroup.user_id,
            func.count(MeetingGroup.meeting_group_id),
        )
        .filter(MeetingGroup.user_id.in_(uids))
        .group_by(MeetingGroup.user_id)
        .all()
    )
    group_counts = {int(uid): int(cnt or 0) for uid, cnt in rows}
    for u in te_users:
        uid = int(u.user_id)
        if group_counts.get(uid, 0) == 0:
            return uid
    return None


def _test_events_log(message: str) -> None:
    """Print and log for admin test-events tooling (watch the Flask terminal)."""
    line = f"[test-events] {message}"
    print(line, flush=True)
    try:
        current_app.logger.info("%s", line)
    except Exception:
        pass


def _admin_test_events_topic_and_tags(
    industry_id: int | None,
) -> tuple[str, list[str]]:
    """Topic label and tag strings for Gemini (bounded)."""
    topic = ""
    if industry_id:
        ind = Industry.query.get(int(industry_id))
        if ind:
            topic = (ind.industry or "").strip()
    q = Tag.query
    if industry_id:
        q = q.filter(Tag.industry_id == int(industry_id))
    rows = q.order_by(Tag.tag.asc()).limit(120).all()
    tags = sorted(
        {(t.tag or "").strip() for t in rows if (t.tag or "").strip()},
        key=lambda s: s.lower(),
    )
    return topic, tags


def _admin_test_events_tag_strings_for_industry(industry_id: int) -> list[str]:
    rows = (
        Tag.query.filter(Tag.industry_id == int(industry_id))
        .order_by(Tag.tag.asc())
        .limit(220)
        .all()
    )
    return sorted(
        {(t.tag or "").strip() for t in rows if (t.tag or "").strip()},
        key=lambda s: s.lower(),
    )


def _admin_test_events_topics_tags_for_prompt(
    restrict_industry_id: int | None,
) -> list[dict]:
    """Compact topic + tag lists for Gemini (bounded per topic)."""
    q = Industry.query.order_by(Industry.industry.asc(), Industry.industry_id)
    industries = q.all()
    if restrict_industry_id is not None:
        rid = int(restrict_industry_id)
        industries = [i for i in industries if int(i.industry_id) == rid]
    out: list[dict] = []
    for ind in industries:
        tags = _admin_test_events_tag_strings_for_industry(int(ind.industry_id))[:42]
        out.append(
            {
                "industry_id": int(ind.industry_id),
                "industry": (ind.industry or "").strip() or f"Topic {ind.industry_id}",
                "tags": tags,
            }
        )
    return out


@bp.route("/admin/test-events/suggest-from-description", methods=["POST"])
def admin_test_events_suggest_from_description():
    """Gemini: infer topic (industry) and keywords from plain description (admin test events)."""
    _test_events_log("suggest-from-description: request started")
    uid = session.get("user_id")
    if not uid:
        return jsonify(ok=False, error="Please sign in."), 401
    if not User.query.get(uid):
        session.pop("user_id", None)
        return jsonify(ok=False, error="Please sign in again."), 401
    if not _session_site_admin_user():
        return jsonify(ok=False, error="Only site administrators can use this."), 403

    payload = request.get_json(silent=True) or {}
    desc = (payload.get("description") or "").strip()[:8000]
    raw_pref = payload.get("industry_id")
    pref_iid: int | None
    try:
        pref_iid = int(raw_pref) if raw_pref not in (None, "") else None
    except (TypeError, ValueError):
        pref_iid = None
    if pref_iid is not None and not Industry.query.get(pref_iid):
        pref_iid = None

    if len(desc) < 40:
        _test_events_log("suggest-from-description: skipped (short text)")
        return jsonify(ok=True, skipped=True, industry_id=None, keywords=[], keywords_csv="")

    if pref_iid is not None:
        topics_for_prompt = _admin_test_events_topics_tags_for_prompt(pref_iid)
        if not topics_for_prompt:
            topics_for_prompt = _admin_test_events_topics_tags_for_prompt(None)
        system = (
            "You help UK business networking administrators tag event descriptions. "
            "Reply with a single JSON object only (no markdown). Keys: "
            '"keywords" (array of strings). Each keyword MUST appear exactly in the '
            "allowed_tags list for the given topic (same spelling). Pick 4–12 relevant tags. "
            "If several subsets fit, prefer a varied mix—not the same default cluster every time. "
            "If nothing fits, use an empty keywords array."
        )
        tags_json = json.dumps(topics_for_prompt[0]["tags"], ensure_ascii=False)
        user_msg = (
            f"Topic: {topics_for_prompt[0]['industry']} (industry_id={topics_for_prompt[0]['industry_id']}).\n"
            f"allowed_tags (JSON array): {tags_json}\n"
            f"Event description:\n---\n{desc}\n---\n"
            "Return JSON as specified."
        )
    else:
        topics_for_prompt = _admin_test_events_topics_tags_for_prompt(None)
        if not topics_for_prompt:
            return jsonify(ok=False, error="No industries or tags are configured."), 500
        topics_json = json.dumps(topics_for_prompt, ensure_ascii=False)
        allowed_ids = [t["industry_id"] for t in topics_for_prompt]
        system = (
            "You help UK business networking administrators classify event descriptions. "
            "Reply with a single JSON object only (no markdown). Keys: "
            '"industry_id" (integer; MUST be one of allowed_industry_ids), '
            '"keywords" (array of strings). Each keyword MUST appear exactly in the '
            "tags array for the chosen industry_id in topics (same spelling). "
            "Pick 4–12 of the most relevant tags for that topic. "
            "When multiple industries fit, choose a reasonable fit but vary keyword selection "
            "rather than always picking the same small set of tags."
        )
        user_msg = (
            f"allowed_industry_ids (JSON): {json.dumps(allowed_ids)}\n"
            f"topics (each has industry_id, industry label, tags): {topics_json}\n"
            f"Event description:\n---\n{desc}\n---\n"
            "Return JSON as specified."
        )

    user_msg += (
        f"\nFreshness nonce (vary interpretation slightly when text is similar to prior runs): "
        f"{secrets.token_hex(5)}\n"
    )
    _test_events_log("suggest-from-description: calling Gemini …")
    ok_ai, result = _gemini_generate_json_object(
        system=system,
        user_msg=user_msg,
        temperature=0.42,
        max_output_tokens=2048,
        timeout_s=60,
        use_json_mime=True,
    )
    if not ok_ai:
        _test_events_log(f"suggest-from-description: Gemini failed: {str(result)[:400]!r}")
        return jsonify(ok=False, error=str(result)[:2000]), 502
    if not isinstance(result, dict):
        return jsonify(ok=False, error="Unexpected AI response shape."), 502

    resolved_iid: int | None
    if pref_iid is not None:
        resolved_iid = int(topics_for_prompt[0]["industry_id"])
    else:
        try:
            raw_iid = result.get("industry_id")
            ai_iid = int(raw_iid) if raw_iid is not None else None
        except (TypeError, ValueError):
            ai_iid = None
        allowed_set = {t["industry_id"] for t in topics_for_prompt}
        resolved_iid = ai_iid if ai_iid in allowed_set else None
        if resolved_iid is None and allowed_set:
            resolved_iid = min(allowed_set)

    if resolved_iid is None:
        return jsonify(ok=False, error="Could not resolve a topic from the description."), 502

    allowed_full = _admin_test_events_tag_strings_for_industry(resolved_iid)
    allowed_norm = {t.lower(): t for t in allowed_full}
    raw_kw = result.get("keywords")
    keywords_out: list[str] = []
    if isinstance(raw_kw, list):
        for k in raw_kw:
            s = str(k).strip()
            if not s:
                continue
            canon = allowed_norm.get(s.lower())
            if canon and canon not in keywords_out:
                keywords_out.append(canon)

    _test_events_log(
        f"suggest-from-description: done industry_id={resolved_iid} n_kw={len(keywords_out)}"
    )
    return jsonify(
        ok=True,
        industry_id=resolved_iid,
        keywords=keywords_out,
        keywords_csv=", ".join(keywords_out),
    )


_TEST_EVENTS_CREATIVE_BRIEFS: tuple[str, ...] = (
    "Lead with a concrete scenario or member problem—not a generic welcome paragraph.",
    "Highlight one distinctive format (speed intros, sector tables, accountability pairs) and name it plainly.",
    "Emphasise outcomes (referrals, skills, accountability) with different vocabulary from typical meetups.",
    "Use a seasonal or local-economy hook tied to the topic; avoid 'vibrant community' clichés.",
    "Stress peer learning and curated matches; open mid-thought rather than 'We are delighted to…'.",
    "Frame the group as solving one narrow pain (time, trust, follow-up) for busy owners.",
    "Adopt a slightly more formal tone suitable for finance or professional services readers.",
    "Adopt a warmer, conversational tone suitable for creative or hospitality sectors.",
    "Mention one believable ritual (stand-up wins, two-minute teach) without inventing sponsors.",
    "Contrast this circle with large expos—intimate, repeat attendance, same faces month to month.",
    "Put the reader in the room: sounds, pace, and what they do in the first ten minutes.",
    "Close with who should not attend as well as who should—adds realism and variety.",
    "Structure as agenda bullets in prose (arrival, segment one, break, close) without sounding like a contract.",
    "Name one believable friction (no-shows, vague asks) and how the group keeps meetings useful anyway.",
    "Centre on a single keyword from the tag list and thread it through title and body differently than last time.",
    "Write shorter sentences and sharper verbs; avoid stacked abstract nouns (growth, synergy, leverage).",
    "Open with a question to the reader, then answer it with what actually happens at a session.",
    "Sound like a host wrote it by hand—slight asymmetry, one specific detail, not brochure polish.",
    "Emphasise post-meeting habits (notes, LinkedIn follow-ups) rather than only the room experience.",
    "Use a 'myth vs reality' contrast about networking for this industry, then position the group as the reality.",
)

# Appended per meeting when admin creates multiple test events so each meeting body differs, not only the title.
_TEST_EVENTS_MEETING_EDITION_SNIPPETS: tuple[str, ...] = (
    "This edition starts with thirty-second intros tied to one current goal each.",
    "This week spotlights breakout trios and a five-minute volunteer teach.",
    "Expect a walking one-to-one round and a shared doc for follow-up actions.",
    "Focus is warm referrals: who you need, who you can introduce, no hard pitches.",
    "We trial a silent agenda board—members vote live on which two topics get airtime.",
    "Guest-free session: only regulars, deeper trust, honest talk about pipeline and capacity.",
    "New-member friendly: paired buddies, printed name tents, and a slower first half.",
    "Speed rotations with a single prompt changed every round so conversations stay fresh.",
    "Accountability pairs check last month’s commitments before any new networking.",
    "Sector tables first, then cross-sector mixing so ideas don’t stay in silos.",
    "Short case clinic: two members bring a real dilemma; peers ask clarifying questions only.",
    "Celebration of small wins—referrals landed, hires made, lessons learned—before new asks.",
    "Workshop tone: one framework on the whiteboard, then apply it in pairs.",
    "Outdoor-adjacent energy even indoors: stand, stretch, sit, keep the pace brisk.",
    "Reverse intros: table states a need; individuals raise hand if they can help.",
    "Coffee-and-cards style: longer unstructured mingling with hosts nudging shy joiners.",
    "Evening slot: tighter runtime, takeaway food optional, respect for day-job fatigue.",
    "Morning slot: early start, no lengthy speeches, out on time for client work.",
)

_TEST_EVENTS_MEETING_TITLE_TAGS: tuple[str, ...] = (
    "Intros week",
    "Referrals focus",
    "Breakout trios",
    "Teach slot",
    "New members",
    "Accountability",
    "Sector tables",
    "Case clinic",
    "Speed rounds",
    "Wins debrief",
    "Pipeline honesty",
    "Follow-up habits",
    "Curated matches",
    "Deep trust session",
    "Cross-sector mix",
    "Volunteer teach",
    "Walking 1:1s",
    "Brisk pace",
)


def _admin_test_events_ai_variation_footer() -> str:
    """Randomised instructions so repeated test-event AI calls do not converge on the same copy."""
    brief = secrets.choice(_TEST_EVENTS_CREATIVE_BRIEFS)
    nonce = secrets.token_hex(6)
    return (
        "\nVariation requirement: group_name and description must read as a fresh draft, not a "
        "near-copy of common UK networking templates. Vary title rhythm, opening sentence structure, "
        "and what you emphasise (people vs format vs outcomes). Do not reuse the same metaphor "
        "twice across obvious boilerplate (e.g. defaulting every time to 'connect, collaborate, grow').\n"
        f"Creative brief (follow closely): {brief}\n"
        f"Freshness nonce (treat each nonce as a unique run): {nonce}\n"
    )


@bp.route("/admin/test-events/stage-group-image", methods=["POST"])
def admin_test_events_stage_group_image():
    """Resize and save a group image for test events (file picker or pasted image)."""
    _test_events_log("stage-group-image: request started")
    uid = session.get("user_id")
    if not uid:
        return jsonify(ok=False, error="Please sign in."), 401
    if not User.query.get(uid):
        session.pop("user_id", None)
        return jsonify(ok=False, error="Please sign in again."), 401
    if not _session_site_admin_user():
        return jsonify(ok=False, error="Only site administrators can use this."), 403
    up = request.files.get("image")
    if not up or not (up.filename or "").strip():
        return jsonify(
            ok=False,
            error="No image was received. Paste from the clipboard or choose a file.",
        ), 400

    mg_dir = os.path.join(current_app.root_path, "static", "meeting_group_images")
    os.makedirs(mg_dir, exist_ok=True)
    image_fn = f"mg_te_{int(time_mod.time())}_{secrets.token_hex(4)}.png"
    dest_abs = os.path.join(mg_dir, image_fn)

    try:
        _resize_meeting_group_image(up, dest_abs)
    except UnidentifiedImageError:
        return jsonify(
            ok=False,
            error="That upload does not look like a valid image. Try JPG, PNG, or WEBP.",
        ), 400
    except Exception:
        current_app.logger.exception("admin_test_events_stage_group_image")
        return jsonify(ok=False, error="Could not process the image file."), 500

    preview_rel = f"meeting_group_images/{image_fn}"
    preview_url = url_for("static", filename=preview_rel)
    _test_events_log(f"stage-group-image: done {image_fn}")
    return jsonify(ok=True, image_filename=image_fn, image_url=preview_url)


@bp.route("/admin/test-events/ai-prepare", methods=["POST"])
def admin_test_events_ai_prepare():
    """Gemini: draft event group name, description, keywords; stage PNG (upload or NoGroupImage)."""
    _test_events_log("ai-prepare: request started")
    uid = session.get("user_id")
    if not uid:
        return jsonify(ok=False, error="Please sign in."), 401
    if not User.query.get(uid):
        session.pop("user_id", None)
        return jsonify(ok=False, error="Please sign in again."), 401
    if not _session_site_admin_user():
        return jsonify(ok=False, error="Only site administrators can use this."), 403
    name_hint = (request.form.get("te_group_name") or "").strip()[:180]
    desc_seed = (request.form.get("te_group_description") or "").strip()[:8000]
    raw_iid = (request.form.get("te_industry_id") or "").strip()
    industry_id: int | None
    try:
        industry_id = int(raw_iid) if raw_iid else None
    except (TypeError, ValueError):
        industry_id = None
    meeting_format = (request.form.get("te_meeting_format") or "Face2Face").strip()
    if meeting_format not in ("Face2Face", "Virtual"):
        meeting_format = "Face2Face"

    form_industry_locked = bool(
        industry_id and Industry.query.get(int(industry_id))
    )
    topics_for_prompt = _admin_test_events_topics_tags_for_prompt(
        int(industry_id) if form_industry_locked else None
    )
    if not topics_for_prompt:
        topics_for_prompt = _admin_test_events_topics_tags_for_prompt(None)
    if not topics_for_prompt:
        return jsonify(ok=False, error="No industries or tags are configured."), 500

    if form_industry_locked:
        tid = int(industry_id)
        topic_row = topics_for_prompt[0]
        tags_json = json.dumps(topic_row["tags"], ensure_ascii=False)
        _test_events_log(
            f"ai-prepare: hints name={name_hint[:40]!r} industry_id={tid} (locked) "
            f"format={meeting_format} prompt_tag_count={len(topic_row['tags'])}"
        )
        system = (
            "You help UK business networking site administrators draft test event group content. "
            "Reply with a single JSON object only (no markdown fences). Keys: "
            '"group_name" (string, catchy but professional, max 120 characters), '
            '"description" (string, plain text, 2–4 short paragraphs; no HTML; no invented prices or legal promises), '
            f'"industry_id" (integer; MUST equal {tid}), '
            '"keywords" (array of strings). '
            "Every keyword MUST appear exactly in allowed_keywords (same spelling). "
            "Pick 4–12 relevant tags. If allowed_keywords is empty, use an empty keywords array. "
            "When the admin runs this repeatedly without new notes, still produce meaningfully different "
            "group_name and description each time—vary hooks, structure, and emphasis."
        )
        user_msg = (
            f"Topic: {topic_row['industry']} (industry_id={tid}).\n"
            f"Meeting format: {meeting_format} (Face2Face = in-person; Virtual = online).\n"
            f"allowed_keywords (JSON array): {tags_json}\n"
            f"Organiser working title / hint (may be empty): {name_hint or '(none)'}\n"
            f"Scratch notes from the admin (may be empty):\n---\n{desc_seed or '(none)'}\n---\n"
            "Return JSON as specified."
        )
        user_msg += _admin_test_events_ai_variation_footer()
    else:
        topics_json = json.dumps(topics_for_prompt, ensure_ascii=False)
        allowed_ids = [t["industry_id"] for t in topics_for_prompt]
        _test_events_log(
            f"ai-prepare: hints name={name_hint[:40]!r} industry_id=(from AI) "
            f"format={meeting_format} topic_count={len(topics_for_prompt)}"
        )
        system = (
            "You help UK business networking site administrators draft test event group content. "
            "Reply with a single JSON object only (no markdown fences). Keys: "
            '"group_name" (string, catchy but professional, max 120 characters), '
            '"description" (string, plain text, 2–4 short paragraphs; no HTML; no invented prices or legal promises), '
            '"industry_id" (integer; MUST be one of allowed_industry_ids), '
            '"keywords" (array of strings). '
            "Each keyword MUST appear exactly in the tags list for the chosen industry_id "
            "inside topics (same spelling). Pick 4–12 relevant tags. "
            "When the admin runs this repeatedly without new notes, still produce meaningfully different "
            "group_name and description each time—vary hooks, structure, and emphasis."
        )
        user_msg = (
            f"allowed_industry_ids (JSON): {json.dumps(allowed_ids)}\n"
            f"topics (each has industry_id, industry label, tags): {topics_json}\n"
            f"Meeting format: {meeting_format} (Face2Face = in-person; Virtual = online).\n"
            f"Organiser working title / hint (may be empty): {name_hint or '(none)'}\n"
            f"Scratch notes from the admin (may be empty):\n---\n{desc_seed or '(none)'}\n---\n"
            "Return JSON as specified."
        )
        user_msg += _admin_test_events_ai_variation_footer()
    _test_events_log("ai-prepare: calling Gemini (JSON) …")
    ok_ai, result = _gemini_generate_json_object(
        system=system,
        user_msg=user_msg,
        temperature=0.52,
        max_output_tokens=4096,
        timeout_s=90,
        use_json_mime=True,
    )
    if not ok_ai:
        _test_events_log(f"ai-prepare: Gemini failed: {str(result)[:500]!r}")
        return jsonify(ok=False, error=str(result)[:2000]), 502

    if not isinstance(result, dict):
        _test_events_log("ai-prepare: Gemini returned non-object JSON")
        return jsonify(ok=False, error="Unexpected AI response shape."), 502

    _test_events_log("ai-prepare: Gemini OK, parsing fields")
    group_name = (result.get("group_name") or name_hint or "Test networking group").strip()
    if len(group_name) > 180:
        group_name = group_name[:177] + "…"
    description = (result.get("description") or "").strip()
    if len(description) > 12000:
        description = description[:11997] + "…"

    if form_industry_locked:
        resolved_iid = int(industry_id)
    else:
        try:
            raw_ai_iid = result.get("industry_id")
            ai_iid = int(raw_ai_iid) if raw_ai_iid is not None else None
        except (TypeError, ValueError):
            ai_iid = None
        id_set = {t["industry_id"] for t in topics_for_prompt}
        resolved_iid = ai_iid if ai_iid in id_set else (min(id_set) if id_set else None)
    if resolved_iid is None:
        return jsonify(ok=False, error="Could not resolve topic from AI response."), 502

    allowed_full = _admin_test_events_tag_strings_for_industry(resolved_iid)
    allowed_norm = {t.lower(): t for t in allowed_full}
    raw_kw = result.get("keywords")
    keywords_out: list[str] = []
    if isinstance(raw_kw, list):
        for k in raw_kw:
            s = str(k).strip()
            if not s:
                continue
            canon = allowed_norm.get(s.lower())
            if canon and canon not in keywords_out:
                keywords_out.append(canon)

    skip_image = (request.form.get("te_skip_image") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if skip_image:
        _test_events_log(
            f"ai-prepare: skip_image=1 (text only) industry_id={resolved_iid} keywords={len(keywords_out)}"
        )
        return jsonify(
            ok=True,
            group_name=group_name,
            description=description,
            industry_id=resolved_iid,
            keywords=keywords_out,
            keywords_csv=", ".join(keywords_out),
            image_filename=None,
            image_url=None,
        )

    mg_dir = os.path.join(current_app.root_path, "static", "meeting_group_images")
    os.makedirs(mg_dir, exist_ok=True)
    image_fn = f"mg_te_{int(time_mod.time())}_{secrets.token_hex(4)}.png"
    dest_abs = os.path.join(mg_dir, image_fn)

    up = request.files.get("image")
    existing_staged = (request.form.get("te_staged_image_filename") or "").strip()
    mg_dir_norm = os.path.normpath(mg_dir)
    try:
        wrote = False
        if up and (up.filename or "").strip():
            _test_events_log(f"ai-prepare: resizing uploaded image -> {image_fn}")
            _resize_meeting_group_image(up, dest_abs)
            wrote = True
        elif existing_staged and _test_events_staged_image_ok(existing_staged):
            src_abs = os.path.normpath(os.path.join(mg_dir, os.path.basename(existing_staged)))
            if (
                os.path.normpath(src_abs).startswith(mg_dir_norm)
                and os.path.isfile(src_abs)
            ):
                _test_events_log(
                    f"ai-prepare: reusing staged image {existing_staged!r} -> {image_fn}"
                )
                _resize_meeting_group_image_from_path(src_abs, dest_abs)
                wrote = True
        if not wrote:
            src_abs = os.path.normpath(
                os.path.join(current_app.root_path, "static", TNW_NO_GROUP_IMAGE_STATIC)
            )
            if not os.path.isfile(src_abs):
                _test_events_log(f"ai-prepare: missing default image at {src_abs}")
                return jsonify(
                    ok=False,
                    error="Default image app/static/images/NoGroupImage.jpg was not found on the server.",
                ), 500
            _test_events_log(f"ai-prepare: copying default NoGroupImage -> {image_fn}")
            _resize_meeting_group_image_from_path(src_abs, dest_abs)
    except UnidentifiedImageError:
        return jsonify(
            ok=False,
            error="That upload does not look like a valid image. Try JPG, PNG, or WEBP.",
        ), 400
    except Exception:
        current_app.logger.exception("admin_test_events_ai_prepare image")
        return jsonify(ok=False, error="Could not process the image file."), 500

    preview_rel = f"meeting_group_images/{image_fn}"
    preview_url = url_for("static", filename=preview_rel)
    _test_events_log(
        f"ai-prepare: done image={image_fn} industry_id={resolved_iid} keywords={len(keywords_out)}"
    )

    return jsonify(
        ok=True,
        group_name=group_name,
        description=description,
        industry_id=resolved_iid,
        keywords=keywords_out,
        keywords_csv=", ".join(keywords_out),
        image_filename=image_fn,
        image_url=preview_url,
    )


def _test_events_staged_image_ok(filename: str) -> bool:
    if not filename or len(filename) > 200:
        return False
    return bool(re.match(r"^mg_te_[A-Za-z0-9_]+\.png$", filename))


def _test_events_stage_default_group_png() -> str:
    """Copy ``NoGroupImage.jpg`` to meeting_group_images as a new mg_te_*.png."""
    mg_dir = os.path.join(current_app.root_path, "static", "meeting_group_images")
    os.makedirs(mg_dir, exist_ok=True)
    image_fn = f"mg_te_{int(time_mod.time())}_{secrets.token_hex(4)}.png"
    dest_abs = os.path.join(mg_dir, image_fn)
    src_abs = os.path.normpath(
        os.path.join(current_app.root_path, "static", TNW_NO_GROUP_IMAGE_STATIC)
    )
    if not os.path.isfile(src_abs):
        raise FileNotFoundError(src_abs)
    _resize_meeting_group_image_from_path(src_abs, dest_abs)
    return image_fn


def _te_test_events_sale_window(
    starts_at: datetime,
    duration_minutes: int,
    now_naive: datetime,
) -> tuple[datetime, datetime]:
    """Return (sales_open_at, sales_close_at) satisfying CK + _first_active_sale_ticket (on sale now when possible)."""
    dur_m = max(1, int(duration_minutes or 60))
    meeting_ends_at = starts_at + timedelta(minutes=dur_m)
    sales_close_at = meeting_ends_at
    sales_open_at = starts_at - timedelta(days=7)
    if sales_open_at >= sales_close_at:
        sales_open_at = sales_close_at - timedelta(hours=2)
    if sales_open_at >= sales_close_at:
        sales_open_at = sales_close_at - timedelta(minutes=1)
    if sales_open_at > now_naive:
        sales_open_at = now_naive - timedelta(minutes=15)
    if sales_open_at >= sales_close_at:
        sales_open_at = sales_close_at - timedelta(hours=2)
    if sales_open_at >= sales_close_at:
        sales_open_at = sales_close_at - timedelta(minutes=1)
    return sales_open_at, sales_close_at


@bp.route("/admin/test-events/create", methods=["POST"])
def admin_test_events_create():
    """Create a meeting group plus N live meetings with Active ticket tiers (dev / test users)."""
    _test_events_log("create: request started")
    uid = session.get("user_id")
    if not uid:
        return jsonify(ok=False, error="Please sign in."), 401
    if not User.query.get(uid):
        session.pop("user_id", None)
        return jsonify(ok=False, error="Please sign in again."), 401
    if not _session_site_admin_user():
        return jsonify(ok=False, error="Only site administrators can use this."), 403
    payload = request.get_json(silent=True) or {}
    try:
        owner_id = int(payload.get("te_user_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Choose a valid test user."), 400
    try:
        industry_id = int(payload.get("te_industry_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Choose a topic (industry) for the group."), 400

    owner = User.query.get(owner_id)
    if not owner or not (owner.username or "").lower().startswith("tnw_tu"):
        return jsonify(ok=False, error="Owner must be a seeded test user (tnw_tu*)."), 400
    if not Industry.query.get(industry_id):
        return jsonify(ok=False, error="That topic was not found."), 400

    group_name = (payload.get("te_group_name") or "").strip()
    if not group_name or len(group_name) > 180:
        return jsonify(ok=False, error="Event group name is required (max 180 characters)."), 400

    desc_plain = (payload.get("te_group_description") or "").strip()
    if not _rich_text_plain_text(desc_plain):
        return jsonify(ok=False, error="Description is required."), 400
    subject_html = _sanitize_rich_text_html(desc_plain)

    meeting_format = (payload.get("te_meeting_format") or "Face2Face").strip()
    if meeting_format not in ("Face2Face", "Virtual"):
        meeting_format = "Face2Face"

    if meeting_format == "Virtual":
        vplat = (payload.get("te_virtual_platform") or "").strip()
        vlink = (payload.get("te_virtual_link") or "").strip()
        if not vplat or not vlink:
            return jsonify(
                ok=False,
                error="Virtual events need a platform name and meeting link.",
            ), 400
    else:
        vplat = ""
        vlink = ""

    try:
        n_events = int(payload.get("te_event_count", 1))
    except (TypeError, ValueError):
        n_events = 0
    if n_events < 1 or n_events > 52:
        return jsonify(ok=False, error="Number of events must be between 1 and 52."), 400

    first_date_raw = (payload.get("te_first_event_date") or "").strip()
    try:
        first_d = date.fromisoformat(first_date_raw)
    except ValueError:
        return jsonify(ok=False, error="First event date is not valid."), 400
    today_utc = datetime.now(timezone.utc).date()
    if first_d < today_utc:
        return jsonify(ok=False, error="First event date must be today or in the future."), 400

    try:
        duration_minutes = int(payload.get("te_duration_minutes", 60))
    except (TypeError, ValueError):
        duration_minutes = 60
    if duration_minutes < 15 or duration_minutes > 480:
        return jsonify(ok=False, error="Duration must be between 15 and 480 minutes."), 400

    ticket_name = (payload.get("te_ticket_name") or "General admission").strip()[:100]
    try:
        price_amount = Decimal(str(payload.get("te_ticket_price", 0)))
    except (InvalidOperation, TypeError, ValueError):
        return jsonify(ok=False, error="Ticket price is not valid."), 400
    if price_amount < 0:
        return jsonify(ok=False, error="Ticket price cannot be negative."), 400
    if price_amount > 20:
        return jsonify(ok=False, error="Test ticket price must be between 0 and 20 GBP."), 400
    try:
        max_qty = int(payload.get("te_ticket_max_qty", 50))
    except (TypeError, ValueError):
        max_qty = 0
    if max_qty < 1:
        return jsonify(ok=False, error="Max tickets must be at least 1."), 400

    image_fn = (payload.get("te_staged_image_filename") or "").strip()
    mg_dir = os.path.join(current_app.root_path, "static", "meeting_group_images")
    if not image_fn or not _test_events_staged_image_ok(image_fn):
        _test_events_log("create: no valid staged image; staging default NoGroupImage PNG")
        try:
            image_fn = _test_events_stage_default_group_png()
        except FileNotFoundError as e:
            _test_events_log(f"create: default image missing {e}")
            return jsonify(
                ok=False,
                error="No staged image and default NoGroupImage.jpg was not found.",
            ), 500
        except Exception:
            current_app.logger.exception("test-events default image")
            return jsonify(ok=False, error="Could not create a default group image file."), 500
    else:
        bn = os.path.basename(image_fn)
        if bn != image_fn or ".." in image_fn:
            return jsonify(ok=False, error="Staged image filename is invalid."), 400
        abs_img = os.path.join(mg_dir, bn)
        mg_dir_norm = os.path.normpath(mg_dir)
        if not os.path.normpath(abs_img).startswith(mg_dir_norm) or not os.path.isfile(
            abs_img
        ):
            return jsonify(ok=False, error="Staged image file is missing or invalid."), 400

    keywords_raw = (payload.get("te_keywords") or "").strip()
    tag_parts = [p.strip() for p in re.split(r"[,;]", keywords_raw) if p.strip()]
    tag_objs: list[Tag] = []
    for nm in tag_parts:
        t = (
            Tag.query.filter(
                Tag.industry_id == industry_id,
                func.lower(Tag.tag) == nm.lower(),
            ).first()
        )
        if t and t not in tag_objs:
            tag_objs.append(t)

    _test_events_log(
        f"create: owner={owner.username!r} group={group_name!r} n_events={n_events} "
        f"first_date={first_d.isoformat()} format={meeting_format} image={image_fn}"
    )

    la = lo = None
    if meeting_format == "Face2Face":
        la = owner.latitude
        lo = owner.longitude
        if la is None or lo is None:
            return jsonify(
                ok=False,
                error="Face-to-face test events need the owner’s latitude and longitude on their profile.",
            ), 400

    try:
        mg = MeetingGroup(
            user_id=owner.user_id,
            meeting_group_name=group_name,
            description=subject_html or None,
            created_at=datetime.utcnow(),
            image_filename=image_fn,
            meeting_format=meeting_format,
            industry_id=industry_id,
        )
        db.session.add(mg)
        db.session.flush()
        mg.tags = tag_objs
        _test_events_log(f"create: meeting_group_id={mg.meeting_group_id}")

        first_start = datetime.combine(first_d, time(10, 0, 0))
        now_naive = datetime.utcnow()
        min_live_start = now_naive + timedelta(hours=2)
        if first_start < min_live_start:
            first_start = min_live_start
        created_meeting_ids: list[int] = []

        # Placeholders satisfy Face2Face format checks (venue/address required).
        te_f2f_city = "Birmingham"
        te_f2f_pc = "B1 1AA"
        te_f2f_country = "United Kingdom"

        if n_events <= 1:
            per_meeting_subject_html: list[str] = [subject_html]
        else:
            per_meeting_subject_html = []
            n_snip = len(_TEST_EVENTS_MEETING_EDITION_SNIPPETS)
            for mi in range(n_events):
                snip = _TEST_EVENTS_MEETING_EDITION_SNIPPETS[mi % n_snip]
                combined_plain = (
                    desc_plain.rstrip()
                    + "\n\n"
                    + f"This is instalment {mi + 1} of {n_events}. "
                    + snip
                ).strip()
                per_meeting_subject_html.append(_sanitize_rich_text_html(combined_plain))

        for i in range(n_events):
            starts_at = first_start + timedelta(weeks=i)
            base_title = f"{group_name} ({i + 1}/{n_events})"
            if n_events > 1:
                tag = _TEST_EVENTS_MEETING_TITLE_TAGS[
                    i % len(_TEST_EVENTS_MEETING_TITLE_TAGS)
                ]
                title = f"{base_title} · {tag}"[:180]
            else:
                title = base_title[:180]
            meeting_subject_html = per_meeting_subject_html[i]
            sales_open_at, sales_close_at = _te_test_events_sale_window(
                starts_at, duration_minutes, now_naive
            )

            if meeting_format == "Face2Face":
                m = Meeting(
                    meeting_group_id=mg.meeting_group_id,
                    creator_user_id=owner.user_id,
                    title=title,
                    subject=meeting_subject_html,
                    starts_at=starts_at,
                    meeting_format=meeting_format,
                    duration_minutes=duration_minutes,
                    location_city=te_f2f_city,
                    location_postcode=te_f2f_pc,
                    location_country=te_f2f_country,
                    venue_name="Test networking venue (admin)",
                    address_line1="1 Chamberlain Square",
                    address_line2=None,
                    address_town=te_f2f_city,
                    address_county="West Midlands",
                    address_postcode=te_f2f_pc,
                    address_country=te_f2f_country,
                    latitude=la,
                    longitude=lo,
                    virtual_platform=None,
                    virtual_link=None,
                    is_paid_and_published=True,
                    status="Live",
                    created_at=datetime.utcnow(),
                )
            else:
                m = Meeting(
                    meeting_group_id=mg.meeting_group_id,
                    creator_user_id=owner.user_id,
                    title=title,
                    subject=meeting_subject_html,
                    starts_at=starts_at,
                    meeting_format=meeting_format,
                    duration_minutes=duration_minutes,
                    location_city=None,
                    location_postcode=None,
                    location_country=None,
                    venue_name=None,
                    address_line1=None,
                    address_line2=None,
                    address_town=None,
                    address_county=None,
                    address_postcode=None,
                    address_country=None,
                    latitude=None,
                    longitude=None,
                    virtual_platform=vplat or None,
                    virtual_link=vlink or None,
                    is_paid_and_published=True,
                    status="Live",
                    created_at=datetime.utcnow(),
                )
            db.session.add(m)
            db.session.flush()
            created_meeting_ids.append(int(m.meeting_id))
            _test_events_log(
                f"create: meeting_id={m.meeting_id} starts_at={starts_at.isoformat()} status=Live"
            )

            max_per_user = min(max_qty, 20)
            tt = MeetingTicketType(
                meeting_id=m.meeting_id,
                ticket_name=ticket_name,
                ticket_description=None,
                currency_code="GBP",
                price_amount=price_amount,
                max_quantity=max_qty,
                max_tickets_per_user=max_per_user,
                sales_open_at=sales_open_at,
                sales_close_at=sales_close_at,
                vat_rate_percent=Decimal("0"),
                refund_policy=None,
                ticket_notes=None,
                status="Active",
                sort_order=0,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.session.add(tt)

        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("admin_test_events_create")
        _test_events_log("create: database error (rolled back)")
        return jsonify(
            ok=False,
            error="Database error while creating the test group or events. See server log.",
        ), 500

    msg = (
        f"Created event group “{group_name}” (id {mg.meeting_group_id}) with {n_events} live "
        f"meeting(s) and Active GBP tickets on sale."
    )
    _test_events_log(f"create: committed — {msg}")
    ind_row = Industry.query.get(industry_id)
    industry_name = (ind_row.industry or "").strip() if ind_row else ""
    suggested_te_user_id = _admin_test_events_first_user_id_without_groups()
    return jsonify(
        ok=True,
        meeting_group_id=mg.meeting_group_id,
        meetings_created=n_events,
        meeting_ids=created_meeting_ids,
        owner_user_id=owner.user_id,
        owner_username=(owner.username or "").strip(),
        group_name=group_name,
        meeting_format=meeting_format,
        first_event_date=first_d.isoformat(),
        industry_id=industry_id,
        industry_name=industry_name,
        duration_minutes=duration_minutes,
        ticket_name=ticket_name,
        ticket_price=str(price_amount),
        ticket_max_qty=max_qty,
        message=msg,
        suggested_te_user_id=suggested_te_user_id,
    )


@bp.route("/profile/image", methods=["POST"])
@login_required
def profile_image():
    user = User.query.get(session["user_id"])
    if not user:
        session.pop("user_id", None)
        return redirect(url_for("main.login"))

    file = request.files.get("image")
    if not file or not file.filename:
        flash("Please choose an image file to upload.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionDetails"))

    os.makedirs(USER_IMAGE_DIR, exist_ok=True)
    new_name = f"user_{user.user_id}_{int(datetime.utcnow().timestamp())}.png"
    target_path = os.path.join(USER_IMAGE_DIR, new_name)

    try:
        _resize_profile_image(file, target_path)
    except UnidentifiedImageError:
        flash("That file doesn't look like a valid image. Try a JPG, PNG, or WEBP.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionDetails"))
    except Exception:
        flash("Something went wrong saving your photo. Please try again.", "danger")
        return redirect(url_for("main.profile", _anchor="sectionDetails"))

    # Remove the old image file (if any) now that the new one is on disk.
    old = (user.image_name or "").strip()
    if old:
        old_path = os.path.join(USER_IMAGE_DIR, old)
        if os.path.isfile(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass

    user.image_name = new_name
    db.session.commit()

    flash("Profile photo updated.", "success")
    return redirect(url_for("main.profile", _anchor="sectionDetails"))


@bp.app_errorhandler(RequestEntityTooLarge)
def _too_large(_err):
    flash("That file is too big. Please upload an image under 10 MB.", "danger")
    return redirect(url_for("main.profile", _anchor="sectionDetails"))


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    return _logout_redirect_response()


# ---------------------------------------------------------------------------
# Marketing pages (static)
# ---------------------------------------------------------------------------
@bp.route("/about")
def about():
    return render_template("about.html")


@bp.route("/faq")
def faq():
    now_utc = datetime.utcnow()
    _, ref_lat, ref_lng, user_tag_ids, logged_in = _home_events_near_and_geo_for_carousels()
    home_featured_meetings, home_more_meetings = _home_carousel_meeting_lists(
        now_utc,
        ref_lat=ref_lat,
        ref_lng=ref_lng,
        user_tag_ids=user_tag_ids if logged_in else None,
        logged_in=logged_in,
    )
    _hydrate_home_meeting_cards(
        list(home_featured_meetings) + list(home_more_meetings), now_utc
    )
    return render_template(
        "faq.html",
        home_featured_meetings=home_featured_meetings,
        home_more_meetings=home_more_meetings,
    )


@bp.route("/terms")
def terms():
    return render_template("terms.html")


@bp.route("/faq/chat", methods=["POST"])
def faq_chat():
    payload = request.get_json(silent=True) or {}
    message = payload.get("message", "")
    answer = get_faq_bot_answer(message)
    return jsonify({"answer": answer})


@bp.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        sender_email = request.form.get("email", "").strip()
        phone_country_code = request.form.get("phone_country_code", "+44").strip()
        phone_number = request.form.get("phone", "").strip()
        message_text = request.form.get("message", "").strip()
        gdpr_consent = request.form.get("gdpr_consent") == "yes"
        sender_name = " ".join(part for part in [first_name, last_name] if part).strip()
        full_phone_number = f"{phone_country_code} {phone_number}".strip()

        if not first_name or not last_name or not sender_email or not message_text:
            flash("Please complete all contact fields.", "danger")
            return render_template("contact.html")

        if not gdpr_consent:
            flash("Please confirm the GDPR consent checkbox before sending.", "danger")
            return render_template("contact.html")

        smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        smtp_user = os.getenv("SMTP_USER", "")
        smtp_password = os.getenv("SMTP_PASSWORD", "")
        contact_to = os.getenv("CONTACT_TO_EMAIL", smtp_user)
        site_name = os.getenv("SITE_NAME", "The Networker")
        site_url = os.getenv("SITE_URL", "https://the-networker.co.uk")
        support_email = os.getenv("SUPPORT_EMAIL", contact_to)
        unsubscribe_email = os.getenv("CONTACT_UNSUBSCRIBE_EMAIL", support_email)

        if not smtp_user or not smtp_password or not contact_to:
            flash(
                "Contact email is not configured yet. Please set SMTP_USER, SMTP_PASSWORD and CONTACT_TO_EMAIL in .env.",
                "danger",
            )
            return render_template("contact.html")

        first_name_safe = escape(first_name)
        last_name_safe = escape(last_name)
        sender_name_safe = escape(sender_name)
        sender_email_safe = escape(sender_email)
        phone_country_code_safe = escape(phone_country_code)
        phone_number_safe = escape(phone_number)
        full_phone_number_safe = escape(full_phone_number)
        message_html_safe = escape(message_text).replace("\n", "<br>")

        email_message = EmailMessage()
        email_message["Subject"] = f"[{site_name}] New contact enquiry from {sender_name}"
        email_message["From"] = smtp_user
        email_message["To"] = contact_to
        email_message["Reply-To"] = sender_email
        email_message["Date"] = formatdate(localtime=True)
        email_message["Message-ID"] = make_msgid(domain=smtp_user.split("@")[-1])
        email_message["X-Auto-Response-Suppress"] = "All"
        email_message["X-Mailer"] = f"{site_name} Contact Form"
        email_message.set_content(
            (
                f"New contact form submission on {site_name}\n\n"
                f"First name: {first_name}\n"
                f"Last name: {last_name}\n"
                f"Full name: {sender_name}\n"
                f"Email: {sender_email}\n"
                f"Phone country code: {phone_country_code}\n"
                f"Phone: {phone_number}\n"
                f"Full phone: {full_phone_number}\n"
                f"GDPR consent: {'Yes' if gdpr_consent else 'No'}\n\n"
                f"Message:\n{message_text}\n"
                "\n---\n"
                f"Website: {site_url}\n"
                f"Support: {support_email}\n"
            )
        )
        email_message.add_alternative(
            (
                "<html><body style=\"font-family:Arial,Helvetica,sans-serif;color:#2f1f3a;\">"
                f"<h2 style=\"color:#5b2d73;\">New contact enquiry - {site_name}</h2>"
                f"<p><strong>First name:</strong> {first_name_safe}<br>"
                f"<strong>Last name:</strong> {last_name_safe}<br>"
                f"<strong>Full name:</strong> {sender_name_safe}<br>"
                f"<strong>Email:</strong> {sender_email_safe}<br>"
                f"<strong>Phone country code:</strong> {phone_country_code_safe}<br>"
                f"<strong>Phone:</strong> {phone_number_safe}<br>"
                f"<strong>Full phone:</strong> {full_phone_number_safe}<br>"
                f"<strong>GDPR consent:</strong> {'Yes' if gdpr_consent else 'No'}</p>"
                "<p><strong>Message</strong><br>"
                f"{message_html_safe}</p>"
                "<hr style=\"border:none;border-top:1px solid #e6dcec;\">"
                f"<p style=\"font-size:12px;color:#6b5c75;\">Website: {escape(site_url)}<br>"
                f"Support: {escape(support_email)}</p>"
                "</body></html>"
            ),
            subtype="html",
        )

        confirmation_message = EmailMessage()
        confirmation_message["Subject"] = f"Thanks for contacting {site_name}"
        confirmation_message["From"] = smtp_user
        confirmation_message["To"] = sender_email
        confirmation_message["Date"] = formatdate(localtime=True)
        confirmation_message["Message-ID"] = make_msgid(domain=smtp_user.split("@")[-1])
        confirmation_message["X-Auto-Response-Suppress"] = "OOF, AutoReply"
        confirmation_message["List-Unsubscribe"] = f"<mailto:{unsubscribe_email}?subject=unsubscribe>"
        confirmation_message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        confirmation_message.set_content(
            (
                f"Hi {first_name},\n\n"
                f"Thanks for contacting {site_name}. We have received your message and will get back to you shortly.\n\n"
                "For reference, here is a copy of what you sent:\n\n"
                f"First name: {first_name}\n"
                f"Last name: {last_name}\n"
                f"Email: {sender_email}\n"
                f"Phone country code: {phone_country_code}\n"
                f"Phone: {phone_number}\n"
                f"Full phone: {full_phone_number}\n\n"
                f"{message_text}\n\n"
                "---\n"
                f"Best regards,\n{site_name} Team\n\n"
                f"Website: {site_url}\n"
                f"Support: {support_email}"
            )
        )
        confirmation_message.add_alternative(
            (
                "<html><body style=\"font-family:Arial,Helvetica,sans-serif;color:#2f1f3a;\">"
                f"<h2 style=\"color:#5b2d73;\">Thanks for contacting {escape(site_name)}</h2>"
                "<p>We have received your message and will get back to you shortly.</p>"
                f"<p><strong>First name:</strong> {first_name_safe}<br>"
                f"<strong>Last name:</strong> {last_name_safe}<br>"
                f"<strong>Email:</strong> {sender_email_safe}<br>"
                f"<strong>Phone country code:</strong> {phone_country_code_safe}<br>"
                f"<strong>Phone:</strong> {phone_number_safe}<br>"
                f"<strong>Full phone:</strong> {full_phone_number_safe}</p>"
                "<p><strong>Your message</strong><br>"
                f"{message_html_safe}</p>"
                "<hr style=\"border:none;border-top:1px solid #e6dcec;\">"
                f"<p style=\"font-size:12px;color:#6b5c75;\">"
                f"Best regards,<br>{escape(site_name)} Team<br>"
                f"Website: {escape(site_url)}<br>"
                f"Support: {escape(support_email)}</p>"
                "</body></html>"
            ),
            subtype="html",
        )

        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as smtp:
                smtp.starttls()
                smtp.login(smtp_user, smtp_password)
                smtp.send_message(email_message)
                smtp.send_message(confirmation_message)
            flash("Thanks for contacting us. We have received your message.", "success")
            return redirect(url_for("main.contact"))
        except Exception:
            flash(
                "Message could not be sent right now. Check SMTP credentials or try again.",
                "danger",
            )
            return render_template("contact.html")

    return render_template("contact.html")


def _search_like_term(raw):
    """Strip characters that would act as wildcards in SQL LIKE patterns."""
    return (raw or "").replace("%", "").replace("_", "").strip()[:180]


def _clean_location_hint(value):
    return " ".join((value or "").strip().split())[:120]


def _normalize_postcode(value):
    txt = " ".join((value or "").strip().upper().split())
    return txt[:20]


def _lookup_latlng_for_postcode(postcode):
    pc = _normalize_postcode(postcode)
    if not pc:
        return None
    try:
        req = urllib.request.Request(
            f"{_postcodes_io_api_base()}/postcodes/{quote(pc)}",
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("status") != 200:
            return None
        result = data.get("result") or {}
        lat = result.get("latitude")
        lng = result.get("longitude")
        if lat is None or lng is None:
            return None
        return float(lat), float(lng)
    except Exception:
        return None


def _lookup_latlng_for_location_query(raw_query):
    query = _clean_location_hint(raw_query)
    if not query:
        return None, ""
    # First try UK postcode lookup (fast + precise).
    by_postcode = _lookup_latlng_for_postcode(query)
    if by_postcode:
        return by_postcode, _normalize_postcode(query)
    # Fallback: geocode city/location text (UK-biased).
    try:
        url = (
            "https://nominatim.openstreetmap.org/search?"
            + "format=jsonv2&limit=1&countrycodes=gb&q="
            + quote(query)
        )
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "TheNetworkerDev/1.0 (search geocoder)",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, list) or not data:
            return None, query
        hit = data[0] or {}
        lat = _numeric_or_none(hit.get("lat"))
        lng = _numeric_or_none(hit.get("lon"))
        if lat is None or lng is None:
            return None, query
        name = _clean_location_hint(hit.get("name") or "")
        if name and len(name) <= 96:
            pretty = name
        else:
            dn = (hit.get("display_name") or "").split(",")
            pretty = _clean_location_hint((dn[0] if dn else "") or query)[:96]
        return (float(lat), float(lng)), pretty
    except Exception:
        return None, query


def _lookup_postcode_for_latlng(lat, lng):
    try:
        req = urllib.request.Request(
            f"{_postcodes_io_api_base()}/postcodes?lon={lng}&lat={lat}&limit=1",
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("status") != 200:
            return None
        result = (data.get("result") or [{}])[0] or {}
        postcode = _normalize_postcode(result.get("postcode") or "")
        return postcode or None
    except Exception:
        return None


def _reverse_short_place_label(lat, lng):
    """Short human label (town / postcode) from coordinates; avoids long forward-geocode strings."""
    try:
        url = (
            "https://nominatim.openstreetmap.org/reverse?"
            f"lat={float(lat)}&lon={float(lng)}&format=jsonv2&addressdetails=1&zoom=12"
        )
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "TheNetworkerDev/1.0 (search reverse geocoder)",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        addr = data.get("address") or {}
        for key in ("city", "town", "village", "hamlet", "suburb", "locality"):
            v = addr.get(key)
            if v:
                return _clean_location_hint(str(v))[:80]
        pc = addr.get("postcode")
        if pc:
            return _normalize_postcode(str(pc)) or None
        name = data.get("name")
        if name:
            return _clean_location_hint(str(name))[:80]
    except Exception:
        pass
    return None


def _haversine_miles(lat1, lng1, lat2, lng2):
    r_miles = 3958.7613
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * (math.sin(dl / 2) ** 2)
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r_miles * c


class _ListPagination:
    def __init__(self, items, page, per_page):
        self.total = len(items)
        self.page = page
        self.per_page = per_page
        self.pages = max(1, math.ceil(self.total / per_page)) if self.total else 0
        self.has_prev = page > 1
        self.has_next = page < self.pages
        self.prev_num = page - 1
        self.next_num = page + 1
        start = (page - 1) * per_page
        end = start + per_page
        self.items = items[start:end]

    def iter_pages(
        self, left_edge=1, left_current=2, right_current=2, right_edge=1
    ):
        last = 0
        if self.pages <= 0:
            return
        for num in range(1, self.pages + 1):
            if (
                num <= left_edge
                or (self.page - left_current - 1 < num < self.page + right_current)
                or num > self.pages - right_edge
            ):
                if last + 1 != num:
                    yield None
                yield num
                last = num


def _search_seed_location():
    uid = session.get("user_id")
    if uid:
        user = User.query.get(uid)
        if user and user.latitude is not None and user.longitude is not None:
            lat = float(user.latitude)
            lng = float(user.longitude)
            postcode = _lookup_postcode_for_latlng(lat, lng)
            if postcode:
                return postcode, lat, lng
            short = _reverse_short_place_label(lat, lng)
            if short:
                return short, lat, lng
            return "", lat, lng
    forwarded_for = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    client_ip = forwarded_for or (request.remote_addr or "").strip()
    try:
        req = urllib.request.Request(
            f"https://ipapi.co/{client_ip}/json/",
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        hint = _clean_location_hint(data.get("postal") or "")
        if not hint:
            hint = _clean_location_hint(
                data.get("city") or data.get("region") or data.get("country_name") or ""
            )
        if not hint:
            return "", None, None
        lat = _numeric_or_none(data.get("latitude"))
        lng = _numeric_or_none(data.get("longitude"))
        return hint, lat, lng
    except Exception:
        return "", None, None


def _search_location_label_from_latlng(lat, lng):
    """Short UK postcode or place name for a coordinate pair (search autofill)."""
    la = float(lat)
    ln = float(lng)
    pc = _lookup_postcode_for_latlng(la, ln)
    if pc:
        return pc
    short = _reverse_short_place_label(la, ln)
    if short:
        return short
    return f"{la:.4f}, {ln:.4f}"


@bp.route("/api/search/reverse-geocode")
def api_search_reverse_geocode():
    """Resolve browser GPS coordinates to a postcode or short place label."""
    lat = request.args.get("lat", type=float)
    lng = request.args.get("lng", type=float)
    if lat is None or lng is None:
        return jsonify(ok=False, error="Missing coordinates."), 400
    if lat < -90 or lat > 90 or lng < -180 or lng > 180:
        return jsonify(ok=False, error="Invalid coordinates."), 400
    try:
        label = _search_location_label_from_latlng(lat, lng)
    except Exception:
        return jsonify(ok=False, error="Could not resolve that location."), 502
    if not label:
        return jsonify(ok=False, error="Could not resolve that location."), 502
    return jsonify(ok=True, label=label)


@bp.route("/api/search/location-hint")
def api_search_location_hint():
    """Profile coordinates when signed in, otherwise approximate IP-based hint."""
    hint, la, ln = _search_seed_location()
    if la is None or ln is None:
        return jsonify(ok=False, error="No location hint available."), 404
    label = (hint or "").strip() or _search_location_label_from_latlng(la, ln)
    if not label:
        return jsonify(ok=False, error="No location hint available."), 404
    src = "profile" if session.get("user_id") else "ip"
    return jsonify(ok=True, label=label, source=src)


def _merge_meeting_group_keywords_into_user_attendee_tags(user, meeting):
    """Copy tags from the meeting's group onto user_attendee_tags (no duplicates)."""
    mg = getattr(meeting, "meeting_group", None)
    if not mg:
        return
    tags = getattr(mg, "tags", None) or []
    if not tags:
        return
    have = {t.tag_id for t in (user.attendee_tags or [])}
    for tag in tags:
        if tag.tag_id not in have:
            user.attendee_tags.append(tag)
            have.add(tag.tag_id)


@bp.route("/api/saved-meetings", methods=["POST"])
def api_saved_meetings_create():
    """Save a Live meeting for the signed-in user (bookmark)."""
    uid = session.get("user_id")
    if not uid:
        return (
            jsonify(
                ok=False,
                need_auth=True,
                register_url=url_for("main.register"),
            ),
            401,
        )
    payload = request.get_json(silent=True) or {}
    mid = payload.get("meeting_id")
    try:
        mid = int(mid)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Invalid meeting id."), 400
    if mid < 1:
        return jsonify(ok=False, error="Invalid meeting id."), 400

    meeting = (
        Meeting.query.options(
            selectinload(Meeting.meeting_group).selectinload(MeetingGroup.tags)
        )
        .filter_by(meeting_id=mid, status="Live")
        .first()
    )
    if not meeting:
        return jsonify(ok=False, error="Meeting not found or not published."), 404

    row = UserSavedMeeting(user_id=int(uid), meeting_id=mid)
    db.session.add(row)
    user = User.query.options(selectinload(User.attendee_tags)).get(int(uid))
    if user:
        _merge_meeting_group_keywords_into_user_attendee_tags(user, meeting)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(ok=True, already_saved=True, meeting_id=mid)
    except Exception:
        db.session.rollback()
        current_app.logger.exception("api_saved_meetings_create")
        return jsonify(ok=False, error="Could not save this meeting."), 500

    return jsonify(ok=True, meeting_id=mid, already_saved=False)


@bp.route("/api/saved-meetings/<int:meeting_id>", methods=["DELETE"])
def api_saved_meetings_delete(meeting_id):
    """Remove a saved meeting row for the signed-in user."""
    uid = session.get("user_id")
    if not uid:
        return (
            jsonify(
                ok=False,
                need_auth=True,
                register_url=url_for("main.register"),
            ),
            401,
        )
    row = UserSavedMeeting.query.filter_by(
        user_id=int(uid), meeting_id=int(meeting_id)
    ).first()
    if not row:
        return jsonify(ok=True, removed=False)
    try:
        db.session.delete(row)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("api_saved_meetings_delete")
        return jsonify(ok=False, error="Could not update your saved list."), 500
    return jsonify(ok=True, removed=True)


@bp.route("/search")
def site_search():
    lbl_mt = table_label("events", "Events")

    q_raw = (request.args.get("q") or "").strip()
    q_term = _search_like_term(q_raw)
    sort = (request.args.get("sort") or "soonest").strip()
    if sort not in {
        "soonest",
        "relevance",
        "name_az",
        "name_za",
        "newest",
        "oldest",
        "distance",
    }:
        sort = "soonest"
    page = request.args.get("page", type=int) or 1
    if page < 1:
        page = 1
    raw_per = request.args.get("per_page", type=int) or 12
    per_page = max(6, min(raw_per, 48))
    # Accept 0 or synonyms (e.g. bookmarks / older clients sending within_miles=national).
    _within_raw = (request.args.get("within_miles") or "").strip().lower()
    if _within_raw in ("national", "nationwide", "all", "0"):
        raw_within = 0
    else:
        raw_within = request.args.get("within_miles", type=int)
    # 0 = National (no radius filter; still uses origin for map hints / distances where shown)
    within_miles = raw_within if raw_within in {0, 5, 10, 25, 50, 100} else 0
    postcode_arg = _clean_location_hint(request.args.get("postcode") or "")

    def _site_search_event_format():
        raw = (request.args.get("event_format") or "").strip().lower()
        if raw in ("virtual", "online", "v"):
            return "virtual"
        if raw in ("f2f", "face", "face2face", "facetoface", "in_person", "in-person"):
            return "f2f"
        f2f = "1" in request.args.getlist("format_f2f")
        virt = "1" in request.args.getlist("format_virtual")
        if virt and not f2f:
            return "virtual"
        if f2f and not virt:
            return "f2f"
        return "f2f"

    search_event_format = _site_search_event_format()
    if search_event_format == "virtual" and sort == "distance":
        sort = "soonest"

    tags_param_raw = (request.args.get("tags") or "").strip()
    tag_ids_selected: list[int] = []
    for part in tags_param_raw.split(","):
        part = part.strip()
        if part.isdigit():
            tid = int(part)
            if tid > 0:
                tag_ids_selected.append(tid)
    seen_tags: set[int] = set()
    tag_ids_selected = [x for x in tag_ids_selected if x not in seen_tags and not seen_tags.add(x)]
    if tag_ids_selected:
        valid_tag_ids = {
            int(r[0])
            for r in db.session.query(Tag.tag_id)
            .filter(Tag.tag_id.in_(tag_ids_selected))
            .all()
        }
        tag_ids_selected = [x for x in tag_ids_selected if x in valid_tag_ids]
    search_tags_param = ",".join(str(t) for t in tag_ids_selected)

    query = (
        Meeting.query.join(
            MeetingGroup, Meeting.meeting_group_id == MeetingGroup.meeting_group_id
        )
        .filter(Meeting.status == "Live")
        .options(
            selectinload(Meeting.meeting_group).selectinload(MeetingGroup.industry),
            selectinload(Meeting.meeting_group).selectinload(MeetingGroup.tags),
        )
    )

    if search_event_format == "virtual":
        query = query.filter(Meeting.meeting_format == "Virtual")
    else:
        query = query.filter(
            or_(
                Meeting.meeting_format == "Face2Face",
                Meeting.meeting_format.is_(None),
            )
        )

    if q_term:
        like_anywhere = f"%{q_term}%"
        query = query.filter(
            or_(
                Meeting.title.ilike(like_anywhere),
                Meeting.subject.ilike(like_anywhere),
                MeetingGroup.meeting_group_name.ilike(like_anywhere),
            )
        )

    if tag_ids_selected:
        tag_gid_subq = (
            select(meeting_group_tags.c.meeting_group_id)
            .where(meeting_group_tags.c.tag_id.in_(tag_ids_selected))
            .distinct()
        )
        query = query.filter(MeetingGroup.meeting_group_id.in_(tag_gid_subq))

    origin_lat = None
    origin_lng = None
    postcode_display = ""
    implicit_origin = False
    search_origin_label = ""
    origin_lat_arg = _numeric_or_none(request.args.get("origin_lat"))
    origin_lng_arg = _numeric_or_none(request.args.get("origin_lng"))
    origin_label_arg = _clean_location_hint(request.args.get("origin_label") or "")

    if postcode_arg:
        origin, pretty_label = _lookup_latlng_for_location_query(postcode_arg)
        if origin:
            origin_lat, origin_lng = origin
            pc_try = _normalize_postcode(postcode_arg)
            if _lookup_latlng_for_postcode(pc_try):
                postcode_display = _normalize_postcode(
                    (pretty_label or "").strip() or postcode_arg
                )
            else:
                pl = _clean_location_hint(pretty_label or "") if pretty_label else ""
                postcode_display = (
                    pl
                    if pl and len(pl) <= 96
                    else _clean_location_hint(postcode_arg)[:96]
                )
            search_origin_label = postcode_display
        else:
            postcode_display = postcode_arg
            search_origin_label = postcode_display
    elif origin_lat_arg is not None and origin_lng_arg is not None:
        origin_lat = float(origin_lat_arg)
        origin_lng = float(origin_lng_arg)
        implicit_origin = True
        search_origin_label = origin_label_arg
    else:
        seed_hint, seed_lat, seed_lng = _search_seed_location()
        if seed_lat is not None and seed_lng is not None:
            origin_lat, origin_lng = float(seed_lat), float(seed_lng)
            implicit_origin = True
            label = (seed_hint or "").strip()
            if not label:
                try:
                    label = _search_location_label_from_latlng(origin_lat, origin_lng) or ""
                except Exception:
                    label = ""
            search_origin_label = label
            postcode_display = label
        else:
            postcode_display = ""
            search_origin_label = ""

    meetings_list: list[Meeting] = list(query.all())
    has_origin = origin_lat is not None and origin_lng is not None
    distance_filter_on = (
        search_event_format == "f2f" and has_origin and within_miles > 0
    )

    for m in meetings_list:
        d = None
        is_virtual = (m.meeting_format or "").strip() == "Virtual"
        if has_origin and not is_virtual:
            mlat = _numeric_or_none(getattr(m, "latitude", None))
            mlng = _numeric_or_none(getattr(m, "longitude", None))
            if mlat is not None and mlng is not None:
                d = _haversine_miles(float(origin_lat), float(origin_lng), mlat, mlng)
        setattr(m, "_distance_miles", d)
        mg = m.meeting_group
        setattr(m, "_group_name", (mg.meeting_group_name or "") if mg else "")

    if distance_filter_on:
        meetings_list = [
            m
            for m in meetings_list
            if (m.meeting_format or "").strip() == "Virtual"
            or (
                getattr(m, "_distance_miles", None) is not None
                and m._distance_miles <= float(within_miles)
            )
        ]

    if sort == "distance" and has_origin:
        meetings_list.sort(
            key=lambda m: (
                (
                    9_999_999.0
                    if (m.meeting_format or "").strip() == "Virtual"
                    else (getattr(m, "_distance_miles", 999999) or 999999)
                ),
                (m.title or "").lower(),
            )
        )
    elif sort == "distance" and not has_origin:
        meetings_list.sort(
            key=lambda m: (m.created_at is None, m.created_at or datetime.min),
            reverse=True,
        )
    elif sort == "name_az":
        meetings_list.sort(key=lambda m: (m.title or "").lower())
    elif sort == "name_za":
        meetings_list.sort(key=lambda m: (m.title or "").lower(), reverse=True)
    elif sort == "newest":
        meetings_list.sort(
            key=lambda m: (m.created_at is None, m.created_at or datetime.min),
            reverse=True,
        )
    elif sort == "oldest":
        meetings_list.sort(key=lambda m: (m.created_at is None, m.created_at or datetime.max))
    elif sort == "soonest":
        soon_now = datetime.utcnow()

        def _soonest_key(mtg: Meeting) -> tuple:
            st = mtg.starts_at
            if st is None:
                return (1, 0.0, (mtg.title or "").lower())
            if st < soon_now:
                return (2, -st.timestamp(), (mtg.title or "").lower())
            return (0, st.timestamp(), (mtg.title or "").lower())

        meetings_list.sort(key=_soonest_key)
    elif sort == "relevance":
        if q_term:
            qt = q_term.lower()

            def _rel_key(meet: Meeting) -> tuple:
                title = (meet.title or "").lower()
                gn = (
                    (meet.meeting_group.meeting_group_name or "").lower()
                    if meet.meeting_group
                    else ""
                )
                if title.startswith(qt):
                    tier = 0
                elif qt in title:
                    tier = 1
                elif gn.startswith(qt):
                    tier = 2
                elif qt in gn:
                    tier = 3
                else:
                    tier = 4
                ts = meet.created_at.timestamp() if meet.created_at else 0.0
                return (tier, -ts)

            meetings_list.sort(key=_rel_key)
        else:
            meetings_list.sort(
                key=lambda m: (m.created_at is None, m.created_at or datetime.min),
                reverse=True,
            )

    matching_meetings_total = len(meetings_list)
    matching_meetings = meetings_list[:500]
    pagination = _ListPagination(meetings_list, page, per_page)

    countdown_now = datetime.utcnow()
    next_event_countdowns: dict[int, dict] = {}
    for m in pagination.items or []:
        if m.starts_at is not None and m.starts_at >= countdown_now:
            seconds_left = int((m.starts_at - countdown_now).total_seconds())
            next_event_countdowns[m.meeting_id] = {
                "label": _event_countdown_label(m.starts_at, countdown_now).replace("In ", ""),
                "urgent": seconds_left <= 86400,
            }
        else:
            next_event_countdowns[m.meeting_id] = {
                "label": "No upcoming date" if m.starts_at is None else "Past event",
                "urgent": False,
            }

    meeting_map_points = []
    for m in matching_meetings:
        mlat = _numeric_or_none(getattr(m, "latitude", None))
        mlng = _numeric_or_none(getattr(m, "longitude", None))
        if mlat is None or mlng is None:
            continue
        meeting_map_points.append(
            {
                "meeting_id": m.meeting_id,
                "title": m.title or "Untitled",
                "group_name": getattr(m, "_group_name", "") or "",
                "event_url": url_for("main.meeting_detail", meeting_id=m.meeting_id),
                "lat": mlat,
                "lng": mlng,
                "starts_at": m.starts_at.strftime("%d %b %Y, %H:%M")
                if m.starts_at
                else "Date TBC",
                "distance_miles": round(getattr(m, "_distance_miles", 0.0), 1)
                if has_origin and getattr(m, "_distance_miles", None) is not None
                else None,
            }
        )

    search_nav_kwargs = {
        "q": q_raw,
        "sort": sort,
        "per_page": per_page,
        "event_format": search_event_format,
    }
    if search_event_format == "f2f":
        search_nav_kwargs["within_miles"] = within_miles
        search_nav_kwargs["postcode"] = postcode_display
    if tag_ids_selected:
        search_nav_kwargs["tags"] = search_tags_param
    if implicit_origin and origin_lat is not None and origin_lng is not None:
        search_nav_kwargs["origin_lat"] = round(float(origin_lat), 6)
        search_nav_kwargs["origin_lng"] = round(float(origin_lng), 6)
        if search_origin_label:
            search_nav_kwargs["origin_label"] = search_origin_label[:120]

    saved_meeting_ids = set()
    _sv_uid = session.get("user_id")
    if _sv_uid and pagination.items:
        _sv_mids = [m.meeting_id for m in pagination.items]
        if _sv_mids:
            saved_meeting_ids = {
                r.meeting_id
                for r in UserSavedMeeting.query.filter(
                    UserSavedMeeting.user_id == int(_sv_uid),
                    UserSavedMeeting.meeting_id.in_(_sv_mids),
                ).all()
            }

    _tag_rows = (
        Tag.query.options(selectinload(Tag.industry))
        .order_by(Tag.industry_id.asc(), Tag.tag.asc())
        .all()
    )
    search_tags_catalog: list[dict] = []
    _ind_order: list[str] = []
    _ind_buckets: dict[str, list[dict]] = {}
    for t in _tag_rows:
        ind_label = (
            ((t.industry.industry or "").strip() if t.industry else "") or "General"
        )
        if ind_label not in _ind_buckets:
            _ind_buckets[ind_label] = []
            _ind_order.append(ind_label)
        _ind_buckets[ind_label].append(
            {"tag_id": int(t.tag_id), "tag": (t.tag or "").strip()}
        )
    for ind_label in _ind_order:
        search_tags_catalog.append(
            {"industry": ind_label, "tags": _ind_buckets[ind_label]}
        )
    user_profile_tag_ids: list[int] = []
    _prof_uid = session.get("user_id")
    if _prof_uid:
        _prof_user = User.query.options(selectinload(User.attendee_tags)).get(int(_prof_uid))
        if _prof_user and _prof_user.attendee_tags:
            user_profile_tag_ids = [
                int(t.tag_id)
                for t in _prof_user.attendee_tags
                if t is not None and t.tag_id is not None
            ]

    return render_template(
        "search.html",
        pagination=pagination,
        q=q_raw,
        sort=sort,
        per_page=per_page,
        within_miles=within_miles,
        postcode=postcode_display,
        search_event_format=search_event_format,
        lbl_mt=lbl_mt,
        matching_meetings=matching_meetings,
        matching_meetings_total=matching_meetings_total,
        matching_meetings_truncated=(matching_meetings_total > len(matching_meetings)),
        distance_filter_on=distance_filter_on,
        next_event_countdowns=next_event_countdowns,
        search_origin_lat=origin_lat,
        search_origin_lng=origin_lng,
        search_origin_label=search_origin_label,
        implicit_origin=implicit_origin,
        search_nav_kwargs=search_nav_kwargs,
        meeting_map_points=meeting_map_points,
        saved_meeting_ids=saved_meeting_ids,
        search_tags_param=search_tags_param,
        search_selected_tag_ids=tag_ids_selected,
        search_tags_catalog=search_tags_catalog,
        user_profile_tag_ids=user_profile_tag_ids,
    )


@bp.route("/event-groups/<int:meeting_group_id>")
def meeting_group_public(meeting_group_id):
    """Read-only preview of a meeting group (any visitor)."""
    lbl_mg = table_label("event_groups", "Event groups")
    lbl_mt = table_label("events", "Events")
    mg = MeetingGroup.query.options(
        selectinload(MeetingGroup.industry),
        selectinload(MeetingGroup.tags),
        selectinload(MeetingGroup.meetings).selectinload(Meeting.ticket_types),
        selectinload(MeetingGroup.owner),
    ).get(meeting_group_id)
    if not mg:
        flash("That event group was not found.", "warning")
        return redirect(url_for("main.site_search"))

    public_meetings = [
        m for m in (mg.meetings or []) if (m.status or "") == "Live"
    ]
    public_meetings.sort(
        key=lambda m: (m.starts_at is None, m.starts_at or datetime.min)
    )
    desc_raw = _fix_utf8_mojibake_from_cp1252(mg.description or "")
    desc_html = _sanitize_rich_text_html(desc_raw)
    description_safe = Markup(desc_html) if desc_html else Markup("")
    description_plain = _rich_text_plain_text(desc_raw)
    uid = session.get("user_id")
    is_owner = bool(uid and mg.user_id == uid)

    all_meetings = list(mg.meetings or [])
    status_counts = _meeting_status_counts(all_meetings)
    draft_n = status_counts.get("Draft", 0)
    live_n = len(public_meetings)
    total_n = len(all_meetings)
    meeting_stats = {
        "total": total_n,
        "live": live_n,
        "draft": draft_n,
    }

    location_meeting = _pick_preview_location_meeting(all_meetings)
    preview_location_lines = (
        _format_meeting_location_lines(location_meeting) if location_meeting else []
    )
    preview_maps_search_url = (
        _google_maps_search_url(location_meeting) if location_meeting else None
    )
    preview_maps_embed_url = (
        _google_maps_embed_url(location_meeting) if location_meeting else None
    )
    if public_meetings and any(_live_meeting_has_paid_options(m) for m in public_meetings):
        pricing_summary = (
            "At least one listed live event has paid options or published pricing."
        )
    elif public_meetings:
        pricing_summary = (
            "No paid ticket prices are set on listed live events in this preview."
        )
    else:
        pricing_summary = None

    owner = mg.owner
    organiser_email = (owner.email or "").strip() if owner else ""
    organiser_message_available = bool(
        organiser_email and EMAIL_RE.match(organiser_email)
    )
    now_utc = datetime.utcnow()
    meeting_ticket_meta = {}
    for m in public_meetings:
        sale_ticket = _first_active_sale_ticket(m, now_utc)
        remaining_qty = _remaining_tickets_for_sale(sale_ticket, m.meeting_id) if sale_ticket else 0
        price_text = "Details to follow"
        if sale_ticket:
            try:
                amount = Decimal(sale_ticket.price_amount or 0)
            except (InvalidOperation, TypeError):
                amount = Decimal("0")
            price_text = "Free" if amount <= 0 else f"GBP {amount:.2f}"
        meeting_ticket_meta[m.meeting_id] = {
            "can_buy": bool(sale_ticket and remaining_qty > 0),
            "price_text": price_text,
            "max_qty": max(1, remaining_qty) if remaining_qty > 0 else 1,
        }

    template_kw = dict(
        mg=mg,
        public_meetings=public_meetings,
        lbl_mg=lbl_mg,
        lbl_mt=lbl_mt,
        description_safe=description_safe,
        description_plain=description_plain,
        is_owner=is_owner,
        meeting_stats=meeting_stats,
        preview_location_lines=preview_location_lines,
        preview_maps_search_url=preview_maps_search_url,
        preview_maps_embed_url=preview_maps_embed_url,
        location_meeting=location_meeting,
        pricing_summary=pricing_summary,
        organiser_message_available=organiser_message_available,
        meeting_ticket_meta=meeting_ticket_meta,
    )
    embed = (request.args.get("embed") or "").strip().lower()
    if embed in ("1", "true", "yes", "card"):
        return render_template("meeting_group_public_embed.html", **template_kw)
    return render_template("meeting_group_public.html", **template_kw)


def _send_event_group_organiser_email(
    *,
    organiser_email: str,
    group_name: str,
    group_public_url: str,
    sender_name: str,
    sender_email: str,
    message_text: str,
) -> None:
    """Deliver a visitor message to an event-group organiser via SMTP_* env (same stack as contact form)."""
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    site_name = os.getenv("SITE_NAME", "The Networker")
    support_email = os.getenv("SUPPORT_EMAIL", smtp_user)
    if not smtp_user or not smtp_password or not organiser_email:
        raise ValueError("mail_not_configured")

    safe_group = (group_name or "Event group").strip() or "Event group"
    subj_sender = (sender_name or sender_email or "Someone").strip() or "Someone"
    email_message = EmailMessage()
    email_message["Subject"] = f'[{site_name}] Message about "{safe_group}" from {subj_sender}'
    email_message["From"] = smtp_user
    email_message["To"] = organiser_email
    email_message["Reply-To"] = sender_email
    email_message["Date"] = formatdate(localtime=True)
    email_message["Message-ID"] = make_msgid(domain=smtp_user.split("@")[-1])
    email_message["X-Auto-Response-Suppress"] = "All"
    email_message["X-Mailer"] = f"{site_name} Organiser message"
    plain = (
        f"Someone used the Message organiser form on {site_name}.\n\n"
        f"Event group: {safe_group}\n"
        f"Group page: {group_public_url}\n\n"
        f"From: {sender_name}\n"
        f"Email: {sender_email}\n\n"
        f"Message:\n{message_text}\n\n"
        f"---\nSupport: {support_email}\n"
    )
    email_message.set_content(plain)
    gn_e = escape(safe_group)
    url_e = escape(group_public_url)
    sn_e = escape(sender_name)
    se_e = escape(sender_email)
    msg_html = escape(message_text).replace("\n", "<br>")
    email_message.add_alternative(
        (
            "<html><body style=\"font-family:Arial,Helvetica,sans-serif;color:#2f1f3a;\">"
            f"<h2 style=\"color:#5b2d73;\">Message about your event group</h2>"
            f"<p><strong>Group:</strong> {gn_e}<br>"
            f"<strong>Page:</strong> <a href=\"{url_e}\">{url_e}</a></p>"
            f"<p><strong>From:</strong> {sn_e}<br><strong>Email:</strong> {se_e}</p>"
            f"<p><strong>Message</strong><br>{msg_html}</p>"
            "<hr style=\"border:none;border-top:1px solid #e6dcec;\">"
            f"<p style=\"font-size:12px;color:#6b5c75;\">Support: {escape(support_email)}</p>"
            "</body></html>"
        ),
        subtype="html",
    )
    with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(email_message)


@bp.route("/event-groups/<int:meeting_group_id>/contact-organiser", methods=["POST"])
def meeting_group_contact_organiser(meeting_group_id):
    """JSON API: email the group organiser (signed-in members only; not the group owner)."""
    payload = request.get_json(silent=True) or {}
    message_text = (payload.get("message") or "").strip()
    uid = session.get("user_id")
    if not uid:
        return jsonify(
            {
                "ok": False,
                "error": "Please sign in or register to contact the organiser. All enquiries are handled through The Networker.",
            }
        ), 401

    mg = MeetingGroup.query.options(selectinload(MeetingGroup.owner)).get(meeting_group_id)
    if not mg:
        return jsonify({"ok": False, "error": "That event group was not found."}), 404

    if mg.user_id == uid:
        return jsonify({"ok": False, "error": "You are the organiser of this group."}), 403

    owner = mg.owner
    organiser_email = (owner.email or "").strip() if owner else ""
    if not organiser_email or not EMAIL_RE.match(organiser_email):
        return jsonify(
            {"ok": False, "error": "This organiser does not have a reachable email on file."}
        ), 400

    if len(message_text) < 10:
        return jsonify({"ok": False, "error": "Please enter a message (at least a few words)."}), 400
    if len(message_text) > 5000:
        return jsonify({"ok": False, "error": "Message is too long (5000 characters maximum)."}), 400

    user = User.query.get(uid)
    if not user:
        return jsonify({"ok": False, "error": "Your session has expired. Please sign in again."}), 401
    sender_email = (user.email or "").strip()
    sender_name = _user_display_name(user) or sender_email
    if not sender_email or not EMAIL_RE.match(sender_email):
        return jsonify({"ok": False, "error": "Your account email is not valid. Please update your profile."}), 400

    site_url = (os.getenv("SITE_URL") or request.url_root or "").rstrip("/")
    group_path = url_for("main.meeting_group_public", meeting_group_id=meeting_group_id)
    group_public_url = urljoin(site_url + "/", group_path.lstrip("/"))
    group_display = _fix_utf8_mojibake_from_cp1252(mg.meeting_group_name or "") or "Event group"

    try:
        _send_event_group_organiser_email(
            organiser_email=organiser_email,
            group_name=group_display,
            group_public_url=group_public_url,
            sender_name=sender_name,
            sender_email=sender_email,
            message_text=message_text,
        )
    except ValueError:
        return jsonify(
            {
                "ok": False,
                "error": "Messaging is temporarily unavailable. Please try again later or contact The Networker support if the problem persists.",
            }
        ), 503
    except Exception:
        return jsonify(
            {"ok": False, "error": "The message could not be sent. Please try again in a few minutes."}
        ), 502

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
USERNAME_RE = re.compile(r"^\S{5,50}$")
MOBILE_RE = re.compile(r"^[0-9+\-\s()]{7,50}$")
VERIFICATION_RESEND_WAIT_SECONDS = 300


def _derive_username_from_email(email: str) -> str:
    """Assign a unique ``users.username`` from email (column remains required in DB)."""
    em = (email or "").strip().lower()
    local = em.split("@", 1)[0] if "@" in em else em
    base = re.sub(r"[^a-z0-9_]", "_", local).strip("_")
    if len(base) < 5:
        base = re.sub(r"[^a-z0-9]", "", em)[:40]
    if len(base) < 5:
        base = "member"
    base = base[:45]
    candidate = base
    n = 0
    while User.query.filter(db.func.lower(User.username) == candidate.lower()).first():
        n += 1
        suffix = f"_{n}"
        candidate = f"{base[: 50 - len(suffix)]}{suffix}"
    return candidate[:50]


def _user_greeting_name(user) -> str:
    """Preferred salutation for emails (name, then email local-part)."""
    if not user:
        return ""
    parts = [(user.first_name or "").strip(), (user.second_name or "").strip()]
    name = " ".join(p for p in parts if p).strip()
    if name:
        return name
    email = (user.email or "").strip()
    if "@" in email:
        local = email.split("@", 1)[0].strip()
        if local:
            return local
    return (user.username or "").strip() or "there"


def _password_is_strong(password):
    return (
        len(password) >= 8
        and any(c.isupper() for c in password)
        and any(c.islower() for c in password)
    )


def _must_wait_before_verification_resend(user):
    if not user:
        return False
    sent_at = user.verification_send or user.created_date
    if not sent_at:
        return False
    elapsed = (datetime.utcnow() - sent_at).total_seconds()
    return elapsed < VERIFICATION_RESEND_WAIT_SECONDS


def _verification_resend_seconds_remaining(user):
    if not user:
        return 0
    sent_at = user.verification_send or user.created_date
    if not sent_at:
        return 0
    elapsed = (datetime.utcnow() - sent_at).total_seconds()
    remaining = int(VERIFICATION_RESEND_WAIT_SECONDS - elapsed)
    return remaining if remaining > 0 else 0


def _format_wait_time(seconds):
    minutes, secs = divmod(max(int(seconds), 0), 60)
    parts = []
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if secs:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " ".join(parts) if parts else "a few seconds"


def _send_verification_email(user, code):
    """Send the user a link that sets verification_confirmed when clicked.

    Uses SMTP_* env vars. If they are missing the function raises so the caller
    can decide what to do (we just flash a warning and let the user register;
    they can resend later).
    """
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    site_name = os.getenv("SITE_NAME", "The Networker")
    site_url = os.getenv("SITE_URL", "https://myailessons.one")
    support_email = os.getenv("SUPPORT_EMAIL", smtp_user)

    if not smtp_user or not smtp_password:
        raise RuntimeError("SMTP credentials are missing")

    verify_path = url_for("main.verify_email", code=code)
    verify_url = urljoin(f"{site_url.rstrip('/')}/", verify_path.lstrip("/"))

    subject = f"Verify your email for {site_name}"
    greet = _user_greeting_name(user)
    text_body = (
        f"Hi {greet},\n\n"
        f"Thanks for registering with {site_name}.\n"
        "Click the link below to verify your email address:\n\n"
        f"{verify_url}\n\n"
        "If you did not create an account, you can ignore this email.\n\n"
        f"{site_name}\n{site_url}"
    )
    html_body = (
        "<html><body style=\"font-family:Arial,Helvetica,sans-serif;color:#2f1f3a;\">"
        f"<h2 style=\"color:#5b2d73;\">Verify your email for {escape(site_name)}</h2>"
        f"<p>Hi {escape(greet)},</p>"
        "<p>Thanks for registering. Click the button below to verify your email address.</p>"
        f"<p><a href=\"{escape(verify_url)}\" "
        "style=\"display:inline-block;background:#5b2d73;color:#fff;padding:10px 18px;"
        "text-decoration:none;border-radius:999px;\">Verify Email</a></p>"
        "<p>If the button doesn't work, paste this link into your browser:</p>"
        f"<p style=\"word-break:break-all;\"><a href=\"{escape(verify_url)}\">{escape(verify_url)}</a></p>"
        "<p>If you did not create an account, you can ignore this email.</p>"
        f"<p style=\"font-size:12px;color:#6b5c75;\">{escape(site_name)}<br>{escape(site_url)}<br>"
        f"Support: {escape(support_email)}</p>"
        "</body></html>"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = user.email
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=smtp_user.split("@")[-1])
    msg["X-Mailer"] = f"{site_name} Mailer"
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)


PASSWORD_RESET_TOKEN_MAX_AGE = 86400  # 24 hours


def _password_reset_serializer():
    secret = current_app.secret_key
    if not secret:
        raise RuntimeError("SECRET_KEY must be set for password reset links")
    return URLSafeTimedSerializer(str(secret), salt="tnw-password-reset-v1")


def _make_password_reset_token(user_id: int) -> str:
    return _password_reset_serializer().dumps({"uid": int(user_id)})


def _load_password_reset_user_id(token: str) -> int | None:
    if not token or not isinstance(token, str):
        return None
    try:
        data = _password_reset_serializer().loads(token, max_age=PASSWORD_RESET_TOKEN_MAX_AGE)
        uid = data.get("uid")
        return int(uid) if uid is not None else None
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None


def _send_password_reset_email(user, token: str):
    """Email a signed link to ``/reset-password?token=…`` (SMTP_* env vars)."""
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    site_name = os.getenv("SITE_NAME", "The Networker")
    site_url = os.getenv("SITE_URL", "https://myailessons.one")
    support_email = os.getenv("SUPPORT_EMAIL", smtp_user)

    if not smtp_user or not smtp_password:
        raise RuntimeError("SMTP credentials are missing")

    reset_path = url_for("main.reset_password", token=token)
    reset_url = urljoin(f"{site_url.rstrip('/')}/", reset_path.lstrip("/"))

    greet = _user_greeting_name(user)
    subject = f"Reset your {site_name} password"
    text_body = (
        f"Hi {greet},\n\n"
        f"We received a request to reset the password for your {site_name} account.\n"
        "Click the link below to choose a new password (link expires in 24 hours):\n\n"
        f"{reset_url}\n\n"
        "If you did not ask for this, you can ignore this email.\n\n"
        f"{site_name}\n{site_url}"
    )
    html_body = (
        "<html><body style=\"font-family:Arial,Helvetica,sans-serif;color:#2f1f3a;\">"
        f"<h2 style=\"color:#5b2d73;\">Reset your password for {escape(site_name)}</h2>"
        f"<p>Hi {escape(greet)},</p>"
        "<p>We received a request to reset your password. Click the button below to choose a new password. "
        "This link expires in <strong>24 hours</strong>.</p>"
        f"<p><a href=\"{escape(reset_url)}\" "
        "style=\"display:inline-block;background:#5b2d73;color:#fff;padding:10px 18px;"
        "text-decoration:none;border-radius:999px;\">Reset password</a></p>"
        "<p>If the button doesn't work, paste this link into your browser:</p>"
        f"<p style=\"word-break:break-all;\"><a href=\"{escape(reset_url)}\">{escape(reset_url)}</a></p>"
        "<p>If you did not request a password reset, you can ignore this email.</p>"
        f"<p style=\"font-size:12px;color:#6b5c75;\">{escape(site_name)}<br>{escape(site_url)}<br>"
        f"Support: {escape(support_email)}</p>"
        "</body></html>"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = user.email
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=smtp_user.split("@")[-1])
    msg["X-Mailer"] = f"{site_name} Mailer"
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)


@bp.route("/register", methods=["GET", "POST"])
def register():
    form_data = {"email": ""}
    errors = {}

    def _register_template(**extra):
        g.register_modal_form_data = form_data
        g.register_modal_errors = errors
        g.open_register_modal = extra.pop("open_register_modal", True)
        return render_template("register.html", form_data=form_data, errors=errors, **extra)

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        form_data["email"] = email

        if not email:
            errors["email"] = "Email is required."
        elif not EMAIL_RE.match(email):
            errors["email"] = "Please enter a valid email address."

        if not password:
            errors["password"] = "Password is required."
        elif not _password_is_strong(password):
            errors["password"] = (
                "Password must be at least 8 characters and include both uppercase and lowercase letters."
            )

        if not confirm_password:
            errors["confirm_password"] = "Please confirm your password."
        elif password and password != confirm_password:
            errors["confirm_password"] = "Passwords do not match."

        if errors:
            return _register_template(), 400

        if User.query.filter_by(email=email).first():
            errors["email"] = "An account already exists with this email."

        if errors:
            return _register_template(), 400

        user = User(
            username=_derive_username_from_email(email),
            email=email,
            created_date=datetime.utcnow(),
            image_name=DEFAULT_IMAGE_NAME,
            country_id=DEFAULT_COUNTRY_ID,
            verification_send=datetime.utcnow(),
            verification_code=secrets.token_urlsafe(32),
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        try:
            _send_verification_email(user, user.verification_code)
            flash(
                "Account created. Please check your email for the verification link.",
                "success",
            )
        except Exception:
            flash(
                "Account created, but the verification email could not be sent right now. "
                "Please contact support.",
                "warning",
            )

        return redirect(url_for("main.login"))

    return _register_template()


@bp.route("/verify-email/<code>")
def verify_email(code):
    user = User.query.filter_by(verification_code=code).first()
    if not user:
        flash("Invalid or expired verification link.", "danger")
        return redirect(url_for("main.login"))

    if user.verification_confirmed:
        flash("Email already verified. Please sign in.", "info")
        return redirect(url_for("main.login"))

    user.verification_confirmed = datetime.utcnow()
    db.session.commit()
    flash("Email verified successfully. You can now sign in.", "success")
    return redirect(url_for("main.login"))


def _maint_env_login_post():
    """Authenticate against TNW_MAINT_LOGIN_USER_* / PASSWORD_* in .env."""
    username = (request.form.get("username") or request.form.get("email") or "").strip()
    password = request.form.get("password", "")
    login_next = _login_next_from_request()

    if not maint_env_login_configured():
        flash(
            "Sign-in is not configured. Set TNW_MAINT_LOGIN_USER_1 and "
            "TNW_MAINT_LOGIN_PASSWORD_1 (and optionally _2) in .env.",
            "danger",
        )
        return render_template("login.html", login_next=login_next)

    if not username or not password:
        flash("Please enter both username and password.", "danger")
        return render_template("login.html", login_next=login_next)

    matched = verify_maint_env_login(username, password)
    if not matched:
        flash("Invalid username or password.", "danger")
        return render_template("login.html", login_next=login_next)

    session.pop(SESSION_IMPERSONATOR_ADMIN_ID, None)
    session.pop("user_id", None)
    session.pop("pending_twofa_user_id", None)
    session[SESSION_MAINT_ENV_USER] = matched
    dest = login_next or url_for("main.admin_events")
    return _redirect_after_sign_in(dest)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_app.config.get("TNW_MAINT_APP") and request.method == "POST":
        return _maint_env_login_post()

    unverified_popup = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Please enter both email and password.", "danger")
            return render_template("login.html", login_next=_login_next_from_request())

        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            flash("Invalid email or password.", "danger")
            return render_template("login.html", login_next=_login_next_from_request())

        if not user.is_verified:
            must_wait = _must_wait_before_verification_resend(user)
            remaining_seconds = _verification_resend_seconds_remaining(user)
            remaining_text = _format_wait_time(remaining_seconds)
            unverified_popup = {
                "email": user.email,
                "can_resend": not must_wait,
                "remaining_seconds": remaining_seconds,
                "message": (
                    "Your account is not verified yet. We sent a verification email recently. "
                    f"Please try again in {remaining_text} in case email delivery is slow."
                    if must_wait
                    else "Your account is not verified yet. You can resend the verification email now."
                ),
            }
            return render_template(
                "login.html",
                unverified_popup=unverified_popup,
                login_next=_login_next_from_request(),
            )

        # Password OK. If 2FA is enabled we don't log them in yet — we
        # stash a pending id in the session and redirect to the challenge
        # page.
        if user.twofa_enabled and user.twofa_secret:
            session.pop("user_id", None)
            session["pending_twofa_user_id"] = user.user_id
            nl = _login_next_from_request()
            if nl:
                session[SESSION_POST_LOGIN_NEXT] = nl
            else:
                session.pop(SESSION_POST_LOGIN_NEXT, None)
            return redirect(url_for("main.login_twofa"))

        session.pop(SESSION_IMPERSONATOR_ADMIN_ID, None)
        session["user_id"] = user.user_id
        dest = _login_next_from_request()
        if not user.is_profile_complete:
            flash("Please complete your profile before using the rest of the site.", "info")
            return _redirect_after_sign_in(url_for("main.profile"))
        return _redirect_after_sign_in(dest)

    return render_template(
        "login.html",
        unverified_popup=unverified_popup,
        login_next=_login_next_from_request(),
    )


@bp.route("/login/twofa", methods=["GET", "POST"])
def login_twofa():
    pending_id = session.get("pending_twofa_user_id")
    if not pending_id:
        return redirect(url_for("main.login"))

    user = User.query.get(pending_id)
    if not user:
        session.pop("pending_twofa_user_id", None)
        session.pop(SESSION_POST_LOGIN_NEXT, None)
        return redirect(url_for("main.login"))

    if request.method == "POST":
        code = (request.form.get("code") or "").strip().replace(" ", "")
        if not user.verify_twofa_code(code):
            flash("That code didn't match. Try the latest code from your app.", "danger")
            return render_template("twofa_challenge.html")

        session.pop("pending_twofa_user_id", None)
        session.pop(SESSION_IMPERSONATOR_ADMIN_ID, None)
        session["user_id"] = user.user_id
        dest = session.pop(SESSION_POST_LOGIN_NEXT, None)
        if not user.is_profile_complete:
            flash("Please complete your profile before using the rest of the site.", "info")
            return _redirect_after_sign_in(url_for("main.profile"))
        return _redirect_after_sign_in(dest)

    return render_template("twofa_challenge.html")


@bp.route("/verify-email/resend", methods=["POST"])
def resend_verification():
    email = request.form.get("email", "").strip().lower()
    if not email:
        flash("Please enter your email address.", "danger")
        return redirect(url_for("main.login"))

    user = User.query.filter_by(email=email).first()
    if not user:
        flash("No account found for that email address.", "danger")
        return redirect(url_for("main.login"))

    if user.is_verified:
        flash("This email is already verified. You can sign in.", "info")
        return redirect(url_for("main.login"))

    if _must_wait_before_verification_resend(user):
        remaining_text = _format_wait_time(_verification_resend_seconds_remaining(user))
        flash(
            f"We sent a verification email recently. Please try again in {remaining_text} in case delivery is slow.",
            "warning",
        )
        return redirect(url_for("main.login"))

    user.verification_code = secrets.token_urlsafe(32)
    user.verification_send = datetime.utcnow()
    db.session.commit()

    try:
        _send_verification_email(user, user.verification_code)
        flash("A new verification email has been sent.", "success")
    except Exception:
        flash(
            "We couldn't send the verification email right now. Please try again later.",
            "warning",
        )
    return redirect(url_for("main.login"))


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    form_email = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        form_email = email
        if not email or not EMAIL_RE.match(email):
            flash("Please enter a valid email address.", "danger")
            return render_template("forgot_password.html", form_email=form_email)

        user = User.query.filter_by(email=email).first()
        if user:
            try:
                token = _make_password_reset_token(user.user_id)
                _send_password_reset_email(user, token)
            except Exception:
                flash(
                    "We could not send email right now. Please try again later or contact support.",
                    "danger",
                )
                return render_template("forgot_password.html", form_email=form_email)

        flash(
            "If an account exists for that email, we have sent a link to reset your password.",
            "info",
        )
        return redirect(url_for("main.login"))

    return render_template("forgot_password.html", form_email=form_email)


@bp.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    token = (request.args.get("token") or request.form.get("token") or "").strip()
    if not token:
        flash("That password reset link is invalid or incomplete.", "danger")
        return redirect(url_for("main.login"))

    user_id = _load_password_reset_user_id(token)
    if not user_id:
        flash("That password reset link is invalid or has expired. Please request a new one.", "danger")
        return redirect(url_for("main.login"))

    user = User.query.get(user_id)
    if not user:
        flash("That password reset link is invalid or has expired. Please request a new one.", "danger")
        return redirect(url_for("main.login"))

    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not new_password or not confirm_password:
            flash("Please enter and confirm your new password.", "danger")
            return render_template("reset_password.html", token=token)
        if new_password != confirm_password:
            flash("The two passwords do not match.", "danger")
            return render_template("reset_password.html", token=token)
        if not _password_is_strong(new_password):
            flash(
                "Password must be at least 8 characters and include both uppercase and lowercase letters.",
                "danger",
            )
            return render_template("reset_password.html", token=token)

        user.set_password(new_password)
        db.session.commit()
        flash("Your password has been updated. You can sign in now.", "success")
        return redirect(url_for("main.login"))

    return render_template("reset_password.html", token=token)


# ---------------------------------------------------------------------------
# Meetings (render only, no DB)
# ---------------------------------------------------------------------------
@bp.route("/meetings")
def meeting_list():
    return render_template("meeting_list.html")


def _debug_meeting_create(message):
    print(f"[meeting-create] {message}", flush=True)
    try:
        current_app.logger.info("[meeting-create] %s", message)
    except Exception:
        pass


def _parse_optional_int(value):
    value = (value or "").strip()
    if not value:
        return None
    return int(value)


def _parse_optional_decimal(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _parse_optional_datetime(value):
    value = (value or "").strip()
    if not value:
        return None
    return datetime.fromisoformat(value)


def _snap_minute_to_15(value: int) -> int:
    if value < 0:
        return 0
    snapped = int(round(value / 15) * 15)
    if snapped >= 60:
        return 45
    return snapped


def _meeting_form_duration_minutes_from_post(form) -> int | None:
    """Resolve duration from hidden field or end-time fields (15-minute steps)."""
    mode = (form.get("meeting_length_mode") or "duration").strip().lower()
    try:
        duration_minutes = _parse_optional_int(form.get("duration_minutes"))
    except ValueError:
        duration_minutes = None

    if mode != "end":
        if duration_minutes is not None and duration_minutes >= 15:
            return duration_minutes
        return None

    try:
        starts_at = _parse_optional_datetime(form.get("starts_at"))
    except ValueError:
        starts_at = None
    if not starts_at:
        if duration_minutes is not None and duration_minutes >= 15:
            return duration_minutes
        return None

    try:
        end_hour = _parse_optional_int(form.get("ends_at_hour"))
        end_minute = _parse_optional_int(form.get("ends_at_minute"))
    except ValueError:
        return None
    if end_hour is None or end_minute is None:
        return None

    end_minute = _snap_minute_to_15(end_minute)
    end_at = starts_at.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    if end_at <= starts_at:
        return None
    delta = int((end_at - starts_at).total_seconds() // 60)
    return delta if delta >= 15 else None


def _parse_optional_date_as_datetime(value, end_of_day=False):
    value = (value or "").strip()
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if end_of_day:
        return parsed.replace(hour=23, minute=59, second=59, microsecond=0)
    return parsed.replace(hour=0, minute=0, second=0, microsecond=0)


_MEETING_RECURRENCE_MAX_TOTAL = 101  # primary + up to 100 additional


def _nth_weekday_dom_in_month(year: int, month: int, weekday: int, nth: int) -> int | None:
    """ weekday: Monday=0..Sunday=6. nth: 1-4 first..fourth, 5=fifth when it exists, -1 last. """
    if weekday < 0 or weekday > 6:
        return None
    ndays = calendar.monthrange(year, month)[1]
    if nth == -1:
        for dom in range(ndays, 0, -1):
            if date(year, month, dom).weekday() == weekday:
                return dom
        return None
    if nth not in {1, 2, 3, 4, 5}:
        return None
    seen = 0
    for dom in range(1, ndays + 1):
        if date(year, month, dom).weekday() == weekday:
            seen += 1
            if seen == nth:
                return dom
    return None


def _advance_one_month(year: int, month: int) -> tuple[int, int]:
    if month == 12:
        return year + 1, 1
    return year, month + 1


def _advance_month_preserving_calendar_day(dt: datetime) -> datetime:
    dom = dt.day
    y, m = _advance_one_month(dt.year, dt.month)
    last_dom = calendar.monthrange(y, m)[1]
    new_dom = min(dom, last_dom)
    return dt.replace(year=y, month=m, day=new_dom)


def _repeat_series_numbered_title(
    _user_title: str, occurrence_1_based: int, *, max_len: int = 180
) -> str:
    """Titles for repeating series: Event 1, Event 2, … (_user_title kept for call-site compatibility)."""
    return f"Event {occurrence_1_based}"[:max_len]


def _organiser_repeat_meeting_series(
    *,
    first: datetime,
    pattern: str,
    until: date,
    nth_week: int | None,
    weekday: int | None,
) -> list[datetime]:
    """Return sorted datetimes starting with ``first`` (naive UTC wall time), through ``until`` inclusive."""
    if first.date() > until:
        return []
    patterns = {"weekly", "monthly_dom", "monthly_nth"}
    if pattern not in patterns:
        return [first]

    out: list[datetime] = [first]
    if pattern == "weekly":
        cur = first
        while len(out) < _MEETING_RECURRENCE_MAX_TOTAL:
            cur = cur + timedelta(days=7)
            if cur.date() > until:
                break
            out.append(cur)
        return out

    if pattern == "monthly_dom":
        cur = first
        while len(out) < _MEETING_RECURRENCE_MAX_TOTAL:
            cur = _advance_month_preserving_calendar_day(cur)
            if cur.date() > until:
                break
            out.append(cur)
        return out

    if pattern == "monthly_nth":
        if nth_week is None or weekday is None:
            return out
        wd = int(weekday)
        nth = int(nth_week)
        y, m = _advance_one_month(first.year, first.month)
        while len(out) < _MEETING_RECURRENCE_MAX_TOTAL:
            dom = _nth_weekday_dom_in_month(y, m, wd, nth)
            if dom is None:
                y, m = _advance_one_month(y, m)
                continue
            nxt = datetime(y, m, dom, first.hour, first.minute, first.second)
            if nxt.date() > until:
                break
            out.append(nxt)
            y, m = _advance_one_month(y, m)
        return out

    return out


def _clone_meeting_ticket_types_organiser(
    source_meeting_id: int,
    dest_meeting_id: int,
    *,
    ticket_status: str = "Draft",
) -> None:
    templates = (
        MeetingTicketType.query.filter_by(meeting_id=source_meeting_id)
        .order_by(MeetingTicketType.sort_order.asc(), MeetingTicketType.ticket_type_id.asc())
        .all()
    )
    if not templates:
        return

    dest_last_sort = (
        db.session.query(func.max(MeetingTicketType.sort_order))
        .filter(MeetingTicketType.meeting_id == dest_meeting_id)
        .scalar()
        or -1
    )
    idx = int(dest_last_sort) + 1
    now = datetime.utcnow()
    for t in templates:
        db.session.add(
            MeetingTicketType(
                meeting_id=dest_meeting_id,
                ticket_name=t.ticket_name or "Ticket",
                ticket_description=t.ticket_description,
                currency_code=(t.currency_code or "GBP")[:3],
                price_amount=t.price_amount if t.price_amount is not None else Decimal("0"),
                max_quantity=max(1, int(t.max_quantity or 1)),
                max_tickets_per_user=max(
                    1,
                    min(
                        int(t.max_tickets_per_user or t.max_quantity or 1),
                        max(1, int(t.max_quantity or 20)),
                    ),
                ),
                sales_open_at=t.sales_open_at,
                sales_close_at=t.sales_close_at,
                vat_rate_percent=t.vat_rate_percent if t.vat_rate_percent is not None else 0,
                vat_treatment=infer_vat_mode(
                    t.vat_rate_percent, getattr(t, "vat_treatment", None)
                ),
                refund_policy=t.refund_policy,
                ticket_notes=t.ticket_notes,
                status=ticket_status if ticket_status in {"Draft", "Active"} else "Draft",
                sort_order=idx,
                created_at=now,
                updated_at=now,
            )
        )
        idx += 1


def _normalize_optional_http_url(value, max_len=500):
    raw = (value or "").strip()
    if not raw:
        return None, None
    if len(raw) > max_len:
        return None, f"Website URL must be {max_len} characters or fewer."
    parts = urlsplit(raw)
    if not parts.scheme:
        raw = "https://" + raw
        parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    if scheme not in {"http", "https"} or not parts.netloc:
        return None, "Website URL must start with http:// or https:// and include a valid host."
    return raw, None


@bp.route("/meetings/tickets/update", methods=["POST"])
@login_required
def meeting_tickets_update():
    uid = session["user_id"]
    meeting_id = request.form.get("meeting_id", type=int)
    meeting = Meeting.query.get(meeting_id) if meeting_id else None
    if not meeting or meeting.creator_user_id != uid:
        flash("Choose a valid event before saving ticket details.", "danger")
        return redirect(url_for("main.platform_dashboard", _anchor="create-event-pane"))

    if meeting.meeting_format != "Face2Face":
        flash("Ticket setup is currently available for face-to-face events only.", "warning")
        return redirect(
            url_for(
                "main.platform_dashboard",
                meeting_group_id=meeting.meeting_group_id,
                _anchor="create-event-pane",
            )
        )

    if _sold_qty_by_meeting_ids([meeting.meeting_id]).get(meeting.meeting_id, 0) > 0:
        flash(MEETING_LOCKED_AFTER_TICKET_SALES_TEXT, "danger")
        return redirect(
            url_for(
                "main.platform_dashboard",
                meeting_group_id=meeting.meeting_group_id,
                ticket_meeting_id=meeting.meeting_id,
                _anchor="create-event-pane",
            )
        )

    ticket_type_id = request.form.get("ticket_type_id", type=int)
    ticket_name = (request.form.get("ticket_name") or "").strip()
    currency_code = (request.form.get("currency_code") or "GBP").strip().upper()
    try:
        max_quantity = _parse_optional_int(request.form.get("max_quantity"))
        vat_mode, vat_rate_percent, price_amount = normalize_vat_from_form(
            request.form, request.form.get("price_amount")
        )
    except ValueError:
        flash("Ticket price and capacity values are not valid.", "danger")
        return redirect(
            url_for(
                "main.platform_dashboard",
                meeting_group_id=meeting.meeting_group_id,
                _anchor="create-event-pane",
            )
        )

    try:
        sales_open_at = _parse_optional_date_as_datetime(request.form.get("sales_open_at"))
        sales_close_at = _parse_optional_date_as_datetime(
            request.form.get("sales_close_at"), end_of_day=True
        )
    except ValueError:
        sales_open_at = None
        sales_close_at = None
        flash("The ticket sales dates are not valid.", "danger")
        return redirect(
            url_for(
                "main.platform_dashboard",
                meeting_group_id=meeting.meeting_group_id,
                _anchor="create-event-pane",
            )
        )

    errors = []
    if not ticket_name:
        errors.append("Ticket name is required.")
    if currency_code != "GBP":
        errors.append("Only GBP is currently supported.")
    if price_amount is None or price_amount < 0:
        errors.append("Ticket price must be zero or more.")
    if max_quantity is None or max_quantity <= 0:
        errors.append("Maximum attendees must be greater than zero.")
    if sales_open_at and sales_close_at and sales_close_at <= sales_open_at:
        errors.append("Sales close must be after sales open.")
    if meeting.starts_at and meeting.duration_minutes and sales_close_at:
        meeting_ends_at = meeting.starts_at + timedelta(minutes=meeting.duration_minutes)
        if sales_close_at > meeting_ends_at:
            errors.append("Sales close cannot be after the event has finished.")

    if errors:
        flash(" ".join(errors), "danger")
        return redirect(
            url_for(
                "main.platform_dashboard",
                meeting_group_id=meeting.meeting_group_id,
                _anchor="create-event-pane",
            )
        )

    status = request.form.get("ticket_status") or "Draft"
    if status not in {"Draft", "Active"}:
        status = "Draft"

    ticket = None
    if ticket_type_id:
        ticket = MeetingTicketType.query.filter_by(
            meeting_id=meeting.meeting_id, ticket_type_id=ticket_type_id
        ).first()
        if ticket is None:
            flash("Choose a valid ticket type for this event.", "danger")
            return redirect(
                url_for(
                    "main.platform_dashboard",
                    meeting_group_id=meeting.meeting_group_id,
                    ticket_meeting_id=meeting.meeting_id,
                    _anchor="create-event-pane",
                )
            )

    if ticket is None:
        last_sort_order = (
            db.session.query(func.max(MeetingTicketType.sort_order))
            .filter(MeetingTicketType.meeting_id == meeting.meeting_id)
            .scalar()
        )
        ticket = MeetingTicketType(
            meeting_id=meeting.meeting_id,
            created_at=datetime.utcnow(),
            sort_order=int(last_sort_order or -1) + 1,
        )
        db.session.add(ticket)

    was_active_before_save = bool(
        ticket
        and getattr(ticket, "ticket_type_id", None)
        and (ticket.status or "Draft") == "Active"
    )
    if (
        ticket
        and getattr(ticket, "ticket_type_id", None)
        and status == "Draft"
        and was_active_before_save
        and _sum_attendee_qty_for_ticket_type(ticket.ticket_type_id) > 0
    ):
        flash(
            "This ticket type already has purchases, so it cannot be moved back to draft.",
            "danger",
        )
        return redirect(
            url_for(
                "main.platform_dashboard",
                meeting_group_id=meeting.meeting_group_id,
                ticket_meeting_id=meeting.meeting_id,
                ticket_type_id=ticket.ticket_type_id,
                _anchor="create-event-pane",
            )
        )

    ticket.ticket_name = ticket_name
    ticket.ticket_description = (request.form.get("ticket_description") or "").strip() or None
    ticket.currency_code = currency_code
    ticket.price_amount = price_amount
    ticket.max_quantity = max_quantity
    ticket.max_tickets_per_user = max_quantity
    ticket.sales_open_at = sales_open_at
    ticket.sales_close_at = sales_close_at
    ticket.vat_rate_percent = vat_rate_percent
    ticket.vat_treatment = vat_mode
    ticket.refund_policy = (request.form.get("refund_policy") or "").strip() or None
    ticket.ticket_notes = (request.form.get("ticket_notes") or "").strip() or None
    ticket.status = status
    ticket.updated_at = datetime.utcnow()

    db.session.commit()
    if status == "Draft":
        flash(
            "Ticket type is back in draft and is not on sale."
            if was_active_before_save
            else "Ticket details saved.",
            "success",
        )
    elif was_active_before_save:
        flash("Ticket details saved.", "success")
    else:
        flash("Ticket type published. Tickets are now available for sale.", "success")
    return redirect(
        url_for(
            "main.platform_dashboard",
            meeting_group_id=meeting.meeting_group_id,
            ticket_meeting_id=meeting.meeting_id,
            ticket_type_id=ticket.ticket_type_id,
            _anchor="create-event-pane",
        )
    )


@bp.route("/meetings/tickets/duplicate", methods=["POST"])
@login_required
def meeting_ticket_type_duplicate():
    uid = session["user_id"]
    meeting_id = request.form.get("meeting_id", type=int)
    source_ticket_type_id = request.form.get("ticket_type_id", type=int)
    meeting = Meeting.query.get(meeting_id) if meeting_id else None
    if not meeting or meeting.creator_user_id != uid:
        flash("Choose a valid event before duplicating a ticket type.", "danger")
        return redirect(url_for("main.platform_dashboard", _anchor="create-event-pane"))
    if meeting.meeting_format != "Face2Face":
        flash("Ticket setup is currently available for face-to-face events only.", "warning")
        return redirect(
            url_for(
                "main.platform_dashboard",
                meeting_group_id=meeting.meeting_group_id,
                _anchor="create-event-pane",
            )
        )
    if _sold_qty_by_meeting_ids([meeting.meeting_id]).get(meeting.meeting_id, 0) > 0:
        flash(MEETING_LOCKED_AFTER_TICKET_SALES_TEXT, "danger")
        return redirect(
            url_for(
                "main.platform_dashboard",
                meeting_group_id=meeting.meeting_group_id,
                ticket_meeting_id=meeting.meeting_id,
                _anchor="create-event-pane",
            )
        )
    source = (
        MeetingTicketType.query.filter_by(
            meeting_id=meeting.meeting_id, ticket_type_id=source_ticket_type_id
        ).first()
        if source_ticket_type_id
        else None
    )
    if source is None:
        flash("Choose a valid ticket type to duplicate.", "danger")
        return redirect(
            url_for(
                "main.platform_dashboard",
                meeting_group_id=meeting.meeting_group_id,
                ticket_meeting_id=meeting.meeting_id,
                _anchor="create-event-pane",
            )
        )
    last_sort = (
        db.session.query(func.max(MeetingTicketType.sort_order))
        .filter(MeetingTicketType.meeting_id == meeting.meeting_id)
        .scalar()
    )
    base_name = (source.ticket_name or "Ticket").strip() or "Ticket"
    copy_name = f"{base_name} (copy)"
    if len(copy_name) > 100:
        copy_name = copy_name[:97] + "..."
    new_tt = MeetingTicketType(
        meeting_id=meeting.meeting_id,
        ticket_name=copy_name,
        ticket_description=source.ticket_description,
        currency_code=source.currency_code or "GBP",
        price_amount=source.price_amount if source.price_amount is not None else 0,
        max_quantity=int(source.max_quantity or 20),
        max_tickets_per_user=int(source.max_tickets_per_user or source.max_quantity or 20),
        sales_open_at=source.sales_open_at,
        sales_close_at=source.sales_close_at,
        vat_rate_percent=source.vat_rate_percent if source.vat_rate_percent is not None else 0,
        vat_treatment=infer_vat_mode(
            source.vat_rate_percent, getattr(source, "vat_treatment", None)
        ),
        refund_policy=source.refund_policy,
        ticket_notes=source.ticket_notes,
        status="Draft",
        sort_order=int(last_sort or -1) + 1,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.session.add(new_tt)
    db.session.commit()
    flash("Ticket type duplicated as a new draft.", "success")
    return redirect(
        url_for(
            "main.platform_dashboard",
            meeting_group_id=meeting.meeting_group_id,
            ticket_meeting_id=meeting.meeting_id,
            ticket_type_id=new_tt.ticket_type_id,
            _anchor="create-event-pane",
        )
    )


@bp.route("/meetings/tickets/delete", methods=["POST"])
@login_required
def meeting_ticket_type_delete():
    uid = session["user_id"]
    meeting_id = request.form.get("meeting_id", type=int)
    ticket_type_id = request.form.get("ticket_type_id", type=int)
    meeting = Meeting.query.get(meeting_id) if meeting_id else None
    if not meeting or meeting.creator_user_id != uid:
        flash("Choose a valid event before deleting a ticket type.", "danger")
        return redirect(url_for("main.platform_dashboard", _anchor="create-event-pane"))
    if meeting.meeting_format != "Face2Face":
        flash("Ticket setup is currently available for face-to-face events only.", "warning")
        return redirect(
            url_for(
                "main.platform_dashboard",
                meeting_group_id=meeting.meeting_group_id,
                _anchor="create-event-pane",
            )
        )
    if _sold_qty_by_meeting_ids([meeting.meeting_id]).get(meeting.meeting_id, 0) > 0:
        flash(MEETING_LOCKED_AFTER_TICKET_SALES_TEXT, "danger")
        return redirect(
            url_for(
                "main.platform_dashboard",
                meeting_group_id=meeting.meeting_group_id,
                ticket_meeting_id=meeting.meeting_id,
                _anchor="create-event-pane",
            )
        )
    ticket = (
        MeetingTicketType.query.filter_by(
            meeting_id=meeting.meeting_id, ticket_type_id=ticket_type_id
        ).first()
        if ticket_type_id
        else None
    )
    if ticket is None:
        flash("That ticket type was not found.", "danger")
        return redirect(
            url_for(
                "main.platform_dashboard",
                meeting_group_id=meeting.meeting_group_id,
                ticket_meeting_id=meeting.meeting_id,
                _anchor="create-event-pane",
            )
        )
    if _sum_attendee_qty_for_ticket_type(ticket.ticket_type_id) > 0:
        flash("This ticket type has bookings and cannot be deleted.", "danger")
        return redirect(
            url_for(
                "main.platform_dashboard",
                meeting_group_id=meeting.meeting_group_id,
                ticket_meeting_id=meeting.meeting_id,
                ticket_type_id=ticket.ticket_type_id,
                _anchor="create-event-pane",
            )
        )
    db.session.delete(ticket)
    db.session.commit()
    flash("Ticket type removed.", "success")
    return redirect(
        url_for(
            "main.platform_dashboard",
            meeting_group_id=meeting.meeting_group_id,
            ticket_meeting_id=meeting.meeting_id,
            _anchor="create-event-pane",
        )
    )


@bp.route("/meetings/duplicate", methods=["POST"])
@login_required
def duplicate_meeting():
    uid = session["user_id"]
    source_meeting_id = request.form.get("source_meeting_id", type=int)
    source = Meeting.query.get(source_meeting_id) if source_meeting_id else None
    if not source or source.creator_user_id != uid:
        flash("Choose a valid event to duplicate.", "danger")
        return redirect(url_for("main.platform_dashboard", _anchor="directory-pane"))

    title = (request.form.get("title") or "").strip()
    starts_at_date = (request.form.get("starts_at_date") or "").strip()
    starts_at_hour = (request.form.get("starts_at_hour") or "12").strip()
    starts_at_minute = (request.form.get("starts_at_minute") or "00").strip()

    errors = []
    if not title:
        errors.append("Event title is required.")
    elif len(title) > 180:
        errors.append("Event title must be 180 characters or fewer.")

    try:
        starts_at = _parse_optional_datetime(
            f"{starts_at_date}T{starts_at_hour}:{starts_at_minute}"
        )
    except ValueError:
        starts_at = None
    if starts_at is None:
        errors.append("Choose when the duplicated event will be held.")

    if errors:
        flash(" ".join(errors), "danger")
        return redirect(
            url_for(
                "main.platform_dashboard",
                meeting_group_id=source.meeting_group_id,
                _anchor="directory-pane",
            )
        )

    duplicate = Meeting(
        meeting_group_id=source.meeting_group_id,
        creator_user_id=uid,
        title=title,
        subject=source.subject,
        starts_at=starts_at,
        meeting_format=source.meeting_format,
        duration_minutes=source.duration_minutes,
        location_city=source.location_city,
        location_postcode=source.location_postcode,
        location_country=source.location_country,
        venue_name=source.venue_name,
        website_url=source.website_url,
        address_line1=source.address_line1,
        address_line2=source.address_line2,
        address_town=source.address_town,
        address_county=source.address_county,
        address_postcode=source.address_postcode,
        address_country=source.address_country,
        latitude=source.latitude,
        longitude=source.longitude,
        virtual_platform=source.virtual_platform,
        virtual_link=source.virtual_link,
        is_paid_and_published=False,
        status="Draft",
        created_at=datetime.utcnow(),
        recurrence_rule_id=source.recurrence_rule_id,
    )
    db.session.add(duplicate)
    db.session.commit()

    flash("Event duplicated as a draft.", "success")
    return redirect(
        url_for(
            "main.platform_dashboard",
            meeting_group_id=duplicate.meeting_group_id,
            edit_meeting_id=duplicate.meeting_id,
            _anchor="directory-pane",
        )
    )


@bp.route("/meetings/delete", methods=["POST"])
@login_required
def organiser_delete_meeting():
    uid = session["user_id"]
    mid = request.form.get("meeting_id", type=int)
    mgid_form = request.form.get("meeting_group_id", type=int)
    meeting = Meeting.query.get(mid) if mid else None
    if not meeting or meeting.creator_user_id != uid:
        flash("Choose a valid event to delete.", "danger")
        return redirect(url_for("main.platform_dashboard", _anchor="directory-pane"))

    mgid = meeting.meeting_group_id
    if mgid_form is not None and mgid_form != mgid:
        flash("That event does not belong to the selected group.", "danger")
        return redirect(
            url_for(
                "main.platform_dashboard",
                meeting_group_id=mgid_form,
                _anchor="directory-pane",
            )
        )

    sold = _sold_qty_by_meeting_ids([mid]).get(mid, 0)
    if sold > 0:
        flash(MEETING_LOCKED_AFTER_TICKET_SALES_TEXT, "danger")
        return redirect(
            url_for(
                "main.platform_dashboard",
                meeting_group_id=mgid,
                _anchor="directory-pane",
            )
        )

    MeetingAttendee.query.filter_by(meeting_id=mid).delete(synchronize_session=False)
    MeetingTicketType.query.filter_by(meeting_id=mid).delete(synchronize_session=False)
    db.session.delete(meeting)
    db.session.commit()
    flash("Event deleted.", "success")
    return redirect(
        url_for(
            "main.platform_dashboard",
            meeting_group_id=mgid,
            _anchor="directory-pane",
        )
    )


@bp.route("/meetings/<int:meeting_id>/image", methods=["POST"])
@login_required
def meeting_event_image_upload(meeting_id: int):
    """Upload a square-cropped event image (organiser). Updates events.image_name / image_location."""
    uid = session["user_id"]
    meeting = Meeting.query.get(meeting_id)
    if not meeting:
        return jsonify(ok=False, error="Event not found."), 404
    mg = MeetingGroup.query.get(meeting.meeting_group_id)
    if not mg or mg.user_id != uid:
        return jsonify(ok=False, error="You do not have permission to edit this event."), 403
    if _sold_qty_by_meeting_ids([meeting.meeting_id]).get(meeting.meeting_id, 0) > 0:
        return jsonify(ok=False, error=MEETING_LOCKED_AFTER_TICKET_SALES_TEXT), 400
    img = request.files.get("image")
    if not img or not (img.filename or "").strip():
        return jsonify(ok=False, error="No image uploaded."), 400
    try:
        img.stream.seek(0)
    except Exception:
        pass
    try:
        ensure_event_image_upload_dir()
        _apply_meeting_event_image_upload(meeting, img, uid)
        db.session.commit()
    except UnidentifiedImageError:
        return jsonify(
            ok=False,
            error="That file is not a valid image (JPG, PNG, or WEBP).",
        ), 400
    except Exception:
        current_app.logger.exception("meeting_event_image_upload meeting_id=%s", meeting_id)
        db.session.rollback()
        return jsonify(ok=False, error="Could not save the event image."), 500
    image_url = meeting_image_url(meeting, mg)
    return jsonify(
        ok=True,
        image_url=image_url,
        image_name=meeting.image_name,
        image_location=meeting.image_location,
    )


@bp.route("/meetings/create", methods=["GET", "POST"])
@login_required
def create_meeting():
    if request.method == "POST":
        _debug_meeting_create("POST received")
        _debug_meeting_create(
            "session_user_id="
            + repr(session.get("user_id"))
            + " args="
            + repr(request.args.to_dict(flat=True))
        )
        safe_form = {}
        for key in request.form.keys():
            safe_form[key] = request.form.getlist(key)
        _debug_meeting_create("form=" + repr(safe_form))

        uid = session["user_id"]
        mgid = request.form.get("meeting_group_id", type=int) or request.args.get(
            "meeting_group_id", type=int
        )
        meeting_group = MeetingGroup.query.get(mgid) if mgid else None
        if not meeting_group or meeting_group.user_id != uid:
            _debug_meeting_create(f"invalid meeting_group_id={mgid!r} for user_id={uid!r}")
            flash("Choose a valid event group before saving the event.", "danger")
            return redirect(url_for("main.platform_dashboard", _anchor="directory-pane"))

        meeting_id = request.form.get("meeting_id", type=int)
        meeting = None
        if meeting_id:
            meeting = Meeting.query.get(meeting_id)
            if (
                not meeting
                or meeting.meeting_group_id != meeting_group.meeting_group_id
                or meeting.creator_user_id != uid
            ):
                _debug_meeting_create(
                    f"invalid edit meeting_id={meeting_id!r} for group_id={mgid!r} user_id={uid!r}"
                )
                flash("Choose a valid draft event before saving.", "danger")
                return redirect(
                    url_for(
                        "main.platform_dashboard",
                        meeting_group_id=mgid,
                        _anchor="directory-pane",
                    )
                )

        if meeting is not None:
            if _sold_qty_by_meeting_ids([meeting.meeting_id]).get(meeting.meeting_id, 0) > 0:
                flash(MEETING_LOCKED_AFTER_TICKET_SALES_TEXT, "danger")
                return redirect(
                    url_for(
                        "main.platform_dashboard",
                        meeting_group_id=mgid,
                        _anchor="directory-pane",
                    )
                )

        status = (request.form.get("status") or "Draft").strip()
        if status not in {"Draft", "Live", "Completed", "Cancelled"}:
            status = "Draft"
        is_draft = status == "Draft"
        previous_meeting_status = (meeting.status if meeting else None)

        title = (request.form.get("title") or "").strip()
        subject = _sanitize_rich_text_html(request.form.get("subject"))
        meeting_format = (request.form.get("meeting_format") or "").strip() or None
        if meeting_format not in {"Face2Face", "Virtual", None}:
            meeting_format = None

        errors = []
        if meeting is None:
            if meeting_format not in {"Face2Face", "Virtual"}:
                errors.append(
                    "Choose face to face or virtual before creating the event (use the matching button)."
                )
        else:
            meeting_format = meeting.meeting_format or "Face2Face"

        if not title:
            errors.append("Title is required.")
        elif len(title) > 180:
            errors.append("Title must be 180 characters or fewer.")
        if not _rich_text_plain_text(subject):
            errors.append("Description is required.")

        try:
            starts_at = _parse_optional_datetime(request.form.get("starts_at"))
        except ValueError:
            starts_at = None
            errors.append("Start date/time is not valid.")

        try:
            duration_minutes = _meeting_form_duration_minutes_from_post(request.form)
        except ValueError:
            duration_minutes = None
            errors.append("Duration must be a whole number of minutes.")
        length_mode = (request.form.get("meeting_length_mode") or "duration").strip().lower()
        if length_mode == "end" and duration_minutes is None and not is_draft:
            errors.append("End time must be after the start time (at least 15 minutes later).")
        website_url, website_err = _normalize_optional_http_url(
            request.form.get("website_url")
        )
        if website_err:
            errors.append(website_err)

        if duration_minutes is not None and duration_minutes < 15:
            errors.append("Duration must be at least 15 minutes.")

        if not is_draft:
            if starts_at is None:
                errors.append("Live events need a start date and time.")
            if duration_minutes is None:
                errors.append("Live events need a duration.")
            if meeting_format == "Face2Face" and not (
                request.form.get("location_postcode") or ""
            ).strip():
                errors.append("Face-to-face events need a venue postcode.")
            if meeting_format == "Virtual":
                if not (request.form.get("virtual_platform") or "").strip():
                    errors.append("Virtual events need a platform.")
                if not (request.form.get("virtual_link") or "").strip():
                    errors.append("Virtual events need a link.")

        repeat_pattern_norm: str | None = None
        repeat_until_date: date | None = None
        repeat_nth_week: int | None = None
        repeat_weekday: int | None = None
        repeat_series_plan: list[datetime] = []

        raw_repeat = (request.form.get("repeat_pattern") or "").strip().lower()
        if raw_repeat in {"weekly", "monthly_dom", "monthly_nth"}:
            repeat_pattern_norm = raw_repeat

        if repeat_pattern_norm:
            if (
                meeting is not None
                and _sold_qty_by_meeting_ids([meeting.meeting_id]).get(meeting.meeting_id, 0) > 0
            ):
                errors.append(
                    "You cannot add a repeating schedule to an event that already has ticket sales."
                )
            if starts_at is None:
                errors.append(
                    "This event needs a start date and time before you add a repeating schedule."
                )

            rut = (request.form.get("repeat_until") or "").strip()
            if not rut:
                errors.append("Choose a repeat-until date for the repeating schedule.")
            else:
                try:
                    repeat_until_date = date.fromisoformat(rut)
                except ValueError:
                    errors.append("Repeat-until date is not valid.")

            if repeat_pattern_norm == "monthly_nth":
                nw_raw = (request.form.get("repeat_nth") or "1").strip().lower()
                if nw_raw == "last":
                    repeat_nth_week = -1
                else:
                    try:
                        repeat_nth_week = int(nw_raw)
                    except (TypeError, ValueError):
                        repeat_nth_week = None
                        errors.append(
                            'Choose “first … last weekday” correctly for the monthly repeat (or pick “Monthly same date”).'
                        )
                if repeat_nth_week is not None and repeat_nth_week not in {-1, 1, 2, 3, 4, 5}:
                    errors.append("Week-in-month choice must be First through Fifth, or Last.")
                    repeat_nth_week = None
                wd_raw = (request.form.get("repeat_weekday") or "").strip()
                try:
                    repeat_weekday = int(wd_raw)
                except (TypeError, ValueError):
                    repeat_weekday = None
                    errors.append("Choose a weekday for the monthly repeating pattern.")
                if repeat_weekday is not None and (repeat_weekday < 0 or repeat_weekday > 6):
                    errors.append("Choose a weekday for the monthly repeating pattern.")

            if starts_at is not None and repeat_until_date is not None:
                if repeat_until_date < starts_at.date():
                    errors.append(
                        "Repeat-until date must fall on or after this event's start date."
                    )

        wants_ajax = _request_wants_json_response()
        if errors:
            _debug_meeting_create("validation errors=" + repr(errors))
            if wants_ajax:
                return jsonify(ok=False, errors=errors), 400
            flash(" ".join(errors), "danger")
            return redirect(
                url_for(
                    "main.platform_dashboard",
                    meeting_group_id=mgid,
                    _anchor="directory-pane",
                )
            )

        if (
            repeat_pattern_norm
            and starts_at is not None
            and repeat_until_date is not None
        ):
            repeat_series_plan = _organiser_repeat_meeting_series(
                first=starts_at,
                pattern=repeat_pattern_norm,
                until=repeat_until_date,
                nth_week=repeat_nth_week,
                weekday=repeat_weekday,
            )

        repeat_extra_n = max(0, len(repeat_series_plan) - 1)

        if meeting is None:
            meeting = Meeting(
                meeting_group_id=meeting_group.meeting_group_id,
                creator_user_id=uid,
                created_at=datetime.utcnow(),
                is_paid_and_published=False,
            )

        meeting.title = title
        meeting.subject = subject
        meeting.starts_at = starts_at
        meeting.meeting_format = meeting_format
        meeting.duration_minutes = duration_minutes
        meeting.location_city = (request.form.get("location_city") or "").strip() or None
        meeting.location_postcode = (
            request.form.get("location_postcode") or ""
        ).strip() or None
        meeting.location_country = (
            request.form.get("location_country") or ""
        ).strip() or None
        meeting.venue_name = (request.form.get("venue_name") or "").strip() or None
        meeting.website_url = website_url
        meeting.address_line1 = (request.form.get("address_line1") or "").strip() or None
        meeting.address_line2 = (request.form.get("address_line2") or "").strip() or None
        meeting.address_town = (
            (request.form.get("address_town") or "").strip()
            or (request.form.get("location_city") or "").strip()
            or None
        )
        meeting.address_county = (
            request.form.get("address_county") or ""
        ).strip() or None
        meeting.address_postcode = (
            (request.form.get("address_postcode") or "").strip()
            or (request.form.get("location_postcode") or "").strip()
            or None
        )
        meeting.address_country = (
            request.form.get("address_country") or ""
        ).strip() or None
        meeting.latitude = _parse_optional_decimal(request.form.get("latitude"))
        meeting.longitude = _parse_optional_decimal(request.form.get("longitude"))
        meeting.virtual_platform = (
            request.form.get("virtual_platform") or ""
        ).strip() or None
        meeting.virtual_link = (request.form.get("virtual_link") or "").strip() or None
        meeting.status = status

        bulk_copies: list[Meeting] = []
        repeat_title_base = title.strip()

        try:
            db.session.add(meeting)
            db.session.flush()

            event_image = request.files.get("event_image")
            if event_image and (event_image.filename or "").strip():
                _apply_meeting_event_image_upload(meeting, event_image, uid)

            if repeat_extra_n > 0:
                meeting.title = _repeat_series_numbered_title(repeat_title_base, 1)
                for idx, occ in enumerate(repeat_series_plan[1:], start=2):
                    dup = Meeting(
                        meeting_group_id=meeting.meeting_group_id,
                        creator_user_id=uid,
                        created_at=datetime.utcnow(),
                        title=_repeat_series_numbered_title(repeat_title_base, idx),
                        subject=meeting.subject,
                        starts_at=occ,
                        meeting_format=meeting.meeting_format,
                        duration_minutes=meeting.duration_minutes,
                        location_city=meeting.location_city,
                        location_postcode=meeting.location_postcode,
                        location_country=meeting.location_country,
                        venue_name=meeting.venue_name,
                        website_url=meeting.website_url,
                        address_line1=meeting.address_line1,
                        address_line2=meeting.address_line2,
                        address_town=meeting.address_town,
                        address_county=meeting.address_county,
                        address_postcode=meeting.address_postcode,
                        address_country=meeting.address_country,
                        latitude=meeting.latitude,
                        longitude=meeting.longitude,
                        virtual_platform=meeting.virtual_platform,
                        virtual_link=meeting.virtual_link,
                        is_paid_and_published=bool(meeting.is_paid_and_published),
                        status=meeting.status or "Draft",
                    )
                    db.session.add(dup)
                    bulk_copies.append(dup)

                db.session.flush()
                master_id = int(meeting.meeting_id)
                for dup in bulk_copies:
                    _clone_meeting_ticket_types_organiser(master_id, int(dup.meeting_id))

            db.session.commit()

            if not is_draft:
                _tnw_commit_event_listing_record(
                    int(uid), int(meeting.meeting_id), previous_meeting_status
                )
                for dup in bulk_copies:
                    if (dup.status or "").strip() == "Live":
                        _tnw_commit_event_listing_record(
                            int(uid), int(dup.meeting_id), "Draft"
                        )

            extra_note = ""
            if repeat_extra_n:
                plural = "event" if repeat_extra_n == 1 else "events"
                extra_note = (
                    f" Another {repeat_extra_n} repeating {plural} were saved with the same details."
                )

            msg = ("Event saved as draft." if is_draft else "Event saved.") + extra_note
            _debug_meeting_create(
                f"saved meeting_id={meeting.meeting_id!r} status={status!r}"
                + (f" repeat_extra={repeat_extra_n}" if repeat_extra_n else "")
            )
            if wants_ajax:
                return jsonify(
                    ok=True,
                    message=msg,
                    meeting_id=int(meeting.meeting_id),
                    events_html=_render_directory_events_sections_html(mgid, uid),
                )
            flash(msg, "success")
        except UnidentifiedImageError:
            db.session.rollback()
            err = "Event image must be a JPG, PNG, or WEBP file."
            if wants_ajax:
                return jsonify(ok=False, errors=[err]), 400
            flash(err, "danger")
            return redirect(
                url_for(
                    "main.platform_dashboard",
                    meeting_group_id=mgid,
                    _anchor="directory-pane",
                )
            )
        except Exception as exc:
            db.session.rollback()
            _debug_meeting_create(
                f"database save failed {type(exc).__name__}: {exc}"
            )
            try:
                current_app.logger.exception("[meeting-create] database save failed")
            except Exception:
                pass
            if wants_ajax:
                return (
                    jsonify(
                        ok=False,
                        error="The event could not be saved. Check the terminal for details.",
                    ),
                    500,
                )
            flash("The event could not be saved. Check the terminal for details.", "danger")

        if wants_ajax:
            return jsonify(ok=True, message=msg)
        if request.form.get("return_to_dashboard") == "1":
            # Return to the group list with the editor collapsed so the saved event
            # appears once in the accordion (no edit_meeting_id + open panel duplicate).
            return redirect(
                url_for(
                    "main.platform_dashboard",
                    meeting_group_id=mgid,
                    _anchor="directory-pane",
                )
            )
        return redirect(url_for("main.meeting_list"))
    return render_template("meeting_create.html")


@bp.route("/meetings/<int:meeting_id>/event.ics")
def meeting_calendar_ics(meeting_id):
    meeting = Meeting.query.get(meeting_id)
    if not meeting or not meeting.starts_at:
        abort(404)
    detail_url = url_for(
        "main.meeting_detail", meeting_id=meeting.meeting_id, _external=True
    )
    title_decoded = unescape(meeting.title or "Event")
    subject_plain = _rich_text_plain_text(meeting.subject or "")
    body = _meeting_calendar_ics_text(
        meeting, detail_url, title_decoded, subject_plain
    )
    if not body:
        abort(404)
    filename = f"thenetworker-event-{meeting_id}.ics"
    return Response(
        body,
        mimetype="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, max-age=300",
        },
    )


@bp.route("/meetings/<int:meeting_id>")
def meeting_detail(meeting_id):
    lbl_mt = table_label("events", "Events")
    lbl_mg = table_label("event_groups", "Event groups")
    meeting = Meeting.query.options(
        selectinload(Meeting.meeting_group).selectinload(MeetingGroup.owner),
        selectinload(Meeting.meeting_group).selectinload(MeetingGroup.industry),
        selectinload(Meeting.ticket_types),
    ).get(meeting_id)
    if not meeting:
        flash("That event was not found.", "warning")
        return redirect(url_for("main.site_search"))

    organiser = meeting.meeting_group.owner if meeting.meeting_group else None
    organiser_name = _user_display_name(organiser) if organiser else ""
    organiser_email = (organiser.email or "").strip() if organiser else ""
    maps_search_url = _google_maps_search_url(meeting)
    now_utc = datetime.utcnow()
    is_live_event = (meeting.status or "").strip().lower() == "live"
    sale_ticket = _first_active_sale_ticket(meeting, now_utc)
    uid = session.get("user_id")
    remaining_global = (
        _remaining_tickets_for_sale(sale_ticket) if sale_ticket else 0
    )
    tickets_on_sale = bool(is_live_event and sale_ticket and remaining_global > 0)
    tickets_sold_out = bool(
        is_live_event and sale_ticket and remaining_global <= 0
    )
    need_login_to_buy = bool(tickets_on_sale and not uid)
    can_buy_ticket = bool(tickets_on_sale and uid)
    ticket_price_text = "Details to follow"
    purchase_max_qty = (
        max(1, remaining_global) if sale_ticket and remaining_global > 0 else 1
    )
    checkout_unit_price = Decimal("0")
    if sale_ticket:
        try:
            checkout_unit_price = buyer_unit_price_for_ticket(sale_ticket)
        except (InvalidOperation, TypeError):
            checkout_unit_price = Decimal("0")
        ticket_price_text = (
            "Free"
            if checkout_unit_price <= 0
            else f"GBP {checkout_unit_price:.2f}"
        )

    subject_html = Markup(_sanitize_rich_text_html(meeting.subject or ""))
    subject_plain = _rich_text_plain_text(meeting.subject or "")

    mg = meeting.meeting_group
    industry_label = ""
    if mg and mg.industry and (mg.industry.industry or "").strip():
        industry_label = (mg.industry.industry or "").strip()
    if not industry_label:
        industry_label = "General Business"

    time_range_text = ""
    if meeting.starts_at:
        st = meeting.starts_at
        try:
            dm = int(meeting.duration_minutes or 60) or 60
            end = st + timedelta(minutes=dm)
            time_range_text = f"{st.strftime('%H:%M')} – {end.strftime('%H:%M')}"
        except (TypeError, ValueError, OverflowError):
            time_range_text = st.strftime("%H:%M")

    tickets_remaining = int(remaining_global) if sale_ticket else 0

    similar_meetings = _similar_meetings_for_detail(meeting, now_utc, limit=20)
    sim_ids = [int(m.meeting_id) for m in similar_meetings]
    sim_booking_by_mid: dict[int, int] = {}
    if sim_ids:
        for smid, n in (
            db.session.query(
                MeetingAttendee.meeting_id,
                func.count(MeetingAttendee.meeting_attendee_id),
            )
            .filter(MeetingAttendee.meeting_id.in_(sim_ids))
            .group_by(MeetingAttendee.meeting_id)
            .all()
        ):
            sim_booking_by_mid[int(smid)] = int(n)
    for sm in similar_meetings:
        setattr(sm, "_home_price_label", _home_featured_meeting_price_label(sm, now_utc))
        setattr(sm, "_home_booking_count", sim_booking_by_mid.get(int(sm.meeting_id), 0))
        smg = sm.meeting_group
        ind = ""
        if smg and smg.industry and (smg.industry.industry or "").strip():
            ind = (smg.industry.industry or "").strip()
        setattr(sm, "_home_industry_label", ind or "General Business")
        setattr(sm, "_home_location_short", _home_meeting_short_location(sm))

    open_checkout = request.args.get("open_checkout") == "1"
    prefill_qty = request.args.get("quantity", type=int)
    buy_availability = _meeting_ticket_buy_availability(meeting, uid)
    checkout_blocked_message = ""
    if open_checkout and not buy_availability.get("can_buy"):
        parts = [
            buy_availability.get("reason_title") or "Tickets unavailable",
            buy_availability.get("reason_detail") or "",
        ]
        checkout_blocked_message = " — ".join(p for p in parts if p)

    meeting_is_saved = False
    if uid and is_live_event:
        meeting_is_saved = (
            UserSavedMeeting.query.filter_by(
                user_id=int(uid), meeting_id=int(meeting.meeting_id)
            ).first()
            is not None
        )

    calendar_add_available = meeting.starts_at is not None
    calendar_google_url = None
    calendar_outlook_url = None
    calendar_ics_url = None
    if calendar_add_available:
        detail_url = url_for(
            "main.meeting_detail", meeting_id=meeting.meeting_id, _external=True
        )
        title_decoded = unescape(meeting.title or "Event")
        calendar_google_url = _google_calendar_add_url(
            meeting, detail_url, title_decoded, subject_plain or ""
        )
        calendar_outlook_url = _outlook_calendar_add_url(
            meeting, detail_url, title_decoded, subject_plain or ""
        )
        calendar_ics_url = url_for("main.meeting_calendar_ics", meeting_id=meeting_id)

    return render_template(
        "meeting_detail.html",
        meeting=meeting,
        lbl_mt=lbl_mt,
        lbl_mg=lbl_mg,
        subject_html=subject_html,
        subject_plain=subject_plain,
        can_buy_ticket=can_buy_ticket,
        need_login_to_buy=need_login_to_buy,
        tickets_sold_out=tickets_sold_out,
        sale_ticket=sale_ticket,
        checkout_unit_price=checkout_unit_price,
        ticket_price_text=ticket_price_text,
        purchase_max_qty=purchase_max_qty,
        organiser_name=organiser_name,
        organiser_email=organiser_email,
        maps_search_url=maps_search_url,
        open_checkout_request=bool(open_checkout and can_buy_ticket),
        checkout_blocked_message=checkout_blocked_message,
        buy_availability=buy_availability,
        checkout_prefill_quantity=prefill_qty,
        industry_label=industry_label,
        time_range_text=time_range_text,
        tickets_remaining=tickets_remaining,
        similar_meetings=similar_meetings,
        meeting_save_allowed=is_live_event,
        meeting_is_saved=meeting_is_saved,
        calendar_add_available=calendar_add_available,
        calendar_google_url=calendar_google_url,
        calendar_outlook_url=calendar_outlook_url,
        calendar_ics_url=calendar_ics_url,
    )


def _first_active_sale_ticket(meeting, now_utc=None):
    now_utc = now_utc or datetime.utcnow()
    for tt in sorted(
        list(meeting.ticket_types or []),
        key=lambda t: (t.sort_order or 0, t.ticket_type_id or 0),
    ):
        if (tt.status or "").strip().lower() != "active":
            continue
        if tt.sales_open_at and tt.sales_open_at > now_utc:
            continue
        if tt.sales_close_at and tt.sales_close_at < now_utc:
            continue
        return tt
    return None


def _explain_no_active_sale_ticket(meeting, now_utc=None) -> tuple[str, str, str]:
    """Human-readable reason when no ticket type is on sale (title, detail, code)."""
    now_utc = now_utc or datetime.utcnow()
    types = sorted(
        list(meeting.ticket_types or []),
        key=lambda t: (t.sort_order or 0, t.ticket_type_id or 0),
    )
    if not types:
        return (
            "No tickets set up",
            "Add at least one ticket type and publish it as Active before buyers can purchase.",
            "no_ticket_types",
        )
    active_types = [t for t in types if (t.status or "").strip().lower() == "active"]
    if not active_types:
        draft_only = all((t.status or "").strip().lower() == "draft" for t in types)
        if draft_only:
            return (
                "Tickets not published",
                "Your ticket types are still in Draft. Publish one as Active to open sales.",
                "draft_only",
            )
        return (
            "No active ticket types",
            "Publish at least one ticket type as Active to open sales.",
            "no_active_types",
        )
    future_opens = [
        t for t in active_types if t.sales_open_at and t.sales_open_at > now_utc
    ]
    if future_opens and len(future_opens) == len(active_types):
        soonest = min(t.sales_open_at for t in future_opens if t.sales_open_at)
        return (
            "Sales not open yet",
            f"Ticket sales open on {soonest.strftime('%d %b %Y')}.",
            "sales_not_open",
        )
    past_closes = [
        t
        for t in active_types
        if t.sales_close_at and t.sales_close_at < now_utc
        and (not t.sales_open_at or t.sales_open_at <= now_utc)
    ]
    if past_closes and len(past_closes) == len(active_types):
        return (
            "Sales closed",
            "The sales window for all active ticket types has ended.",
            "sales_closed",
        )
    return (
        "Tickets not on sale",
        "No ticket type is currently available for purchase. Check status and sales dates.",
        "not_on_sale",
    )


def _meeting_ticket_buy_availability(meeting: Meeting, user_id=None) -> dict:
    """Public purchase availability for an event (mirrors meeting_detail buy panel)."""
    now_utc = datetime.utcnow()
    uid = user_id if user_id is not None else session.get("user_id")
    status = (meeting.status or "").strip().lower()
    fmt = (meeting.meeting_format or "").strip()
    public_url = url_for("main.meeting_detail", meeting_id=meeting.meeting_id)
    buy_url = url_for(
        "main.meeting_detail",
        meeting_id=meeting.meeting_id,
        open_checkout=1,
    )

    out = {
        "can_buy": False,
        "need_login": False,
        "public_url": public_url,
        "buy_url": buy_url,
        "reason_title": "",
        "reason_detail": "",
        "reason_code": "",
        "ticket_price_text": "",
        "tickets_remaining": 0,
        "is_live": status == "live",
    }

    if fmt == "Virtual":
        out["reason_code"] = "virtual"
        out["reason_title"] = "Virtual event"
        out["reason_detail"] = (
            "Ticket purchases apply to in-person events. "
            "Virtual events do not use this ticket checkout."
        )
        return out

    if status != "live":
        label = (meeting.status or "Draft").strip() or "Draft"
        out["reason_code"] = "not_live"
        out["reason_title"] = "Event not live"
        out["reason_detail"] = (
            f"This event is {label}. Tickets open when the event status is Live."
        )
        return out

    sale_ticket = _first_active_sale_ticket(meeting, now_utc)
    if not sale_ticket:
        title, detail, code = _explain_no_active_sale_ticket(meeting, now_utc)
        out["reason_code"] = code
        out["reason_title"] = title
        out["reason_detail"] = detail
        return out

    remaining = int(_remaining_tickets_for_sale(sale_ticket) or 0)
    out["tickets_remaining"] = remaining
    try:
        unit = buyer_unit_price_for_ticket(sale_ticket)
    except (InvalidOperation, TypeError):
        unit = Decimal("0")
    out["ticket_price_text"] = (
        "Free" if unit <= 0 else f"GBP {unit:.2f}"
    )

    if remaining <= 0:
        out["reason_code"] = "sold_out"
        out["reason_title"] = "Sold out"
        out["reason_detail"] = "All available tickets for this event have been sold."
        return out

    if not uid:
        out["need_login"] = True
        out["reason_code"] = "login_required"
        out["reason_title"] = "Sign in required"
        out["reason_detail"] = (
            "Tickets are on sale. Sign in with the account you want to use at checkout."
        )
        return out

    out["can_buy"] = True
    out["reason_code"] = "available"
    return out


# Rows that reduce inventory for ticket-type capacity.
_ATTENDEE_COUNTABLE_STATUSES = ("Reserved", "Confirmed", "Attended")

MEETING_LOCKED_AFTER_TICKET_SALES_TEXT = (
    "This event can no longer be edited or deleted because tickets have already been sold. "
    "This protects buyers and ensures fairness."
)


def _sold_qty_by_meeting_ids(meeting_ids: list[int]) -> dict[int, int]:
    """Total countable ticket quantities per meeting (Reserved/Confirmed/Attended)."""
    if not meeting_ids:
        return {}
    rows = (
        db.session.query(
            MeetingAttendee.meeting_id,
            func.coalesce(func.sum(MeetingAttendee.quantity), 0),
        )
        .filter(
            MeetingAttendee.meeting_id.in_(meeting_ids),
            MeetingAttendee.status.in_(_ATTENDEE_COUNTABLE_STATUSES),
        )
        .group_by(MeetingAttendee.meeting_id)
        .all()
    )
    return {int(mid): int(qty or 0) for mid, qty in rows}


def _meeting_group_ids_with_ticket_sales(meeting_group_ids: list[int]) -> set[int]:
    """Meeting group IDs that have at least one sold ticket on any event."""
    if not meeting_group_ids:
        return set()
    rows = (
        db.session.query(Meeting.meeting_group_id, Meeting.meeting_id)
        .filter(Meeting.meeting_group_id.in_(meeting_group_ids))
        .all()
    )
    if not rows:
        return set()
    mg_to_mids: dict[int, list[int]] = {}
    all_mids: list[int] = []
    for mgid, mid in rows:
        mgid_i = int(mgid)
        mid_i = int(mid)
        mg_to_mids.setdefault(mgid_i, []).append(mid_i)
        all_mids.append(mid_i)
    sold = _sold_qty_by_meeting_ids(all_mids)
    return {
        mgid
        for mgid, mids in mg_to_mids.items()
        if any(sold.get(m, 0) > 0 for m in mids)
    }


def _sold_qty_by_ticket_type_ids(ticket_type_ids: list[int]) -> dict[int, int]:
    """Countable ticket quantities sold per ticket type (Reserved/Confirmed/Attended)."""
    if not ticket_type_ids:
        return {}
    rows = (
        db.session.query(
            MeetingAttendee.ticket_type_id,
            func.coalesce(func.sum(MeetingAttendee.quantity), 0),
        )
        .filter(
            MeetingAttendee.ticket_type_id.in_(ticket_type_ids),
            MeetingAttendee.status.in_(_ATTENDEE_COUNTABLE_STATUSES),
        )
        .group_by(MeetingAttendee.ticket_type_id)
        .all()
    )
    return {int(tid): int(qty or 0) for tid, qty in rows}


def _sum_attendee_qty_for_ticket_type(ticket_type_id, user_id=None):
    q = db.session.query(func.coalesce(func.sum(MeetingAttendee.quantity), 0)).filter(
        MeetingAttendee.ticket_type_id == ticket_type_id,
        MeetingAttendee.status.in_(_ATTENDEE_COUNTABLE_STATUSES),
    )
    if user_id is not None:
        q = q.filter(MeetingAttendee.user_id == user_id)
    return int(q.scalar() or 0)


def _remaining_tickets_for_sale(ticket, meeting_id=None):
    """Unsold seats for this ticket type (global, all users). meeting_id is optional (callers may pass it)."""
    try:
        max_qty = int(ticket.max_quantity or 0)
    except (TypeError, ValueError):
        max_qty = 0
    sold = _sum_attendee_qty_for_ticket_type(ticket.ticket_type_id, user_id=None)
    return max(0, max_qty - sold)


def _home_meeting_has_tickets_available(meeting: Meeting, now_utc=None):
    now_utc = now_utc or datetime.utcnow()
    tt = _first_active_sale_ticket(meeting, now_utc)
    if not tt:
        return False
    return _remaining_tickets_for_sale(tt, meeting.meeting_id) > 0


def _meeting_tag_overlap_count(meeting: Meeting, user_tag_ids: set[int]) -> int:
    mg = meeting.meeting_group
    if not mg or not user_tag_ids or not mg.tags:
        return 0
    gid_tags = {int(t.tag_id) for t in mg.tags if t.tag_id is not None}
    return len(gid_tags & user_tag_ids)


def _favourites_page_similar_meetings(
    user: User,
    favourite_meetings: list[Meeting],
    *,
    now_utc: datetime | None = None,
    pool_limit: int = 450,
    result_limit: int = 25,
) -> list[Meeting]:
    """Live upcoming meetings with tickets, excluding favourites, ranked by tag overlap with
    the user's profile keywords plus tags from groups of their saved meetings."""
    now_utc = now_utc or datetime.utcnow()
    interest: set[int] = set()
    for t in user.attendee_tags or []:
        if t and t.tag_id is not None:
            interest.add(int(t.tag_id))
    for m in favourite_meetings:
        mg = m.meeting_group
        if not mg or not mg.tags:
            continue
        for t in mg.tags:
            if t and t.tag_id is not None:
                interest.add(int(t.tag_id))
    if not interest:
        return []

    fav_ids = {int(m.meeting_id) for m in favourite_meetings if m and m.meeting_id is not None}
    q = (
        Meeting.query.join(
            MeetingGroup, Meeting.meeting_group_id == MeetingGroup.meeting_group_id
        )
        .filter(Meeting.status == "Live")
        .filter(Meeting.starts_at.isnot(None))
        .filter(Meeting.starts_at >= now_utc)
        .filter(
            or_(
                and_(
                    MeetingGroup.image_filename.isnot(None),
                    MeetingGroup.image_filename != "",
                ),
                Meeting.meeting_format == "Virtual",
            )
        )
        .options(
            selectinload(Meeting.meeting_group).selectinload(MeetingGroup.industry),
            selectinload(Meeting.meeting_group).selectinload(MeetingGroup.tags),
            selectinload(Meeting.ticket_types),
        )
    )
    if fav_ids:
        q = q.filter(~Meeting.meeting_id.in_(fav_ids))
    q = q.order_by(Meeting.starts_at.asc(), Meeting.meeting_id.asc()).limit(pool_limit)
    rows = q.all()
    with_tickets = [m for m in rows if _home_meeting_has_tickets_available(m, now_utc)]
    scored = [m for m in with_tickets if _meeting_tag_overlap_count(m, interest) > 0]

    def _overlap(m: Meeting) -> int:
        return _meeting_tag_overlap_count(m, interest)

    def _start_key(m: Meeting) -> float:
        if not m.starts_at:
            return 0.0
        try:
            return m.starts_at.timestamp()
        except (OSError, OverflowError, ValueError):
            return 0.0

    scored.sort(
        key=lambda m: (-_overlap(m), _start_key(m), int(m.meeting_id)),
    )
    seen_gid: set[int] = set()
    out: list[Meeting] = []
    for m in scored:
        gid = int(m.meeting_group_id)
        if gid in seen_gid:
            continue
        seen_gid.add(gid)
        out.append(m)
        if len(out) >= result_limit:
            break
    return out


def _home_distance_sort_miles(
    meeting: Meeting, ref_lat: float, ref_lng: float
) -> float | None:
    la = _numeric_or_none(meeting.latitude)
    lo = _numeric_or_none(meeting.longitude)
    if la is None or lo is None:
        return None
    return _haversine_miles(ref_lat, ref_lng, la, lo)


def _home_carousel_meeting_lists(
    now_utc: datetime,
    *,
    ref_lat: float | None,
    ref_lng: float | None,
    user_tag_ids: set[int] | None,
    logged_in: bool,
    carousel1_limit: int = 30,
    carousel2_limit: int = 30,
) -> tuple[list[Meeting], list[Meeting]]:
    """Home carousels: F2F (nearby / personalised) and Virtual (soonest / tags); tickets required; one row per group each."""
    rows = (
        Meeting.query.join(
            MeetingGroup, Meeting.meeting_group_id == MeetingGroup.meeting_group_id
        )
        .filter(Meeting.status == "Live")
        .filter(Meeting.starts_at.isnot(None))
        .filter(Meeting.starts_at >= now_utc)
        .filter(
            or_(
                and_(
                    MeetingGroup.image_filename.isnot(None),
                    MeetingGroup.image_filename != "",
                ),
                Meeting.meeting_format == "Virtual",
            )
        )
        .options(
            selectinload(Meeting.meeting_group).selectinload(MeetingGroup.industry),
            selectinload(Meeting.meeting_group).selectinload(MeetingGroup.tags),
            selectinload(Meeting.ticket_types),
        )
        .order_by(Meeting.starts_at.asc(), Meeting.meeting_id.asc())
        .limit(600)
        .all()
    )
    with_tickets = [m for m in rows if _home_meeting_has_tickets_available(m, now_utc)]

    def _dedupe_next_per_group(meetings: list[Meeting]) -> list[Meeting]:
        seen_gid: set[int] = set()
        out: list[Meeting] = []
        for m in meetings:
            gid = int(m.meeting_group_id)
            if gid in seen_gid:
                continue
            seen_gid.add(gid)
            out.append(m)
        return out

    f2f_pool = [
        m
        for m in with_tickets
        if (m.meeting_format or "").strip() != "Virtual"
    ]
    virt_pool = [
        m
        for m in with_tickets
        if (m.meeting_format or "").strip() == "Virtual"
    ]
    f2f_next = _dedupe_next_per_group(f2f_pool)
    virt_next = _dedupe_next_per_group(virt_pool)

    tag_ids = user_tag_ids or set()

    def _carousel_is_virtual(m: Meeting) -> bool:
        return (m.meeting_format or "").strip() == "Virtual"

    def _carousel_ts(m: Meeting) -> float:
        return m.starts_at.timestamp() if m.starts_at else 0.0

    def _carousel_dist_miles(m: Meeting) -> float | None:
        if ref_lat is None or ref_lng is None:
            return None
        return _home_distance_sort_miles(m, ref_lat, ref_lng)

    def _cmp_guest_carousel(a: Meeting, b: Meeting) -> int:
        """Face-to-face: nearer first, then sooner. Virtual: ignore distance; mix with F2F by start time."""
        va, vb = _carousel_is_virtual(a), _carousel_is_virtual(b)
        sa, sb = _carousel_ts(a), _carousel_ts(b)
        if va and vb:
            return (sa > sb) - (sa < sb)
        if va or vb:
            return (sa > sb) - (sa < sb)
        da, db = _carousel_dist_miles(a), _carousel_dist_miles(b)
        if da is not None and db is not None and da != db:
            return (da > db) - (da < db)
        if da is not None and db is None:
            return -1
        if da is None and db is not None:
            return 1
        return (sa > sb) - (sa < sb)

    def _cmp_logged_carousel(a: Meeting, b: Meeting) -> int:
        """Higher keyword overlap first; then same rules as guest (virtual ignores distance)."""
        oa = _meeting_tag_overlap_count(a, tag_ids)
        ob = _meeting_tag_overlap_count(b, tag_ids)
        if oa != ob:
            return -1 if oa > ob else (1 if oa < ob else 0)
        return _cmp_guest_carousel(a, b)

    if logged_in:
        f2f_next.sort(key=cmp_to_key(_cmp_logged_carousel))
        virt_next.sort(key=cmp_to_key(_cmp_logged_carousel))
    else:
        f2f_next.sort(key=cmp_to_key(_cmp_guest_carousel))
        virt_next.sort(key=cmp_to_key(_cmp_guest_carousel))

    return f2f_next[:carousel1_limit], virt_next[:carousel2_limit]


def _meeting_is_face_to_face(meeting):
    return (getattr(meeting, "meeting_format", None) or "Face2Face") == "Face2Face"


def _attendee_event_format_label(meeting):
    if not meeting:
        return "—"
    return (
        "Virtual"
        if (getattr(meeting, "meeting_format", None) or "Face2Face") == "Virtual"
        else "In person"
    )


def _ticket_checkin_qr_payload(meeting_id, entry_token):
    """Payload embedded in attendee QR codes for in-person door check-in (organiser scanner to follow)."""
    return f"tnw1|{int(meeting_id)}|{entry_token}"


def _legacy_f2f_attendee_checkin_payload(meeting_attendee_id: int) -> str:
    """QR payload when per-seat ``event_ticket_entries`` rows are absent (older bookings or pre-migration)."""
    sk = current_app.config.get("SECRET_KEY") or ""
    sk_bytes = sk.encode("utf-8") if isinstance(sk, str) else bytes(sk) if sk else b""
    if not sk_bytes:
        sk_bytes = b"tnw-qr-fallback-no-secret"
    mac = hmac.new(
        sk_bytes,
        f"tnw-f2f-attendee|{int(meeting_attendee_id)}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:40]
    return f"tnw1|legacy|{int(meeting_attendee_id)}|{mac}"


def _send_ticket_purchase_email(
    recipient_email,
    meeting,
    ticket,
    quantity,
    total_amount,
    checkin_qr_data_uris=None,
    *,
    invoice_pdf: bytes | None = None,
    invoice_attach_name: str | None = None,
    vat_note: str | None = None,
    invoice_no: str | None = None,
):
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    site_name = os.getenv("SITE_NAME", "The Networker")
    site_url = os.getenv("SITE_URL", "https://myailessons.one")
    support_email = os.getenv("SUPPORT_EMAIL", smtp_user)
    if not smtp_user or not smtp_password:
        raise RuntimeError("SMTP credentials are missing")

    meeting_link = urljoin(
        f"{site_url.rstrip('/')}/",
        url_for("main.meeting_detail", meeting_id=meeting.meeting_id).lstrip("/"),
    )
    unit_price = buyer_unit_price_for_ticket(ticket)
    starts_at_text = (
        meeting.starts_at.strftime("%a %d %b %Y, %H:%M")
        if meeting.starts_at
        else "Date/time to be confirmed"
    )
    location_text = (
        meeting.location_city
        or meeting.address_town
        or meeting.location_postcode
        or "Location to be confirmed"
    )

    inv_plain = ""
    if invoice_pdf and invoice_attach_name:
        inv_plain = (
            f"Your tax invoice ({invoice_no or invoice_attach_name}) is attached as a PDF.\n"
            + (f"{vat_note}\n\n" if vat_note else "\n")
        )
    subject = f"Your tickets for {meeting.title}"
    text_body = (
        f"Thanks for your purchase — tickets to {meeting.title}.\n\n"
        f"Quantity: {quantity}\n"
        f"Ticket: {ticket.ticket_name}\n"
        f"Unit price: GBP {unit_price:.2f}\n"
        f"Total: GBP {total_amount:.2f}\n"
        f"When: {starts_at_text}\n"
        f"Where: {location_text}\n\n"
        + inv_plain
        + (
            "This is an in-person event: admission QR codes are in the HTML version of this email "
            "(one code per ticket). Show each code at the door for the organiser to scan.\n\n"
            if checkin_qr_data_uris
            else ""
        )
        + "This is a confirmation from our dummy checkout flow. Live payment capture "
        "will be added shortly.\n\n"
        f"Event page: {meeting_link}\n\n"
        f"{site_name}\nSupport: {support_email}"
    )
    qr_html = ""
    if checkin_qr_data_uris:
        parts = [
            "<div style=\"margin:18px 0;padding:14px 16px;border:1px solid #cdb4df;border-radius:10px;"
            "background:#faf6fc;\">"
            "<p style=\"margin:0 0 10px;font-weight:bold;color:#5b2d73;\">In-person admission</p>"
            "<p style=\"margin:0 0 12px;font-size:14px;\">Show <strong>one QR code per guest</strong> "
            "when you arrive. The organiser will scan them at the door.</p>"
        ]
        for i, uri in enumerate(checkin_qr_data_uris, start=1):
            parts.append(
                f"<div style=\"display:inline-block;margin:6px 12px 6px 0;text-align:center;vertical-align:top;\">"
                f"<img src=\"{escape(uri)}\" width=\"168\" height=\"168\" alt=\"Admission QR {i}\" "
                "style=\"display:block;border-radius:8px;border:1px solid #e2d4ee;\">"
                f"<span style=\"font-size:12px;color:#5b2d73;\">Guest {i}</span></div>"
            )
        parts.append("</div>")
        qr_html = "".join(parts)
    html_body = (
        "<html><body style=\"font-family:Arial,Helvetica,sans-serif;color:#2f1f3a;\">"
        f"<h2 style=\"color:#5b2d73;\">Thanks for purchasing tickets to {escape(meeting.title)}</h2>"
        "<p>We have recorded your ticket request details:</p>"
        "<ul>"
        f"<li><strong>Quantity:</strong> {quantity}</li>"
        f"<li><strong>Ticket:</strong> {escape(ticket.ticket_name or 'General admission')}</li>"
        f"<li><strong>Unit price:</strong> GBP {unit_price:.2f}</li>"
        f"<li><strong>Total:</strong> GBP {total_amount:.2f}</li>"
        f"<li><strong>When:</strong> {escape(starts_at_text)}</li>"
        f"<li><strong>Where:</strong> {escape(location_text)}</li>"
        "</ul>"
        + qr_html
        + (
            f"<p style=\"font-size:13px;color:#6b5c75;\"><strong>Tax invoice</strong> "
            f"({escape(invoice_no or 'attached')}) is included as a PDF with this email."
            + (f"<br>{escape(vat_note)}" if vat_note else "")
            + "</p>"
            if invoice_pdf
            else ""
        )
        + "<p>This is currently a dummy checkout confirmation (no payment captured yet).</p>"
        f"<p><a href=\"{escape(meeting_link)}\">View event details</a></p>"
        f"<p style=\"font-size:12px;color:#6b5c75;\">{escape(site_name)}<br>Support: {escape(support_email)}</p>"
        "</body></html>"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = recipient_email
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=smtp_user.split("@")[-1])
    msg["X-Mailer"] = f"{site_name} Ticketing"
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    if invoice_pdf and invoice_attach_name:
        msg.add_attachment(
            invoice_pdf,
            maintype="application",
            subtype="pdf",
            filename=invoice_attach_name,
        )

    with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)


def _send_meeting_question_email(
    meeting,
    organiser_email,
    sender_name,
    sender_email,
    question_text,
):
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    site_name = os.getenv("SITE_NAME", "The Networker")
    if not smtp_user or not smtp_password:
        raise RuntimeError("SMTP credentials are missing")

    subject = f"[{site_name}] Question about {meeting.title}"
    safe_title = escape(meeting.title or "event")
    safe_sender_name = escape(sender_name)
    safe_sender_email = escape(sender_email)
    safe_question_html = escape(question_text).replace("\n", "<br>")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = organiser_email
    msg["Cc"] = sender_email
    msg["Reply-To"] = sender_email
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=smtp_user.split("@")[-1])
    msg.set_content(
        (
            f"Question about event: {meeting.title}\n\n"
            f"From: {sender_name} <{sender_email}>\n\n"
            f"{question_text}\n"
        )
    )
    msg.add_alternative(
        (
            "<html><body style=\"font-family:Arial,Helvetica,sans-serif;color:#2f1f3a;\">"
            f"<h2 style=\"color:#5b2d73;\">Event question: {safe_title}</h2>"
            f"<p><strong>From:</strong> {safe_sender_name} ({safe_sender_email})</p>"
            f"<p>{safe_question_html}</p>"
            "<p style=\"font-size:12px;color:#6b5c75;\">A copy was sent to both organiser and sender.</p>"
            "</body></html>"
        ),
        subtype="html",
    )

    with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)


@bp.route("/meetings/<int:meeting_id>/buy-ticket", methods=["POST"])
def meeting_buy_ticket(meeting_id):
    meeting = Meeting.query.options(selectinload(Meeting.ticket_types)).get(meeting_id)
    if not meeting:
        flash("That event was not found.", "warning")
        return redirect(url_for("main.site_search"))

    now_utc = datetime.utcnow()
    is_live_event = (meeting.status or "").strip().lower() == "live"
    sale_ticket = None
    for tt in sorted(
        list(meeting.ticket_types or []),
        key=lambda t: (t.sort_order or 0, t.ticket_type_id or 0),
    ):
        if (tt.status or "").strip().lower() != "active":
            continue
        if tt.sales_open_at and tt.sales_open_at > now_utc:
            continue
        if tt.sales_close_at and tt.sales_close_at < now_utc:
            continue
        sale_ticket = tt
        break

    if not is_live_event or not sale_ticket:
        flash("Tickets are not currently available for this event.", "warning")
        return redirect(url_for("main.meeting_detail", meeting_id=meeting_id))

    flash(
        "Ticket reserved for this event. Payment and checkout will be added next.",
        "success",
    )
    return redirect(url_for("main.meeting_detail", meeting_id=meeting_id))


@bp.route("/meetings/<int:meeting_id>/ticket-payment")
@login_required
def meeting_ticket_payment(meeting_id):
    """Checkout is inline on the event page; keep URL for bookmarks and redirects."""
    quantity = max(1, request.args.get("quantity", type=int) or 1)
    return redirect(
        url_for(
            "main.meeting_detail",
            meeting_id=meeting_id,
            open_checkout=1,
            quantity=quantity,
        )
    )


def _ticket_checkout_fail(ajax, message, redirect_to, flash_category="warning", http_status=400):
    if ajax:
        return jsonify({"ok": False, "message": message}), http_status
    flash(message, flash_category)
    return redirect(redirect_to)


@bp.route("/meetings/<int:meeting_id>/ticket-complete", methods=["POST"])
@login_required
def meeting_ticket_complete(meeting_id):
    ajax = request.form.get("ajax") == "1"
    quantity_req = request.form.get("quantity", type=int) or 1
    meeting = Meeting.query.options(
        selectinload(Meeting.ticket_types),
        selectinload(Meeting.meeting_group),
    ).get(meeting_id)
    if not meeting:
        return _ticket_checkout_fail(
            ajax,
            "That event was not found.",
            url_for("main.site_search"),
            "warning",
            404,
        )
    if (meeting.status or "").strip().lower() != "live":
        return _ticket_checkout_fail(
            ajax,
            "This event is not live yet.",
            url_for("main.meeting_detail", meeting_id=meeting_id),
        )

    ticket = _first_active_sale_ticket(meeting, datetime.utcnow())
    if not ticket:
        return _ticket_checkout_fail(
            ajax,
            "Tickets are not currently on sale for this event.",
            url_for("main.meeting_detail", meeting_id=meeting_id),
        )

    uid = session.get("user_id")
    buyer = User.query.get(uid) if uid else None
    if not buyer:
        return _ticket_checkout_fail(
            ajax,
            "Please sign in again to complete your purchase.",
            url_for("main.login"),
            "warning",
            401,
        )

    buyer_email = (buyer.email or "").strip().lower()
    if not buyer_email or not EMAIL_RE.match(buyer_email):
        return _ticket_checkout_fail(
            ajax,
            "Add a valid email on your profile to receive ticket confirmation.",
            url_for("main.profile"),
            "danger",
        )

    # Serialize oversell checks for this ticket SKU (SELECT … FOR UPDATE).
    MeetingTicketType.query.filter_by(ticket_type_id=ticket.ticket_type_id).with_for_update().first()
    remaining_qty = _remaining_tickets_for_sale(ticket)
    allowed = remaining_qty
    if allowed <= 0:
        return _ticket_checkout_fail(
            ajax,
            "Tickets for this event are sold out.",
            url_for("main.meeting_detail", meeting_id=meeting_id),
        )
    quantity = max(1, min(int(quantity_req), allowed))

    unit_price = buyer_unit_price_for_ticket(ticket)
    total_amount = unit_price * Decimal(quantity)
    payment_ref = (request.form.get("payment_reference") or "").strip()
    if not payment_ref:
        payment_ref = f"DUMMY-CARD-{secrets.token_hex(4).upper()}"
    attendee = MeetingAttendee(
        meeting_id=meeting.meeting_id,
        ticket_type_id=ticket.ticket_type_id,
        user_id=uid,
        quantity=int(quantity),
        amount_paid=total_amount,
        status="Confirmed",
        booked_at=datetime.utcnow(),
    )
    checkin_qr_data_uris = []
    try:
        db.session.add(attendee)
        db.session.flush()
        if _meeting_is_face_to_face(meeting):
            for _ in range(int(quantity)):
                tok = secrets.token_urlsafe(32)
                db.session.add(
                    MeetingTicketEntry(
                        meeting_attendee_id=attendee.meeting_attendee_id,
                        entry_token=tok,
                    )
                )
                checkin_qr_data_uris.append(_qr_data_uri(_ticket_checkin_qr_payload(meeting.meeting_id, tok)))
        from .user_account_tx import (
            record_organiser_platform_fee_for_sale,
            record_ticket_purchase_transaction,
        )

        vat_rate = getattr(ticket, "vat_rate_percent", None)
        buyer_tx = record_ticket_purchase_transaction(
            int(uid),
            meeting=meeting,
            attendee_id=int(attendee.meeting_attendee_id),
            ticket_name=ticket.ticket_name or "General admission",
            quantity=int(quantity),
            total_amount=total_amount,
            unit_price=unit_price,
            payment_reference=payment_ref,
            vat_rate_percent=vat_rate,
        )
        organiser_id = int(meeting.creator_user_id or 0)
        if organiser_id and total_amount > 0:
            record_organiser_platform_fee_for_sale(
                organiser_id,
                meeting=meeting,
                buyer_user_id=int(uid),
                attendee_id=int(attendee.meeting_attendee_id),
                quantity=int(quantity),
                unit_price=unit_price,
                payment_reference=payment_ref,
            )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        try:
            current_app.logger.exception("[ticket-complete] save failed")
        except Exception:
            pass
        return _ticket_checkout_fail(
            ajax,
            f"Your tickets could not be saved: {exc}",
            url_for("main.meeting_detail", meeting_id=meeting_id, open_checkout=1, quantity=quantity),
            "danger",
        )

    email_sent = False
    email_error = None
    invoice_pdf = None
    invoice_attach_name = None
    vat_note = None
    invoice_no = None
    if total_amount > 0 and buyer_tx is not None:
        try:
            from .purchase_invoicing import (
                bill_to_from_user,
                build_purchase_invoice_pdf,
                invoice_filename,
                invoice_number,
                should_issue_vat_invoice,
                vat_footer_note,
            )
            from .user_account_tx import get_user_transaction

            buyer_user = User.query.get(int(uid))
            tx_dict = get_user_transaction(int(uid), int(buyer_tx.user_transaction_id))
            if buyer_user and tx_dict and should_issue_vat_invoice(tx_dict):
                invoice_pdf = build_purchase_invoice_pdf(
                    tx_dict, bill_to=bill_to_from_user(buyer_user)
                )
                invoice_attach_name = invoice_filename(tx_dict)
                invoice_no = invoice_number(tx_dict)
                vat_note = vat_footer_note(tx_dict)
        except Exception:
            try:
                current_app.logger.exception("[ticket-complete] invoice PDF build failed")
            except Exception:
                pass
    try:
        _send_ticket_purchase_email(
            buyer_email,
            meeting,
            ticket,
            quantity,
            total_amount,
            checkin_qr_data_uris=checkin_qr_data_uris or None,
            invoice_pdf=invoice_pdf,
            invoice_attach_name=invoice_attach_name,
            vat_note=vat_note,
            invoice_no=invoice_no,
        )
        email_sent = True
    except Exception as exc:
        email_error = str(exc)

    if ajax:
        return jsonify(
            {
                "ok": True,
                "message": "Your tickets are confirmed.",
                "my_tickets_url": url_for("main.attendee_dashboard"),
                "event_url": url_for("main.meeting_detail", meeting_id=meeting.meeting_id),
                "quantity": int(quantity),
                "total_amount": f"{total_amount:.2f}",
                "unit_price": f"{unit_price:.2f}",
                "ticket_name": ticket.ticket_name or "General admission",
                "buyer_email": buyer_email,
                "email_sent": email_sent,
                "email_error": email_error,
            }
        )

    flash("Your tickets are confirmed.", "success")
    return redirect(url_for("main.attendee_dashboard"))


@bp.route("/meetings/<int:meeting_id>/contact-organiser", methods=["POST"])
def meeting_contact_organiser(meeting_id):
    from_attendee = (request.form.get("from_attendee_ticket") or "").strip() == "1"
    is_ajax = (request.form.get("ajax") or "").strip() == "1"

    def redirect_detail():
        return redirect(url_for("main.meeting_detail", meeting_id=meeting_id))

    def redirect_attendee():
        return redirect(url_for("main.attendee_dashboard"))

    def fail(msg, *, status=400, flash_cat="danger"):
        if is_ajax:
            return jsonify({"ok": False, "error": msg}), status
        flash(msg, flash_cat)
        return redirect_attendee() if from_attendee else redirect_detail()

    meeting = Meeting.query.options(
        selectinload(Meeting.meeting_group).selectinload(MeetingGroup.owner),
    ).get(meeting_id)
    if not meeting:
        if is_ajax:
            return jsonify({"ok": False, "error": "That event was not found."}), 404
        flash("That event was not found.", "warning")
        return redirect_attendee() if from_attendee else redirect(url_for("main.site_search"))

    if from_attendee:
        uid = session.get("user_id")
        if not uid:
            return fail("Please sign in to continue.", status=401)
        has_ticket = (
            MeetingAttendee.query.filter_by(user_id=int(uid), meeting_id=meeting_id).first()
            is not None
        )
        if not has_ticket:
            return fail(
                "You can only message the organiser for events you have a ticket to.",
                status=403,
            )

    organiser = meeting.meeting_group.owner if meeting.meeting_group else None
    organiser_email = (organiser.email or "").strip() if organiser else ""
    if not organiser_email:
        return fail("Organiser email is not available for this event.", flash_cat="warning")

    sender_name = (request.form.get("sender_name") or "").strip()
    sender_email = (request.form.get("sender_email") or "").strip().lower()
    question_text = (request.form.get("message") or "").strip()
    if not sender_name or not sender_email or not question_text:
        return fail("Please fill in your name, email and message.")
    if not EMAIL_RE.match(sender_email):
        return fail("Please enter a valid email address.")

    try:
        _send_meeting_question_email(
            meeting=meeting,
            organiser_email=organiser_email,
            sender_name=sender_name,
            sender_email=sender_email,
            question_text=question_text,
        )
    except Exception as exc:
        return fail(f"Could not send your message right now: {exc}", status=500)

    ok_msg = "Message sent to the organiser. A copy was also sent to you."
    if is_ajax:
        return jsonify({"ok": True, "message": ok_msg})
    flash(ok_msg, "success")
    return redirect_attendee() if from_attendee else redirect_detail()


@bp.route("/meeting-groups/create", methods=["GET", "POST"])
def create_meeting_group():
    if request.method == "POST":
        flash("Creating meeting groups is being rebuilt against the new database.", "info")
    return render_template("meeting_group_create.html")


# ---------------------------------------------------------------------------
# Networking directory (render only, no DB)
# ---------------------------------------------------------------------------
@bp.route("/networking-directory")
def networking_directory():
    return render_template("networking_directory.html")


@bp.route("/networking-directory/create", methods=["GET", "POST"])
def networking_event_create():
    if request.method == "POST":
        flash("Creating events is being rebuilt against the new database.", "info")
    return render_template("networking_event_create.html")


@bp.route("/networking-directory/<int:event_id>")
def networking_event_detail(event_id):
    return render_template("networking_event_detail.html")


@bp.route("/organiser/dashboard")
def organiser_dashboard():
    return render_template("organiser_dashboard.html")


@bp.route("/favourites")
@login_required
def saved_meetings():
    """List Live meetings the signed-in user has saved (favourites)."""
    lbl_mt = table_label("events", "Events")
    uid = int(session["user_id"])
    now_utc = datetime.utcnow()
    saved_rows = (
        db.session.query(UserSavedMeeting, Meeting)
        .join(Meeting, UserSavedMeeting.meeting_id == Meeting.meeting_id)
        .filter(UserSavedMeeting.user_id == uid)
        .filter(Meeting.status == "Live")
        .options(
            selectinload(Meeting.meeting_group).selectinload(MeetingGroup.industry),
            selectinload(Meeting.meeting_group).selectinload(MeetingGroup.tags),
        )
        .order_by(UserSavedMeeting.saved_at.desc())
        .all()
    )
    favourite_meetings = []
    for usm, m in saved_rows:
        setattr(m, "_favourite_saved_at", usm.saved_at)
        favourite_meetings.append(m)

    similar_meetings: list[Meeting] = []
    user = User.query.options(selectinload(User.attendee_tags)).get(uid)
    if user:
        similar_meetings = _favourites_page_similar_meetings(
            user, favourite_meetings, now_utc=now_utc, result_limit=25
        )
        if similar_meetings:
            _hydrate_home_meeting_cards(similar_meetings, now_utc)

    return render_template(
        "saved_meetings.html",
        favourite_meetings=favourite_meetings,
        similar_meetings=similar_meetings,
        lbl_mt=lbl_mt,
    )


@bp.route("/attendee/dashboard")
def attendee_dashboard():
    if not session.get("user_id"):
        flash("You must be logged in to access these pages.", "info")
        return redirect(url_for("main.register"))

    uid = int(session["user_id"])
    rows = (
        MeetingAttendee.query.options(
            selectinload(MeetingAttendee.meeting)
            .selectinload(Meeting.meeting_group)
            .selectinload(MeetingGroup.tags),
            selectinload(MeetingAttendee.meeting)
            .selectinload(Meeting.meeting_group)
            .selectinload(MeetingGroup.industry),
            selectinload(MeetingAttendee.meeting)
            .selectinload(Meeting.meeting_group)
            .selectinload(MeetingGroup.owner),
            selectinload(MeetingAttendee.ticket_type),
            selectinload(MeetingAttendee.user),
        )
        .filter(MeetingAttendee.user_id == uid)
        .order_by(MeetingAttendee.booked_at.desc())
        .all()
    )
    purchase_history = []
    ticket_checkin_modal_by_attendee_id = {}
    group_modal_by_id: dict[int, dict] = {}
    for att in rows:
        m = att.meeting
        tt = att.ticket_type
        qty = int(att.quantity or 0)
        try:
            total_dec = Decimal(att.amount_paid or 0)
        except (InvalidOperation, TypeError):
            total_dec = Decimal("0")
        unit_dec = (total_dec / Decimal(qty)) if qty else Decimal("0")
        checkin_qr_mode = None
        f2f_qr_uris = []
        if m and _meeting_is_face_to_face(m):
            try:
                entry_rows = list(att.ticket_entries)
            except ProgrammingError:
                entry_rows = []
            if entry_rows:
                checkin_qr_mode = "per_guest"
                for ent in entry_rows:
                    f2f_qr_uris.append(
                        _qr_data_uri(_ticket_checkin_qr_payload(m.meeting_id, ent.entry_token))
                    )
            else:
                checkin_qr_mode = "party"
                f2f_qr_uris.append(
                    _qr_data_uri(_legacy_f2f_attendee_checkin_payload(att.meeting_attendee_id))
                )
        if f2f_qr_uris and checkin_qr_mode:
            ticket_checkin_modal_by_attendee_id[str(att.meeting_attendee_id)] = {
                "uris": f2f_qr_uris,
                "mode": checkin_qr_mode,
            }
        if m and m.meeting_group:
            mg = m.meeting_group
            gid = int(mg.meeting_group_id)
            if gid not in group_modal_by_id:
                desc_raw = _fix_utf8_mojibake_from_cp1252(mg.description or "")
                desc_plain = _rich_text_plain_text(desc_raw) if desc_raw else ""
                if len(desc_plain) > 12000:
                    desc_plain = desc_plain[:12000] + "…"
                topic = ""
                if mg.industry:
                    topic = (mg.industry.industry or "").strip()
                tag_parts = sorted(
                    {(t.tag or "").strip() for t in (mg.tags or []) if (t.tag or "").strip()}
                )
                tags_joined = ", ".join(tag_parts)
                site = (mg.website_url or "").strip()
                group_modal_by_id[gid] = {
                    "name": (mg.meeting_group_name or "").strip(),
                    "description_plain": desc_plain,
                    "website_url": site,
                    "format_label": (
                        "Virtual" if (mg.meeting_format or "").strip() == "Virtual" else "In person"
                    ),
                    "image_url": meeting_group_image_url(mg),
                    "public_url": url_for("main.meeting_group_public", meeting_group_id=gid),
                    "topic": topic,
                    "tags": tags_joined,
                }
        purchase_history.append(
            {
                "meeting_attendee_id": att.meeting_attendee_id,
                "meeting": m,
                "ticket_name": (tt.ticket_name if tt else "General admission"),
                "quantity": qty,
                "unit_price": f"{unit_dec:.2f}",
                "total_amount": f"{total_dec:.2f}",
                "buyer_email": (att.user.email or "").strip() if att.user else "",
                "purchased_at": att.booked_at.isoformat() if att.booked_at else "",
                "status": (att.status or "").strip(),
                "event_format_label": _attendee_event_format_label(m),
            }
        )

    now_utc = datetime.utcnow()
    for row in purchase_history:
        m = row.get("meeting")
        maps_url = ""
        if m and _meeting_is_face_to_face(m):
            u = _google_maps_search_url(m)
            if u:
                maps_url = u
        row["maps_search_url"] = maps_url
        stn = _meeting_starts_at_naive_utc(m) if m else None
        row["is_past_event"] = bool(stn and stn < now_utc)
        row["countdown_end_iso"] = ""
        if stn:
            row["countdown_end_iso"] = stn.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        row["organiser_message_ok"] = False
        row["organiser_event_when_line"] = ""
        if m:
            row["organiser_event_when_line"] = (
                m.starts_at.strftime("%d %b %Y, %H:%M") if m.starts_at else "Date to be confirmed"
            )
            own = m.meeting_group.owner if m and m.meeting_group else None
            em = (own.email or "").strip() if own else ""
            row["organiser_message_ok"] = bool(em and EMAIL_RE.match(em))

    upcoming_ticket_rows = [r for r in purchase_history if not r["is_past_event"]]
    past_ticket_rows = [r for r in purchase_history if r["is_past_event"]]

    def _ticket_row_start_key(r):
        stn = _meeting_starts_at_naive_utc(r.get("meeting"))
        return (stn is None, stn or datetime.max)

    upcoming_ticket_rows.sort(key=_ticket_row_start_key)
    past_ticket_rows.sort(
        key=lambda r: _meeting_starts_at_naive_utc(r.get("meeting")) or datetime.min,
        reverse=True,
    )

    ticketed_meetings = [row["meeting"] for row in purchase_history if row.get("meeting")]
    similar_meetings: list[Meeting] = []
    user = User.query.options(selectinload(User.attendee_tags)).get(uid)
    if user:
        similar_meetings = _favourites_page_similar_meetings(
            user, ticketed_meetings, now_utc=now_utc, result_limit=25
        )
        if similar_meetings:
            _hydrate_home_meeting_cards(similar_meetings, now_utc)

    return render_template(
        "attendee_dashboard.html",
        purchase_history=purchase_history,
        upcoming_ticket_rows=upcoming_ticket_rows,
        past_ticket_rows=past_ticket_rows,
        ticket_checkin_modal_by_attendee_id=ticket_checkin_modal_by_attendee_id,
        group_modal_by_id=group_modal_by_id,
        similar_meetings=similar_meetings,
    )


# Discovery stub API is not registered on the admin maint app (see app/maint_gate.py).
