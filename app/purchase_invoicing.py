"""VAT invoices (PDF) and purchase confirmation emails for The Networker Hub."""

from __future__ import annotations

import os
import re
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html import escape
from pathlib import Path
from typing import Any

from fpdf import FPDF

from .models import User, db
from .user_account_tx import get_user_transaction, payment_method_display

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LOGO_PATH = Path(__file__).resolve().parent / "static" / "images" / "The-NetworkerLogo.png"

_BRAND_RGB = (91, 45, 115)
_MUTED_RGB = (107, 92, 117)


def _company_settings() -> dict[str, str]:
    return {
        "legal_name": os.getenv("TNW_INVOICE_LEGAL_NAME", "The Networker Group Ltd").strip(),
        "trading_name": os.getenv("TNW_INVOICE_TRADING_AS", "The Networker Hub").strip(),
        "company_number": os.getenv("TNW_COMPANY_NUMBER", "15252227").strip(),
        "vat_number": os.getenv("TNW_VAT_NUMBER", "454 4092 94").strip(),
        "address": os.getenv(
            "TNW_INVOICE_ADDRESS",
            "Magpas HQ, Barnwell Road, Alconbury Weald, Huntingdon, Cambridgeshire PE28 4YF",
        ).strip(),
        "support_email": os.getenv("SUPPORT_EMAIL", os.getenv("SMTP_USER", "hello@the-networker.co.uk")).strip(),
        "site_name": os.getenv("SITE_NAME", "The Networker").strip(),
    }


def _pdf_safe(text: str) -> str:
    if not text:
        return ""
    return (
        text.replace("\u2014", "-")
        .replace("\u2013", "-")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u00a3", "GBP ")
        .replace("\u2026", "...")
        .encode("latin-1", errors="replace")
        .decode("latin-1")
    )


def invoice_number(tx: dict) -> str:
    return f"TNW-{int(tx.get('user_tx_number') or tx.get('tx_id') or 0):06d}"


def invoice_filename(tx: dict) -> str:
    return f"{invoice_number(tx)}.pdf"


def should_issue_vat_invoice(tx: dict) -> bool:
    """Paid GBP purchases get a VAT invoice (not £0 ledger rows or organiser fees)."""
    try:
        total = float(tx.get("total_amount") or 0)
    except (TypeError, ValueError):
        total = 0.0
    if total <= 0:
        return False
    tt = (tx.get("tx_type") or "").strip().lower()
    return tt == "purchase"


def vat_footer_note(tx: dict) -> str:
    pt = (tx.get("product_type") or "").strip().lower()
    try:
        vat = float(tx.get("vat") or 0)
    except (TypeError, ValueError):
        vat = 0.0
    rate = tx.get("vat_rate_percent")
    co = _company_settings()
    hub = co["trading_name"]
    legal = co["legal_name"]
    if pt == "promotion_bundle":
        return (
            f"Supply from {hub} ({legal}). Boost bundle prices include UK VAT at 20%."
        )
    if vat > 0 and rate:
        return f"Supply from {hub} ({legal}). Amounts include UK VAT at {float(rate):g}%."
    if vat > 0:
        return f"Supply from {hub} ({legal}). Amounts include UK VAT where shown."
    return f"Supply from {hub} ({legal}). No VAT applies to this supply."


def _invoice_line_description(tx: dict) -> str:
    """Single clear product line for the invoice table (no payment reference)."""
    raw = (tx.get("description") or "Purchase").strip()
    lines = [ln.strip() for ln in raw.replace("\r", "\n").split("\n") if ln.strip()]
    lines = [ln for ln in lines if not ln.lower().startswith("reference:")]
    if not lines:
        return "Purchase"
    return lines[-1]


