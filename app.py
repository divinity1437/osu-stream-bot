#!/usr/bin/env python3
import argparse
import asyncio
import html
import logging
import mimetypes
import os
import re
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from aiohttp import web

from config import Config, ROOT, check_role_requirements, load_env
from ordr_client import OrdrClient, RenderRequestError
from queue_store import QueueStore, read_json, write_json_atomic
from replay_source import (
    ReplayCandidate,
    ReplaySource,
    format_mods,
    parse_replay_header,
    replay_has_double_time,
)
from submission_store import SubmissionStore


def setup_logging() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        ROOT / "logs" / "stream.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[stream, file_handler])


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json_atomic(path: Path, data: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def detect_image_content_type(filename: str, advertised_type: str, data: bytes) -> str:
    """Return an image MIME type without limiting uploads to a fixed extension list."""
    header = data[:4096]
    detected = ""
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    elif header.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    elif header.startswith((b"GIF87a", b"GIF89a")):
        detected = "image/gif"
    elif len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        detected = "image/webp"
    elif header.startswith(b"BM"):
        detected = "image/bmp"
    elif header.startswith((b"II*\x00", b"MM\x00*")):
        detected = "image/tiff"
    elif header.startswith(b"\x00\x00\x01\x00"):
        detected = "image/x-icon"
    elif header.startswith(b"\xff\x0a") or header.startswith(b"\x00\x00\x00\x0cJXL \r\n\x87\n"):
        detected = "image/jxl"
    elif len(header) >= 12 and header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand in {b"avif", b"avis"}:
            detected = "image/avif"
        elif brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            detected = "image/heic"
    if not detected:
        text = header.decode("utf-8-sig", "ignore")
        if re.search(r"<svg(?:\s|>)", text, re.IGNORECASE):
            detected = "image/svg+xml"
    if detected:
        return detected

    advertised = advertised_type.partition(";")[0].strip().lower()
    if advertised.startswith("image/"):
        return advertised
    guessed = (mimetypes.guess_type(filename)[0] or "").lower()
    if guessed.startswith("image/"):
        return guessed
    raise ValueError("The uploaded file is not recognized as an image")


class BackgroundStore:
    """Keep the optional custom waiting background in memory for this run only."""

    def __init__(self, fallback_path: Path, max_bytes: int):
        self.fallback_path = fallback_path
        self.max_bytes = max_bytes
        self._data: bytes | None = None
        self._name = fallback_path.name
        self._content_type = "image/svg+xml"
        self._revision = 0
        self._lock = asyncio.Lock()

    def _info(self) -> dict[str, Any]:
        custom = self._data is not None
        try:
            fallback_size = self.fallback_path.stat().st_size
        except OSError:
            fallback_size = 0
        return {
            "custom": custom,
            "name": self._name if custom else self.fallback_path.name,
            "content_type": self._content_type if custom else "image/svg+xml",
            "size": len(self._data) if custom else fallback_size,
            "revision": self._revision,
            "url": f"/background?v={self._revision}",
            "max_size": self.max_bytes,
            "temporary": custom,
        }

    async def info(self) -> dict[str, Any]:
        async with self._lock:
            return self._info()

    async def set(self, data: bytes, name: str, content_type: str) -> dict[str, Any]:
        if not data:
            raise ValueError("The image file is empty")
        if len(data) > self.max_bytes:
            raise ValueError("The image exceeds the configured size limit")
        safe_name = re.sub(r"[\x00-\x1f\\/]", "_", name).strip(". ")[:255] or "background"
        async with self._lock:
            self._data = bytes(data)
            self._name = safe_name
            self._content_type = content_type
            self._revision += 1
            return self._info()

    async def reset(self) -> dict[str, Any]:
        async with self._lock:
            self._data = None
            self._name = self.fallback_path.name
            self._content_type = "image/svg+xml"
            self._revision += 1
            return self._info()

    async def payload(self) -> tuple[bytes | None, str]:
        async with self._lock:
            return self._data, self._content_type


class RuntimeState:
    def __init__(self, config: Config, store: QueueStore):
        self.config = config
        self.store = store
        self.phase = "starting"
        self.last_error = ""
        self.websocket_connected = False
        self.current_render_id: int | None = None
        self.render_progress = ""

    async def snapshot(self) -> dict[str, Any]:
        queue = await self.store.snapshot()
        return {
            "phase": self.phase,
            "last_error": self.last_error,
            "websocket_connected": self.websocket_connected,
            "current_render_id": self.current_render_id,
            "render_progress": self.render_progress,
            "buffered": len(queue),
            "buffer_target": self.config.buffer_size,
            "queue": queue,
            "render_backend": self.config.render_backend,
            "developer_mode": self.config.developer_mode,
            "played_count": int(self.store.state.get("played_count", 0)),
            "skipped_count": int(self.store.state.get("skipped_count", 0)),
            "deleted_count": int(self.store.state.get("deleted_count", 0)),
            "current_video": self.store.state.get("current_video"),
            "player_volume": int(self.store.state.get("player_volume", self.config.initial_volume)),
            "playback_revision": int(self.store.state.get("playback_revision", 0)),
        }


class Producer:
    def __init__(
        self,
        config: Config,
        store: QueueStore,
        runtime: RuntimeState,
        source: ReplaySource,
        ordr: OrdrClient | None,
        submissions: SubmissionStore,
        danser: Any = None,
    ):
        self.config = config
        self.store = store
        self.runtime = runtime
        self.source = source
        self.ordr = ordr
        self.submissions = submissions
        self.danser = danser

    def _reject(self, candidate: ReplayCandidate, reason: str) -> None:
        if not candidate.path.exists():
            return
        if candidate.manual:
            destination = self.store.rejected_dir / candidate.path.name
            candidate.path.replace(destination)
            destination.with_suffix(destination.suffix + ".txt").write_text(reason, encoding="utf-8")
        else:
            candidate.path.unlink(missing_ok=True)

    async def run(self) -> None:
        if self.ordr:
            await self.ordr.connect()
        while True:
            candidate: ReplayCandidate | None = None
            render_id: int | None = None
            try:
                if self.store.buffered_count() >= self.config.buffer_size:
                    self.runtime.phase = "buffer ready"
                    self.runtime.last_error = ""
                    await asyncio.sleep(2)
                    continue
                self.runtime.phase = "looking for a replay"
                candidate = await self.source.next()
                if self.config.render_backend == "danser":
                    if self.danser is None:
                        raise RuntimeError("Local danser renderer is unavailable")
                    beatmap = await self.danser.prepare(candidate)
                    if candidate.submission_id is not None:
                        await self.submissions.mark_rendering(candidate.submission_id, None)
                    result = await self.danser.render(candidate, beatmap)
                    temporary_video = result.path
                    metadata = {
                        "score_id": candidate.score_id,
                        "submission_id": candidate.submission_id,
                        "render_id": None,
                        "renderer": "danser",
                        "beatmap_set_id": result.beatmap_set_id,
                        "beatmap_id": result.beatmap_id,
                        "mods": candidate.mods_label,
                        "mods_value": candidate.mods,
                        "skin": result.skin,
                        "description": result.description,
                        "player": result.player_name,
                        "stars": result.star_rating,
                        "label": " • ".join(
                            part
                            for part in (
                                result.description,
                                f"{result.star_rating:.2f}★" if result.star_rating is not None else "",
                                result.player_name,
                                candidate.mods_label,
                            )
                            if part
                        ),
                        "duration": result.duration,
                        "queued_at": int(time.time()),
                    }
                else:
                    if self.ordr is None:
                        raise RuntimeError("o!rdr client is unavailable")
                    render_id = await self.ordr.submit(candidate)
                    if candidate.submission_id is not None:
                        await self.submissions.mark_rendering(candidate.submission_id, render_id)
                    outcome = await self.ordr.wait(render_id)
                    if self.config.developer_mode:
                        if candidate.submission_id is not None:
                            await self.submissions.mark_failed(
                                candidate.submission_id,
                                "Developer-mode simulation completed without a real video",
                            )
                        candidate.path.unlink(missing_ok=True)
                        candidate = None
                        self.runtime.phase = "developer simulation completed"
                        self.runtime.last_error = ""
                        self.runtime.current_render_id = None
                        self.runtime.render_progress = ""
                        logging.info("Developer-mode render lifecycle completed successfully")
                        while self.config.developer_mode:
                            await asyncio.sleep(3600)
                    temporary_video, duration = await self.ordr.download(render_id, outcome)
                    selected_skin = self.config.dt_skin if replay_has_double_time(candidate.mods) else self.config.skin
                    metadata = {
                        "score_id": candidate.score_id,
                        "submission_id": candidate.submission_id,
                        "render_id": render_id,
                        "renderer": "ordr",
                        "mods": candidate.mods_label,
                        "mods_value": candidate.mods,
                        "skin": outcome.get("skin") or selected_skin,
                        "description": outcome.get("description") or outcome.get("title") or f"Replay render {render_id}",
                        "video_url": outcome.get("videoUrl"),
                        "duration": duration,
                        "queued_at": int(time.time()),
                    }
                queued = await self.store.enqueue(temporary_video, metadata)
                if candidate.submission_id is not None:
                    await self.submissions.mark_ready(
                        candidate.submission_id,
                        queued.name,
                        str(metadata.get("skin") or "") or None,
                    )
                candidate.path.unlink(missing_ok=True)
                candidate = None
                self.runtime.phase = f"queued {queued.name}"
                self.runtime.last_error = ""
                self.runtime.current_render_id = None
                self.runtime.render_progress = ""
                logging.info("Video added to playback queue: %s", queued.name)
            except asyncio.CancelledError:
                raise
            except RenderRequestError as error:
                message = f"o!rdr error{f' {error.code}' if error.code is not None else ''}: {error}"
                logging.error(message)
                self.runtime.last_error = message
                self.runtime.phase = "render failed"
                if candidate:
                    if candidate.submission_id is not None:
                        if error.status is None and error.code is None and "request failed" in str(error).lower():
                            await self.submissions.requeue(candidate.submission_id, message)
                        else:
                            await self.submissions.mark_failed(candidate.submission_id, message)
                    self._reject(candidate, message)
                    candidate = None
                self.runtime.current_render_id = None
                self.runtime.render_progress = ""
                delay = 60 if error.code in {36, 40, 41} or error.status == 429 else 15
                await asyncio.sleep(delay)
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, RuntimeError, OSError) as error:
                message = str(error) or type(error).__name__
                logging.exception("Producer error: %s", message)
                self.runtime.last_error = message
                self.runtime.phase = "waiting after an error"
                if candidate:
                    if candidate.submission_id is not None:
                        if self.config.render_backend == "danser":
                            await self.submissions.mark_failed(candidate.submission_id, message)
                        elif render_id is None:
                            await self.submissions.requeue(candidate.submission_id, message)
                        else:
                            await self.submissions.mark_failed(candidate.submission_id, message)
                    self._reject(candidate, message)
                    candidate = None
                self.runtime.current_render_id = None
                self.runtime.render_progress = ""
                await asyncio.sleep(15)


