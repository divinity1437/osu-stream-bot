# 🇷🇺 **[РУКОВОДСТВО НА РУССКОМ ЯЗЫКЕ — ОТКРЫТЬ README_RU.md](README_RU.md)**

---

# osu! replay stream

Local service for an endless osu!standard replay stream. It accepts `.osr` files, queues them, renders videos through o!rdr or local danser, and exposes the result through an OBS Browser Source.

## Features

- Web player at `/` for OBS Browser Source.
- Control panel at `/control`.
- `.osr` uploads through the browser or `inbox/`.
- Queue priority: Discord submissions, local replays, then random standard scores.
- Atomic MP4 queueing with `ffprobe` validation.
- Background rendering of the next video.
- Skip, delete, and volume controls in `/control`.
- Hosted o!rdr or local danser-go rendering.
- Local osu! skin auto-detection, fixed/random danser skin selection, and replay information overlay.
- Discord slash commands without the privileged Message Content intent.
- Interactive English `configure.sh` TUI for `.env`, with inline hints, validation, secret masking, and backups.
- Rotating logs in `logs/stream.log`.
- Temporary waiting-background upload from `/control`:
  - `static/fallback.svg` is the default.
  - Browser-supported image formats can be uploaded, including PNG, JPEG, GIF, WebP, SVG, and AVIF.
  - The image is stored only in process memory and disappears after restart.
  - `fallback.svg` is automatically restored after restart or can be restored immediately.

## ROAD MAP

### Completed

- [x] OBS-ready player with automatic video transitions.
- [x] `/control` management panel.
- [x] Replay uploads and manual `inbox/` queueing.
- [x] Skip and deletion of current/queued videos.
- [x] Ready-video buffering.
- [x] o!rdr and local danser renderers.
- [x] Beatmap lookup by MD5 and safe `.osz` extraction.
- [x] `ffprobe` output validation.
- [x] Discord `/submit`, `/status`, `/queue`, and `/cancel` commands.
- [x] Temporary background with automatic reset after restart.
- [x] Volume control and o!rdr render recovery.
- [x] Interactive `configure.sh` TUI for safe `.env` configuration.
- [x] Local skin discovery/randomization and configurable danser visuals/replay overlay.
- [x] MIT license.

### WIP / planned

- [ ] Authentication and roles for `/control`.
- [ ] Multiple named background profiles.
- [ ] WebSocket-based scene control.
- [ ] Background crop, contain, and positioning settings.
- [ ] Prometheus metrics and health dashboard.
- [ ] Docker/Compose deployment.
- [ ] Full unit and integration test suite in CI.
- [ ] Manual beatmap and skin selection from the control panel.

## Architecture

- `app.py` — aiohttp server, queue, producer, and API.
- `static/player.html` — OBS player.
- `static/control.html` — control panel.
- `static/fallback.svg` — default waiting background.
- `danser_renderer.py` — local renderer integration.
- `discord_ingest.py` — Discord adapter.
- `submission_store.py` — SQLite storage for Discord submissions.
- `inbox/` — incoming replays; `video_queue/` — completed MP4 files.
- `archive/`, `work/`, `rejected/`, and `logs/` — runtime data.

## Requirements and dependencies

- Linux x86_64 and Python 3.12+.
- `ffmpeg` and `ffprobe`.
- For danser: GPU/OpenGL, SDL3, and a working GPU driver; VAAPI is used for hardware encoding when configured.
- For Discord: a Discord application, bot token, and channel permissions.
- Internet access for o!rdr, osuokayu, beatmap mirrors, and Discord Gateway.

Direct Python dependencies from `requirements.txt`:

```text
aiohttp>=3.13,<4
python-socketio[asyncio_client]>=5.14,<6
discord.py>=2.6,<3
```

`aiohttp` provides the HTTP server/client and uploads, `python-socketio` connects to o!rdr, and `discord.py` provides the bot and slash commands. SQLite and the remaining imports use Python's standard library.

The tested virtual environment contained:

