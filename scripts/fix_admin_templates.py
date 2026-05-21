"""One-off: point admin panel partials at separate page routes."""
from __future__ import annotations

import re
from pathlib import Path

ADMIN = Path(__file__).resolve().parents[1] / "app" / "templates" / "admin"


def patch(text: str, path: Path) -> str:
    text = text.replace(
        "url_for('main.admin_events', panel='overview')",
        "url_for('main.admin_events')",
    )
    text = text.replace(
        "url_for('main.admin_events', panel='keywords', **admin_ignore_test_users_nav)",
        "url_for('main.admin_keywords', **admin_ignore_test_users_nav)",
    )
    text = text.replace(
        "url_for('main.admin_events', panel='users')",
        "url_for('main.admin_users')",
    )
    text = text.replace(
        "url_for('main.admin_events', panel='move_events', **admin_ignore_test_users_nav)",
        "url_for('main.admin_move_events', **admin_ignore_test_users_nav)",
    )
    text = text.replace(
        "url_for('main.admin_events', panel='delete_group_events', del_group_id=",
        "url_for('main.admin_delete_group_events', del_group_id=",
    )
    text = text.replace(
        "url_for('main.admin_events', panel='delete_group_events')",
        "url_for('main.admin_delete_group_events')",
    )
    text = re.sub(
        r'\s*<input type="hidden" name="panel" value="[^"]+">\s*\n',
        "\n",
        text,
    )
    text = text.replace(
        'id="adminUsersFilterForm" method="get" action="{{ url_for(\'main.admin_events\') }}"',
        'id="adminUsersFilterForm" method="get" action="{{ url_for(\'main.admin_users\') }}"',
    )
    text = text.replace(
        'id="adminMoveEventsFilterForm" class="mb-0" method="get" action="{{ url_for(\'main.admin_events\') }}"',
        'id="adminMoveEventsFilterForm" class="mb-0" method="get" action="{{ url_for(\'main.admin_move_events\') }}"',
    )
    text = re.sub(
        r"url_for\('main\.admin_events', users_page=",
        "url_for('main.admin_users', users_page=",
        text,
    )
    text = re.sub(
        r"url_for\('main\.admin_events', mv_page=",
        "url_for('main.admin_move_events', mv_page=",
        text,
    )
    if path.name == "move_events.html" and "panels" in path.parts:
        text = text.replace("{% if active_panel == 'move_events' %}\n", "")
        text = re.sub(r"\n\s*{% endif %}\s*$", "\n", text.rstrip()) + "\n"
    return text


def main() -> None:
    for path in ADMIN.rglob("*.html"):
        original = path.read_text(encoding="utf-8")
        updated = patch(original, path)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            print("updated", path.relative_to(ADMIN))


if __name__ == "__main__":
    main()
