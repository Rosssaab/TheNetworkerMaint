"""UK ticket VAT modes for organiser pricing (none / plus / included)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

UK_VAT_RATE_PERCENT = Decimal("20.00")

VAT_MODE_NONE = "none"
VAT_MODE_PLUS = "plus"
VAT_MODE_INCLUDED = "included"

VALID_VAT_MODES = frozenset({VAT_MODE_NONE, VAT_MODE_PLUS, VAT_MODE_INCLUDED})


def _q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def infer_vat_mode(
    vat_rate_percent,
    vat_treatment: str | None = None,
) -> str:
    """Resolve mode from stored treatment, else legacy vat_rate_percent only."""
    mode = (vat_treatment or "").strip().lower()
    if mode in VALID_VAT_MODES:
        return mode
    try:
        rate = Decimal(str(vat_rate_percent if vat_rate_percent is not None else 0))
    except Exception:
        rate = Decimal("0")
    if rate <= 0:
        return VAT_MODE_NONE
    return VAT_MODE_INCLUDED


def vat_rate_for_mode(mode: str) -> Decimal:
    if mode == VAT_MODE_NONE:
        return Decimal("0")
    return UK_VAT_RATE_PERCENT


def vat_multiplier(mode: str) -> Decimal:
    rate = vat_rate_for_mode(mode) / Decimal("100")
    return Decimal("1") + rate


def display_price_from_stored(
    price_amount,
    vat_rate_percent=None,
    vat_treatment: str | None = None,
) -> Decimal:
    """Amount shown in the organiser ticket price field."""
    stored = Decimal(str(price_amount if price_amount is not None else 0))
    mode = infer_vat_mode(vat_rate_percent, vat_treatment)
    if mode == VAT_MODE_PLUS:
        mult = vat_multiplier(mode)
        if mult > 0:
            return _q2(stored / mult)
    return _q2(stored)


def buyer_unit_price_from_display(
    display_price,
    mode: str,
) -> Decimal:
    """Convert organiser-entered price to amount charged to the buyer."""
    mode = (mode or VAT_MODE_NONE).strip().lower()
    if mode not in VALID_VAT_MODES:
        mode = VAT_MODE_NONE
    amount = Decimal(str(display_price if display_price is not None else 0))
    if amount < 0:
        amount = Decimal("0")
    if mode == VAT_MODE_PLUS:
        return _q2(amount * vat_multiplier(mode))
    return _q2(amount)


def buyer_unit_price_for_ticket(ticket) -> Decimal:
    """Stored price_amount is always the buyer-facing unit price."""
    return _q2(Decimal(str(getattr(ticket, "price_amount", None) or 0)))


def parse_vat_mode_from_form(form, *, index: int | None = None) -> str:
    if index is None:
        raw = (form.get("vat_mode") or "").strip().lower()
    else:
        lst = form.getlist("vat_mode")
        raw = (lst[index] if index < len(lst) else "").strip().lower()
    if raw in VALID_VAT_MODES:
        return raw
    return VAT_MODE_NONE


def normalize_vat_from_form(
    form,
    display_price_raw: str | None,
    *,
    index: int | None = None,
) -> tuple[str, Decimal, Decimal]:
    """
    Returns (vat_mode, vat_rate_percent, buyer_unit_price).
    display_price_raw is the value from the ticket price input.
    """
    mode = parse_vat_mode_from_form(form, index=index)
    try:
        display_price = Decimal(str((display_price_raw or "").strip() or "0"))
    except Exception:
        display_price = Decimal("0")
    if display_price < 0:
        display_price = Decimal("0")
    buyer_price = buyer_unit_price_from_display(display_price, mode)
    return mode, vat_rate_for_mode(mode), buyer_price
