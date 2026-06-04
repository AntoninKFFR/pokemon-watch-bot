#!/usr/bin/env bash

set -euo pipefail

CRON_MARKER="pokemon-watch-bot-auto"

current_crontab="$(crontab -l 2>/dev/null || true)"

if printf '%s\n' "$current_crontab" | grep -q "$CRON_MARKER"; then
    echo "Mode automatique actif."
    printf '%s\n' "$current_crontab" | grep "$CRON_MARKER"
else
    echo "Mode automatique inactif."
fi
