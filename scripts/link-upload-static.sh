#!/usr/bin/env bash
# Symlink maint upload folders to the main site static tree (same files on disk).
set -euo pipefail

MAINT_STATIC="${TNW_MAINT_STATIC:-$(cd "$(dirname "$0")/../app/static" && pwd)}"
MAIN_STATIC="${TNW_MAIN_STATIC:-$(cd "$(dirname "$0")/../../staging/app/static" 2>/dev/null && pwd || true)}"

if [ -z "$MAIN_STATIC" ] || [ ! -d "$MAIN_STATIC" ]; then
  echo "ERROR: main static dir not found. Set TNW_MAIN_STATIC, e.g.:"
  echo "  TNW_MAIN_STATIC=/home/ubuntu/PythonRoot/staging/app/static $0"
  exit 1
fi

for name in meeting_group_images event_images user_images; do
  link="$MAINT_STATIC/$name"
  target="$MAIN_STATIC/$name"
  mkdir -p "$target"
  if [ -L "$link" ]; then
    echo "OK (already linked): $name -> $(readlink "$link")"
    continue
  fi
  if [ -d "$link" ] && [ ! -L "$link" ]; then
    echo "==> merging $name from maint into main before linking"
    cp -a "$link/." "$target/"
    rm -rf "$link"
  elif [ -e "$link" ]; then
    echo "WARN: skip $name — exists and is not a directory"
    continue
  fi
  ln -s "$target" "$link"
  echo "Linked $name -> $target"
done
