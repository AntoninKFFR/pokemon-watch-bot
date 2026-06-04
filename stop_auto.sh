#!/usr/bin/env bash

set -euo pipefail

CRON_MARKER="pokemon-watch-bot-auto"

current_crontab="$(crontab -l 2>/dev/null || true)"

if [ -z "$current_crontab" ]; then
    echo "Mode automatique deja inactif."
    exit 0
fi

cleaned_crontab="$(printf '%s\n' "$current_crontab" | sed '/pokemon-watch-bot-auto/d')"

if [ -n "$cleaned_crontab" ]; then
    printf '%s\n' "$cleaned_crontab" | crontab -
else
    crontab -r
fi

echo "Mode automatique desactive."
