#!/usr/bin/env bash
# Removes SongSnag. Your music is never touched.
#   ./uninstall.sh          remove the app, keep settings and the catalog
#   ./uninstall.sh --purge  also remove settings, catalog and logs
set -euo pipefail

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

if [ "$(uname -s)" = Darwin ]; then
    APP_HOME="$HOME/Library/Application Support/SongSnag"
else
    APP_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/songsnag"
fi

# Stop a running copy first, so it can't rewrite files we are about to delete.
# Anything running on SongSnag's private Python is SongSnag.
if command -v pgrep >/dev/null; then
    pids=$(pgrep -f "^$APP_HOME/venv/bin/python" || true)
    if [ -n "$pids" ]; then
        kill $pids 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            pgrep -f "^$APP_HOME/venv/bin/python" >/dev/null || break
            sleep 0.5
        done
        kill -9 $pids 2>/dev/null || true
    fi
fi

if [ "$(uname -s)" = Darwin ]; then
    APP_HOME="$HOME/Library/Application Support/SongSnag"
    CONFIG="$APP_HOME"
    rm -rf "$HOME/Applications/SongSnag.app"
    rm -f "$HOME/Library/LaunchAgents/io.github.songsnag.plist"
else
    DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
    APP_HOME="$DATA/songsnag"
    CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/songsnag"
    rm -f "$DATA/applications/songsnag.desktop" \
          "$DATA/icons/hicolor/scalable/apps/songsnag.svg" \
          "$DATA/icons/hicolor/256x256/apps/songsnag.png" \
          "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/songsnag.desktop"
fi

[ -L "$HOME/.local/bin/songsnag" ] && rm -f "$HOME/.local/bin/songsnag"
rm -rf "$APP_HOME/venv" "$APP_HOME/ytdlp"
rm -f "$APP_HOME/uninstall.sh"

if [ "$PURGE" = 1 ]; then
    rm -rf "$APP_HOME" "$CONFIG"
    echo "SongSnag removed, including settings and catalog. Your music folder was left alone."
else
    rmdir "$APP_HOME" 2>/dev/null || true
    echo "SongSnag removed. Settings and catalog were kept in $CONFIG and $APP_HOME (use --purge to delete)."
fi
