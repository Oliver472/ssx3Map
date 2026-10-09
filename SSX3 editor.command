#!/bin/bash
# Double-click to start the map editor (macOS). It first pulls the latest version when it can.
cd "$(dirname "$0")" || exit 1
git pull --ff-only -q 2>/dev/null || true
exec python3 -m ssx3map editor
