#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
PYTHON_BIN="/usr/bin/python3"
if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
fi
CRON_MARKER="# pokemon-watch-bot-auto"
CRON_LINE="0 * * * * cd \"$PROJECT_DIR\" && $PYTHON_BIN main.py >> bot.log 2>&1 $CRON_MARKER"

current_crontab="$(crontab -l 2>/dev/null || true)"
cleaned_crontab="$(printf '%s\n' "$current_crontab" | sed '/pokemon-watch-bot-auto/d')"

{
    if [ -n "$cleaned_crontab" ]; then
        printf '%s\n' "$cleaned_crontab"
    fi
    printf '%s\n' "$CRON_LINE"
} | crontab -

echo "Mode automatique active."
echo "$CRON_LINE"
