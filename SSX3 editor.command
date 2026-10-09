#!/bin/bash
# Dvojklik spustí editor mapy (macOS). Najprv stiahne najnovšiu verziu, ak sa dá.
cd "$(dirname "$0")" || exit 1
git pull --ff-only -q 2>/dev/null || true
exec python3 -m ssx3map editor