```text
aiohappyeyeballs==2.7.1
aiohttp==3.14.5
aiosignal==1.4.0
attrs==26.1.0
audioop-lts==0.2.2
bidict==0.24.1
certifi==2026.10.23
charset-normalizer==3.5.2
discord.py==2.7.1
frozenlist==1.8.0
h11==0.16.0
idna==3.20
multidict==7.2.1
propcache==0.5.4
python-engineio==4.15.1
python-socketio==5.17.1
requests==2.34.2
simple-websocket==1.1.0
urllib3==2.8.0
websocket-client==1.9.2
wsproto==1.3.2
yarl==1.25.1
```

Additional system/runtime dependencies are `bash`, `curl`, `unzip`, SQLite, a modern browser, and OBS Studio. The `configure.sh` menu also needs `whiptail` or `dialog`. `install-danser.sh` installs danser-go `0.12.1`, verifies its SHA-256 checksum, and installs danser libraries, SDL3, BASS, bundled FFmpeg, `assets.dpak`, `ffmpeg`, and `ffprobe` under `tools/danser/`.

## Setup and launch

```bash
cd /path/to/osu-stream-bot
./configure.sh
./check.sh
./run.sh
```

`configure.sh` provides an English interactive terminal menu with hints and
validation for every `.env` setting. Secrets are masked, and the previous file
is backed up as the Git-ignored `.env.bak`. You can still create the file
manually with `cp .env.example .env` if preferred.

For o!rdr, configure `ORDR_API_KEY` and disable developer mode:

```env
RENDER_BACKEND=ordr
ORDR_DEVELOPER_MODE=false
ORDR_API_KEY=your_key_here
```

## Local danser Setup (Windows and Linux/Unix)

For local danser-go rendering:

**Linux / Unix:**
```bash
./install-danser.sh
```

**Windows:**
```cmd
install-danser.bat
```

Configure `.env`:
```env
RENDER_BACKEND=danser
DANSER_DIR=tools/danser
DANSER_SETTINGS=stream-cpu
DANSER_SKINS_DIR=auto
DANSER_SKIN=default
DANSER_RANDOM_SKIN=false
DANSER_USE_SKIN_CURSOR=true
```

### danser Settings and Platform Profiles

- **`stream-cpu` (Recommended / Universal):** Uses software `libx264` (`preset: veryfast`, `CRF: 20`, 1280x720 @ 60 FPS). Works out of the box on both **Windows** and **Linux/Unix** without requiring GPU hardware encoder setup.
- **`stream` (Linux hardware VAAPI):** Uses `h264_vaapi` with `/dev/dri/renderD128`. Intended for Linux machines with properly configured VAAPI drivers.
- **Windows Graphics/Driver Note:** When running on Windows, ensure the bot runs in an active interactive user desktop session (e.g., via Task Scheduler on logon or `run.bat`). Headless background sessions (Session 0) lack an active display context and may cause OpenGL/driver initialization errors (e.g. heap corruption or driver code 43) in `danser-cli.exe`.

`DANSER_SKINS_DIR=auto` searches common native and Wine osu!stable locations:
- **Windows:** `%LOCALAPPDATA%\osu!\Skins`, `C:\osu!\Skins`
- **Linux/Unix:** `~/.local/share/osu-wine/osu!/Skins`, `~/.wine/drive_c/Program Files/osu!/Skins`, `~/.osu/Skins`, `~/.var/app/sh.ppy.osu/data/osu/Skins`

Set an explicit directory path if osu! is installed elsewhere. `configure.sh` lists every detected directory containing `skin.ini`. Enable `DANSER_RANDOM_SKIN=true` to pick a different custom skin for each replay. `DANSER_USE_SKIN_CURSOR=true` applies the selected skin's cursor and trail.

The generated video can display the mod-adjusted star rating, replay player name, and mods through `DANSER_REPLAY_OVERLAY=true`.

Background controls are exposed as percentages: `DANSER_BACKGROUND_DIM_PERCENT` (95 by default), `DANSER_PARALLAX_AMOUNT_PERCENT` (10), and `DANSER_PARALLAX_SPEED_PERCENT` (50). `DANSER_PARALLAX_ENABLED` toggles the effect.

