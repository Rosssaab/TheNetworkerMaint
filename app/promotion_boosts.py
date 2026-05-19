"""Promotion Boosts — wallet credits for featured placement (bundle purchase + spend)."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from flask import current_app
from sqlalchemy import func

from .models import Meeting, MeetingGroup, PromotionCreditLedger, PromotionOrder, db
from .user_account_tx import (
    account_transactions_for_user,
    payment_method_display,
    record_transaction,
    split_gross_inc_vat,
)

# User-facing name for credits
BOOST_LABEL = "Boost"
BOOST_LABEL_PLURAL = "Boosts"

VAT_RATE = Decimal("0.20")
PROMOTION_DURATION_DAYS = 30

# Promotion levels (Boost costs). Base: 2 Boosts / single live event, 8 Boosts / event group.
# Edit tagline, summary, and benefits here — they appear on the dashboard, buy-boosts, and pricing table.
PROMOTION_TIERS: list[dict] = [
    {
        "key": "featured",
        "label": "Featured",
        "level": 1,
        "event": 2,
        "group": 8,
        "tagline": "Featured Events, weekly newsletter, and optional images per event.",
        "summary": "Featured Events near users, weekly newsletter, optional per-event images",
        "benefits": [
            "Listed in Featured Events shown to users near you",
            "Included in the weekly newsletter we send to your audience",
            "Optionally use a different image for each event instead of the group image",
            "Active for 30 days on live event and group listings",
        ],
        "icon": "bi-stars",
        "badge": "Popular",
    },
    {
        "key": "featured_plus",
        "label": "Featured plus",
        "level": 2,
        "event": 4,
        "group": 16,
        "tagline": "Everything in Featured, plus your public listing page prioritised for search crawlers.",
        "summary": "Featured placement and crawl priority for your listing URL",
        "benefits": [
            "Everything in Featured",
            "Your public event or group page URL prioritised in our sitemap and crawl notifications (Google, Bing, and similar)",
            "Description tools and guidance when you activate (for search engines and attendees)",
            "Extra placement on homepage and discovery areas",
            "Active for 30 days — search engines decide whether and how to list your page",
        ],
        "icon": "bi-megaphone-fill",
    },
    {
        "key": "featured_max",
        "label": "Featured maximum",
        "level": 3,
        "event": 6,
        "group": 24,
        "tagline": "Top-tier reach — newsletter priority plus our social channels.",
        "summary": "Featured plus with priority placement and social video eligibility",
        "benefits": [
            "Everything in Featured plus",
            "Top priority in featured slots and the weekly member newsletter",
            "Eligible for a short social video on The Networker's channels — built from your image and listing details with voiceover (subject to approval)",
            "We post from our accounts (not yours); views and timing are not guaranteed",
            "Active for 30 days on live event and group listings",
        ],
        "icon": "bi-trophy-fill",
        "badge": "Top reach",
    },
]

# Legacy tier keys from earlier builds → current keys
_LEGACY_TIER_KEYS: dict[str, str] = {
    "spotlight": "featured",
    "amplify": "featured_plus",
    "premiere": "featured_max",
}

TIER_BOOST_COSTS: dict[str, dict[str, int]] = {
    t["key"]: {"event": int(t["event"]), "group": int(t["group"])} for t in PROMOTION_TIERS
}

TIER_PACKAGE_LEVEL: dict[str, int] = {t["key"]: int(t["level"]) for t in PROMOTION_TIERS}

_TIER_BY_KEY: dict[str, dict] = {t["key"]: t for t in PROMOTION_TIERS}

# Bundle catalog: list price £1 per Boost; larger packs discount the per-Boost rate.
BOOST_LIST_GBP_PER_UNIT = Decimal("1.00")

PROMOTION_BUNDLES: list[dict] = [
    {
        "key": "starter",
        "label": "Starter",
        "boosts": 10,
        "price_gbp": Decimal("10.00"),
        "tagline": "Enough for five single-event promotions (2 Boosts each)",
        "badge": "Basic starter pack",
        "badge_style": "label",
    },
    {
        "key": "growth",
        "label": "Growth",
        "boosts": 30,
        "price_gbp": Decimal("23.00"),
        "tagline": "Best for organisers with a few events",
        "badge": "Save 23%",
    },
    {
        "key": "pro",
        "label": "Pro",
        "boosts": 75,
        "price_gbp": Decimal("50.00"),
        "tagline": "Regular promotion across your groups",
        "badge": "Save 33%",
    },
    {
        "key": "scale",
        "label": "Scale",
        "boosts": 150,
        "price_gbp": Decimal("87.00"),
        "tagline": "Maximum reach for busy calendars",
        "badge": "Save 42%",
        "highlight": True,
    },
]


def _bundle_by_key(key: str) -> dict | None:
    k = (key or "").strip().lower()
    for b in PROMOTION_BUNDLES:
        if b["key"] == k:
            return b
    return None


def _gbp_display(amount: Decimal) -> str:
    q = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if q == q.to_integral_value():
        return str(int(q))
    return f"{q:.2f}"


def _starter_per_boost_gbp() -> Decimal:
    b = PROMOTION_BUNDLES[0]
    return (b["price_gbp"] / Decimal(b["boosts"])).quantize(Decimal("0.01"))


def bundle_catalog_for_json() -> list[dict]:
    """Bundles with derived per-Boost and list-price pricing for checkout UI."""
    starter_rate = _starter_per_boost_gbp()
    out: list[dict] = []
    for b in PROMOTION_BUNDLES:
        boosts = int(b["boosts"])
        price = b["price_gbp"]
        list_price = (BOOST_LIST_GBP_PER_UNIT * Decimal(boosts)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        per = (price / Decimal(boosts)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        save_pct = 0
        if starter_rate > 0 and per < starter_rate:
            save_pct = int(
                ((starter_rate - per) / starter_rate * Decimal("100")).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
        save_gbp = max(Decimal("0"), list_price - price).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        out.append(
            {
                "key": b["key"],
                "label": b["label"],
                "boosts": boosts,
                "price_gbp": float(price),
                "price_display": f"{price:.2f}",
                "price_display_short": _gbp_display(price),
                "list_price_gbp": float(list_price),
                "list_price_display": f"{list_price:.2f}",
                "list_price_display_short": _gbp_display(list_price),
                "per_boost_gbp": float(per),
                "per_boost_display": f"{per:.2f}",
                "per_boost_display_short": _gbp_display(per),
                "save_percent": save_pct,
                "save_gbp_display_short": _gbp_display(save_gbp) if save_gbp > 0 else None,
                "tagline": b.get("tagline") or "",
                "badge": b.get("badge"),
                "badge_style": (b.get("badge_style") or "save").strip(),
                "highlight": bool(b.get("highlight")),
            }
        )
    return out


def tier_costs_for_json() -> dict:
    return {k: dict(v) for k, v in TIER_BOOST_COSTS.items()}


def promotion_tiers_for_json() -> list[dict]:
    out: list[dict] = []
    for t in PROMOTION_TIERS:
        out.append(
            {
                "key": t["key"],
                "label": t["label"],
                "level": int(t["level"]),
                "boost_event": int(t["event"]),
                "boost_group": int(t["group"]),
                "tagline": t.get("tagline") or "",
                "summary": t.get("summary") or "",
                "benefits": list(t.get("benefits") or []),
                "icon": t.get("icon") or "bi-stars",
                "badge": t.get("badge"),
            }
        )
    return out


def normalize_tier_key(tier_key: str) -> str:
    k = (tier_key or "").strip().lower()
    return _LEGACY_TIER_KEYS.get(k, k)


def tier_display(tier_key: str) -> dict | None:
    k = normalize_tier_key(tier_key)
    return _TIER_BY_KEY.get(k)


def get_boost_balance(user_id: int) -> int:
    total = (
        db.session.query(func.coalesce(func.sum(PromotionCreditLedger.delta), 0))
        .filter(PromotionCreditLedger.user_id == user_id)
        .scalar()
    )
    try:
        return max(0, int(total))
    except (TypeError, ValueError):
        return 0


def get_boosts_used(user_id: int) -> int:
    """Total Boosts spent on promotions (sum of ledger debits)."""
    total = (
        db.session.query(
            func.coalesce(func.sum(-PromotionCreditLedger.delta), 0)
        )
        .filter(
            PromotionCreditLedger.user_id == int(user_id),
            PromotionCreditLedger.delta < 0,
        )
        .scalar()
    )
    try:
        return max(0, int(total))
    except (TypeError, ValueError):
        return 0


def _append_ledger(
    user_id: int,
    delta: int,
    source_type: str,
    *,
    user_transaction_id: int | None = None,
    promotion_order_id: int | None = None,
    bundle_key: str | None = None,
    notes: str | None = None,
) -> PromotionCreditLedger:
    balance = get_boost_balance(user_id) + int(delta)
    row = PromotionCreditLedger(
        user_id=user_id,
        delta=int(delta),
        balance_after=balance,
        source_type=source_type,
        user_transaction_id=user_transaction_id,
        promotion_order_id=promotion_order_id,
        bundle_key=bundle_key,
        notes=notes,
        created_at=datetime.utcnow(),
    )
    db.session.add(row)
    return row


def purchase_boost_bundle(
    user_id: int,
    bundle_key: str,
    *,
    payment_method: str | None = None,
    payment_reference: str | None = None,
    paypal_email: str | None = None,
    card_last4: str | None = None,
) -> dict:
    bundle = _bundle_by_key(bundle_key)
    if not bundle:
        return {"ok": False, "error": "Unknown bundle."}

    method = (payment_method or "card").strip().lower()
    if method not in ("card", "paypal"):
        return {"ok": False, "error": "Choose card or PayPal."}

    ref = (payment_reference or "").strip()
    if not ref:
        ref = f"DUMMY-{'PP' if method == 'paypal' else 'CARD'}-{secrets.token_hex(4).upper()}"

    boosts = int(bundle["boosts"])
    gross = bundle["price_gbp"]
    label = bundle["label"]

    pay_note = payment_method_display(ref)
    if method == "paypal" and (paypal_email or "").strip():
        pay_note += f" ({paypal_email.strip()})"
    elif method == "card" and (card_last4 or "").strip():
        pay_note += f" (•••• {card_last4.strip()})"

    tx = record_transaction(
        user_id,
        description=f"{BOOST_LABEL_PLURAL} bundle — {label} ({boosts} {BOOST_LABEL_PLURAL.lower()})",
        total_amount=gross,
        vat_rate_percent=Decimal("20.00"),
        product_type="promotion_bundle",
        tx_type="purchase",
        payment_reference=ref[:120],
        notes=f"Dummy checkout · {pay_note}"[:1000],
    )
    db.session.flush()

    _append_ledger(
        user_id,
        boosts,
        "bundle_purchase",
        user_transaction_id=int(tx.user_transaction_id),
        bundle_key=bundle["key"],
        notes=f"Purchased {label} bundle",
    )
    db.session.commit()

    try:
        from .purchase_invoicing import send_purchase_invoice_for_transaction

        bundle_note = (
            f"You purchased the <strong>{label}</strong> bundle "
            f"({boosts} {BOOST_LABEL_PLURAL.lower()} added to your wallet)."
        )
        send_purchase_invoice_for_transaction(
            int(user_id),
            int(tx.user_transaction_id),
            extra_html=f"<p style=\"margin:0 0 12px;\">{bundle_note}</p>",
            extra_plain=(
                f"You purchased the {label} bundle ({boosts} {BOOST_LABEL_PLURAL.lower()}).\n"
            ),
        )
    except Exception:
        current_app.logger.exception("boost_bundle_invoice_email user_id=%s", user_id)

    balance = get_boost_balance(user_id)
    return {
        "ok": True,
        "message": f"Added {boosts} {BOOST_LABEL_PLURAL} to your wallet.",
        "boost_balance": balance,
        "boosts_added": boosts,
        "transaction_id": int(tx.user_transaction_id),
    }


def spend_boosts_on_promotion(
    user_id: int,
    *,
    scope: str,
    meeting_group_id: int,
    meeting_id: int | None,
    tier_key: str,
    target_label: str = "",
) -> dict:
    scope = (scope or "").strip().lower()
    if scope not in ("group", "event"):
        return {"ok": False, "error": "Invalid promotion scope."}

    tier = normalize_tier_key(tier_key)
    meta = tier_display(tier)
    costs = TIER_BOOST_COSTS.get(tier)
    if not costs or not meta:
        return {"ok": False, "error": "Unknown promotion level."}

    cost = int(costs.get(scope) or 0)
    if cost <= 0:
        return {"ok": False, "error": "Invalid promotion cost."}

    balance = get_boost_balance(user_id)
    if balance < cost:
        return {
            "ok": False,
            "error": f"You need {cost} {BOOST_LABEL_PLURAL} but only have {balance}. Buy a bundle below.",
            "boost_balance": balance,
            "boosts_required": cost,
        }

    if scope == "event" and not meeting_id:
        return {"ok": False, "error": "Choose an event to promote."}

    now = datetime.utcnow()
    ends = now + timedelta(days=PROMOTION_DURATION_DAYS)
    tier_label = str(meta.get("label") or "Promotion")
    mg = MeetingGroup.query.get(int(meeting_group_id))
    if scope == "event" and meeting_id:
        meeting = Meeting.query.get(int(meeting_id))
        desc_target = (target_label or "").strip() or (
            (meeting.title or "").strip() if meeting else ""
        ) or "Event"
    else:
        desc_target = (target_label or "").strip() or (
            (mg.meeting_group_name or "").strip() if mg else ""
        ) or "Event group"

    product_type = "group_promotion" if scope == "group" else "event_promotion"
    tx = record_transaction(
        user_id,
        description=(
            f"{tier_label} promotion — {desc_target} "
            f"({cost} {BOOST_LABEL_PLURAL}, {PROMOTION_DURATION_DAYS} days)"
        ),
        total_amount=Decimal("0.00"),
        amount=Decimal("0.00"),
        vat=Decimal("0.00"),
        product_type=product_type,
        tx_type="promotion",
        meeting_group_id=int(meeting_group_id),
        meeting_id=int(meeting_id) if scope == "event" and meeting_id else None,
        tx_date=now,
    )
    db.session.flush()

    order = PromotionOrder(
        user_id=user_id,
        user_transaction_id=int(tx.user_transaction_id),
        scope=scope,
        meeting_group_id=int(meeting_group_id),
        meeting_id=int(meeting_id) if scope == "event" and meeting_id else None,
        package_tier=int(TIER_PACKAGE_LEVEL.get(tier, 1)),
        price_amount=Decimal("0.00"),
        currency_code="GBP",
        starts_at=now,
        ends_at=ends,
        status="active",
        created_at=now,
    )
    db.session.add(order)
    db.session.flush()

    _append_ledger(
        user_id,
        -cost,
        "promotion_spend",
        promotion_order_id=int(order.promotion_order_id),
        notes=f"{tier_label} — {desc_target}",
    )
    db.session.commit()

    try:
        from .search_index import register_after_promotion

        register_after_promotion(
            int(user_id),
            scope=scope,
            meeting_group_id=int(meeting_group_id),
            meeting_id=int(meeting_id) if scope == "event" and meeting_id else None,
        )
        # Search crawl submission (sitemap + IndexNow) — not enabled yet; see search_submission.py
        # from .search_submission import submit_promoted_listing
        # submit_promoted_listing(
        #     owner_user_id=int(user_id),
        #     scope=scope,
        #     meeting_group_id=int(meeting_group_id),
        #     meeting_id=int(meeting_id) if scope == "event" and meeting_id else None,
        #     tier_key=tier,
        #     promotion_order_id=int(order.promotion_order_id),
        # )
        # Social video queue (image + TTS) — not enabled yet; see social_promotion.py
        # from .social_promotion import queue_social_promotion
        # queue_social_promotion(
        #     owner_user_id=int(user_id),
        #     scope=scope,
        #     meeting_group_id=int(meeting_group_id),
        #     meeting_id=int(meeting_id) if scope == "event" and meeting_id else None,
        #     tier_key=tier,
        #     promotion_order_id=int(order.promotion_order_id),
        # )
    except Exception:
        current_app.logger.exception("register_after_promotion")

    new_balance = get_boost_balance(user_id)
    return {
        "ok": True,
        "message": (
            f"Activated <strong>{tier_label}</strong> for {PROMOTION_DURATION_DAYS} days "
            f"({cost} {BOOST_LABEL_PLURAL} used)."
        ),
        "boost_balance": new_balance,
        "promotion_order_id": int(order.promotion_order_id),
    }


def promote_wallet_json(user_id: int | None) -> dict:
    if not user_id:
        return {
            "boost_label": BOOST_LABEL,
            "boost_label_plural": BOOST_LABEL_PLURAL,
            "boost_balance": 0,
            "bundles": bundle_catalog_for_json(),
            "tier_costs": tier_costs_for_json(),
            "tiers": promotion_tiers_for_json(),
        }
    return {
        "boost_label": BOOST_LABEL,
        "boost_label_plural": BOOST_LABEL_PLURAL,
        "boost_balance": get_boost_balance(user_id),
        "bundles": bundle_catalog_for_json(),
        "tier_costs": tier_costs_for_json(),
        "tiers": promotion_tiers_for_json(),
    }
