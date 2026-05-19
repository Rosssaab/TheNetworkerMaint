"""Idempotent schema patches applied at app startup when the DB is reachable.

Also save equivalent SQL under scripts/ddl/ for the server (see capture_schema_ddl.py).
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from .models import db

_log = logging.getLogger(__name__)


def _column_names(table: str) -> set[str]:
    insp = inspect(db.engine)
    return {c["name"].lower() for c in insp.get_columns(table)}


def ensure_meeting_ticket_types_vat_treatment() -> None:
    """Add event_ticket_types.vat_treatment if the model expects it but SQL is behind."""
    table = "event_ticket_types"
    col = "vat_treatment"
    try:
        cols = _column_names(table)
    except Exception as exc:
        _log.warning("Could not inspect %s: %s", table, exc)
        return

    if col in cols:
        return

    dialect = db.engine.dialect.name
    _log.warning("Adding missing column %s.%s (%s)", table, col, dialect)

    if dialect == "mssql":
        db.session.execute(
            text(
                """
                IF COL_LENGTH('event_ticket_types', 'vat_treatment') IS NULL
                BEGIN
                    ALTER TABLE event_ticket_types
                        ADD vat_treatment VARCHAR(16) NOT NULL
                            CONSTRAINT DF_event_ticket_types_vat_treatment DEFAULT 'none';
                END
                """
            )
        )
        db.session.execute(
            text(
                """
                UPDATE event_ticket_types
                SET vat_treatment = 'included'
                WHERE vat_rate_percent > 0 AND vat_treatment = 'none'
                """
            )
        )
    elif dialect in ("mysql", "mariadb"):
        db.session.execute(
            text(
                """
                ALTER TABLE event_ticket_types
                  ADD COLUMN IF NOT EXISTS vat_treatment VARCHAR(16) NOT NULL DEFAULT 'none'
                """
            )
        )
        db.session.execute(
            text(
                """
                UPDATE event_ticket_types
                SET vat_treatment = 'included'
                WHERE vat_rate_percent > 0
                  AND (vat_treatment IS NULL OR vat_treatment = '' OR vat_treatment = 'none')
                """
            )
        )
    else:
        _log.error("No vat_treatment patch for dialect %s", dialect)
        return

    db.session.commit()
    _log.info("Added %s.%s and backfilled rows", table, col)


def apply_startup_schema_patches() -> None:
    ensure_meeting_ticket_types_vat_treatment()