## Proxy Configuration for Russia (Обход блокировок для пользователей из РФ)

In Russia, Discord API/Gateway endpoints and beatmap/replay CDN mirrors (including Cloudflare edge networks) are heavily throttled or blocked by ISP DPI (TSPU).

The bot natively supports standard HTTP/HTTPS/SOCKS5 proxy environment variables. Both `aiohttp` and `discord.py` automatically route API requests and WebSocket Gateway traffic through the configured proxy.

### How to Configure:

In `run.sh` (Linux) or `run.bat` (Windows), or directly in your environment, export the proxy before launching `app.py`:

**Linux / Unix (`run.sh`):**
```bash
export HTTP_PROXY="http://127.0.0.1:10809"
export HTTPS_PROXY="http://127.0.0.1:10809"
export ALL_PROXY="http://127.0.0.1:10809"
```

**Windows (`run.bat`):**
```cmd
set HTTP_PROXY=http://127.0.0.1:10809
set HTTPS_PROXY=http://127.0.0.1:10809
set ALL_PROXY=http://127.0.0.1:10809
```

### Setup Options:
1. **Local VPN Client with Inbound Proxy:** Run your VPN client (such as Xray, Mihomo/Clash, Happ, CasperVPN, etc.) on the same machine and enable the local HTTP proxy listener (e.g. `127.0.0.1:10809` or `127.0.0.1:7890`).
2. **LAN Proxy Relay:** If running on a dedicated laptop or secondary machine, you can point the proxy variables to another PC on your local network running a VPN client (e.g. `http://192.168.2.222:10809`).
3. **Low Latency Requirement for Discord Slash Commands:** Discord interactions require an acknowledgment within 3 seconds, otherwise Discord displays *"Application did not respond"*. Ensure your proxy node provides stable, low-latency connectivity.

Run only the web player with `./run.sh --no-producer` (or `run.bat --no-producer`). URLs are `http://127.0.0.1:8787/`, `/control`, and `/health`.

## OBS

Add a Browser Source using:

- URL: `http://127.0.0.1:8787/`
- Width: `1280`
- Height: `720`
- FPS: `60`

Enable Browser Source audio if required. Do not enable `Shutdown source when not visible` if playback must continue across scene changes.

## Temporary background

Open `/control`, choose an image in **Waiting background**, and click **Set temporary background**. The size limit is controlled by `MAX_BACKGROUND_MIB` and defaults to 20 MiB. The image is never written to disk or `state.json`; it disappears when the process stops or restarts. **Restore fallback.svg** resets it immediately.

API endpoints:

- `GET /api/background` — current status.
- `POST /api/background` — multipart field `background`.
- `DELETE /api/background` — restore `static/fallback.svg`.
- `GET /background` — current image response.

The server listens on localhost by default. Do not expose `/control` without authentication and a reverse proxy.

## Discord

Create a Discord application, add a bot, and invite it with `bot` and `applications.commands` scopes. Required permissions are View Channel, Send Messages, Embed Links, and Read Message History.

```env
DISCORD_ENABLED=true
DISCORD_TOKEN=put_the_bot_token_here
DISCORD_GUILD_ID=123456789012345678
DISCORD_CHANNEL_ID=123456789012345678
DISCORD_QUEUE_LIMIT=50
DISCORD_USER_COOLDOWN_SECONDS=300
DISCORD_MAX_FILE_MIB=10
```

Commands: `/submit replay:<file.osr>`, `/status`, `/queue`, and `/cancel submission_id:<id>`. `/status` shows all replays that are waiting, being prepared, or rendering; `/queue` is an alias for the same view. Never commit the bot token.

## Configuration

Run `./configure.sh` to change settings without editing `.env` manually. Use
`./configure.sh --validate` for a non-interactive validation pass. See
`.env.example` for all settings. Main options:

