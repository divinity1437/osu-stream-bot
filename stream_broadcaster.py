#!/usr/bin/env python3
"""Headless 24/7 RTMP stream broadcaster for osu! replay stream.

Streams replays to Twitch & YouTube concurrently via FFmpeg and local controller API,
with zero CPU transcoding overhead (-c copy) and seamless loop of tools/standby.mp4.
"""

import argparse
import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

from config import ROOT, env_bool, env_float, load_env


@dataclass
class BroadcasterConfig:
    api_url: str
    twitch_key: str
    twitch_url: str
    youtube_key: str
    youtube_url: str
    custom_rtmp_url: str
    standby_path: Path
    queue_dir: Path
    poll_interval: float
    dry_run: bool

    @classmethod
    def from_env(cls, root: Path | None = None) -> "BroadcasterConfig":
        base_root = (root or ROOT).resolve()
        load_env(base_root / ".env")

        web_host = os.getenv("WEB_HOST", "127.0.0.1").strip()
        web_port = os.getenv("WEB_PORT", "8787").strip()
        default_api = f"http://{web_host}:{web_port}"
        api_url = (
            os.getenv("STREAM_API_URL", "").strip()
            or os.getenv("CONTROLLER_URL", "").strip()
            or default_api
        ).rstrip("/")

        twitch_key = (
            os.getenv("STREAM_TWITCH_KEY", "").strip()
            or os.getenv("TWITCH_STREAM_KEY", "").strip()
        )
        twitch_url = (
            os.getenv("STREAM_TWITCH_URL", "").strip()
            or os.getenv("TWITCH_RTMP_URL", "").strip()
            or "rtmp://live.twitch.tv/app/"
        ).rstrip("/")

        youtube_key = (
            os.getenv("STREAM_YOUTUBE_KEY", "").strip()
            or os.getenv("YOUTUBE_STREAM_KEY", "").strip()
        )
        youtube_url = (
            os.getenv("STREAM_YOUTUBE_URL", "").strip()
            or os.getenv("YOUTUBE_RTMP_URL", "").strip()
            or "rtmp://a.rtmp.youtube.com/live2/"
        ).rstrip("/")

        custom_rtmp_url = (
            os.getenv("STREAM_CUSTOM_RTMP_URL", "").strip()
            or os.getenv("RTMP_URL", "").strip()
        )

        standby_raw = os.getenv("STREAM_STANDBY_PATH", "tools/standby.mp4").strip()
        standby_path = Path(standby_raw)
        if not standby_path.is_absolute():
            standby_path = base_root / standby_path

        queue_dir = base_root / "video_queue"
        poll_interval = env_float("STREAM_POLL_INTERVAL_SECONDS", 1.0)
        dry_run = env_bool("STREAM_DRY_RUN", False)

        return cls(
            api_url=api_url,
            twitch_key=twitch_key,
            twitch_url=twitch_url,
            youtube_key=youtube_key,
            youtube_url=youtube_url,
            custom_rtmp_url=custom_rtmp_url,
            standby_path=standby_path,
            queue_dir=queue_dir,
            poll_interval=max(0.5, poll_interval),
            dry_run=dry_run,
        )


