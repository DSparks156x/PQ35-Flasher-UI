#!/bin/sh
# PQ35 Flasher UI installer for Linux (and macOS, untested).
#
#   curl -LsSf https://raw.githubusercontent.com/DSparks156x/PQ35-Flasher-UI/main/install.sh | sh
#
# or ./install.sh from a checkout. Re-run it any time to update. No root needed.
set -eu

REPO="DSparks156x/PQ35-Flasher-UI"
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/pq35-flasher-ui"

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }

here=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || true)
if [ -n "$here" ] && [ -f "$here/pyproject.toml" ]; then
    APP_DIR="$here"
    step "Using this checkout: $APP_DIR"
    [ -d "$APP_DIR/.git" ] && git -C "$APP_DIR" submodule update --init --recursive
else
    command -v git >/dev/null || { echo "Please install git first (e.g. sudo apt install git)."; exit 1; }
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

step "Creating launcher"
if [ "$(uname)" = "Linux" ]; then
    apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    mkdir -p "$apps"
    cat > "$apps/pq35-flasher.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=PQ35 Flasher
Comment=Flash and back up PQ35 Haldex and EPS modules
Exec="$APP_DIR/.venv/bin/python" -m pq35ui
Path=$APP_DIR
Terminal=false
Categories=Utility;Development;
EOF
fi
cat > "$APP_DIR/pq35-flasher" <<EOF
#!/bin/sh
cd "$APP_DIR" && exec "$APP_DIR/.venv/bin/python" -m pq35ui "\$@"
EOF
chmod +x "$APP_DIR/pq35-flasher"

echo
printf '\033[32mPQ35 Flasher is installed.\033[0m Start it from your app menu, or run %s/pq35-flasher\n' "$APP_DIR"
if [ "$(uname)" = "Linux" ]; then
    echo "SocketCAN adapters must be up before use, for example:"
    echo "  sudo ip link set can0 up type can bitrate 500000"
    echo "(CANable/candleLight show up as can0 on Linux: pick SocketCAN, not gs_usb.)"
fi
