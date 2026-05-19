"""Search-index registry for promoted listings.

Planned crawl submission (sitemap + IndexNow) lives in search_submission.py — see
deploy/FEATURED_PLUS_SEARCH_INDEXING.md. Registry rows are written today; outbound
submission is not enabled on dev/staging.
"""

from __future__ import annotations

from datetime import datetime

from flask import current_app

from .models import Meeting, MeetingGroup, SearchIndexEntity, db

SEO_MIN_PLAIN_TEXT_LENGTH = 120

ENTITY_EVENT_GROUP = "event_group"
ENTITY_EVENT = "event"


def _plain_text_from_html(value: str | None) -> str:
    import re
    from html import unescape

    text = re.sub(r"<[^>]*>", " ", value or "")
    return unescape(text).replace("\xa0", " ").strip()


def _plain_len(html: str | None) -> int:
    return len(_plain_text_from_html(html))


def promotion_target_seo_payload(
    *,
    scope: str,
    meeting_group: MeetingGroup,
    meeting: Meeting | None = None,
) -> dict:
    """JSON-safe SEO context for the promote confirmation modal."""
    scope = (scope or "").strip().lower()
    if scope == "group":
        title = (meeting_group.meeting_group_name or "Event group").strip() or "Event group"
        html = meeting_group.description or ""
        entity_type = ENTITY_EVENT_GROUP
        entity_id = int(meeting_group.meeting_group_id)
        field_label = "Event group description"
        edit_hint = (
            "A detailed group description helps search engines understand your whole calendar "
            "and improves discovery when you promote the group."
        )
    else:
        if not meeting:
            raise ValueError("meeting required for event scope")
        title = (meeting.title or "Event").strip() or "Event"
        html = meeting.subject or ""
        entity_type = ENTITY_EVENT
        entity_id = int(meeting.meeting_id)
        field_label = "Event description"
        edit_hint = (
            "A detailed event description helps search engines and attendees find your listing "
            "when your promotion is active."
        )

    plain_len = _plain_len(html)
    return {
        "scope": scope,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "meeting_group_id": int(meeting_group.meeting_group_id),
        "meeting_id": int(meeting.meeting_id) if meeting else None,
        "title": title[:180],
        "description_html": (html or "")[:20000],
        "plain_text_length": plain_len,
        "min_recommended_length": SEO_MIN_PLAIN_TEXT_LENGTH,
        "is_adequate": plain_len >= SEO_MIN_PLAIN_TEXT_LENGTH,
        "field_label": field_label,
        "edit_hint": edit_hint,
    }


def save_promotion_target_description(
    user_id: int,
    *,
    scope: str,
    meeting_group_id: int,
    meeting_id: int | None,
    description_html: str,
) -> tuple[bool, str | None]:
    """Persist description for group (description) or event (subject). Returns (ok, error)."""
    from .routes import _sanitize_rich_text_html

    scope = (scope or "").strip().lower()
    mg = MeetingGroup.query.get(int(meeting_group_id))
    if not mg or mg.user_id != int(user_id):
        return False, "Invalid event group."

    cleaned = _sanitize_rich_text_html(description_html) or None

    if scope == "group":
        mg.description = cleaned
    elif scope == "event":
        if not meeting_id:
            return False, "Invalid event."
        meeting = Meeting.query.get(int(meeting_id))
        if (
            not meeting
            or meeting.meeting_group_id != mg.meeting_group_id
            or meeting.creator_user_id != int(user_id)
        ):
            return False, "Invalid event."
        meeting.subject = cleaned or ""
    else:
        return False, "Invalid promotion scope."

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("save_promotion_target_description")
        return False, "Could not save the description."

    return True, None


def register_search_index_entity(
    *,
    owner_user_id: int,
    entity_type: str,
    entity_id: int,
    title: str,
    plain_text_summary: str | None,
    source: str = "promotion",
) -> None:
    """Upsert a row for future search-engine submission (no-op if table is missing)."""
    from .routes import _rich_text_plain_text

    now = datetime.utcnow()
    plain = (_plain_text_from_html(plain_text_summary or "") or "").strip()[:12000]
    title_clean = (title or "").strip()[:180] or "Listing"
    et = (entity_type or "").strip().lower()
    if et not in (ENTITY_EVENT_GROUP, ENTITY_EVENT):
        return

    try:
        row = SearchIndexEntity.query.filter_by(
            entity_type=et,
            entity_id=int(entity_id),
        ).first()
        if row:
            row.owner_user_id = int(owner_user_id)
            row.title = title_clean
            row.plain_text_summary = plain or None
            row.status = row.status or "pending"
            row.source = source
            row.updated_at = now
            row.last_promoted_at = now
        else:
            db.session.add(
                SearchIndexEntity(
                    entity_type=et,
                    entity_id=int(entity_id),
                    owner_user_id=int(owner_user_id),
                    title=title_clean,
                    plain_text_summary=plain or None,
                    status="pending",
                    source=source,
                    created_at=now,
                    updated_at=now,
                    last_promoted_at=now,
                )
            )
        db.session.commit()
    except Exception:
        db.session.rollback()
        try:
            current_app.logger.warning(
                "register_search_index_entity skipped (table missing or DB error) type=%s id=%s",
                et,
                entity_id,
                exc_info=True,
            )
        except Exception:
            pass


def register_after_promotion(
    user_id: int,
    *,
    scope: str,
    meeting_group_id: int,
    meeting_id: int | None,
) -> None:
    """Record promoted listing in search_index_entities for later SEO work."""
    mg = MeetingGroup.query.get(int(meeting_group_id))
    if not mg:
        return
    scope = (scope or "").strip().lower()
    if scope == "group":
        payload = promotion_target_seo_payload(scope="group", meeting_group=mg)
        register_search_index_entity(
            owner_user_id=int(user_id),
            entity_type=ENTITY_EVENT_GROUP,
            entity_id=int(mg.meeting_group_id),
            title=payload["title"],
            plain_text_summary=payload["description_html"],
        )
    elif scope == "event" and meeting_id:
        meeting = Meeting.query.get(int(meeting_id))
        if not meeting:
            return
        payload = promotion_target_seo_payload(
            scope="event", meeting_group=mg, meeting=meeting
        )
        register_search_index_entity(
            owner_user_id=int(user_id),
            entity_type=ENTITY_EVENT,
            entity_id=int(meeting.meeting_id),
            title=payload["title"],
            plain_text_summary=payload["description_html"],
        )
