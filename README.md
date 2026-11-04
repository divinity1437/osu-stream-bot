# 🇷🇺 [РУССКАЯ ВЕРСИЯ — README_RU.md](README_RU.md) | 📖 [COMPLETE WIKI / WIKI RU](docs/ru/WIKI.md)

---

# osu! replay stream

Automated 24/7 osu!standard replay stream service for Twitch and YouTube. It accepts `.osr` files, queues them, renders videos via hosted `o!rdr` or local `danser-go`, and broadcasts the stream using an OBS Studio Browser Source.

Supports both **standalone (all-in-one)** operation and **distributed two-tier architecture (Main Controller + Render Worker)** over private VPN networks.

---

## Table of Contents

- [Features](#features)
- [Hardware Tested Configurations](#hardware-tested-configurations)
- [Quick Start](#quick-start)
  - [Prerequisites](#prerequisites)
  - [Installation & Setup](#installation--setup)
  - [Running the Stream](#running-the-stream)
  - [Tests & Verification](#tests--verification)
- [Production Deployment](#production-deployment)
- [License & Disclaimer](#license--disclaimer)

---

## Features

- **OBS-Ready Web Player** at `/` for Browser Source with seamless video transitions.
- **Web Control Dashboard** at `/control` with queue inspection, skip, volume controls, preset/custom waiting screens, and a **collapsible Connected Render Workers panel** showing real-time hardware specs, activity, and rendering progress.
- **Multiple Replay Sources**: viewer submissions via Discord slash commands, manual uploads through browser or `inbox/`, and automatic discovery of random osu! scores.
- **Dual Render Backends**: hosted [o!rdr](https://ordr.issou.best/) or high-performance local [danser-go](https://github.com/Wieku/danser-go).
- **Auto-Detect Danser Profile**: unified `danser-stream.json` generated on the fly (`[D]` option in `configure.sh`) with auto-detection of CPU cores, virtualization, and GPU encoders (`nvenc` / `libx264`).
- **Rich Stream Metadata**: real-time display of Beatmap Artist, Title, Difficulty, Star Rating (★), Player Name, and Mods.
- **Dynamic Skin Support**: multi-skin synchronization with random skin selection per replay.
- **Distributed Architecture**: offload intensive danser rendering to dedicated worker nodes over WireGuard or Tailscale VPN.
- **Fail-Closed Safety & Validation**: strict atomic queuing, lease authorization tokens, and `ffprobe` stream validation.

---

## Roadmap

- [x] **Distributed Controller / Worker Architecture**: offload danser rendering across separate remote nodes with secure token authentication and auto-recovery.
- [x] **Live Worker Monitoring & Telemetry**: dynamic spoiler panel in `/control` reporting connected worker specs (CPU, RAM, GPU, OS) and real-time danser progress.
- [x] **Hardware Auto-Detection**: single adaptive danser profile generator tailored to VM/bare-metal and CPU/GPU capabilities.
- [x] **Rich Metadata Delivery**: worker-extracted beatmap and player metadata displayed directly in the web player and control queue.
- [x] **Native Stream Broadcaster (`stream_broadcaster`)**:
  - Headless direct-to-RTMP broadcasting via FFmpeg without requiring an active OBS instance or local GPU.
  - Multi-destination restreaming (simultaneous Twitch & YouTube output) using zero-copy stream passthrough (`-c copy`).
  - Seamless standby screen loop (`tools/standby.mp4`) during queue starvation or paused stream state without disconnecting RTMP.
  - Strict FIFO queue playback consuming `/api/next` and auto-acknowledging `/api/complete`.
- [ ] **Automated Headless Web Overlay Compositor**: optional lightweight virtual framebuffer compositor for viewer donation alerts and chat on headless servers.

---

## Hardware Tested Configurations

The codebase, rendering pipeline, and network stack have been tested on real physical and virtual hardware:

| Specification | Linux PC (Development / Primary) | Windows Laptop (Dedicated Renderer) | Cloud VPS (Dedicated Render Worker) |
|---|---|---|---|
| **Operating System** | Arch Linux (Kernel `7.2.11-zen1-1-zen`, x86_64) | Windows 10 Pro (Build `19045.6480`, 64-bit) | Ubuntu 24.04 LTS (Kernel `6.8.0-generic`, x86_64) |
| **Processor (CPU)** | Intel Xeon E5-2643 @ 3.30 GHz (4C / 8T) | AMD Ryzen 5 3500U with Vega Mobile Gfx (4C / 8T) | AMD Ryzen 9 5950X (4 vCPU dedicated) |
| **System Memory (RAM)** | 16 GiB DDR3 | 12 GiB DDR4 | 8 GiB RAM |
| **Graphics (GPU)** | AMD Radeon RX 550 / RX 560 (4GB VRAM) | AMD Radeon Vega 8 Graphics (Integrated) | Paravirtual QXL (Software rendering) |
| **danser-go Backend** | `0.12.1` (`stream-cpu` libx264 & `stream` VAAPI) | `0.12.1` (`stream-amf` Vega 8 & `stream-cpu`) | `0.12.1` (`stream-cpu` libx264 via Mesa llvmpipe) |
| **Render Performance** | ~1.8x–3.2x realtime speed | ~1.3x–3.6x realtime speed (Vega 8 AMF) | ~1.3x–1.76x realtime speed (CPU llvmpipe) |

---

## Quick Start

### Prerequisites

- **Linux** or **Windows** (64-bit)
- **Python 3.10+** (Python 3.12 / 3.14 tested)
- **FFmpeg & FFprobe** installed and available in system PATH
- **OpenGL-compatible drivers** or Mesa LLVMpipe

### Installation & Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/divinity1437/osu-stream-bot.git
   cd osu-stream-bot
   ```

2. **Run configuration wizard:**
   ```bash
   ./configure.sh
   ```
   *(Or copy `.env.example` to `.env` and configure manually).*

3. **Install danser-go (if using local rendering):**
   ```bash
   ./install-danser.sh
   ```

### Running the Stream

- **Full service with replay producer:**
  ```bash
  ./run.sh
  ```
- **Player UI only (without automatic queueing):**
  ```bash
  ./run.sh --no-producer
  ```
- **Headless 24/7 RTMP Broadcaster (Direct to Twitch & YouTube via FFmpeg):**
  ```bash
  ./run-broadcaster.sh
  # Or test locally without pushing to RTMP:
  ./run-broadcaster.sh --dry-run
  ```
- **Access URLs:**
  - Web Player: `http://127.0.0.1:8787/` (add as OBS Browser Source if using OBS)
  - Control Panel: `http://127.0.0.1:8787/control`
  - Health check: `http://127.0.0.1:8787/health`

### Tests & Verification

```bash
# Run unit & integration test suite
pytest -v

# Run syntax and configuration verification
./check.sh

# Run end-to-end distributed network pipeline test
python smoke_test_e2e.py
```

---

## Production Deployment

For full instructions on setting up two VPS instances (Controller + Render Worker), WireGuard, headless OBS streaming, Nginx TLS with Basic Auth, and systemd services, refer to the **[Complete Wiki](docs/en/WIKI.md)**.

---

## License & Disclaimer

Distributed under the **[MIT License](LICENSE)**.

osu!, o!rdr, danser, Discord, and osuokayu are trademarks or services of their respective owners. Users are responsible for complying with third-party service terms and securing their credentials.
