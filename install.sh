#!/usr/bin/env bash
# SongSnag installer for Linux and macOS.
#
#   ./install.sh               install from this folder and start SongSnag
#   ./install.sh --no-launch   install without starting it
#   curl -fsSL https://raw.githubusercontent.com/BlindingBright/SongSnag/main/install.sh | bash
#
# Everything goes into your home folder; no root needed (except, optionally, to
# install ffmpeg with your package manager).
set -euo pipefail

REPO="${SONGSNAG_REPO:-BlindingBright/SongSnag}"
LAUNCH=1
for arg in "$@"; do
    case "$arg" in
        --no-launch) LAUNCH=0 ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

bold=$(tput bold 2>/dev/null || true); reset=$(tput sgr0 2>/dev/null || true)
say()  { printf '%s==>%s %s\n' "$bold" "$reset" "$*"; }
warn() { printf '%s!!%s  %s\n' "$bold" "$reset" "$*" >&2; }
die()  { warn "$*"; exit 1; }

OS=$(uname -s)
if [ "$OS" = Darwin ]; then
    APP_HOME="$HOME/Library/Application Support/SongSnag"
else
    APP_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/songsnag"
fi
VENV="$APP_HOME/venv"
BIN_DIR="$HOME/.local/bin"

# --- source: this checkout, or the GitHub repo when piped from curl ---------
SRC=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/pyproject.toml" ]; then
    SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
PKG="${SRC:-git+https://github.com/$REPO.git}"

# --- Python 3.10+ with venv --------------------------------------------------
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3.14 python3; do
    if command -v "$cand" >/dev/null 2>&1 &&
       "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        PY=$(command -v "$cand"); break
    fi
done
[ -n "$PY" ] || die "Python 3.10 or newer is required. Install it with your package manager and run this again."
"$PY" -c 'import venv, ensurepip' 2>/dev/null ||
    die "Python's venv module is missing. On Debian/Ubuntu: sudo apt install python3-venv"
say "Using $("$PY" --version) at $PY"

# Reuse the distro's Qt bindings when present: saves ~250 MB of download.
SYSTEM_QT=0
if "$PY" -c 'import PySide6.QtWidgets, PySide6.QtNetwork, PySide6.QtSvg' 2>/dev/null; then
    SYSTEM_QT=1
    say "Found system PySide6; reusing it"
fi

# --- virtual environment -------------------------------------------------------
mkdir -p "$APP_HOME"
if [ -x "$VENV/bin/python" ] && ! "$VENV/bin/python" -c 'import sys' 2>/dev/null; then
    rm -rf "$VENV"  # broken by a Python upgrade
fi
if [ ! -x "$VENV/bin/python" ]; then
    say "Creating environment in $VENV"
    if [ "$SYSTEM_QT" = 1 ]; then "$PY" -m venv --system-site-packages "$VENV"; else "$PY" -m venv "$VENV"; fi
fi
PIP=("$VENV/bin/python" -m pip --disable-pip-version-check -q)
"${PIP[@]}" install --upgrade pip

say "Installing SongSnag (this can take a minute)"
if [ "$SYSTEM_QT" = 1 ]; then
    "${PIP[@]}" install --upgrade "yt-dlp[default]"
    "${PIP[@]}" install --upgrade --force-reinstall --no-deps "$PKG"
else
    "${PIP[@]}" install --upgrade "$PKG"
fi

# --- JavaScript runtime (YouTube needs one) ------------------------------------
if command -v deno >/dev/null || command -v node >/dev/null || command -v bun >/dev/null ||
   [ -x "$HOME/.deno/bin/deno" ] || [ -x "$VENV/bin/deno" ]; then
    say "JavaScript runtime found"
else
    say "Installing Deno (YouTube needs a JavaScript runtime)"
    "${PIP[@]}" install --upgrade deno || warn "Couldn't install Deno. Install Deno or Node.js yourself."
fi

