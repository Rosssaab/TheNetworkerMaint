"""
Record user purchases and platform activity in user_transactions (My Account).

Covers: event listing, ticket purchases, promotion (Boosts), and organiser platform fees.
"""

from __future__ import annotations

import secrets
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func

from .models import (
    Meeting,
    MeetingAttendee,
    MeetingGroup,
    PromotionCreditLedger,
    PromotionOrder,
    UserTransaction,
    db,
)

_ATTENDEE_COUNTABLE_STATUSES = ("Reserved", "Confirmed", "Attended")

VAT_RATE_DEFAULT = Decimal("0.20")
PLATFORM_FEE_RATE = Decimal("0.04")
PLATFORM_FEE_FIXED_GBP = Decimal("0.20")
WITHDRAWAL_HOLD_HOURS = 48

PRODUCT_TYPE_LABELS: dict[str, str] = {
    "ticket": "Tickets",
    "group_promotion": "Promotion",
    "event_promotion": "Promotion",
    "promotion_bundle": "Boosts",
    "platform_fee": "Platform fee",
    "refund": "Refund",
    "adjustment": "Adjustment",
    "other": "Event listing",
}


def _q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def split_gross_inc_vat(gross: Decimal, vat_rate: Decimal | None = None) -> tuple[Decimal, Decimal]:
    """Split VAT-inclusive gross into net + VAT."""
    gross = _q2(Decimal(str(gross)))
    rate = VAT_RATE_DEFAULT if vat_rate is None else (Decimal(str(vat_rate)) / Decimal("100"))
    if rate <= 0:
        return gross, Decimal("0.00")
    net = (gross / (Decimal("1") + rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    vat = _q2(gross - net)
    return net, vat


def next_tx_id(user_id: int) -> int:
    last = (
        db.session.query(func.max(UserTransaction.tx_id))
        .filter(UserTransaction.user_id == user_id)
        .scalar()
    )
    return int(last or 0) + 1


def payment_method_display(payment_reference: str | None) -> str:
    ref = (payment_reference or "").strip().upper()
    if ref.startswith("DUMMY-PP"):
        return "PayPal (test)"
    if ref.startswith("DUMMY-CARD"):
        return "Card (test)"
    if ref.startswith("STRIPE"):
        return "Stripe"
    if not ref:
        return "—"
    return "Other"


def product_type_label(product_type: str | None, tx_type: str | None = None) -> str:
    pt = (product_type or "").strip().lower()
    if pt in PRODUCT_TYPE_LABELS:
        return PRODUCT_TYPE_LABELS[pt]
    tt = (tx_type or "").strip().lower()
    if tt == "promotion":
        return "Promotion"
    if tt == "fee":
        return "Fee"
    return "Other"


def record_transaction(
    user_id: int,
    *,
    description: str,
    total_amount: Decimal,
    product_type: str,
    tx_type: str = "purchase",
    tx_status: str = "completed",
    amount: Decimal | None = None,
    vat: Decimal | None = None,
    vat_rate_percent: Decimal | None = None,
    meeting_attendee_id: int | None = None,
    meeting_group_id: int | None = None,
    meeting_id: int | None = None,
    payment_reference: str | None = None,
    notes: str | None = None,
    tx_date: datetime | None = None,
) -> UserTransaction:
    """Insert a user_transactions row (caller must commit)."""
    gross = _q2(Decimal(str(total_amount)))
    if amount is None or vat is None:
        rate = vat_rate_percent
        if rate is None and gross > 0:
            rate = Decimal("20.00")
        amount, vat = split_gross_inc_vat(gross, rate)

    now = tx_date or datetime.utcnow()
    tx = UserTransaction(
        user_id=int(user_id),
        tx_id=next_tx_id(user_id),
        tx_date=now,
        description=(description or "")[:500],
        currency_code="GBP",
        amount=_q2(Decimal(str(amount))),
        vat=_q2(Decimal(str(vat))),
        total_amount=gross,
        vat_rate_percent=vat_rate_percent,
        product_type=(product_type or "other")[:30],
        tx_type=(tx_type or "purchase")[:20],
        tx_status=(tx_status or "completed")[:20],
        meeting_attendee_id=meeting_attendee_id,
        meeting_group_id=meeting_group_id,
        meeting_id=meeting_id,
        payment_reference=(payment_reference or "")[:120] or None,
        notes=(notes or "")[:1000] or None,
        created_at=now,
    )
    db.session.add(tx)
    return tx


def platform_fee_amount(ticket_unit_price: Decimal, quantity: int) -> Decimal:
    """4% + 20p per ticket (organiser platform fee)."""
    qty = max(1, int(quantity))
    unit = _q2(Decimal(str(ticket_unit_price)))
    per = _q2(unit * PLATFORM_FEE_RATE + PLATFORM_FEE_FIXED_GBP)
    return _q2(per * Decimal(qty))


def _listing_tx_exists(user_id: int, meeting_id: int) -> bool:
    return (
        db.session.query(UserTransaction.user_transaction_id)
        .filter(
            UserTransaction.user_id == user_id,
            UserTransaction.meeting_id == int(meeting_id),
            UserTransaction.product_type == "other",
            UserTransaction.description.like("Event listed%"),
        )
        .first()
        is not None
    )


def maybe_record_event_listed(
    user_id: int,
    meeting: Meeting,
    *,
    previous_status: str | None,
) -> UserTransaction | None:
    """
    Record a listing row when an event becomes Live (free listing = £0).
    Idempotent per user + meeting.
    """
    prev = (previous_status or "").strip()
    new = (meeting.status or "").strip()
    if new != "Live" or prev == "Live":
        return None
    mid = int(meeting.meeting_id)
    uid = int(user_id)
    if _listing_tx_exists(uid, mid):
        return None

    title = (meeting.title or "Event").strip()[:200]
    mgid = int(meeting.meeting_group_id) if meeting.meeting_group_id else None
    notes = "Free event listing on The Networker."
    if bool(getattr(meeting, "is_paid_and_published", False)):
        notes = "Listed with paid ticketing enabled (no listing fee)."

    return record_transaction(
        uid,
        description=f"Event listed — {title}",
        total_amount=Decimal("0.00"),
        amount=Decimal("0.00"),
        vat=Decimal("0.00"),
        product_type="other",
        tx_type="purchase",
        meeting_group_id=mgid,
        meeting_id=mid,
        notes=notes,
    )


def record_ticket_purchase_transaction(
    buyer_user_id: int,
    *,
    meeting: Meeting,
    attendee_id: int,
    ticket_name: str,
    quantity: int,
    total_amount: Decimal,
    unit_price: Decimal,
    payment_reference: str | None = None,
    payment_notes: str | None = None,
    vat_rate_percent: Decimal | None = None,
) -> UserTransaction:
    qty = max(1, int(quantity))
    gross = _q2(Decimal(str(total_amount)))
    title = (meeting.title or "Event").strip()[:160]
    tname = (ticket_name or "Ticket").strip()[:80]
    ref = (payment_reference or "").strip()
    if not ref:
        ref = f"DUMMY-CARD-{secrets.token_hex(4).upper()}"

    desc = f"Tickets — {title} × {qty} ({tname})"
    notes = payment_notes or f"Dummy checkout · {payment_method_display(ref)}"

    rate = vat_rate_percent
    if rate is None:
        try:
            rate = Decimal(str(getattr(meeting, "vat_rate_percent", None) or "20"))
        except Exception:
            rate = Decimal("20.00")

    return record_transaction(
        int(buyer_user_id),
        description=desc,
        total_amount=gross,
        vat_rate_percent=rate if gross > 0 else Decimal("0"),
        product_type="ticket",
        tx_type="purchase",
        meeting_attendee_id=int(attendee_id),
        meeting_group_id=int(meeting.meeting_group_id) if meeting.meeting_group_id else None,
        meeting_id=int(meeting.meeting_id),
        payment_reference=ref,
        notes=notes[:1000],
    )


def record_organiser_platform_fee_for_sale(
    organiser_user_id: int,
    *,
    meeting: Meeting,
    buyer_user_id: int,
    attendee_id: int,
    quantity: int,
    unit_price: Decimal,
    payment_reference: str | None = None,
) -> UserTransaction | None:
    """Platform fee (4% + 20p per ticket) on the organiser's account when tickets sell."""
    uid = int(organiser_user_id)
    if uid == int(buyer_user_id):
        return None

    fee = platform_fee_amount(unit_price, quantity)
    if fee <= 0:
        return None

    title = (meeting.title or "Event").strip()[:160]
    qty = max(1, int(quantity))
    ref = (payment_reference or "").strip()
    suffix = f"-FEE-{int(attendee_id)}" if attendee_id else ""
    fee_ref = (ref + suffix)[:120] if ref else f"FEE-{secrets.token_hex(4).upper()}"

    return record_transaction(
        uid,
        description=f"Platform fee — {title} ({qty} ticket{'s' if qty != 1 else ''} sold)",
        total_amount=fee,
        product_type="platform_fee",
        tx_type="fee",
        meeting_attendee_id=int(attendee_id),
        meeting_group_id=int(meeting.meeting_group_id) if meeting.meeting_group_id else None,
        meeting_id=int(meeting.meeting_id),
        payment_reference=fee_ref,
        notes="4% + 20p per ticket (deducted when payouts are enabled).",
    )


def commit_event_listing_record(
    user_id: int,
    meeting_id: int,
    previous_status: str | None,
) -> None:
    """Post-commit helper: record listing transaction without failing the caller."""
    from flask import current_app

    try:
        meeting = Meeting.query.get(int(meeting_id))
        if not meeting:
            return
        row = maybe_record_event_listed(int(user_id), meeting, previous_status=previous_status)
        if row is not None:
            db.session.commit()
    except Exception:
        db.session.rollback()
        try:
            current_app.logger.exception(
                "commit_event_listing_record user_id=%s meeting_id=%s",
                user_id,
                meeting_id,
            )
        except Exception:
            pass


def organiser_account_summary(user_id: int) -> dict:
    """
    Organiser-facing balances for My Account.

    Ticket sales stay pending until recorded as payout (tx_type=payout, completed).
    Available to withdraw is the sum of completed payouts released by admin.
    """
    uid = int(user_id)
    from .promotion_boosts import get_boost_balance, get_boosts_used

    boosts_used = get_boosts_used(uid)
    boosts_remaining = get_boost_balance(uid)

    mg_ids = [
        int(r[0])
        for r in db.session.query(MeetingGroup.meeting_group_id)
        .filter(MeetingGroup.user_id == uid)
        .all()
    ]

    gross = Decimal("0.00")
    ticket_qty = 0
    if mg_ids:
        row = (
            db.session.query(
                func.coalesce(func.sum(MeetingAttendee.amount_paid), 0),
                func.coalesce(func.sum(MeetingAttendee.quantity), 0),
            )
            .join(Meeting, Meeting.meeting_id == MeetingAttendee.meeting_id)
            .filter(
                Meeting.meeting_group_id.in_(mg_ids),
                MeetingAttendee.status.in_(_ATTENDEE_COUNTABLE_STATUSES),
            )
            .first()
        )
        if row:
            gross = _q2(Decimal(str(row[0] or 0)))
            ticket_qty = int(row[1] or 0)

    fees = _q2(
        Decimal(
            str(
                db.session.query(func.coalesce(func.sum(UserTransaction.total_amount), 0))
                .filter(
                    UserTransaction.user_id == uid,
                    UserTransaction.product_type == "platform_fee",
                )
                .scalar()
                or 0
            )
        )
    )

    payouts_released = _q2(
        Decimal(
            str(
                db.session.query(func.coalesce(func.sum(UserTransaction.total_amount), 0))
                .filter(
                    UserTransaction.user_id == uid,
                    UserTransaction.tx_type == "payout",
                    UserTransaction.tx_status == "completed",
                )
                .scalar()
                or 0
            )
        )
    )

    net_held = _q2(max(Decimal("0.00"), gross - fees))
    pending = _q2(max(Decimal("0.00"), net_held - payouts_released))
    available = payouts_released

    return {
        "boosts_used": boosts_used,
        "boosts_remaining": boosts_remaining,
        "ticket_count": ticket_qty,
        "ticket_sales_gross_gbp": float(gross),
        "ticket_sales_pending_gbp": float(pending),
        "ticket_sales_pending_display": f"{pending:.2f}",
        "platform_fees_gbp": float(fees),
        "available_to_withdraw_gbp": float(available),
        "available_to_withdraw_display": f"{available:.2f}",
        "payouts_released_gbp": float(payouts_released),
        "is_organiser": bool(mg_ids),
    }


def transaction_flow(tx_type: str | None, product_type: str | None, total_amount: Decimal) -> str:
    """User-facing flow: credit (money in), debit (money out), neutral (no GBP movement)."""
    tt = (tx_type or "").strip().lower()
    if tt in ("payout", "refund", "credit"):
        return "credit"
    if tt in ("purchase", "fee", "promotion"):
        return "debit"
    if _q2(total_amount) > 0:
        return "debit"
    return "neutral"


def serialize_user_transaction(tx: UserTransaction) -> dict:
    when = tx.tx_date
    pt = (tx.product_type or "").strip()
    tt = (tx.tx_type or "").strip()
    amount = _q2(Decimal(str(tx.amount or 0)))
    vat = _q2(Decimal(str(tx.vat or 0)))
    total = _q2(Decimal(str(tx.total_amount or 0)))
    flow = transaction_flow(tt, pt, total)
    return {
        "user_transaction_id": int(tx.user_transaction_id),
        "tx_id": int(tx.tx_id),
        "user_tx_number": int(tx.tx_id),
        "tx_date": when.isoformat() if when else None,
        "tx_date_iso": when.strftime("%Y-%m-%d") if when else "",
        "tx_date_display": when.strftime("%d %b %Y, %H:%M") if when else "—",
        "description": (tx.description or "").strip(),
        "amount": float(amount),
        "vat": float(vat),
        "total_amount": float(total),
        "amount_display": f"{amount:.2f}",
        "vat_display": f"{vat:.2f}",
        "total_display": f"GBP {total:.2f}" if total > 0 else "—",
        "total_display_short": f"£{total:.2f}" if total > 0 else "—",
        "tx_status": (tx.tx_status or "").strip(),
        "product_type": pt,
        "category": product_type_label(pt, tt),
        "tx_type": tt,
        "flow": flow,
        "payment_reference": (tx.payment_reference or "").strip(),
        "payment_method": payment_method_display(tx.payment_reference),
        "notes": (tx.notes or "").strip(),
        "currency_code": (tx.currency_code or "GBP").strip(),
        "vat_rate_percent": float(tx.vat_rate_percent) if tx.vat_rate_percent is not None else None,
        "meeting_id": tx.meeting_id,
        "meeting_group_id": tx.meeting_group_id,
        "meeting_attendee_id": tx.meeting_attendee_id,
    }


def default_account_tx_filter_dates() -> tuple[str, str]:
    """Default My Account transaction filter: from one calendar month ago through today (ISO dates)."""
    import calendar
    from datetime import date

    today = date.today()
    y, m = today.year, today.month - 1
    if m < 1:
        y -= 1
        m = 12
    day = min(today.day, calendar.monthrange(y, m)[1])
    date_from = date(y, m, day)
    return date_from.isoformat(), today.isoformat()


def _parse_filter_date(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        if len(raw) == 10:
            d = datetime.strptime(raw, "%Y-%m-%d")
            if end_of_day:
                return datetime.combine(d.date(), time(23, 59, 59))
            return d
        return datetime.fromisoformat(raw.replace("Z", "+00:00").split("+")[0])
    except ValueError:
        return None


def _apply_tx_list_filters(
    items: list[dict],
    *,
    description_q: str | None = None,
    flow: str | None = None,
) -> list[dict]:
    desc = (description_q or "").strip().lower()
    flow_filter = (flow or "all").strip().lower()
    out: list[dict] = []
    for item in items:
        if desc:
            hay = (
                (item.get("description") or "")
                + " "
                + (item.get("target_name") or "")
            ).lower()
            if desc not in hay:
                continue
        if flow_filter in ("debit", "credit") and item.get("flow") != flow_filter:
            continue
        out.append(item)
    return out


def is_gbp_user_transaction(tx: UserTransaction) -> bool:
    """GBP ledger rows only — excludes £0 Boost promotion spends."""
    return _q2(Decimal(str(tx.total_amount or 0))) > 0


def account_transactions_query(
    user_id: int,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    description_q: str | None = None,
    flow: str | None = None,
    limit: int = 2000,
    gbp_only: bool = False,
) -> list[dict]:
    """Filtered transaction list for My Account (newest first)."""
    uid = int(user_id)
    q = UserTransaction.query.filter(UserTransaction.user_id == uid)

    dt_from = _parse_filter_date(date_from)
    dt_to = _parse_filter_date(date_to, end_of_day=True)
    if dt_from:
        q = q.filter(UserTransaction.tx_date >= dt_from)
    if dt_to:
        q = q.filter(UserTransaction.tx_date <= dt_to)

    rows = (
        q.order_by(UserTransaction.tx_date.desc(), UserTransaction.user_transaction_id.desc())
        .limit(max(1, min(int(limit), 5000)))
        .all()
    )

    out: list[dict] = []
    for tx in rows:
        if gbp_only and not is_gbp_user_transaction(tx):
            continue
        out.append(serialize_user_transaction(tx))
    return _apply_tx_list_filters(out, description_q=description_q, flow=flow)


def gbp_transactions_query(
    user_id: int,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    description_q: str | None = None,
    flow: str | None = None,
    limit: int = 2000,
) -> list[dict]:
    return account_transactions_query(
        user_id,
        date_from=date_from,
        date_to=date_to,
        description_q=description_q,
        flow=flow,
        limit=limit,
        gbp_only=True,
    )


def _promotion_target_label(
    order: PromotionOrder | None,
    *,
    groups: dict[int, MeetingGroup] | None = None,
    meetings: dict[int, Meeting] | None = None,
) -> str:
    if not order:
        return ""
    groups = groups or {}
    meetings = meetings or {}
    if (order.scope or "").strip().lower() == "event" and order.meeting_id:
        meeting = meetings.get(int(order.meeting_id))
        if meeting and (meeting.title or "").strip():
            return (meeting.title or "").strip()
        return "Event"
    mg = groups.get(int(order.meeting_group_id)) if order.meeting_group_id else None
    if mg and (mg.meeting_group_name or "").strip():
        return (mg.meeting_group_name or "").strip()
    return "Event group"


def serialize_boost_ledger_entry(
    row: PromotionCreditLedger,
    *,
    linked_tx: UserTransaction | None = None,
    promotion_order: PromotionOrder | None = None,
    target_name: str = "",
    boost_label: str = "Boost",
    boost_label_plural: str = "Boosts",
) -> dict:
    when = row.created_at
    delta = int(row.delta)
    boosts = abs(delta)
    flow = "credit" if delta > 0 else "debit"
    bl = boost_label if boosts == 1 else boost_label_plural
    target = (target_name or "").strip()
    scope = ""
    if promotion_order:
        scope = (promotion_order.scope or "").strip().lower()

    if row.source_type == "bundle_purchase":
        if linked_tx and (linked_tx.description or "").strip():
            description = (linked_tx.description or "").strip()
        else:
            description = f"Bundle purchase — {boosts} {bl} added to wallet"
        category = "Bundle purchase"
        target = "Your wallet"
    elif row.source_type == "promotion_spend":
        tier_part = (row.notes or "Promotion").strip()
        if target:
            description = f"{tier_part} — {target}"
        else:
            description = tier_part
        category = "Promotion"
    else:
        description = (row.notes or row.source_type or "Boost activity").strip()
        category = "Purchased" if delta > 0 else "Spent"

    boosts_display = f"-{boosts} {bl}" if flow == "debit" else f"+{boosts} {bl}"
    return {
        "ledger_id": int(row.ledger_id),
        "tx_date": when.isoformat() if when else None,
        "tx_date_iso": when.strftime("%Y-%m-%d") if when else "",
        "tx_date_display": when.strftime("%d %b %Y, %H:%M") if when else "—",
        "description": description,
        "target_name": target,
        "scope": scope,
        "flow": flow,
        "category": category,
        "boosts": boosts,
        "boosts_display": boosts_display,
        "balance_after": int(row.balance_after),
        "balance_after_display": str(int(row.balance_after)),
        "source_type": (row.source_type or "").strip(),
        "user_transaction_id": row.user_transaction_id,
        "promotion_order_id": row.promotion_order_id,
    }


def boost_ledger_transactions_for_user(
    user_id: int,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    description_q: str | None = None,
    flow: str | None = None,
    limit: int = 2000,
    boost_label: str = "Boost",
    boost_label_plural: str = "Boosts",
) -> list[dict]:
    uid = int(user_id)
    q = PromotionCreditLedger.query.filter(PromotionCreditLedger.user_id == uid)

    dt_from = _parse_filter_date(date_from)
    dt_to = _parse_filter_date(date_to, end_of_day=True)
    if dt_from:
        q = q.filter(PromotionCreditLedger.created_at >= dt_from)
    if dt_to:
        q = q.filter(PromotionCreditLedger.created_at <= dt_to)

    rows = (
        q.order_by(
            PromotionCreditLedger.created_at.desc(),
            PromotionCreditLedger.ledger_id.desc(),
        )
        .limit(max(1, min(int(limit), 5000)))
        .all()
    )

    tx_ids = [int(r.user_transaction_id) for r in rows if r.user_transaction_id]
    tx_map: dict[int, UserTransaction] = {}
    if tx_ids:
        for tx in UserTransaction.query.filter(UserTransaction.user_transaction_id.in_(tx_ids)).all():
            tx_map[int(tx.user_transaction_id)] = tx

    promo_ids = [int(r.promotion_order_id) for r in rows if r.promotion_order_id]
    promo_map: dict[int, PromotionOrder] = {}
    if promo_ids:
        for order in PromotionOrder.query.filter(
            PromotionOrder.promotion_order_id.in_(promo_ids)
        ).all():
            promo_map[int(order.promotion_order_id)] = order

    mg_ids = {
        int(o.meeting_group_id)
        for o in promo_map.values()
        if o.meeting_group_id
    }
    meeting_ids = {
        int(o.meeting_id) for o in promo_map.values() if o.meeting_id
    }
    group_map: dict[int, MeetingGroup] = {}
    if mg_ids:
        for mg in MeetingGroup.query.filter(MeetingGroup.meeting_group_id.in_(mg_ids)).all():
            group_map[int(mg.meeting_group_id)] = mg
    meeting_map: dict[int, Meeting] = {}
    if meeting_ids:
        for meeting in Meeting.query.filter(Meeting.meeting_id.in_(meeting_ids)).all():
            meeting_map[int(meeting.meeting_id)] = meeting

    out: list[dict] = []
    for row in rows:
        linked = tx_map.get(int(row.user_transaction_id)) if row.user_transaction_id else None
        promo = (
            promo_map.get(int(row.promotion_order_id))
            if row.promotion_order_id
            else None
        )
        target = _promotion_target_label(
            promo, groups=group_map, meetings=meeting_map
        )
        out.append(
            serialize_boost_ledger_entry(
                row,
                linked_tx=linked,
                promotion_order=promo,
                target_name=target,
                boost_label=boost_label,
                boost_label_plural=boost_label_plural,
            )
        )
    return _apply_tx_list_filters(out, description_q=description_q, flow=flow)


def _meeting_release_timing(meeting: Meeting, *, now: datetime | None = None) -> dict:
    """When ticket income may be released (48 hours after the event ends)."""
    now = now or datetime.utcnow()
    starts = meeting.starts_at
    if not starts:
        return {
            "event_ends_display": "—",
            "withdrawable_at_display": "—",
            "hours_until_withdrawable": None,
            "release_status": "unknown",
            "release_label": "Event date not set",
        }
    duration = max(15, int(meeting.duration_minutes or 60))
    ends = starts + timedelta(minutes=duration)
    withdrawable = ends + timedelta(hours=WITHDRAWAL_HOLD_HOURS)
    secs = (withdrawable - now).total_seconds()
    if secs > 0:
        hours = max(1, int((secs + 3599) // 3600))
        return {
            "event_ends_at_iso": ends.isoformat(),
            "event_ends_display": ends.strftime("%d %b %Y, %H:%M"),
            "withdrawable_at_iso": withdrawable.isoformat(),
            "withdrawable_at_display": withdrawable.strftime("%d %b %Y, %H:%M"),
            "hours_until_withdrawable": hours,
            "release_status": "scheduled",
            "release_label": f"Available in {hours} hour{'s' if hours != 1 else ''}",
        }
    return {
        "event_ends_at_iso": ends.isoformat(),
        "event_ends_display": ends.strftime("%d %b %Y, %H:%M"),
        "withdrawable_at_iso": withdrawable.isoformat(),
        "withdrawable_at_display": withdrawable.strftime("%d %b %Y, %H:%M"),
        "hours_until_withdrawable": 0,
        "release_status": "eligible",
        "release_label": "Eligible for release (48h after event ended)",
    }


def withdraw_funds_panel_for_user(user_id: int) -> dict:
    """Withdraw tab: available balance and pending ticket income with release countdown."""
    summary = organiser_account_summary(int(user_id))
    pending = pending_ticket_sales_for_user(int(user_id))
    lines = list(pending.get("lines") or [])
    scheduled = [
        ln for ln in lines if ln.get("release_status") in ("scheduled", "unknown")
    ]
    eligible = [ln for ln in lines if ln.get("release_status") == "eligible"]
    scheduled.sort(key=lambda x: (x.get("hours_until_withdrawable") or 99999, x.get("title") or ""))
    eligible.sort(key=lambda x: x.get("title") or "")
    awaiting_scheduled_gbp = sum(float(ln.get("net_pending_gbp") or 0) for ln in scheduled)
    awaiting_eligible_gbp = sum(float(ln.get("net_pending_gbp") or 0) for ln in eligible)
    return {
        "available_gbp": float(summary.get("available_to_withdraw_gbp") or 0),
        "available_display": summary.get("available_to_withdraw_display", "0.00"),
        "total_pending_gbp": float(pending.get("total_pending_gbp") or 0),
        "total_pending_display": pending.get("total_pending_display", "0.00"),
        "awaiting_scheduled_gbp": awaiting_scheduled_gbp,
        "awaiting_scheduled_display": f"{awaiting_scheduled_gbp:.2f}",
        "awaiting_eligible_gbp": awaiting_eligible_gbp,
        "awaiting_eligible_display": f"{awaiting_eligible_gbp:.2f}",
        "hold_hours": WITHDRAWAL_HOLD_HOURS,
        "scheduled_lines": scheduled,
        "eligible_lines": eligible,
        "is_organiser": bool(pending.get("is_organiser")),
    }


def pending_ticket_sales_for_user(user_id: int) -> dict:
    """Per-event ticket sales awaiting admin payout release."""
    uid = int(user_id)
    summary = organiser_account_summary(uid)
    mg_ids = [
        int(r[0])
        for r in db.session.query(MeetingGroup.meeting_group_id)
        .filter(MeetingGroup.user_id == uid)
        .all()
    ]
    if not mg_ids:
        return {
            "lines": [],
            "total_pending_gbp": 0.0,
            "total_pending_display": "0.00",
            "released_gbp": float(summary.get("payouts_released_gbp") or 0),
            "released_display": summary.get("available_to_withdraw_display", "0.00"),
            "is_organiser": False,
        }

    payouts_released = _q2(Decimal(str(summary.get("payouts_released_gbp") or 0)))
    meetings = (
        Meeting.query.filter(Meeting.meeting_group_id.in_(mg_ids))
        .order_by(Meeting.starts_at.desc(), Meeting.meeting_id.desc())
        .all()
    )

    lines: list[dict] = []
    for meeting in meetings:
        mid = int(meeting.meeting_id)
        agg = (
            db.session.query(
                func.coalesce(func.sum(MeetingAttendee.amount_paid), 0),
                func.coalesce(func.sum(MeetingAttendee.quantity), 0),
            )
            .filter(
                MeetingAttendee.meeting_id == mid,
                MeetingAttendee.status.in_(_ATTENDEE_COUNTABLE_STATUSES),
            )
            .first()
        )
        gross = _q2(Decimal(str((agg[0] if agg else 0) or 0)))
        qty = int((agg[1] if agg else 0) or 0)
        if gross <= 0 and qty <= 0:
            continue

        fees = _q2(
            Decimal(
                str(
                    db.session.query(func.coalesce(func.sum(UserTransaction.total_amount), 0))
                    .filter(
                        UserTransaction.user_id == uid,
                        UserTransaction.product_type == "platform_fee",
                        UserTransaction.meeting_id == mid,
                    )
                    .scalar()
                    or 0
                )
            )
        )
        net = _q2(max(Decimal("0.00"), gross - fees))
        when = meeting.starts_at
        timing = _meeting_release_timing(meeting)
        lines.append(
            {
                "meeting_id": mid,
                "title": (meeting.title or "Event").strip(),
                "event_date_display": when.strftime("%d %b %Y") if when else "—",
                "ticket_count": qty,
                "gross_gbp": float(gross),
                "gross_display": f"{gross:.2f}",
                "fees_gbp": float(fees),
                "fees_display": f"{fees:.2f}",
                "net_pending_gbp": float(net),
                "net_pending_display": f"{net:.2f}",
                **timing,
            }
        )

    lines.sort(key=lambda x: (-x["net_pending_gbp"], x["title"]))
    pending = _q2(Decimal(str(summary.get("ticket_sales_pending_gbp") or 0)))

    return {
        "lines": lines,
        "total_pending_gbp": float(pending),
        "total_pending_display": summary.get("ticket_sales_pending_display", "0.00"),
        "released_gbp": float(payouts_released),
        "released_display": summary.get("available_to_withdraw_display", "0.00"),
        "is_organiser": True,
    }


def get_user_transaction(user_id: int, user_transaction_id: int) -> dict | None:
    tx = UserTransaction.query.filter_by(
        user_id=int(user_id),
        user_transaction_id=int(user_transaction_id),
    ).first()
    if not tx:
        return None
    return serialize_user_transaction(tx)


def account_transactions_for_user(user_id: int, limit: int = 500) -> list[dict]:
    return gbp_transactions_query(user_id, limit=limit)
