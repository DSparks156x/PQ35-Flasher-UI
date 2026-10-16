#!/bin/sh
# PQ35 Flasher UI installer for Linux and macOS.
#
#   curl -LsSf https://raw.githubusercontent.com/DSparks156x/PQ35-Flasher-UI/main/install.sh | sh
#
# or ./install.sh from a checkout. Re-run it any time to update. No root needed.
# Set PQ35_YES=1 to accept every question (shortcuts) without asking.
# The app's "Update" button runs this with PQ35_UPDATE=1 and PQ35_WAIT_PID set:
# it waits for the app to close, updates without asking anything, and reopens it.
set -eu
UPDATING="${PQ35_UPDATE:-}"

REPO="DSparks156x/PQ35-Flasher-UI"
OS=$(uname)
if [ "$OS" = "Darwin" ]; then
    INSTALL_DIR="$HOME/Library/Application Support/PQ35-Flasher-UI"
else
    INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/pq35-flasher-ui"
fi

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }

# Yes/no question that still works when this script is piped into sh.
ask() {
    [ -n "${PQ35_YES:-}" ] && return 0
    if ! { : < /dev/tty; } 2>/dev/null; then
        return 0
    fi
    printf '%s [Y/n] ' "$1" > /dev/tty
    read -r answer < /dev/tty || answer=""
    case "$answer" in [Nn]*) return 1 ;; *) return 0 ;; esac
}

if [ -n "${PQ35_WAIT_PID:-}" ]; then
    step "Waiting for PQ35 Flasher to close"
    i=0
    while kill -0 "$PQ35_WAIT_PID" 2>/dev/null && [ $i -lt 60 ]; do sleep 1; i=$((i + 1)); done
fi

here=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || true)
if [ -n "$here" ] && [ -f "$here/pyproject.toml" ]; then
    APP_DIR="$here"
    step "Using this checkout: $APP_DIR"
    if [ -d "$APP_DIR/.git" ]; then
        [ -n "$UPDATING" ] && git -C "$APP_DIR" pull --ff-only
        git -C "$APP_DIR" submodule update --init --recursive
    fi
else
    if ! command -v git >/dev/null; then
        if [ "$OS" = "Darwin" ]; then
            echo "Please install git first: run 'xcode-select --install', then try again."
        else
            echo "Please install git first (e.g. sudo apt install git)."
        fi
        exit 1
    fi
    APP_DIR="$INSTALL_DIR/app"
    if [ -d "$APP_DIR/.git" ]; then
        step "Updating $APP_DIR"
        git -C "$APP_DIR" pull --ff-only
        git -C "$APP_DIR" submodule update --init --recursive
    else
        step "Downloading into $APP_DIR"
        mkdir -p "$INSTALL_DIR"
        git clone --recurse-submodules "https://github.com/$REPO.git" "$APP_DIR"
    fi
fi
[ -f "$APP_DIR/flasher/runner.py" ] || { echo "The flasher submodule is missing."; exit 1; }

if ! command -v uv >/dev/null; then
    step "Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"
fi

step "Installing the app (this takes a minute the first time)"
uv sync --project "$APP_DIR" --frozen 2>/dev/null || uv sync --project "$APP_DIR"

PYTHON="$APP_DIR/.venv/bin/python"
cat > "$APP_DIR/pq35-flasher" <<EOF
#!/bin/sh
cd "$APP_DIR" && exec "$PYTHON" -m pq35ui "\$@"
EOF
chmod +x "$APP_DIR/pq35-flasher"

if [ -n "$UPDATING" ]; then
    step "Updated. Reopening PQ35 Flasher"
    nohup "$APP_DIR/pq35-flasher" >/dev/null 2>&1 &
    exit 0
fi

if [ "$OS" = "Darwin" ]; then
    if ask "Add PQ35 Flasher to your Applications folder?"; then
        bundle="$HOME/Applications/PQ35 Flasher.app"
        mkdir -p "$bundle/Contents/MacOS"
        cat > "$bundle/Contents/MacOS/PQ35 Flasher" <<EOF
#!/bin/sh
cd "$APP_DIR" && exec "$PYTHON" -m pq35ui
EOF
        chmod +x "$bundle/Contents/MacOS/PQ35 Flasher"
        cat > "$bundle/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>PQ35 Flasher</string>
  <key>CFBundleExecutable</key><string>PQ35 Flasher</string>
  <key>CFBundleIdentifier</key><string>com.dsparks156x.pq35flasher</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
EOF
        echo "Added $bundle"
    fi
else
    desktop_entry() {
        cat <<EOF
[Desktop Entry]
Type=Application
Name=PQ35 Flasher
Comment=Flash and back up PQ35 Haldex and EPS modules
Exec="$APP_DIR/pq35-flasher"
Path=$APP_DIR
Terminal=false
Categories=Utility;Development;
EOF
    }
    if ask "Add PQ35 Flasher to your applications menu?"; then
        apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
        mkdir -p "$apps"
        desktop_entry > "$apps/pq35-flasher.desktop"
    fi
    desktop=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")
    if [ -d "$desktop" ] && ask "Add a desktop shortcut?"; then
        desktop_entry > "$desktop/pq35-flasher.desktop"
        chmod +x "$desktop/pq35-flasher.desktop"
        command -v gio >/dev/null && gio set "$desktop/pq35-flasher.desktop" metadata::trusted true 2>/dev/null || true
    fi
fi

echo
printf '\033[32mPQ35 Flasher is installed.\033[0m You can always start it with: "%s/pq35-flasher"\n' "$APP_DIR"
if [ "$OS" = "Darwin" ]; then
    echo "On macOS only CANable/candleLight (gs_usb) adapters work, and that's untested."
else
    echo "SocketCAN adapters (including CANable/candleLight, which show up as can0) can be"
    echo "brought up at 500 kbit/s from the app's Connect page; it asks for your password."
fi