def _line_items_for_tx(tx: dict) -> list[dict[str, Any]]:
    desc = _invoice_line_description(tx)
    qty = 1
    m = re.search(r"[×x]\s*(\d+)", desc, re.IGNORECASE)
    if m:
        try:
            qty = max(1, int(m.group(1)))
        except ValueError:
            qty = 1
    net = float(tx.get("amount") or 0)
    vat = float(tx.get("vat") or 0)
    gross = float(tx.get("total_amount") or 0)
    rate = tx.get("vat_rate_percent")
    if rate is None and vat > 0:
        rate = 20.0
    return [
        {
            "description": desc,
            "qty": qty,
            "net": net,
            "vat": vat,
            "gross": gross,
            "vat_rate": float(rate) if rate is not None else 0.0,
        }
    ]


def bill_to_from_user(user: User) -> dict[str, str]:
    parts = [(user.first_name or "").strip(), (user.second_name or "").strip()]
    name = " ".join(p for p in parts if p).strip() or (user.username or "").strip() or "Customer"
    return {"name": name, "email": (user.email or "").strip()}


class _TnwInvoicePdf(FPDF):
    def __init__(self, *, company: dict[str, str]):
        super().__init__()
        self._company = company

    def header(self):
        co = self._company
        self.set_fill_color(*_BRAND_RGB)
        self.rect(0, 0, self.w, 32, style="F")
        logo_x = self.l_margin
        if _LOGO_PATH.is_file():
            try:
                self.image(str(_LOGO_PATH), x=logo_x, y=6, w=42)
                text_x = logo_x + 46
            except Exception:
                text_x = logo_x
        else:
            text_x = logo_x
        self.set_xy(text_x, 8)
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 13)
        self.cell(0, 6, _pdf_safe(co["trading_name"]), ln=True)
        self.set_x(text_x)
        self.set_font("Helvetica", "", 8)
        self.cell(0, 4, _pdf_safe(co["legal_name"]), ln=True)
        self.set_text_color(0, 0, 0)
        self.set_y(36)

    def footer(self):
        self.set_y(-18)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(*_MUTED_RGB)
        co = self._company
        lines = [
            f"{co['legal_name']} · Company No. {co['company_number']} · VAT No. {co['vat_number']}",
            _pdf_safe(co["address"][:120]),
            f"Questions: {co['support_email']}",
        ]
        for line in lines:
            self.cell(0, 3.5, line, align="C", ln=True)
        self.set_text_color(0, 0, 0)


