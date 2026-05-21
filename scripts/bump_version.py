"""Bump APP_VERSION in config.py (supports optional suffix, e.g. 1.29.0-maint).

Usage:
  python scripts/bump_version.py patch   # 1.29.0-maint -> 1.29.1-maint
  python scripts/bump_version.py minor   # 1.29.0-maint -> 1.30.0-maint
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.py"
PATTERN = re.compile(
    r'(?P<indent>\s*)APP_VERSION\s*=\s*"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?P<suffix>[^"]*)"'
)


def read_version() -> tuple[int, int, int, str]:
    text = CONFIG.read_text(encoding="utf-8")
    m = PATTERN.search(text)
    if not m:
        raise SystemExit(f"APP_VERSION not found in {CONFIG}")
    return (
        int(m["major"]),
        int(m["minor"]),
        int(m["patch"]),
        m["suffix"],
    )


def write_version(major: int, minor: int, patch: int, suffix: str) -> str:
    text = CONFIG.read_text(encoding="utf-8")
    version = f"{major}.{minor}.{patch}{suffix}"

    def repl(match: re.Match[str]) -> str:
        return f'{match.group("indent")}APP_VERSION = "{version}"'

    new_text, n = PATTERN.subn(repl, text, count=1)
    if n != 1:
        raise SystemExit(f"Failed to update APP_VERSION in {CONFIG}")
    CONFIG.write_text(new_text, encoding="utf-8")
    return version


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("patch", "minor"):
        raise SystemExit(__doc__)

    major, minor, patch, suffix = read_version()
    kind = sys.argv[1]
    if kind == "patch":
        patch += 1
    else:
        minor += 1
        patch = 0

    version = write_version(major, minor, patch, suffix)
    print(version)


if __name__ == "__main__":
    main()
