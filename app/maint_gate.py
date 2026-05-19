"""Restrict the maint Flask app to admin console routes and sign-in only."""

from __future__ import annotations

from flask import abort, current_app, redirect, request, url_for

# Endpoints allowed outside /admin/* (auth and assets).
_MAINT_AUTH_ENDPOINTS = frozenset(
    {
        "main.login",
        "main.login_twofa",
        "main.logout",
        "main.verify_email",
        "main.resend_verification",
        "main.admin_preview",
        "main.admin_review_about_v3_draft",
        "static",
    }
)

# JSON/API helpers used by admin_events and keyword tools.
_MAINT_API_ENDPOINTS = frozenset(
    {
        "main.admin_keywords_suggest",
        "main.admin_keywords_apply_suggestions",
        "main.admin_meeting_groups_suggest_keywords",
        "main.admin_meeting_groups_apply_tag_suggestions",
        "main.admin_meeting_groups_lookup_for_move",
        "main.admin_meeting_groups_provision_transfer_user",
        "main.admin_meeting_group_events_for_delete",
        "main.admin_meeting_group_description",
        "main.admin_user_edit_data",
        "main.admin_test_events_suggest_from_description",
        "main.admin_test_events_stage_group_image",
        "main.admin_test_events_ai_prepare",
        "main.admin_test_events_create",
        "main.polish_meeting_group_description",
        "main.polish_meeting_description",
        "main.api_meeting_group_suggest_keywords",
        "main.impersonate_users_json",
        "main.impersonate_start",
        "main.impersonate_stop",
        "main.set_admin_bootstrap_theme",
        "main.keyword_maintenance",
        "main.topic_add",
        "main.topic_update",
        "main.topic_delete",
        "main.keyword_add",
        "main.keyword_update",
        "main.keyword_delete",
    }
)


def _endpoint_allowed(endpoint: str | None) -> bool:
    if not endpoint:
        return False
    if endpoint in _MAINT_AUTH_ENDPOINTS or endpoint in _MAINT_API_ENDPOINTS:
        return True
    if endpoint.startswith("main.admin_"):
        return True
    return False


def register_maint_gate(app) -> None:
    @app.before_request
    def _maint_console_only():
        if not current_app.config.get("TNW_MAINT_APP"):
            return None
        path = (request.path or "").rstrip("/") or "/"
        if path.startswith("/static/"):
            return None
        ep = request.endpoint
        if _endpoint_allowed(ep):
            return None
        if path.startswith("/admin"):
            return None
        if path in ("/login", "/login/twofa", "/logout"):
            return None
        if path == "/":
            return redirect(url_for("main.admin_events"))
        abort(404)
