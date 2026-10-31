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

For local danser:

```bash
./install-danser.sh
```

```env
RENDER_BACKEND=danser
DANSER_DIR=tools/danser
DANSER_SETTINGS=stream
DANSER_SKIN=default
```

Run only the web player with `./run.sh --no-producer`. URLs are `http://127.0.0.1:8787/`, `/control`, and `/health`.

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

Commands: `/submit replay:<file.osr>`, `/status submission_id:<id>`, `/queue`, and `/cancel submission_id:<id>`. Never commit the bot token.

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

## Tests and verification

There is currently no separate pytest suite. Run the smoke check:

```bash
./check.sh
```

It compiles Python modules and checks required files, renderer, `ffprobe`, and configuration. For a manual background test, run `./run.sh --no-producer`, upload an image through `/control`, verify it on `/`, restart the process, and confirm that `fallback.svg` is restored.

## Tested configuration

Development and smoke testing were performed on:

- Arch Linux rolling, x86_64.
- Intel Xeon E5-2643 @ 3.30 GHz, 4 physical cores / 8 threads, 1 socket, up to 3.50 GHz Turbo.
- 16 GiB RAM installed, approximately 15 GiB available to the OS, and 13 GiB swap.
- RAM generation could not be determined because SPD/DMI information was unavailable in the environment.
- Python 3.14.8 in a local `venv`.
- FFmpeg/ffprobe 9.0.2.
- AMD Radeon RX 550 (Baffin), VAAPI at `/dev/dri/renderD128`.
- 1280x720 at 60 FPS, `RENDER_BACKEND=danser`, and `h264_vaapi` hardware encoding.
- danser-go 0.12.1.

Other Linux distributions may require additional packages and GPU permissions.

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
