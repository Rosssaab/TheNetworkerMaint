"""Social promotion queue for Featured maximum (image + TTS video).

NOT ENABLED YET — production wiring is deferred while the site is in development.
See deploy/FEATURED_MAX_SOCIAL.md for the full implementation plan.

Planned flow when enabled:
  1. Organiser activates Featured maximum.
  2. Queue row with listing image(s), title, description, public URL.
  3. Auto-build a short spoken script from the description (length-capped).
  4. Render a 9:16 template video: images + on-screen captions + TTS voiceover.
  5. Admin previews, approves, and posts to The Networker's social accounts.

This is templated slideshow + text-to-speech — not generative text-to-video.
"""

from __future__ import annotations

from typing import Any

from flask import current_app

# Only Featured maximum includes social video eligibility (see PROMOTION_TIERS).
SOCIAL_PROMOTION_TIER_KEYS: frozenset[str] = frozenset({"featured_max"})

SOCIAL_PROMOTION_ENABLED = False

# Spoken script length targets for ~15–30 second reels.
SOCIAL_SCRIPT_MAX_WORDS = 55
SOCIAL_VIDEO_ASPECT = "9:16"


def is_social_promotion_enabled() -> bool:
    if not SOCIAL_PROMOTION_ENABLED:
        return False
    return is_production_host()


def is_production_host() -> bool:
    try:
        allowed = current_app.config.get("SOCIAL_PROMOTION_PRODUCTION_HOSTS") or ()
        if not allowed:
            return False
        host = (current_app.config.get("CANONICAL_HOST") or "").strip().lower()
        if not host:
            return False
        return host in {str(h).strip().lower() for h in allowed}
    except RuntimeError:
        return False


def build_spoken_script(
    *,
    title: str,
    description_plain: str,
    event_date_label: str | None = None,
    location_label: str | None = None,
) -> str:
    """
    Build a short voiceover script from listing fields.

    TODO: optional LLM summarise; v1 can truncate description_plain with rules.
    """
    # TODO: implement summarisation / truncation to SOCIAL_SCRIPT_MAX_WORDS
    parts = [(title or "").strip()]
    if event_date_label:
        parts.append(event_date_label.strip())
    if location_label:
        parts.append(location_label.strip())
    body = (description_plain or "").strip()
    if body:
        parts.append(body[:280])
    return ". ".join(p for p in parts if p)


def queue_social_promotion(
    *,
    owner_user_id: int,
    scope: str,
    meeting_group_id: int,
    meeting_id: int | None,
    tier_key: str,
    promotion_order_id: int | None = None,
) -> dict[str, Any]:
    """
    Enqueue a listing for social video production after Featured maximum activation.

    Returns e.g. {"ok": True, "skipped": True, "reason": "not_enabled"}.
    """
    tier = (tier_key or "").strip().lower()
    if tier not in SOCIAL_PROMOTION_TIER_KEYS:
        return {"ok": True, "skipped": True, "reason": "tier_not_eligible"}

    if not is_social_promotion_enabled():
        return {"ok": True, "skipped": True, "reason": "not_enabled"}

    # --- Implementation checklist (deploy/FEATURED_MAX_SOCIAL.md) ---
    # 1. Load group/event + image URL(s)
    # 2. build_spoken_script(...)
    # 3. INSERT social_promotion_queue status=pending
    raise NotImplementedError(
        "Social promotion queue is not wired up yet. See deploy/FEATURED_MAX_SOCIAL.md"
    )


def generate_social_video_asset(*, queue_id: int) -> dict[str, Any]:
    """
    Render TTS audio + template video for one queue row (admin or worker).

    TODO: FFmpeg / Remotion / Creatomate API; store paths on queue row.
    """
    if not is_social_promotion_enabled():
        return {"ok": True, "skipped": True, "reason": "not_enabled"}
    raise NotImplementedError(
        "Social video generation is not wired up yet. See deploy/FEATURED_MAX_SOCIAL.md"
    )


def process_pending_social_promotions(limit: int = 10) -> dict[str, Any]:
    """Cron/worker: generate assets for pending rows (no auto-post without admin approval)."""
    if not is_social_promotion_enabled():
        return {"ok": True, "skipped": True, "reason": "not_enabled", "processed": 0}
    raise NotImplementedError(
        "Social promotion worker is not wired up yet. See deploy/FEATURED_MAX_SOCIAL.md"
    )