| Variable | Default | Purpose |
|---|---:|---|
| `RENDER_BACKEND` | `ordr` | `ordr` or `danser` |
| `BUFFER_SIZE` | `2` | Target buffer size |
| `DELETE_AFTER_PLAY` | `true` | Delete MP4 after playback |
| `PRODUCER_ENABLED` | `true` | Run producer |
| `WEB_HOST` / `WEB_PORT` | `127.0.0.1 / 8787` | HTTP binding |
| `MAX_UPLOAD_MIB` | `20` | `.osr` upload limit |
| `MAX_BACKGROUND_MIB` | `20` | Temporary background limit |
| `PLAYER_VOLUME` | `20` | Initial volume |
| `DANSER_SKINS_DIR` | `auto` | osu!stable skin directory detection or explicit path |
| `DANSER_SKIN` / `DANSER_RANDOM_SKIN` | `default / false` | Fixed or per-render random local skin |
| `DANSER_REPLAY_OVERLAY` | `true` | Show modded SR, player name, and mods in local renders |
| `DANSER_BACKGROUND_DIM_PERCENT` | `95` | Local-render gameplay background dim |

## Tests and verification

There is currently no separate pytest suite. Run the smoke check:

```bash
./check.sh
```

It compiles Python modules and checks required files, renderer, `ffprobe`, and configuration. For a manual background test, run `./run.sh --no-producer`, upload an image through `/control`, verify it on `/`, restart the process, and confirm that `fallback.svg` is restored.

## Tested Hardware and System Configurations

Extensive development, load testing, and verification were performed on real-world hardware running both Linux and Windows environments:

| Specification | Linux Desktop (Development / Primary) | Windows Laptop (Dedicated Player / Renderer) |
|---|---|---|
| **Operating System** | Arch Linux (Kernel `7.2.11-zen1-1-zen`, x86_64) | Microsoft Windows 10 Pro (Build `19045.6480`, 64-bit) |
| **CPU** | Intel Xeon E5-2643 @ 3.30 GHz (4 cores / 8 threads, Turbo 3.50 GHz) | AMD Ryzen 5 3500U with Radeon Vega Mobile Gfx (4 cores / 8 threads, 2.10–3.70 GHz) |
| **RAM** | 16 GiB DDR3 (15 GiB available to OS, 13 GiB swap) | 12 GiB DDR4 (10 GiB available to OS) |
| **GPU / Graphics** | AMD Radeon RX 550 640SP / RX 560/560X (Baffin, 4GB VRAM) | AMD Radeon(TM) Vega 8 Graphics (Integrated Mobile GPU) |
| **danser Backend** | danser-go `0.12.1` (`stream-cpu` libx264 & `stream` h264_vaapi) | danser-go `0.12.1` (`stream-cpu` libx264, interactive user session) |
| **Python Version** | Python 3.14.8 (`venv`) | Python 3.12.6 (`venv`) |
| **FFmpeg Version** | FFmpeg 9.0.2 / bundled danser FFmpeg libraries | Bundled danser FFmpeg binaries (`ffmpeg.exe`, `ffprobe.exe`) |
| **Network & Relay** | Native connection + TUN VPN (`happ-xray`) / SOCKS proxy provider | SOCKS5/HTTP Proxy Relay via LAN (`192.168.2.222:10809`) |
| **Tested Render Resolution** | 1280x720 @ 60 FPS, CRF 20 | 1280x720 @ 60 FPS, CRF 20 |

Other Linux distributions and Windows versions are fully compatible, provided requirements (Python 3.10+, FFmpeg, and OpenGL-capable drivers) are met.

## Useful commands

```bash
./configure.sh
./configure.sh --validate
./install-danser.sh
./run.sh --no-producer
./run.sh --recover-render 5094318
./open-player.sh
./open-control.sh
```

## License and third-party services

This project is distributed under the [MIT License](LICENSE). You may freely download, use, modify, distribute, sublicense, and include it in other projects, provided that the license and copyright notice are retained.

osu!, o!rdr, danser, Discord, and osuokayu are third-party projects/services with their own terms. Users are responsible for complying with their rules, respecting rights to uploaded images, beatmaps, and replays, and protecting tokens and API keys.
