#!/usr/bin/env bash
# ==============================================================================
# Setup and install systemd services & stream CLI for osu-stream-bot
# Supports auto-detecting current repo path and current user, or /opt/osu-stream-bot
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CURRENT_USER="${SUDO_USER:-$(id -un)}"
CURRENT_GROUP="$(id -gn "$CURRENT_USER")"

if [[ $EUID -ne 0 ]]; then
    echo "Error: This script must be run with sudo." >&2
    echo "Usage: sudo ./deploy/install-services.sh [broadcaster|controller|worker|all]" >&2
    exit 1
fi

ROLE="${1:-}"

if [[ -z "$ROLE" ]]; then
    echo "=================================================="
    echo " osu-stream-bot systemd & CLI installer"
    echo "=================================================="
    echo "Target directory: $SCRIPT_DIR"
    echo "Target user:      $CURRENT_USER:$CURRENT_GROUP"
    echo ""
    echo "Available roles to install:"
    echo "  1) broadcaster - Headless RTMP broadcaster (stream-broadcaster.service)"
    echo "  2) controller  - Main stream controller (app.py) & broadcaster"
    echo "  3) worker      - Distributed danser render worker (render_worker.py)"
    echo "  4) all         - All services (controller, broadcaster, worker)"
    echo ""
    read -rp "Select role [1-4 or name] (default: broadcaster): " CHOICE
    CHOICE="${CHOICE:-1}"
    case "$CHOICE" in
        1|broadcaster) ROLE="broadcaster" ;;
        2|controller)  ROLE="controller" ;;
        3|worker)      ROLE="worker" ;;
        4|all)         ROLE="all" ;;
        *)             echo "Invalid option. Exiting." >&2; exit 1 ;;
    esac
fi

# Ensure whiptail and locales for TUI
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq || true
    apt-get install -y -qq whiptail locales >/dev/null 2>&1 || true
    locale-gen en_US.UTF-8 ru_RU.UTF-8 >/dev/null 2>&1 || true
elif command -v pacman >/dev/null 2>&1; then
    pacman -S --needed --noconfirm libnewt >/dev/null 2>&1 || true
fi

# 1. Install CLI alias for stream manager
if [[ -f "$SCRIPT_DIR/stream-manager.sh" ]]; then
    chmod +x "$SCRIPT_DIR/stream-manager.sh"
    ln -sf "$SCRIPT_DIR/stream-manager.sh" /usr/local/bin/stream
    ln -sf "$SCRIPT_DIR/stream-manager.sh" /usr/local/bin/stream-manager
    echo "[OK] Installed CLI symlinks: 'stream' and 'stream-manager' -> $SCRIPT_DIR/stream-manager.sh"
fi

# Function to generate broadcaster service unit
install_broadcaster_service() {
    cat << EOF > /etc/systemd/system/stream-broadcaster.service
[Unit]
Description=osu! replay stream Headless RTMP Broadcaster (Twitch & YouTube)
After=network.target network-online.target

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_GROUP
WorkingDirectory=$SCRIPT_DIR

Environment="HOME=/home/$CURRENT_USER"
Environment="LANG=en_US.UTF-8"
Environment="LC_ALL=en_US.UTF-8"

ExecStart=$SCRIPT_DIR/venv/bin/python $SCRIPT_DIR/stream_broadcaster.py

Restart=always
RestartSec=5s

LimitNOFILE=65536
TimeoutStopSec=15s

[Install]
WantedBy=multi-user.target
EOF
    echo "[OK] Installed /etc/systemd/system/stream-broadcaster.service"
}

# Function to generate controller service unit
install_controller_service() {
    cat << EOF > /etc/systemd/system/osu-stream-controller.service
[Unit]
Description=osu! replay stream Controller (Producer & Web queue)
After=network.target network-online.target

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_GROUP
WorkingDirectory=$SCRIPT_DIR

Environment="HOME=/home/$CURRENT_USER"
Environment="LANG=en_US.UTF-8"
Environment="LC_ALL=en_US.UTF-8"

ExecStart=$SCRIPT_DIR/venv/bin/python $SCRIPT_DIR/app.py

Restart=always
RestartSec=5s

LimitNOFILE=65536
TimeoutStopSec=15s

[Install]
WantedBy=multi-user.target
EOF
    echo "[OK] Installed /etc/systemd/system/osu-stream-controller.service"
}

# Function to generate worker service unit
install_worker_service() {
    local exec_cmd
    if command -v xvfb-run >/dev/null 2>&1; then
        exec_cmd="/usr/bin/xvfb-run -a $SCRIPT_DIR/venv/bin/python render_worker.py"
    else
        exec_cmd="$SCRIPT_DIR/venv/bin/python render_worker.py"
    fi

    cat << EOF > /etc/systemd/system/osu-render-worker.service
[Unit]
Description=osu-stream-bot Render Worker
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_GROUP
WorkingDirectory=$SCRIPT_DIR

Environment="HOME=/home/$CURRENT_USER"
Environment="LANG=en_US.UTF-8"
Environment="LC_ALL=en_US.UTF-8"

ExecStart=$exec_cmd

Restart=always
RestartSec=5s

LimitNOFILE=65536
TimeoutStopSec=15s

[Install]
WantedBy=multi-user.target
EOF
    echo "[OK] Installed /etc/systemd/system/osu-render-worker.service"
}

case "$ROLE" in
    broadcaster)
        install_broadcaster_service
        systemctl daemon-reload
        systemctl enable stream-broadcaster.service
        echo "[INFO] Broadcaster service enabled. To start: sudo systemctl start stream-broadcaster.service"
        ;;
    controller)
        install_controller_service
        install_broadcaster_service
        systemctl daemon-reload
        systemctl enable osu-stream-controller.service stream-broadcaster.service
        echo "[INFO] Controller & Broadcaster services enabled."
        ;;
    worker)
        install_worker_service
        systemctl daemon-reload
        systemctl enable osu-render-worker.service
        echo "[INFO] Worker service enabled. To start: sudo systemctl start osu-render-worker.service"
        ;;
    all)
        install_controller_service
        install_broadcaster_service
        install_worker_service
        systemctl daemon-reload
        echo "[INFO] All services installed and daemon reloaded."
        ;;
esac

echo "=================================================="
echo " Installation completed successfully!"
echo " Use 'stream' command to open the TUI manager."
echo "=================================================="