def build_purchase_invoice_pdf(
    tx: dict,
    *,
    bill_to: dict[str, str] | None = None,
    user_email: str = "",
) -> bytes:
    co = _company_settings()
    inv_no = invoice_number(tx)
    inv_date = (tx.get("tx_date_display") or tx.get("tx_date_iso") or "").strip() or datetime.utcnow().strftime(
        "%d %b %Y"
    )
    bt = bill_to or {}
    if not bt.get("email") and user_email:
        bt = {**bt, "email": user_email}
    if not bt.get("name"):
        bt["name"] = "Customer"

    pdf = _TnwInvoicePdf(company=co)
    pdf.set_auto_page_break(auto=True, margin=22)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(*_BRAND_RGB)
    pdf.cell(0, 10, "TAX INVOICE", ln=True)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    detail_col_w = (pdf.w - pdf.l_margin - pdf.r_margin) / 2
    block_y = pdf.get_y()
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(detail_col_w, 5, "Invoice details", ln=True)
    pdf.set_font("Helvetica", "", 9)
    for label, val in (
        ("Invoice number", inv_no),
        ("Invoice date", inv_date),
        ("Status", (tx.get("tx_status") or "Completed").strip().capitalize()),
        ("Payment", payment_method_display(tx.get("payment_reference")) or "-"),
        ("Reference", (tx.get("payment_reference") or "-")[:40]),
    ):
        pdf.cell(detail_col_w, 5, _pdf_safe(f"{label}: {val}"), ln=True)
    y_after_details = pdf.get_y()

    pdf.set_xy(pdf.l_margin + detail_col_w, block_y)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(detail_col_w, 5, "Bill to", ln=True)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_x(pdf.l_margin + detail_col_w)
    pdf.cell(detail_col_w, 5, _pdf_safe(bt.get("name", "Customer")), ln=True)
    if bt.get("email"):
        pdf.set_x(pdf.l_margin + detail_col_w)
        pdf.cell(detail_col_w, 5, _pdf_safe(bt["email"]), ln=True)
    y_after_bill_to = pdf.get_y()

    pdf.set_y(max(y_after_details, y_after_bill_to) + 6)

    items = _line_items_for_tx(tx)
    table_w = pdf.w - pdf.l_margin - pdf.r_margin
    cols = [78, 14, 28, 16, 24, 30]
    scale = table_w / sum(cols)
    col_w = [c * scale for c in cols]
    headers = ["Description", "Qty", "Net (ex VAT)", "VAT %", "VAT", "Total (inc VAT)"]

    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(243, 235, 248)
    for i, h in enumerate(headers):
        pdf.cell(col_w[i], 7, _pdf_safe(h), border=1, fill=True)
    pdf.ln()

    pdf.set_font("Helvetica", "", 8)
    row_h = 8
    for row in items:
        desc = _pdf_safe(row["description"][:95])
        x0 = pdf.l_margin
        y_row = pdf.get_y()
        pdf.cell(col_w[0], row_h, desc, border=1)
        rest = [
            str(row["qty"]),
            f"GBP {row['net']:.2f}",
            f"{row['vat_rate']:.0f}%" if row["vat"] > 0 else "-",
            f"GBP {row['vat']:.2f}",
            f"GBP {row['gross']:.2f}",
        ]
        for j, val in enumerate(rest, start=1):
            pdf.set_xy(x0 + sum(col_w[:j]), y_row)
            pdf.cell(col_w[j], row_h, _pdf_safe(val), border=1)
        pdf.set_y(y_row + row_h)

    pdf.ln(4)
    net_total = sum(r["net"] for r in items)
    vat_total = sum(r["vat"] for r in items)
    gross_total = sum(r["gross"] for r in items)
    box_w = 70
    box_x = pdf.w - pdf.r_margin - box_w
    pdf.set_x(box_x)
    pdf.set_font("Helvetica", "", 9)
    for label, amount in (
        ("Subtotal (ex VAT)", net_total),
        ("VAT", vat_total),
        ("Total (inc VAT)", gross_total),
    ):
        pdf.cell(box_w * 0.55, 6, _pdf_safe(label), border=0)
        pdf.cell(box_w * 0.45, 6, _pdf_safe(f"GBP {amount:.2f}"), align="R", ln=True)
        pdf.set_x(box_x)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(box_w * 0.55, 7, "Amount paid", border="T")
    pdf.cell(box_w * 0.45, 7, _pdf_safe(f"GBP {gross_total:.2f}"), align="R", border="T", ln=True)

    pdf.ln(5)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(*_MUTED_RGB)
    pdf.multi_cell(0, 4, _pdf_safe(vat_footer_note(tx)))
    pdf.set_text_color(0, 0, 0)

    out = pdf.output()
    if isinstance(out, (bytes, bytearray)):
        return bytes(out)
    return str(out).encode("latin-1")


def _smtp_config() -> dict[str, Any] | None:
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    if not smtp_user or not smtp_password:
        return None
    return {
        "host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
        "port": int(os.getenv("SMTP_PORT", "587")),
        "user": smtp_user,
        "password": smtp_password,
        "site_name": os.getenv("SITE_NAME", "The Networker").strip(),
    }