# --- ffmpeg ----------------------------------------------------------------------
if ! command -v ffmpeg >/dev/null; then
    cmd=""
    if [ "$OS" = Darwin ]; then command -v brew >/dev/null && cmd="brew install ffmpeg"
    elif command -v pacman >/dev/null; then cmd="sudo pacman -S --needed ffmpeg"
    elif command -v apt-get >/dev/null; then cmd="sudo apt-get install -y ffmpeg"
    elif command -v dnf >/dev/null; then cmd="sudo dnf install -y ffmpeg"
    elif command -v zypper >/dev/null; then cmd="sudo zypper install -y ffmpeg"
    fi
    warn "ffmpeg is not installed. SongSnag needs it to tag songs and add cover art."
    if [ -n "$cmd" ] && [ -t 0 ]; then
        read -r -p "Install it now with '$cmd'? [Y/n] " reply
        case "${reply:-y}" in [Yy]*) $cmd || warn "ffmpeg install failed; install it yourself later." ;; esac
    else
        warn "Install ffmpeg with your package manager${cmd:+ ($cmd)}."
    fi
fi

# --- launcher, menu entry, icon --------------------------------------------------
mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/songsnag" "$BIN_DIR/songsnag"
ASSETS=$("$VENV/bin/python" -c 'import songsnag, pathlib; print(pathlib.Path(songsnag.__file__).parent / "assets")')

if [ "$OS" = Darwin ]; then
    APP="$HOME/Applications/SongSnag.app"
    mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
    cat > "$APP/Contents/MacOS/SongSnag" <<EOF
#!/bin/sh
exec "$VENV/bin/songsnag" "\$@"
EOF
    chmod +x "$APP/Contents/MacOS/SongSnag"
    cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>SongSnag</string>
  <key>CFBundleIdentifier</key><string>io.github.songsnag</string>
  <key>CFBundleExecutable</key><string>SongSnag</string>
  <key>CFBundleIconFile</key><string>SongSnag</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
</dict></plist>
EOF
    if command -v sips >/dev/null && command -v iconutil >/dev/null; then
        set_dir=$(mktemp -d)/SongSnag.iconset; mkdir -p "$set_dir"
        for s in 16 32 128 256; do
            sips -z $s $s "$ASSETS/songsnag.png" --out "$set_dir/icon_${s}x${s}.png" >/dev/null
        done
        iconutil -c icns "$set_dir" -o "$APP/Contents/Resources/SongSnag.icns" 2>/dev/null || true
    fi
    say "Added SongSnag to ~/Applications"
else
    DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
    mkdir -p "$DATA/applications" "$DATA/icons/hicolor/scalable/apps" "$DATA/icons/hicolor/256x256/apps"
    cp "$ASSETS/songsnag.svg" "$DATA/icons/hicolor/scalable/apps/songsnag.svg"
    cp "$ASSETS/songsnag.png" "$DATA/icons/hicolor/256x256/apps/songsnag.png"
    cat > "$DATA/applications/songsnag.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=SongSnag
GenericName=Music Saver
Comment=Save the music you play on YouTube in your browser
Exec="$VENV/bin/songsnag"
Icon=songsnag
Categories=AudioVideo;Audio;Network;
Keywords=youtube;music;download;mp3;brave;chrome;edge;
StartupNotify=false
EOF
    command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" 2>/dev/null || true
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q "$DATA/icons/hicolor" 2>/dev/null || true
    say "Added SongSnag to your application menu"
fi

case ":$PATH:" in *":$BIN_DIR:"*) ;; *) warn "$BIN_DIR is not on your PATH; start SongSnag from the app menu instead." ;; esac

VERSION=$("$VENV/bin/python" -c 'import songsnag; print(songsnag.__version__)')
say "SongSnag $VERSION installed. Uninstall any time with: $APP_HOME/uninstall.sh"
if [ -n "$SRC" ]; then
    cp "$SRC/uninstall.sh" "$APP_HOME/uninstall.sh"
else
    curl -fsSL "https://raw.githubusercontent.com/$REPO/main/uninstall.sh" -o "$APP_HOME/uninstall.sh" || true
fi
chmod +x "$APP_HOME/uninstall.sh" 2>/dev/null || true

if [ "$LAUNCH" = 1 ]; then
    say "Starting SongSnag. Look for its icon in your system tray."
    nohup "$VENV/bin/songsnag" >/dev/null 2>&1 &
fi
