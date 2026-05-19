"""Table comments for UI copy: SQL Server MS_Description or MariaDB/MySQL TABLE_COMMENT."""

from __future__ import annotations

import re

from sqlalchemy import text

from .models import db

_LABELS_LOADED = False
_TABLE_MS_DESCRIPTIONS: dict[str, str] = {}

# When the DB has no comment metadata (or is unreachable), use these sensible defaults.
DEFAULT_TABLE_LABELS: dict[str, str] = {
    "events": "Events",
    "event_groups": "Event groups",
    "users": "Users",
    "countries": "Countries",
    "industries": "Topics",
    "tags": "Tags",
    "user_attendee_tags": "Attendee tags",
}


def _humanize_table(table_name: str) -> str:
    return table_name.replace("_", " ").replace("-", " ").strip().title()


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html_from_label(value: str) -> str:
    """TABLE_COMMENT must be plain text; strip accidental markup so UI never shows raw tags."""
    return _HTML_TAG_RE.sub("", value).strip()


def _load_all_table_ms_descriptions() -> None:
    global _LABELS_LOADED, _TABLE_MS_DESCRIPTIONS
    if _LABELS_LOADED:
        return
    _TABLE_MS_DESCRIPTIONS = {}
    try:
        bind = db.session.get_bind()
        dialect = (bind.dialect.name or "").lower()
        if dialect == "mssql":
            stmt = text(
                """
                SELECT t.name AS tbl, CONVERT(NVARCHAR(4000), ep.value) AS v
                FROM sys.tables AS t
                INNER JOIN sys.extended_properties AS ep
                  ON ep.major_id = t.object_id
                 AND ep.class = 1
                 AND ep.minor_id = 0
                 AND ep.name = N'MS_Description'
                WHERE SCHEMA_NAME(t.schema_id) = N'dbo'
                  AND t.is_ms_shipped = 0
                """
            )
        elif dialect in ("mysql", "mariadb"):
            stmt = text(
                """
                SELECT TABLE_NAME AS tbl,
                       NULLIF(TRIM(TABLE_COMMENT), '') AS v
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_TYPE = 'BASE TABLE'
                """
            )
        else:
            stmt = None
        if stmt is not None:
            for row in db.session.execute(stmt):
                tbl = str(row[0]).strip() if row[0] is not None else ""
                if not tbl or row[1] is None:
                    continue
                val = _strip_html_from_label(str(row[1]).strip())
                if val:
                    _TABLE_MS_DESCRIPTIONS[tbl] = val
    except Exception:
        pass
    _LABELS_LOADED = True


def table_label(table_name: str, default: str | None = None) -> str:
    """Label for *table_name* from DB table comment metadata, else *default*, else DEFAULT_TABLE_LABELS, else title-cased name."""
    _load_all_table_ms_descriptions()
    if table_name in _TABLE_MS_DESCRIPTIONS:
        return _strip_html_from_label(_TABLE_MS_DESCRIPTIONS[table_name])
    if default is not None:
        return _strip_html_from_label(default)
    if table_name in DEFAULT_TABLE_LABELS:
        return DEFAULT_TABLE_LABELS[table_name]
    return _humanize_table(table_name)


def get_dbo_table_ms_description(table_name: str, default: str) -> str:
    """Same as :func:`table_label` with an explicit *default* (legacy name for SQL Server-era callers)."""
    return table_label(table_name, default)


def clear_schema_label_cache() -> None:
    global _LABELS_LOADED, _TABLE_MS_DESCRIPTIONS
    _LABELS_LOADED = False
    _TABLE_MS_DESCRIPTIONS = {}