def _invoice_email_bodies(
    tx: dict,
    *,
    bill_to: dict[str, str],
    extra_html: str = "",
    extra_plain: str = "",
) -> tuple[str, str, str]:
    co = _company_settings()
    inv_no = invoice_number(tx)
    total = tx.get("total_display") or f"GBP {float(tx.get('total_amount') or 0):.2f}"
    desc = escape((tx.get("description") or "Your purchase").strip())
    name = escape(bill_to.get("name") or "there")
    vat_line = escape(vat_footer_note(tx))
    subject = f"Invoice {inv_no} — {co['trading_name']}"
    plain = (
        f"Hello {bill_to.get('name') or 'there'},\n\n"
        f"Thank you for your purchase on {co['site_name']}.\n\n"
        f"Invoice: {inv_no}\n"
        f"Description: {tx.get('description') or 'Purchase'}\n"
        f"Total: {total}\n\n"
        f"{vat_footer_note(tx)}\n\n"
        "Your VAT invoice is attached as a PDF.\n\n"
        f"{extra_plain}"
        f"Support: {co['support_email']}\n"
        f"{co['legal_name']} trading as {co['trading_name']}\n"
    )
    html = (
        "<html><body style=\"font-family:Segoe UI,Arial,Helvetica,sans-serif;"
        "color:#2f1f3a;background:#faf8fc;margin:0;padding:24px;\">"
        "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" "
        "style=\"max-width:560px;margin:0 auto;background:#fff;border-radius:12px;"
        "border:1px solid #e8dff0;overflow:hidden;\">"
        "<tr><td style=\"background:#5b2d73;padding:20px 24px;\">"
        f"<h1 style=\"margin:0;font-size:20px;color:#fff;\">{escape(co['trading_name'])}</h1>"
        f"<p style=\"margin:6px 0 0;font-size:13px;color:#e8dff0;\">Tax invoice {escape(inv_no)}</p>"
        "</td></tr>"
        "<tr><td style=\"padding:24px;\">"
        f"<p style=\"margin:0 0 12px;\">Hello {name},</p>"
        "<p style=\"margin:0 0 16px;\">Thank you for your purchase. "
        "Please find your VAT invoice attached as a PDF.</p>"
        f"<table role=\"presentation\" width=\"100%\" style=\"border-collapse:collapse;"
        "font-size:14px;margin-bottom:16px;\">"
        f"<tr><td style=\"padding:8px 0;border-bottom:1px solid #eee;color:#6b5c75;\">Description</td>"
        f"<td style=\"padding:8px 0;border-bottom:1px solid #eee;text-align:right;\">{desc}</td></tr>"
        f"<tr><td style=\"padding:8px 0;color:#6b5c75;\">Total paid</td>"
        f"<td style=\"padding:8px 0;text-align:right;font-weight:bold;color:#5b2d73;\">"
        f"{escape(str(total))}</td></tr></table>"
        f"<p style=\"margin:0 0 16px;font-size:13px;color:#6b5c75;\">{vat_line}</p>"
        f"{extra_html}"
        f"<p style=\"margin:0;font-size:12px;color:#9a8aa3;\">"
        f"{escape(co['legal_name'])} · VAT {escape(co['vat_number'])}<br>"
        f"Support: <a href=\"mailto:{escape(co['support_email'])}\" style=\"color:#5b2d73;\">"
        f"{escape(co['support_email'])}</a></p>"
        "</td></tr></table></body></html>"
    )
    return subject, plain, html


def send_purchase_invoice_email(
    *,
    recipient_email: str,
    tx: dict,
    pdf_bytes: bytes,
    bill_to: dict[str, str] | None = None,
    extra_html: str = "",
    extra_plain: str = "",
) -> None:
    to_addr = (recipient_email or "").strip()
    if not to_addr or not _EMAIL_RE.match(to_addr):
        raise ValueError("Invalid recipient email")
    cfg = _smtp_config()
    if not cfg:
        raise RuntimeError("SMTP credentials are missing")

    bt = bill_to or {"name": "Customer", "email": to_addr}
    subject, plain, html = _invoice_email_bodies(
        tx, bill_to=bt, extra_html=extra_html, extra_plain=extra_plain
    )
    co = _company_settings()

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["user"]
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg["user"].split("@")[-1])
    msg["X-Mailer"] = f"{co['trading_name']} Invoicing"
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    msg.add_attachment(
        pdf_bytes,
        maintype="application",
        subtype="pdf",
        filename=invoice_filename(tx),
    )

    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=25) as smtp:
        smtp.starttls()
        smtp.login(cfg["user"], cfg["password"])
        smtp.send_message(msg)


