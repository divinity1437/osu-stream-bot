#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Hardened Installer script for osu-stream-bot
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

# 1. Install base utilities needed for deployment
apt-get update
apt-get install -y rsync python3 python3-venv python3-pip

# 2. Create system group and user with /usr/sbin/nologin
if ! getent group "$APP_GROUP" >/dev/null; then
    groupadd --system "$APP_GROUP"
fi

if ! id "$APP_USER" >/dev/null 2>&1; then
    echo "===> Creating service user '$APP_USER'..."
    useradd \
        --system \
        --gid "$APP_GROUP" \
        --shell /usr/sbin/nologin \
        --create-home \
        --home-dir "/home/$APP_USER" \
        "$APP_USER"
fi

# Config directory must be root-only (mode 0700)
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
chown root:root "$CONFIG_DIR"

# 3. Setup project folder in /opt/osu-stream-bot (clean rsync excluding secrets and runtime state)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$SCRIPT_DIR" != "$TARGET_DIR" ]]; then
    echo "===> Syncing codebase to $TARGET_DIR (excluding runtime state and local secrets)..."
    mkdir -p "$TARGET_DIR"
    rsync -a \
        --delete \
        --delete-delay \
        --exclude='/.git/' \
        --exclude='/venv/' \
        --exclude='/__pycache__/' \
        --exclude='/tests/' \
        --exclude='/.env' \
        --exclude='/.env.*' \
        --exclude='/state.json' \
        --exclude='/*.db' \
        --exclude='/*.db-wal' \
        --exclude='/*.db-shm' \
        --exclude='/logs/***' \
        --exclude='/video_queue/***' \
        --exclude='/inbox/***' \
        --exclude='/work/***' \
        --exclude='/rejected/***' \
        --exclude='/archive/***' \
        --exclude='/submissions/***' \
        --exclude='/tools/danser/***' \
        "$SCRIPT_DIR/" "$TARGET_DIR/"
fi

# Production configuration is loaded only from /etc/osu-stream/*.env.
# Remove secrets left by older deployments.
find "$TARGET_DIR" -maxdepth 1 -type f -name '.env*' -delete

# 4. Setup Python virtual environment
echo "===> Setting up Python virtual environment..."
python3 -m venv "$TARGET_DIR/venv"
"$TARGET_DIR/venv/bin/pip" install --upgrade pip
if [[ -f "$TARGET_DIR/requirements.txt" ]]; then
    "$TARGET_DIR/venv/bin/pip" install -r "$TARGET_DIR/requirements.txt"
fi

# ------------------------------------------------------------------------------
# CONTROLLER SETUP
# ------------------------------------------------------------------------------
if [[ "$MODE" == "controller" ]]; then
    echo "===> Installing Controller dependencies..."
    apt-get install -y \
        ffmpeg \
        xvfb \
        xauth \
        x11-utils \
        obs-studio \
        nginx \
        apache2-utils \
        wireguard \
        curl \
        libgl1-mesa-dri \
        mesa-utils

    # Create OBS configuration and cache directories
    install -d -o "$APP_USER" -g "$APP_GROUP" -m 0700 \
        "/home/$APP_USER/.config/obs-studio" \
        "/home/$APP_USER/.cache/obs-studio" \
        "/home/$APP_USER/.local/share/obs-studio"

    # Setup controller environment file template (owner root:root, mode 0600)
    if [[ ! -f "$CONFIG_DIR/controller.env" ]]; then
        install -o root -g root -m 0600 \
            "$TARGET_DIR/deploy/controller.env.example" \
            "$CONFIG_DIR/controller.env"
        echo "===> Created $CONFIG_DIR/controller.env. Please edit it and set RENDER_API_TOKEN!"
    else
        chown root:root "$CONFIG_DIR/controller.env"
        chmod 600 "$CONFIG_DIR/controller.env"
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
    echo "  1. Generate token (openssl rand -hex 32) and configure $CONFIG_DIR/controller.env"
    echo "  2. Configure WireGuard connection to Worker VPS"
    echo "  3. Configure OBS profile and Browser Source (http://127.0.0.1:8787/)"
    echo "  4. Setup Nginx with SSL (Certbot) and htpasswd using deploy/nginx.conf"
    echo "  5. Start services: systemctl start xvfb osu-stream-controller obs"
fi

# ------------------------------------------------------------------------------
# RENDER WORKER SETUP
# ------------------------------------------------------------------------------
if [[ "$MODE" == "worker" ]]; then
    echo "===> Installing Worker dependencies..."
    apt-get install -y ffmpeg libgl1-mesa-dri libgl1-mesa-glx mesa-utils unzip curl wireguard

    # Ensure tools directory exists before running danser install script
    mkdir -p "$TARGET_DIR/tools"
    chown -R "$APP_USER":"$APP_GROUP" "$TARGET_DIR"

    # Install danser-go 0.12.1 as service user
    echo "===> Installing danser-go (0.12.1)..."
    runuser -u "$APP_USER" -- bash "$TARGET_DIR/install-danser.sh"

    # Ensure tools/danser/songs directory exists and has correct permissions
    mkdir -p "$TARGET_DIR/tools/danser/songs"
    chown -R "$APP_USER":"$APP_GROUP" "$TARGET_DIR/tools/danser/songs"

    # Setup worker environment file template (owner root:root, mode 0600)
    if [[ ! -f "$CONFIG_DIR/worker.env" ]]; then
        install -o root -g root -m 0600 \
            "$TARGET_DIR/deploy/worker.env.example" \
            "$CONFIG_DIR/worker.env"
        echo "===> Created $CONFIG_DIR/worker.env. Please edit it and set RENDER_API_TOKEN!"
    else
        chown root:root "$CONFIG_DIR/worker.env"
        chmod 600 "$CONFIG_DIR/worker.env"
    fi

    # Install systemd unit
    echo "===> Installing Systemd units for Worker..."
    cp "$TARGET_DIR/deploy/systemd/osu-stream-worker.service" /etc/systemd/system/

    systemctl daemon-reload
    systemctl enable osu-stream-worker.service

    echo "===> Render Worker installation complete!"
    echo "Next steps:"
    echo "  1. Copy the RENDER_API_TOKEN from Controller into $CONFIG_DIR/worker.env"
    echo "  2. IMPORTANT: Synchronize beatmaps into $TARGET_DIR/tools/danser/songs/"
    echo "  3. Configure WireGuard connection to Controller VPS"
    echo "  4. Start service: systemctl start osu-stream-worker"
fi

# Fix ownership for runtime paths
chown -R "$APP_USER":"$APP_GROUP" "$TARGET_DIR"
echo "===> Done."