def create_web_app(
    config: Config,
    store: QueueStore,
    runtime: RuntimeState,
    backgrounds: BackgroundStore | None = None,
) -> web.Application:
    backgrounds = backgrounds or BackgroundStore(ROOT / "static" / "fallback.svg", config.max_background_bytes)
    app = web.Application(
        client_max_size=max(config.max_upload_bytes, config.max_background_bytes) + 1024 * 1024
    )

    async def player(_: web.Request) -> web.FileResponse:
        return web.FileResponse(ROOT / "static" / "player.html", headers={"Cache-Control": "no-store"})

    async def control(_: web.Request) -> web.FileResponse:
        return web.FileResponse(ROOT / "static" / "control.html", headers={"Cache-Control": "no-store"})

    async def fallback(_: web.Request) -> web.FileResponse:
        return web.FileResponse(
            ROOT / "static" / "fallback.svg",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def background_image(_: web.Request) -> web.Response:
        data, content_type = await backgrounds.payload()
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if data is None:
            return web.FileResponse(ROOT / "static" / "fallback.svg", headers=headers)
        return web.Response(body=data, headers={**headers, "Content-Type": content_type})

    async def background_status(_: web.Request) -> web.Response:
        return web.json_response(await backgrounds.info(), headers={"Cache-Control": "no-store"})

    async def upload_background(request: web.Request) -> web.Response:
        try:
            reader = await request.multipart()
            field = await reader.next()
            while field is not None and field.name != "background":
                field = await reader.next()
            if field is None or not field.filename:
                return web.json_response({"error": "Missing background image"}, status=400)
            original = str(field.filename)
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = await field.read_chunk(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > config.max_background_bytes:
                    limit_mib = config.max_background_bytes // (1024 * 1024)
                    return web.json_response(
                        {"error": f"Background image is too large (limit: {limit_mib} MiB)"},
                        status=413,
                    )
                chunks.append(chunk)
            data = b"".join(chunks)
            content_type = detect_image_content_type(
                original,
                field.headers.get("Content-Type", ""),
                data,
            )
            info = await backgrounds.set(data, original, content_type)
        except web.HTTPRequestEntityTooLarge:
            limit_mib = config.max_background_bytes // (1024 * 1024)
            return web.json_response(
                {"error": f"Background image is too large (limit: {limit_mib} MiB)"},
                status=413,
            )
        except (AssertionError, OSError, ValueError) as error:
            return web.json_response({"error": str(error) or "Invalid background image"}, status=400)
        logging.info("Temporary waiting background uploaded through control page: %s", info["name"])
        return web.json_response(info, status=201, headers={"Cache-Control": "no-store"})

    async def reset_background(_: web.Request) -> web.Response:
        info = await backgrounds.reset()
        logging.info("Waiting background reset to fallback.svg from control page")
        return web.json_response(info, headers={"Cache-Control": "no-store"})

    async def status(_: web.Request) -> web.Response:
        payload = await runtime.snapshot()
        payload["background"] = await backgrounds.info()
        return web.json_response(payload, headers={"Cache-Control": "no-store"})

    async def player_state(_: web.Request) -> web.Response:
        payload = await store.player_state()
        payload["background"] = await backgrounds.info()
        return web.json_response(payload, headers={"Cache-Control": "no-store"})

    async def next_video(_: web.Request) -> web.Response:
        item = await store.get_next()
        if not item:
            return web.json_response(
                {"available": False, "phase": runtime.phase},
                headers={"Cache-Control": "no-store"},
            )
        return web.json_response({"available": True, **item}, headers={"Cache-Control": "no-store"})

    async def complete(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response({"error": "Invalid JSON"}, status=400)
        name = Path(str(payload.get("name", ""))).name
        reason = str(payload.get("reason", "ended"))[:100]
        if not name:
            return web.json_response({"error": "Missing name"}, status=400)
        completed = await store.complete(name, reason)
        return web.json_response({"completed": completed})

    async def set_volume(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            volume = int(payload["volume"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response({"error": "Volume must be an integer from 0 to 100"}, status=400)
        if not 0 <= volume <= 100:
            return web.json_response({"error": "Volume must be between 0 and 100"}, status=400)
        saved = await store.set_volume(volume)
        return web.json_response({"volume": saved})

    async def skip_current(_: web.Request) -> web.Response:
        skipped, name = await store.skip_current()
        if skipped:
            logging.info("Current video skipped from control page: %s", name)
        return web.json_response({"skipped": skipped, "name": name})

    async def remove_video(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        removed, was_current = await store.remove_video(name, "control-delete")
        if not removed:
            return web.json_response({"error": "Video not found"}, status=404)
        logging.info("Video deleted from queue%s: %s", " (current)" if was_current else "", name)
        return web.json_response({"deleted": True, "name": name, "was_current": was_current})

    async def video(request: web.Request) -> web.StreamResponse:
        name = Path(request.match_info["name"]).name
        path = store.queue_dir / name
        if not path.is_file() or path.suffix.lower() != ".mp4":
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    async def upload(request: web.Request) -> web.Response:
        try:
            reader = await request.multipart()
        except (AssertionError, ValueError):
            return web.json_response({"error": "Expected multipart form data"}, status=400)
        field = await reader.next()
        while field is not None and field.name != "replay":
            field = await reader.next()
        if field is None or not field.filename:
            return web.json_response({"error": "Missing replay file"}, status=400)
        original = Path(field.filename).name
        if Path(original).suffix.lower() != ".osr":
            return web.json_response({"error": "Only .osr files are accepted"}, status=400)
        safe_name = re.sub(r"[^A-Za-z0-9_. -]+", "_", original).strip(". ") or "replay.osr"
        final_name = f"{int(time.time())}-{uuid.uuid4().hex[:8]}-{safe_name}"
        temporary = store.inbox_dir / f".{final_name}.part"
        destination = store.inbox_dir / final_name
        size = 0
        try:
            with temporary.open("wb") as output:
                while True:
                    chunk = await field.read_chunk(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > config.max_upload_bytes:
                        raise ValueError("Replay is too large")
                    output.write(chunk)
            if size < 32:
                raise ValueError("Replay file is empty or invalid")
            temporary.replace(destination)
        except (OSError, ValueError) as error:
            temporary.unlink(missing_ok=True)
            return web.json_response({"error": str(error)}, status=400)
        logging.info("Replay uploaded through control page: %s", destination.name)
        return web.json_response({"queued": True, "name": destination.name, "size": size}, status=201)

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app.router.add_get("/", player)
    app.router.add_get("/control", control)
    app.router.add_get("/static/fallback.svg", fallback)
    app.router.add_get("/background", background_image)
    app.router.add_get("/api/background", background_status)
    app.router.add_post("/api/background", upload_background)
    app.router.add_delete("/api/background", reset_background)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/player-state", player_state)
    app.router.add_get("/api/next", next_video)
    app.router.add_post("/api/complete", complete)
    app.router.add_post("/api/settings/volume", set_volume)
    app.router.add_post("/api/skip", skip_current)
    app.router.add_delete("/api/queue/{name}", remove_video)
    app.router.add_post("/api/replays", upload)
    app.router.add_get("/video/{name}", video)
    app.router.add_get("/health", health)
    return app


async def run(config: Config, no_producer: bool) -> None:
    store = QueueStore(config)
    submissions = SubmissionStore(
        ROOT,
        config.discord_queue_limit,
        config.discord_user_cooldown_seconds,
    )
    runtime = RuntimeState(config, store)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=180)
    connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
    session = aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        trust_env=True,
        headers={"User-Agent": "osuokayu-replay-stream/1.0"},
    )
    source = ReplaySource(config, store, session, submissions)
    ordr: OrdrClient | None = None
    danser: Any = None
    if config.render_backend == "ordr":
        ordr = OrdrClient(config, store, runtime, session)
    else:
        from danser_renderer import DanserRenderer

        danser = DanserRenderer(config, runtime, session)
    producer = Producer(config, store, runtime, source, ordr, submissions, danser)
    web_app = create_web_app(config, store, runtime)
    runner = web.AppRunner(web_app, access_log=None, shutdown_timeout=2)
    await runner.setup()
    site = web.TCPSite(runner, config.web_host, config.web_port)
    try:
        await site.start()
    except Exception:
        await session.close()
        await submissions.close()
        await runner.cleanup()
        raise
    runtime.phase = "web player ready"
    logging.info("Player:  http://%s:%d/", config.web_host, config.web_port)
    logging.info("Control: http://%s:%d/control", config.web_host, config.web_port)
    logging.info("Render backend: %s", config.render_backend)
    if config.render_backend == "danser":
        logging.info("Local danser executable: %s", config.danser_executable)
    elif config.developer_mode:
        logging.warning("o!rdr developer mode is enabled; no real render will be created")
    elif not config.api_key:
        logging.warning("No o!rdr API key: requests are limited to one every five minutes")
    producer_task: asyncio.Task[Any] | None = None
    if config.producer_enabled and not no_producer:
        producer_task = asyncio.create_task(producer.run(), name="producer")
    else:
        runtime.phase = "web player ready (producer disabled)"
    discord_service: Any = None
    discord_task: asyncio.Task[Any] | None = None
    if config.discord_enabled:
        from discord_ingest import DiscordReplayService

        def validate_discord_replay(path: Path) -> tuple[int, int, str, str]:
            mode, mods = parse_replay_header(path)
            mods_label = format_mods(mods)
            if config.render_backend == "danser":
                skin = "random" if config.danser_random_skin else config.danser_skin
            else:
                skin = config.dt_skin if replay_has_double_time(mods) else config.skin
            return mode, mods, mods_label, skin

        discord_service = DiscordReplayService(config, submissions, validate_discord_replay)
        discord_task = asyncio.create_task(discord_service.start(), name="discord-bot")

        def discord_done(task: asyncio.Task[Any]) -> None:
            if task.cancelled():
                return
            error = task.exception()
            if error:
                logging.error("Discord bot stopped: %s", error)

        discord_task.add_done_callback(discord_done)
        logging.info("Discord replay intake enabled for channel %d", config.discord_channel_id)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass
    try:
        await stop.wait()
    finally:
        logging.info("Shutting down stream services")
        if producer_task:
            producer_task.cancel()
            await asyncio.gather(producer_task, return_exceptions=True)
            logging.info("Producer stopped")
        if discord_service:
            try:
                await asyncio.wait_for(discord_service.close(), timeout=10)
            except asyncio.TimeoutError:
                logging.warning("Discord shutdown timed out; cancelling the gateway task")
        if discord_task:
            discord_task.cancel()
            await asyncio.gather(discord_task, return_exceptions=True)
            logging.info("Discord bot stopped")
        if ordr:
            await ordr.close()
        if danser:
            await danser.close()
        logging.info("Render backend stopped")
        await session.close()
        logging.info("HTTP client stopped")
        await submissions.close()
        logging.info("Submission store stopped")
        await site.stop()
        try:
            await asyncio.wait_for(runner.cleanup(), timeout=3)
        except asyncio.TimeoutError:
            logging.warning("Web server shutdown timed out")
        logging.info("Shutdown complete")


async def recover_render(config: Config, render_id: int) -> None:
    store = QueueStore(config)
    for item in await store.snapshot():
        if item.get("render_id") == render_id:
            print(f"Render {render_id} is already queued as {item['name']}")
            return
    runtime = RuntimeState(config, store)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=180)
    session = aiohttp.ClientSession(
        timeout=timeout,
        connector=aiohttp.TCPConnector(limit=4, ttl_dns_cache=300),
        headers={"User-Agent": "osuokayu-replay-stream/1.0"},
    )
    ordr = OrdrClient(config, store, runtime, session)
    try:
        outcome = await ordr._poll(render_id)
        if not outcome:
            raise RuntimeError(f"Render {render_id} is not finished or does not exist")
        kind, payload = outcome
        if kind == "failed":
            raise RuntimeError(payload.get("errorMessage") or f"Render {render_id} failed")
        temporary, duration = await ordr.download(render_id, payload)
        queued = await store.enqueue(
            temporary,
            {
                "score_id": None,
                "render_id": render_id,
                "description": payload.get("description") or payload.get("title") or f"Replay render {render_id}",
                "video_url": payload.get("videoUrl"),
                "duration": duration,
                "queued_at": int(time.time()),
                "recovered": True,
            },
        )
        print(f"Recovered render {render_id}: {queued}")
    finally:
        await session.close()


def check(config: Config) -> None:
    check_role_requirements(config)


def main() -> None:
    parser = argparse.ArgumentParser(description="Endless o!rdr replay stream queue")
    parser.add_argument("--check", action="store_true", help="validate local configuration and exit")
    parser.add_argument("--no-producer", action="store_true", help="run only the local web player")
    parser.add_argument("--recover-render", type=int, metavar="ID", help="download an already finished o!rdr render")
    arguments = parser.parse_args()
    load_env(ROOT / ".env")
    config = Config.from_env()
    if arguments.check:
        check(config)
        return
    setup_logging()
    try:
        if arguments.recover_render:
            asyncio.run(recover_render(config, arguments.recover_render))
        else:
            asyncio.run(run(config, arguments.no_producer))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
