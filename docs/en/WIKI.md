# osu-stream-bot Wiki & Complete Reference (EN)

Complete technical documentation and reference guide for the `osu-stream-bot` system.

---

## Table of Contents

1. [Architecture Overview & Components](#1-architecture-overview--components)
2. [Operating Modes (RUN_MODE)](#2-operating-modes-run_mode)
3. [Two-VPS Deployment (Controller + Render Worker)](#3-two-vps-deployment)
4. [Security & Hardening](#4-security--hardening)
5. [Configuration Variables Reference](#5-configuration-variables-reference)
6. [Discord Intake Integration](#6-discord-intake-integration)
7. [OBS Studio Headless Setup](#7-obs-studio-headless-setup)
8. [Cache & Storage Management](#8-cache--storage-management)
9. [Troubleshooting & Diagnostics](#9-troubleshooting--diagnostics)

---

## 1. Architecture Overview & Components

`osu-stream-bot` provides an automated, endless 24/7 osu!standard replay stream for Twitch and YouTube. It supports both standalone monolithic operation and a distributed master/worker model over private network tunnels.

```
       [ Twitch / YouTube RTMP ]
                   ▲
                   │
      ┌────────────┴────────────┐
      │   Main Controller VPS   │
      │ ┌─────────────────────┐ │
      │ │   Xvfb + Headless   │ │
      │ │      OBS Studio     │ │
      │ └──────────▲──────────┘ │
      │            │ Browser    │
      │ ┌──────────┴──────────┐ │
      │ │  Web Player :8787   │ │
      │ └──────────▲──────────┘ │
      │ ┌──────────┴──────────┐ │
      │ │  Render API :8790   │ │
      │ └──────────▲──────────┘ │
      └────────────┼────────────┘
                   │ WireGuard VPN (10.10.0.0/24)
                   │ Bearer Token + Lease Auth
      ┌────────────┴────────────┐
      │    Render Worker VPS    │
      │ ┌─────────────────────┐ │
      │ │  render_worker.py   │ │
      │ └──────────▲──────────┘ │
      │ ┌──────────┴──────────┐ │
      │ │ danser-go (CPU/GPU) │ │
      │ └─────────────────────┘ │
      └─────────────────────────┘
```

### Core Modules:
- **`app.py`**: Main application orchestrator and Controller web server. Serves web player (`/`), `/control` dashboard, background Producer loop, and completion watcher.
- **`config.py`**: Strongly typed environment validation and fail-closed role preflight checks.
- **`queue_store.py`**: Manages ready MP4 videos in `video_queue/`, buffering limits, and state tracking.
- **`render_job_store.py`**: SQLite-backed async job repository with atomic claims, lease tokens, heartbeats, and job archiving.
- **`render_api.py`**: Internal authenticated REST API for remote render workers.
- **`render_worker.py`**: Autonomous background rendering worker executing jobs with `danser-go` and streaming MP4 artifacts.
- **`discord_ingest.py`**: Slash-command Discord bot for viewers to submit replays directly to the queue.
- **`danser_renderer.py`**: Local danser runner and beatmap library management with automatic mirrors and LRU pruning.

---

## 2. Operating Modes (RUN_MODE)

- **`standalone`** (default): All-in-one process. Handles web server, audio/video playback, and renders replays locally via danser or o!rdr.
- **`controller`**: Central streaming coordinator. Runs player UI, internal Render API, queues jobs into SQLite, accepts rendered MP4s from workers, and feeds OBS.
- **`render-worker`**: Dedicated rendering daemon. Does not bind any public web ports; polls controller via HTTP, executes danser renders, and streams video artifacts back.

---

## 3. Two-VPS Deployment

### Step 1. Configure Private Network (WireGuard)
Connect the Controller and Worker VPS instances via a WireGuard VPN tunnel:
- Controller VPN IP: `10.10.0.1/24`
- Worker VPN IP: `10.10.0.2/24`

### Step 2. Setup Main Controller VPS
```bash
sudo ./deploy/install.sh controller
```
Edit `/etc/osu-stream/controller.env`:
```env
RUN_MODE=controller
RENDER_API_HOST=10.10.0.1
RENDER_API_PORT=8790
RENDER_API_TOKEN=<generate_via_openssl_rand_-hex_32>
```
Start the services:
```bash
sudo systemctl start xvfb osu-stream-controller obs
```

### Step 3. Setup Render Worker VPS
```bash
sudo ./deploy/install.sh worker
```
Edit `/etc/osu-stream/worker.env`:
```env
RUN_MODE=render-worker
CONTROLLER_RENDER_URL=http://10.10.0.1:8790
RENDER_API_TOKEN=<same_secret_token>
DANSER_SETTINGS=stream-cpu
```
Ensure beatmaps are synchronized into `tools/danser/songs/` and start the worker:
```bash
sudo systemctl start osu-stream-worker
```

---

## 4. Security & Hardening

1. **Reverse Proxy & TLS**:
   `/control` and internal management APIs are shielded with bcrypt HTTP Basic Auth and forced HTTPS redirects (308). The raw `Authorization` header is stripped before reaching Python backends.
2. **Systemd Sandboxing**:
   - `ProtectSystem=strict`
   - `ProtectHome=true` (for OBS: `ProtectHome=read-only` with designated profile paths)
   - `NoNewPrivileges=true`
   - `UMask=0077` ensuring all created files are accessible only by the service user.
3. **Xvfb Hardening**:
   Xvfb runs with `-nolisten tcp` and cookie authentication via `xauth` stored in a restricted `RuntimeDirectory=osu-stream-xvfb`.
4. **Twitch Stream Key**:
   Keep stream keys strictly inside OBS `service.json` (mode 0600) or systemd credentials. Never commit stream keys to version control.

---

## 5. Configuration Variables Reference

| Variable | Default | Description |
|---|---|---|
| `RUN_MODE` | `standalone` | Mode: `standalone`, `controller`, or `render-worker` |
| `RENDER_BACKEND` | `ordr` | Renderer backend in standalone (`ordr` or `danser`) |
| `BUFFER_SIZE` | `2` | Buffer target of queued videos + in-flight jobs |
| `DELETE_AFTER_PLAY` | `true` | Delete MP4 videos after playback completes |
| `PRODUCER_ENABLED` | `true` | Enable background queue producer |
| `WEB_HOST` / `WEB_PORT` | `127.0.0.1:8787` | Web player and control dashboard interface |
| `RENDER_API_HOST` | `127.0.0.1` | Binding interface for Render API (set to VPN IP) |
| `RENDER_API_PORT` | `8788` | Port for Render API on controller |
| `RENDER_API_TOKEN` | *(empty)* | Secret Bearer token for worker authentication |
| `CONTROLLER_RENDER_URL` | *(empty)* | Controller endpoint for workers (`http://<VPN_IP>:8790`) |
| `WORKER_ID` | `worker-<hostname>` | Unique worker identifier |
| `RENDER_HEARTBEAT_SECONDS`| `15.0` | Heartbeat interval from worker (1..20s) |
| `MAX_ARTIFACT_MIB` | `1024` | Maximum allowed size of uploaded artifact MP4 |
| `DANSER_DIR` | `tools/danser` | Directory of danser-go installation |
| `DANSER_SETTINGS` | `stream-cpu` | danser configuration profile |
| `DANSER_MAX_SONGS_MIB` | `5120` | Size threshold for LRU beatmap pruning |
| `DANSER_RENDER_TIMEOUT_SECONDS`| `3600` | Process timeout for rendering a replay |
| `PLAYER_VOLUME` | `20` | Initial player volume percentage |
| `DISCORD_ENABLED` | `false` | Enable Discord slash-command intake bot |

---

## 6. Discord Intake Integration

1. Create application on [Discord Developer Portal](https://discord.com/developers/applications).
2. Assign `applications.commands` permission scope.
3. Set `.env` parameters:
   ```env
   DISCORD_ENABLED=true
   DISCORD_TOKEN=your_bot_token
   DISCORD_GUILD_ID=your_guild_id
   DISCORD_CHANNEL_ID=your_channel_id
   ```
4. Commands: `/submit replay:<file.osr>`, `/status` (or `/queue`), and `/cancel submission_id:<id>`.

---

## 7. OBS Studio Headless Setup

- **Browser Source Settings**: URL `http://127.0.0.1:8787/`, resolution matching stream canvas, with *Control audio via OBS* checked.
- **Headless Execution**: Handled via `obs.service` using `llvmpipe` software rendering on display `:99`.

---

## 8. Cache & Storage Management

- **Songs LRU Cache**: Standalone danser tracks access times (`mtime` refreshed upon use) and prunes oldest entries down to 80% of `DANSER_MAX_SONGS_MIB`. Active beatmaps are guaranteed protection from eviction.
- **In-memory Background**: Custom background images uploaded through `/control` are maintained strictly in memory and automatically revert to `static/fallback.svg` upon process reboot.

---

## 9. Troubleshooting & Diagnostics

- **Inspect Service Logs**:
  ```bash
  sudo journalctl -u osu-stream-controller -f
  sudo journalctl -u osu-stream-worker -f
  ```
- **Query Job Database**:
  ```bash
  sqlite3 render_jobs.db "SELECT id, status, worker_id, attempts FROM render_jobs ORDER BY created_at DESC LIMIT 5;"
  ```
- **Beatmaps Missing on Worker**:
  Render Worker enforces fail-closed startup validation requiring at least one `.osu` file in `tools/danser/songs`.
