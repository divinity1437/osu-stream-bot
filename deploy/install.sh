#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Installer script for osu-stream-bot
# Usage:
#   sudo ./deploy/install.sh controller
#   sudo ./deploy/install.sh worker
# ==============================================================================

TARGET_DIR="/opt/osu-stream-bot"
APP_USER="osu-stream"
APP_GROUP="osu-stream"
CONFIG_DIR="/etc/osu-stream"

if [[ $EUID -ne 0 ]]; then
   echo "Error: This script must be run as root (sudo)." >&2
   exit 1
fi

MODE="${1:-}"
if [[ "$MODE" != "controller" && "$MODE" != "worker" ]]; then
    echo "Usage: $0 [controller|worker]" >&2
    exit 1
fi

echo "===> Starting setup for mode: $MODE"

# 1. Create system user and directories
if ! id "$APP_USER" &>/dev/null; then
    echo "===> Creating service user '$APP_USER'..."
    useradd --system --shell /bin/bash --create-home --home-dir /home/"$APP_USER" "$APP_USER"
fi

mkdir -p "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"
chown root:"$APP_GROUP" "$CONFIG_DIR"

# 2. Setup project folder in /opt/osu-stream-bot
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$SCRIPT_DIR" != "$TARGET_DIR" ]]; then
    echo "===> Copying codebase to $TARGET_DIR..."
    mkdir -p "$TARGET_DIR"
    rsync -a --exclude='.git' --exclude='venv' --exclude='__pycache__' --exclude='tests' "$SCRIPT_DIR/" "$TARGET_DIR/"
fi

# 3. Setup Python virtual environment
echo "===> Setting up Python virtual environment..."
apt-get update
apt-get install -y python3 python3-venv python3-pip

python3 -m venv "$TARGET_DIR/venv"
"$TARGET_DIR/venv/bin/pip" install --upgrade pip
if [[ -f "$TARGET_DIR/requirements.txt" ]]; then
    "$TARGET_DIR/venv/bin/pip" install -r "$TARGET_DIR/requirements.txt"
fi

# ------------------------------------------------------------------------------
# CONTROLLER SETUP
# ------------------------------------------------------------------------------
if [[ "$MODE" == "controller" ]]; then
    echo "===> Installing Controller dependencies (ffmpeg, xvfb, obs-studio, nginx)..."
    apt-get install -y ffmpeg xvfb obs-studio nginx apache2-utils wireguard

    # Setup controller environment file template if missing
    if [[ ! -f "$CONFIG_DIR/controller.env" ]]; then
        cp "$TARGET_DIR/deploy/controller.env.example" "$CONFIG_DIR/controller.env"
        chmod 600 "$CONFIG_DIR/controller.env"
        chown "$APP_USER":"$APP_GROUP" "$CONFIG_DIR/controller.env"
        echo "===> Created $CONFIG_DIR/controller.env. Please edit it with your settings!"
    fi

    # Install systemd units
    echo "===> Installing Systemd units for Controller..."
    cp "$TARGET_DIR/deploy/systemd/xvfb.service" /etc/systemd/system/
    cp "$TARGET_DIR/deploy/systemd/obs.service" /etc/systemd/system/
    cp "$TARGET_DIR/deploy/systemd/osu-stream-controller.service" /etc/systemd/system/

    systemctl daemon-reload
    systemctl enable xvfb.service osu-stream-controller.service obs.service

    echo "===> Controller installation complete!"
    echo "Next steps:"
    echo "  1. Edit $CONFIG_DIR/controller.env"
    echo "  2. Configure WireGuard connection to Worker VPS"
    echo "  3. Configure OBS profile and Browser Source (http://127.0.0.1:8787/)"
    echo "  4. Start services: systemctl start xvfb osu-stream-controller obs"
fi

# ------------------------------------------------------------------------------
# RENDER WORKER SETUP
# ------------------------------------------------------------------------------
if [[ "$MODE" == "worker" ]]; then
    echo "===> Installing Worker dependencies (ffmpeg, mesa llvmpipe, unzip, curl)..."
    apt-get install -y ffmpeg libgl1-mesa-dri libgl1-mesa-glx mesa-utils unzip curl wireguard

    # Install danser-go 0.12.1
    echo "===> Installing danser-go (0.12.1)..."
    su - "$APP_USER" -c "bash $TARGET_DIR/install-danser.sh"

    # Setup worker environment file template if missing
    if [[ ! -f "$CONFIG_DIR/worker.env" ]]; then
        cp "$TARGET_DIR/deploy/worker.env.example" "$CONFIG_DIR/worker.env"
        chmod 600 "$CONFIG_DIR/worker.env"
        chown "$APP_USER":"$APP_GROUP" "$CONFIG_DIR/worker.env"
        echo "===> Created $CONFIG_DIR/worker.env. Please edit it with your settings!"
    fi

    # Install systemd unit
    echo "===> Installing Systemd units for Worker..."
    cp "$TARGET_DIR/deploy/systemd/osu-stream-worker.service" /etc/systemd/system/

    systemctl daemon-reload
    systemctl enable osu-stream-worker.service

    echo "===> Render Worker installation complete!"
    echo "Next steps:"
    echo "  1. Edit $CONFIG_DIR/worker.env"
    echo "  2. Configure WireGuard connection to Controller VPS"
    echo "  3. Start service: systemctl start osu-stream-worker"
fi

# Fix ownership
chown -R "$APP_USER":"$APP_GROUP" "$TARGET_DIR"
echo "===> Done."
