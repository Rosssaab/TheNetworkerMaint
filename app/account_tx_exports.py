"""CSV and PDF export for My Account transactions."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from fpdf import FPDF


def _pdf_safe(text: str) -> str:
    """FPDF core fonts are Latin-1; replace common Unicode punctuation."""
    if not text:
        return ""
    return (
        text.replace("\u2014", "-")
        .replace("\u2013", "-")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2026", "...")
        .encode("latin-1", errors="replace")
        .decode("latin-1")
    )


class _TnwTxPdf(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 14)
        self.cell(0, 8, _pdf_safe("The Networker - Transaction statement"), ln=True)
        self.set_font("Helvetica", "", 9)
        self.set_text_color(100, 100, 100)
        self.cell(0, 5, _pdf_safe(f"Generated {datetime.utcnow().strftime('%d %b %Y %H:%M')} UTC"), ln=True)
        self.set_text_color(0, 0, 0)
        self.ln(4)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 8, _pdf_safe(f"Page {self.page_no()}"), align="C")


def build_transactions_csv(transactions: list[dict], *, user_email: str = "") -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "Transaction #",
            "Date",
            "Flow",
            "Type",
            "Description",
            "Net (GBP)",
            "VAT (GBP)",
            "Total (GBP)",
            "Status",
            "Payment method",
            "Payment reference",
            "Product type",
            "Notes",
        ]
    )
    for tx in transactions:
        writer.writerow(
            [
                tx.get("user_tx_number", ""),
                tx.get("tx_date_iso", ""),
                tx.get("flow", ""),
                tx.get("category", ""),
                tx.get("description", ""),
                f"{tx.get('amount', 0):.2f}",
                f"{tx.get('vat', 0):.2f}",
                f"{tx.get('total_amount', 0):.2f}",
                tx.get("tx_status", ""),
                tx.get("payment_method", ""),
                tx.get("payment_reference", ""),
                tx.get("product_type", ""),
                tx.get("notes", ""),
            ]
        )
    return buf.getvalue().encode("utf-8-sig")


_PDF_TABLE_LINE_H = 6.0
_PDF_TABLE_DESC_COL = 4


def _pdf_wrap_text(pdf: FPDF, text: str, width: float) -> list[str]:
    """Split text into lines that fit within width (mm) using current font."""
    safe = _pdf_safe(text or "").strip()
    if not safe:
        return [""]
    words = safe.split()
    lines: list[str] = []
    current = ""
    max_w = max(width - 1.0, 4.0)

    def _flush() -> None:
        nonlocal current
        if current:
            lines.append(current)
            current = ""

    def _split_long_word(word: str) -> None:
        nonlocal current
        chunk = ""
        for ch in word:
            trial = chunk + ch
            if pdf.get_string_width(trial) <= max_w:
                chunk = trial
            else:
                if chunk:
                    lines.append(chunk)
                chunk = ch
        current = chunk

    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        if pdf.get_string_width(candidate) <= max_w:
            current = candidate
        elif not current:
            _split_long_word(word)
        else:
            _flush()
            if pdf.get_string_width(word) <= max_w:
                current = word
            else:
                _split_long_word(word)
    _flush()
    return lines or [""]


def _pdf_table_row_height(pdf: FPDF, col_w: list[float], row: list[str], wrap_cols: set[int]) -> float:
    pdf.set_font("Helvetica", "", 8)
    max_lines = 1
    for i in wrap_cols:
        max_lines = max(max_lines, len(_pdf_wrap_text(pdf, row[i], col_w[i])))
    return _PDF_TABLE_LINE_H * max_lines


def _pdf_draw_table_row(
    pdf: FPDF,
    col_w: list[float],
    row: list[str],
    *,
    wrap_cols: set[int] | None = None,
    header: bool = False,
) -> None:
    wrap_cols = wrap_cols if wrap_cols is not None else {_PDF_TABLE_DESC_COL}
    x0 = pdf.l_margin
    y0 = pdf.get_y()
    style = ("Helvetica", "B" if header else "", 8)
    pdf.set_font(*style)

    if header:
        pdf.set_fill_color(243, 235, 248)
        for i, cell in enumerate(row):
            pdf.cell(col_w[i], 7, _pdf_safe(cell), border=1, fill=True)
        pdf.ln()
        return

    row_h = _pdf_table_row_height(pdf, col_w, row, wrap_cols)
    wrapped: dict[int, list[str]] = {}
    for i in wrap_cols:
        wrapped[i] = _pdf_wrap_text(pdf, row[i], col_w[i])

    for i, cell in enumerate(row):
        if i in wrap_cols:
            continue
        x = x0 + sum(col_w[:i])
        pdf.set_xy(x, y0)
        pdf.cell(col_w[i], row_h, _pdf_safe(str(cell)), border=1)

    for i in sorted(wrap_cols):
        x = x0 + sum(col_w[:i])
        pdf.set_xy(x, y0)
        pdf.multi_cell(
            col_w[i],
            _PDF_TABLE_LINE_H,
            "\n".join(wrapped[i]),
            border=1,
            align="L",
        )

    pdf.set_y(y0 + row_h)


def _pdf_detail_lines(tx: dict) -> list[tuple[str, str]]:
    return [
        ("Transaction #", str(tx.get("user_tx_number", ""))),
        ("Date", tx.get("tx_date_display", "")),
        ("Flow", (tx.get("flow") or "").capitalize()),
        ("Category", tx.get("category", "")),
        ("Description", tx.get("description", "")),
        ("Net amount", f"GBP {tx.get('amount_display', '0.00')}"),
        ("VAT", f"GBP {tx.get('vat_display', '0.00')}"),
        ("Total", tx.get("total_display", "GBP 0.00")),
        ("Status", tx.get("tx_status", "")),
        ("Payment", tx.get("payment_method", "")),
        ("Reference", tx.get("payment_reference", "") or "-"),
        ("Product type", tx.get("product_type", "")),
        ("Transaction type", tx.get("tx_type", "")),
        ("Notes", tx.get("notes", "") or "-"),
    ]


def build_transaction_pdf(tx: dict, *, user_email: str = "", bill_to: dict | None = None) -> bytes:
    from .purchase_invoicing import build_purchase_invoice_pdf, should_issue_vat_invoice

    if should_issue_vat_invoice(tx):
        return build_purchase_invoice_pdf(tx, bill_to=bill_to, user_email=user_email)

    pdf = _TnwTxPdf()
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()
    if user_email:
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 6, _pdf_safe(f"Account: {user_email}"), ln=True)
        pdf.ln(2)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, _pdf_safe("Transaction details"), ln=True)
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 10)
    label_w = 48.0
    for label, value in _pdf_detail_lines(tx):
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(label_w, 7, _pdf_safe(f"{label}:"), ln=0)
        pdf.set_font("Helvetica", "", 10)
        value_w = pdf.w - pdf.r_margin - pdf.get_x()
        pdf.multi_cell(max(value_w, 20), 7, _pdf_safe(str(value)))
    out = pdf.output()
    if isinstance(out, (bytes, bytearray)):
        return bytes(out)
    return str(out).encode("latin-1")


def build_transactions_pdf(
    transactions: list[dict],
    *,
    user_email: str = "",
    title: str = "Filtered transactions",
) -> bytes:
    pdf = _TnwTxPdf()
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()
    if user_email:
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 6, _pdf_safe(f"Account: {user_email}"), ln=True)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, _pdf_safe(title), ln=True)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, _pdf_safe(f"{len(transactions)} transaction(s)"), ln=True)
    pdf.ln(3)

    col_w = [14, 32, 14, 22, 62, 22, 18]
    headers = ["#", "Date", "Flow", "Type", "Description", "Total", "Status"]

    def _draw_header() -> None:
        _pdf_draw_table_row(pdf, col_w, headers, header=True)

    _draw_header()

    for tx in transactions:
        row = [
            str(tx.get("user_tx_number", "")),
            (tx.get("tx_date_display", "") or "")[:16],
            (tx.get("flow") or "").capitalize()[:10],
            (tx.get("category") or "")[:20],
            (tx.get("description") or ""),
            tx.get("total_display_short", "-"),
            (tx.get("tx_status") or "")[:12],
        ]
        row_h = _pdf_table_row_height(pdf, col_w, row, {_PDF_TABLE_DESC_COL})
        if pdf.get_y() + row_h > 270:
            pdf.add_page()
            _draw_header()
        _pdf_draw_table_row(pdf, col_w, row)

    out = pdf.output()
    if isinstance(out, (bytes, bytearray)):
        return bytes(out)
    return str(out).encode("latin-1")
