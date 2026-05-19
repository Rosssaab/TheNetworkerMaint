"""Search-engine submission for Featured plus (and related tiers).

NOT ENABLED YET — production wiring is deferred while the site is in development.
See deploy/FEATURED_PLUS_SEARCH_INDEXING.md for the full implementation plan.

When enabled, activating Featured plus will:
  1. Resolve the canonical public URL for the promoted group or event.
  2. Upsert search_index_entities (via search_index.register_search_index_entity).
  3. Refresh sitemap.xml entries for live listings.
  4. Ping IndexNow (Bing, Yandex, and partners) with that URL.
  5. Rely on Google Search Console sitemap for Google (domain verified once).

This module is intentionally a no-op until SEARCH_SUBMISSION_ENABLED is True
and the app runs on the production host.
"""

from __future__ import annotations

from typing import Any

from flask import current_app

# Tiers that include search crawl priority (see PROMOTION_TIERS in promotion_boosts.py).
SEARCH_SUBMISSION_TIER_KEYS: frozenset[str] = frozenset({"featured_plus", "featured_max"})

# Master switch — keep False on dev/staging until launch checklist in deploy doc is complete.
SEARCH_SUBMISSION_ENABLED = False


def is_search_submission_enabled() -> bool:
    """True only when explicitly enabled and running on production (see is_production_host)."""
    if not SEARCH_SUBMISSION_ENABLED:
        return False
    return is_production_host()


def is_production_host() -> bool:
    """Override via SEARCH_SUBMISSION_PRODUCTION_HOSTS in config when wiring up."""
    try:
        allowed = current_app.config.get("SEARCH_SUBMISSION_PRODUCTION_HOSTS") or ()
        if not allowed:
            return False
        host = (current_app.config.get("CANONICAL_HOST") or "").strip().lower()
        if not host:
            return False
        return host in {str(h).strip().lower() for h in allowed}
    except RuntimeError:
        return False


def canonical_listing_url(
    *,
    scope: str,
    meeting_group_id: int,
    meeting_id: int | None,
) -> str | None:
    """Public URL for the promoted listing (_external). Returns None if not buildable."""
    # TODO: url_for meeting_group_public / meeting_detail with _external=True
    return None


def submit_promoted_listing(
    *,
    owner_user_id: int,
    scope: str,
    meeting_group_id: int,
    meeting_id: int | None,
    tier_key: str,
    promotion_order_id: int | None = None,
) -> dict[str, Any]:
    """
    Queue or perform search submission for one promotion.

    Returns a JSON-safe dict, e.g. {"ok": True, "skipped": True, "reason": "not_enabled"}.
    """
    tier = (tier_key or "").strip().lower()
    if tier not in SEARCH_SUBMISSION_TIER_KEYS:
        return {"ok": True, "skipped": True, "reason": "tier_not_eligible"}

    if not is_search_submission_enabled():
        return {"ok": True, "skipped": True, "reason": "not_enabled"}

    # --- Implementation checklist (deploy/FEATURED_PLUS_SEARCH_INDEXING.md) ---
    # 1. canonical_listing_url(...)
    # 2. register_search_index_entity(...) status=pending -> submitted
    # 3. refresh_sitemap()
    # 4. indexnow_ping(url)
    raise NotImplementedError(
        "Search submission is not wired up yet. See deploy/FEATURED_PLUS_SEARCH_INDEXING.md"
    )


def process_pending_search_index_entities(limit: int = 50) -> dict[str, Any]:
    """
    Cron/worker entry: process search_index_entities with status=pending.

    Not scheduled until SEARCH_SUBMISSION_ENABLED is True on production.
    """
    if not is_search_submission_enabled():
        return {"ok": True, "skipped": True, "reason": "not_enabled", "processed": 0}
    raise NotImplementedError(
        "Search submission worker is not wired up yet. See deploy/FEATURED_PLUS_SEARCH_INDEXING.md"
    )