def generate_standby_mp4(destination: Path, fallback_svg: Path) -> None:
    """Generate high-quality 1280x720 60fps h264+aac standby.mp4 file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_png = destination.with_suffix(".tmp.png")
    temp_mp4 = destination.with_suffix(".tmp.mp4")

    try:
        if fallback_svg.is_file():
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(fallback_svg), str(temp_png)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
            v_input = ["-loop", "1", "-r", "60", "-i", str(temp_png)]
        else:
            v_input = ["-f", "lavfi", "-i", "testsrc=size=1280x720:rate=60"]

        cmd = [
            "ffmpeg",
            "-y",
            *v_input,
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-c:v",
            "libx264",
            "-x264-params",
            "nal-hrd=cbr:filler=1",
            "-b:v",
            "3000k",
            "-minrate",
            "3000k",
            "-maxrate",
            "3000k",
            "-bufsize",
            "3000k",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "60",
            "-r",
            "60",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-t",
            "5",
            "-shortest",
            str(temp_mp4),
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
        temp_mp4.replace(destination)
        logging.info("Generated standby video at %s", destination)
    finally:
        temp_png.unlink(missing_ok=True)
        temp_mp4.unlink(missing_ok=True)


class StreamBroadcaster:
    def __init__(self, config: BroadcasterConfig):
        self.config = config
        self.stop_event = asyncio.Event()
        self.downstream_process: subprocess.Popen | None = None
        self.current_feeder: subprocess.Popen | None = None
        self.current_video_name: str | None = None

    def get_destinations(self) -> list[str]:
        destinations = []
        if self.config.twitch_key:
            destinations.append(f"{self.config.twitch_url}/{self.config.twitch_key}")
        if self.config.youtube_key:
            destinations.append(f"{self.config.youtube_url}/{self.config.youtube_key}")
        if self.config.custom_rtmp_url:
            destinations.append(self.config.custom_rtmp_url)
        return destinations

    def _start_downstream(self) -> None:
        """Start long-running downstream RTMP FFmpeg muxer process."""
        self._kill_downstream()

        destinations = self.get_destinations()
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+genpts+discardcorrupt",
            "-use_wallclock_as_timestamps",
            "0",
            "-f",
            "mpegts",
            "-i",
            "pipe:0",
        ]

        if self.config.dry_run or not destinations:
            if not self.config.dry_run and not destinations:
                logging.warning(
                    "No RTMP destinations configured. Running in DRY-RUN mode (null output)."
                )
            cmd.extend(["-c:v", "copy", "-c:a", "copy", "-f", "null", "-"])
        else:
            for dest in destinations:
                # Mask key in logs
                masked = dest
                if "/" in masked:
                    base, _, key = masked.rpartition("/")
                    masked = f"{base}/******" if len(key) > 4 else base
                logging.info("Broadcasting to RTMP target: %s", masked)
                cmd.extend([
                    "-c:v",
                    "copy",
                    "-c:a",
                    "copy",
                    "-f",
                    "flv",
                    "-flvflags",
                    "no_duration_filesize",
                    dest,
                ])

        self.downstream_process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        logging.info("Downstream RTMP broadcaster process started (PID %d)", self.downstream_process.pid)

    def _kill_downstream(self) -> None:
        if self.downstream_process:
            try:
                if self.downstream_process.stdin:
                    self.downstream_process.stdin.close()
                self.downstream_process.terminate()
                self.downstream_process.wait(timeout=3)
            except Exception:
                try:
                    self.downstream_process.kill()
                except Exception:
                    pass
            self.downstream_process = None

    def _kill_feeder(self) -> None:
        if self.current_feeder:
            try:
                self.current_feeder.terminate()
                self.current_feeder.wait(timeout=2)
            except Exception:
                try:
                    self.current_feeder.kill()
                except Exception:
                    pass
            self.current_feeder = None

    async def _fetch_next(self, session: aiohttp.ClientSession) -> dict[str, Any] | None:
        try:
            async with session.get(f"{self.config.api_url}/api/next", timeout=aiohttp.ClientTimeout(total=4)) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as error:
            logging.debug("Error querying /api/next: %s", error)
        return None

    async def _fetch_player_state(self, session: aiohttp.ClientSession) -> dict[str, Any] | None:
        try:
            async with session.get(f"{self.config.api_url}/api/player-state", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception:
            pass
        return None

    async def _complete_replay(self, session: aiohttp.ClientSession, name: str, reason: str = "ended") -> bool:
        try:
            async with session.post(
                f"{self.config.api_url}/api/complete",
                json={"name": name, "reason": reason},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return bool(data.get("completed"))
        except Exception as error:
            logging.warning("Failed to report completion for %s: %s", name, error)
        return False

    def _resolve_video_path(self, item: dict[str, Any]) -> str:
        name = item.get("name", "")
        local_file = self.config.queue_dir / name
        if local_file.is_file():
            return str(local_file)
        url = item.get("url", f"/video/{quote(name)}")
        if url.startswith("http://") or url.startswith("https://"):
            return url
        return f"{self.config.api_url}{url}"

    async def _stream_source(
        self,
        source_path: str,
        is_standby: bool,
        session: aiohttp.ClientSession,
    ) -> bool:
        """Stream video into downstream FFmpeg without CPU recoding."""
        if not self.downstream_process or self.downstream_process.poll() is not None:
            self._start_downstream()

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-re",
            "-i",
            source_path,
            "-c",
            "copy",
            "-f",
            "mpegts",
            "-",
        ]

        self.current_feeder = subprocess.Popen(
            cmd,
            stdout=self.downstream_process.stdin,
            stderr=subprocess.DEVNULL,
        )

        completed_normally = False
        try:
            while not self.stop_event.is_set():
                poll_res = self.current_feeder.poll()
                if poll_res is not None:
                    completed_normally = (poll_res == 0)
                    break

                if self.downstream_process.poll() is not None:
                    logging.warning("Downstream RTMP process died during playback. Restarting downstream...")
                    self._kill_feeder()
                    self._start_downstream()
                    break

                # Poll player state for skips, pauses or pending videos during standby
                state = await self._fetch_player_state(session)
                if state:
                    if state.get("paused"):
                        if not is_standby:
                            logging.info("Stream paused via control panel. Switching to standby screen.")
                            self._kill_feeder()
                            break
                    if not is_standby:
                        current = state.get("current_video")
                        if current != self.current_video_name:
                            logging.info("Current replay was skipped/deleted in control. Advancing.")
                            self._kill_feeder()
                            break

                if is_standby:
                    next_item = await self._fetch_next(session)
                    if next_item and next_item.get("available"):
                        logging.info("New replay ready during standby: %s. Switching immediately.", next_item.get("name"))
                        self._kill_feeder()
                        return True

                await asyncio.sleep(self.config.poll_interval)
        finally:
            self._kill_feeder()

        return completed_normally

    async def run(self) -> None:
        """Main non-stop 24/7 broadcasting loop."""
        if not self.config.standby_path.is_file():
            fallback_svg = ROOT / "static" / "fallback.svg"
            generate_standby_mp4(self.config.standby_path, fallback_svg)

        self._start_downstream()
        logging.info("Stream Broadcaster started. API target: %s", self.config.api_url)

        async with aiohttp.ClientSession() as session:
            last_standby_log = 0.0
            while not self.stop_event.is_set():
                try:
                    if self.downstream_process is None or self.downstream_process.poll() is not None:
                        self._start_downstream()

                    item = await self._fetch_next(session)

                    if item and item.get("available"):
                        self.current_video_name = item.get("name")
                        source = self._resolve_video_path(item)
                        label = item.get("label") or self.current_video_name
                        logging.info("Broadcasting queue replay: %s (%s)", self.current_video_name, label)

                        ended = await self._stream_source(source, is_standby=False, session=session)
                        if ended and self.current_video_name:
                            await self._complete_replay(session, self.current_video_name, "ended")
                            logging.info("Replay completed successfully: %s", self.current_video_name)
                        self.current_video_name = None
                    else:
                        now = time.time()
                        if now - last_standby_log > 30:
                            phase = (item or {}).get("phase") or "renderers generating replays / queue empty"
                            logging.info("No video ready (%s). Playing standby.mp4 loop...", phase)
                            last_standby_log = now

                        self.current_video_name = None
                        await self._stream_source(str(self.config.standby_path), is_standby=True, session=session)

                except asyncio.CancelledError:
                    break
                except Exception as error:
                    logging.exception("Broadcaster loop error: %s. Retrying in 3s...", error)
                    await asyncio.sleep(3.0)

        self._kill_feeder()
        self._kill_downstream()
        logging.info("Stream Broadcaster stopped cleanly.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Headless 24/7 RTMP stream broadcaster for osu! replay stream")
    parser.add_argument("--dry-run", action="store_true", help="Run without pushing to RTMP")
    parser.add_argument("--api-url", type=str, default="", help="Custom controller API base URL")
    parser.add_argument("--twitch-key", type=str, default="", help="Twitch stream key override")
    parser.add_argument("--youtube-key", type=str, default="", help="YouTube stream key override")
    parser.add_argument("--custom-rtmp", type=str, default="", help="Custom RTMP output URL")
    parser.add_argument("--standby", type=str, default="", help="Standby video path override")
    parser.add_argument("--generate-standby", action="store_true", help="Generate standby.mp4 and exit")
    parser.add_argument("--check", action="store_true", help="Validate environment and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = BroadcasterConfig.from_env()

    if args.dry_run:
        config.dry_run = True
    if args.api_url:
        config.api_url = args.api_url.rstrip("/")
    if args.twitch_key:
        config.twitch_key = args.twitch_key
    if args.youtube_key:
        config.youtube_key = args.youtube_key
    if args.custom_rtmp:
        config.custom_rtmp_url = args.custom_rtmp
    if args.standby:
        config.standby_path = Path(args.standby)

    if args.generate_standby:
        generate_standby_mp4(config.standby_path, ROOT / "static" / "fallback.svg")
        print(f"Standby video generated at {config.standby_path}")
        return

    if args.check:
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            print("ERROR: ffmpeg and ffprobe must be in PATH", file=sys.stderr)
            sys.exit(1)
        print("Role:           Stream Broadcaster")
        print(f"API Target:     {config.api_url}")
        print(f"Standby video:  {config.standby_path} ({'exists' if config.standby_path.is_file() else 'will generate'})")
        print(f"Twitch:         {'configured' if config.twitch_key else 'none'}")
        print(f"YouTube:        {'configured' if config.youtube_key else 'none'}")
        print(f"Custom RTMP:    {'configured' if config.custom_rtmp_url else 'none'}")
        print(f"Dry-run:        {config.dry_run}")
        print("Broadcaster configuration check passed.")
        return

    broadcaster = StreamBroadcaster(config)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def handle_signal():
        logging.info("Shutdown signal received.")
        broadcaster.stop_event.set()
        broadcaster._kill_feeder()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(broadcaster.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