def send_account_transaction_pdf_email(
    *,
    recipient_email: str,
    tx: dict,
    pdf_bytes: bytes,
    filename: str,
    bill_to: dict[str, str] | None = None,
) -> None:
    """Email a single transaction PDF (invoice or statement) to the account holder."""
    to_addr = (recipient_email or "").strip()
    if not to_addr or not _EMAIL_RE.match(to_addr):
        raise ValueError("Invalid recipient email")
    cfg = _smtp_config()
    if not cfg:
        raise RuntimeError("SMTP credentials are missing")

    co = _company_settings()
    bt = bill_to or {"name": "Customer", "email": to_addr}
    ref = invoice_number(tx) if should_issue_vat_invoice(tx) else f"TNW-{tx.get('user_tx_number', '')}"
    is_invoice = should_issue_vat_invoice(tx)
    doc_label = "Tax invoice" if is_invoice else "Transaction statement"
    total = tx.get("total_display") or f"GBP {float(tx.get('total_amount') or 0):.2f}"
    desc = (tx.get("description") or "Transaction").strip()
    subject = f"{doc_label} {ref} — {co['trading_name']}"
    plain = (
        f"Hello {bt.get('name') or 'there'},\n\n"
        f"Please find your {doc_label.lower()} attached as a PDF.\n\n"
        f"Reference: {ref}\n"
        f"Description: {desc}\n"
        f"Total: {total}\n\n"
        f"Support: {co['support_email']}\n"
        f"{co['legal_name']} trading as {co['trading_name']}\n"
    )
    html = (
        "<html><body style=\"font-family:Segoe UI,Arial,sans-serif;color:#2f1f3a;\">"
        f"<p>Hello {escape(bt.get('name') or 'there')},</p>"
        f"<p>Please find your <strong>{escape(doc_label.lower())}</strong> attached as a PDF.</p>"
        f"<p><strong>Reference:</strong> {escape(ref)}<br>"
        f"<strong>Description:</strong> {escape(desc)}<br>"
        f"<strong>Total:</strong> {escape(str(total))}</p>"
        f"<p style=\"font-size:12px;color:#6b5c75;\">Support: {escape(co['support_email'])}</p>"
        "</body></html>"
    )
    attach_name = filename or (invoice_filename(tx) if is_invoice else f"transaction-{ref}.pdf")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["user"]
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg["user"].split("@")[-1])
    msg["X-Mailer"] = f"{co['trading_name']} Account"
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    msg.add_attachment(
        pdf_bytes,
        maintype="application",
        subtype="pdf",
        filename=attach_name,
    )

    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=25) as smtp:
        smtp.starttls()
        smtp.login(cfg["user"], cfg["password"])
        smtp.send_message(msg)


def send_purchase_invoice_for_transaction(
    user_id: int,
    user_transaction_id: int,
    *,
    extra_html: str = "",
    extra_plain: str = "",
) -> bool:
    """Build and email a VAT invoice; returns False if skipped or on SMTP skip (logs errors)."""
    from flask import current_app

    tx = get_user_transaction(int(user_id), int(user_transaction_id))
    if not tx or not should_issue_vat_invoice(tx):
        return False
    user = db.session.get(User, int(user_id))
    if not user:
        return False
    bill_to = bill_to_from_user(user)
    try:
        pdf_bytes = build_purchase_invoice_pdf(tx, bill_to=bill_to)
        send_purchase_invoice_email(
            recipient_email=bill_to["email"],
            tx=tx,
            pdf_bytes=pdf_bytes,
            bill_to=bill_to,
            extra_html=extra_html,
            extra_plain=extra_plain,
        )
        return True
    except RuntimeError as exc:
        if "SMTP" in str(exc):
            try:
                current_app.logger.warning(
                    "Purchase invoice not emailed (SMTP not configured): user=%s tx=%s",
                    user_id,
                    user_transaction_id,
                )
            except Exception:
                pass
            return False
        raise
    except Exception:
        try:
            current_app.logger.exception(
                "send_purchase_invoice_for_transaction user=%s tx=%s",
                user_id,
                user_transaction_id,
            )
        except Exception:
            pass
        return False
