#!/usr/bin/env python3
import argparse
import asyncio
import html
import json
import logging
import mimetypes
import os
import re
import shutil
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
from render_api import create_render_api_app
from render_job_store import RenderJobStore
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


def generate_preset_svg(title: str, subtitle: str) -> bytes:
    title_escaped = html.escape(title)
    subtitle_escaped = html.escape(subtitle)
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
  <defs>
    <radialGradient id="bg" cx="50%" cy="45%" r="75%">
      <stop offset="0" stop-color="#261e46"/>
      <stop offset="0.55" stop-color="#121224"/>
      <stop offset="1" stop-color="#080910"/>
    </radialGradient>
    <linearGradient id="accent" x1="0" y1="0" x2="1" y2="1">
      <stop stop-color="#ff66aa"/>
      <stop offset="1" stop-color="#8f77ff"/>
    </linearGradient>
    <filter id="glow"><feGaussianBlur stdDeviation="12"/></filter>
  </defs>
  <rect width="1280" height="720" fill="url(#bg)"/>
  <circle cx="640" cy="320" r="190" fill="none" stroke="#ff66aa" stroke-width="18" opacity=".12" filter="url(#glow)"/>
  <circle cx="640" cy="320" r="150" fill="none" stroke="url(#accent)" stroke-width="10" opacity=".9"/>
  <circle cx="640" cy="320" r="68" fill="url(#accent)" opacity=".9"/>
  <circle cx="640" cy="320" r="32" fill="#161426"/>
  <text x="640" y="540" fill="#ffffff" font-family="Inter,Arial,sans-serif" font-size="44" font-weight="700" text-anchor="middle">{title_escaped}</text>
  <text x="640" y="595" fill="#b8b2cb" font-family="Inter,Arial,sans-serif" font-size="24" text-anchor="middle">{subtitle_escaped}</text>
</svg>"""
    return svg.encode("utf-8")


BUILTIN_BACKGROUND_PROFILES: dict[str, dict[str, str]] = {
    "default": {
        "title": "osu! replay stream",
        "subtitle": "rendering the next play…",
        "label": "Rendering the next play (Default)",
    },
    "technical_issue": {
        "title": "osu! replay stream",
        "subtitle": "Technical Issue. Please stand by…",
        "label": "Technical Issue",
    },
    "temporary_pause": {
        "title": "osu! replay stream",
        "subtitle": "Stream paused. We'll be right back!",
        "label": "Temporary Pause",
    },
    "waiting_replays": {
        "title": "osu! replay stream",
        "subtitle": "Waiting for replays. Submit with !submit",
        "label": "Waiting for replays",
    },
}


class BackgroundStore:
    """Keep the optional custom waiting background in memory for this run only."""

    def __init__(self, fallback_path: Path, max_bytes: int):
        self.fallback_path = fallback_path
        self.max_bytes = max_bytes
        self._data: bytes | None = None
        self._name = fallback_path.name
        self._content_type = "image/svg+xml"
        self._profile_id = "default"
        self._revision = 0
        self._lock = asyncio.Lock()

    def _info(self) -> dict[str, Any]:
        is_custom = self._profile_id == "custom" and self._data is not None
        try:
            fallback_size = self.fallback_path.stat().st_size
        except OSError:
            fallback_size = 0
        return {
            "custom": is_custom,
            "profile": self._profile_id,
            "available_profiles": [
                {"id": k, "label": v["label"], "active": (k == self._profile_id)}
                for k, v in BUILTIN_BACKGROUND_PROFILES.items()
            ],
            "name": self._name if is_custom else BUILTIN_BACKGROUND_PROFILES.get(self._profile_id, {}).get("label", self.fallback_path.name),
            "content_type": self._content_type,
            "size": len(self._data) if self._data is not None else fallback_size,
            "revision": self._revision,
            "url": f"/background?v={self._revision}",
            "max_size": self.max_bytes,
            "temporary": is_custom,
        }

    async def info(self) -> dict[str, Any]:
        async with self._lock:
            return self._info()

    async def set_custom(self, data: bytes, name: str, content_type: str) -> dict[str, Any]:
        if not data:
            raise ValueError("The image file is empty")
        if len(data) > self.max_bytes:
            raise ValueError("The image exceeds the configured size limit")
        safe_name = re.sub(r"[\x00-\x1f\\/]", "_", name).strip(". ")[:255] or "background"
        async with self._lock:
            self._data = bytes(data)
            self._name = safe_name
            self._content_type = content_type
            self._profile_id = "custom"
            self._revision += 1
            return self._info()

    async def set_profile(self, profile_id: str) -> dict[str, Any]:
        if profile_id not in BUILTIN_BACKGROUND_PROFILES:
            raise ValueError(f"Unknown background profile: {profile_id}")
        profile = BUILTIN_BACKGROUND_PROFILES[profile_id]
        svg_data = generate_preset_svg(profile["title"], profile["subtitle"])
        async with self._lock:
            self._data = svg_data
            self._name = profile["label"]
            self._content_type = "image/svg+xml"
            self._profile_id = profile_id
            self._revision += 1
            return self._info()

    async def reset(self) -> dict[str, Any]:
        async with self._lock:
            self._data = None
            self._name = self.fallback_path.name
            self._content_type = "image/svg+xml"
            self._profile_id = "default"
            self._revision += 1
            return self._info()

    async def payload(self) -> tuple[bytes | None, str]:
        async with self._lock:
            return self._data, self._content_type


class RuntimeState:
    def __init__(self, config: Config, store: QueueStore, job_store: RenderJobStore | None = None):
        self.config = config
        self.store = store
        self.job_store = job_store
        self.phase = "starting"
        self.last_error = ""
        self.websocket_connected = False
        self.current_render_id: int | str | None = None
        self.render_progress = ""

    async def snapshot(self) -> dict[str, Any]:
        queue = await self.store.snapshot()
        phase = self.phase
        current_render_id = self.current_render_id
        render_progress = self.render_progress

        if self.config.run_mode == "controller" and self.job_store:
            active_job = await self.job_store.get_active_leased_job()
            if active_job:
                current_render_id = active_job["id"][:8]
                worker = active_job.get("worker_id") or "worker"
                progress_str = active_job.get("progress") or ""
                render_progress = progress_str if progress_str else f"rendering on {worker}"
                phase = f"rendering job {active_job['id'][:8]} on {worker}"
            else:
                current_render_id = None
                render_progress = ""
                ready_count = len(queue)
                if ready_count >= self.config.buffer_size:
                    phase = f"buffer is full ({ready_count}/{self.config.buffer_size}), waiting for playback slot"

        return {
            "phase": phase,
            "last_error": self.last_error,
            "websocket_connected": self.websocket_connected,
            "current_render_id": current_render_id,
            "render_progress": render_progress,
            "buffered": len(queue),
            "buffer_target": self.config.buffer_size,
            "queue": queue,
            "run_mode": self.config.run_mode,
            "render_backend": self.config.render_backend,
            "developer_mode": self.config.developer_mode,
            "played_count": int(self.store.state.get("played_count", 0)),
            "skipped_count": int(self.store.state.get("skipped_count", 0)),
            "deleted_count": int(self.store.state.get("deleted_count", 0)),
            "current_video": self.store.state.get("current_video"),
            "queue_paused": bool(self.store.state.get("paused", False)),
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
        job_store: RenderJobStore | None = None,
    ):
        self.config = config
        self.store = store
        self.runtime = runtime
        self.source = source
        self.ordr = ordr
        self.submissions = submissions
        self.danser = danser
        self.job_store = job_store

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
        if self.config.run_mode == "controller":
            await self._run_controller()
            return

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

    async def _run_controller(self) -> None:
        if self.job_store is None:
            raise RuntimeError("RenderJobStore is required in controller mode")
        while True:
            candidate: ReplayCandidate | None = None
            try:
                active_jobs = await self.job_store.count_active_jobs()
                ready_count = len(self.store.ready_videos)
                if ready_count + active_jobs >= self.config.buffer_size:
                    if ready_count >= self.config.buffer_size:
                        self.runtime.phase = f"buffer is full ({ready_count}/{self.config.buffer_size}), waiting for playback slot"
                    else:
                        self.runtime.phase = f"buffer waiting for {active_jobs} active render(s)"
                    self.runtime.last_error = ""
                    await asyncio.sleep(2)
                    continue

                self.runtime.phase = "controller: looking for a replay"
                candidate = await self.source.next()
                if candidate.submission_id is not None:
                    await self.submissions.mark_rendering(candidate.submission_id, None)

                description = candidate.original_name
                job_id = await self.job_store.create_job(
                    source_replay=str(candidate.path.resolve()),
                    score_id=candidate.score_id,
                    submission_id=candidate.submission_id,
                    mods=candidate.mods,
                    mods_label=candidate.mods_label,
                    description=description,
                )
                logging.info(
                    "Created render job %s for replay %s (%s)",
                    job_id,
                    candidate.original_name,
                    candidate.mods_label,
                )
                self.runtime.phase = f"job {job_id[:8]} queued"
                self.runtime.last_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as error:
                message = str(error) or type(error).__name__
                logging.exception("Controller Producer error: %s", message)
                self.runtime.last_error = message
                self.runtime.phase = "controller: waiting after error"
                if candidate:
                    if candidate.submission_id is not None:
                        await self.submissions.mark_failed(candidate.submission_id, message)
                    self._reject(candidate, message)
                    candidate = None
                await asyncio.sleep(10)


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
            info = await backgrounds.set_custom(data, original, content_type)
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

    async def select_background_profile(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            profile_id = str(payload.get("profile", "")).strip()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response({"error": "Invalid JSON"}, status=400)
        try:
            info = await backgrounds.set_profile(profile_id)
        except ValueError as err:
            return web.json_response({"error": str(err)}, status=400)
        logging.info("Waiting background profile changed to %s", profile_id)
        return web.json_response(info, headers={"Cache-Control": "no-store"})

    async def reset_background(_: web.Request) -> web.Response:
        info = await backgrounds.reset()
        logging.info("Waiting background reset to default from control page")
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

    async def set_playback_pause(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            paused = bool(payload["paused"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response({"error": "paused must be a boolean"}, status=400)
        result = await store.set_paused(paused)
        logging.info("Stream playback pause toggled from control page: %s", result)
        return web.json_response({"paused": result})

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
    app.router.add_post("/api/background/profile", select_background_profile)
    app.router.add_delete("/api/background", reset_background)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/player-state", player_state)
    app.router.add_get("/api/next", next_video)
    app.router.add_post("/api/complete", complete)
    app.router.add_post("/api/settings/volume", set_volume)
    app.router.add_post("/api/settings/pause", set_playback_pause)
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
    job_store: RenderJobStore | None = None
    internal_runner: web.AppRunner | None = None
    internal_site: web.TCPSite | None = None
    completion_task: asyncio.Task[Any] | None = None

    if config.run_mode == "controller":
        job_store = RenderJobStore(config.root)
        runtime.job_store = job_store
        reconciled = await job_store.reconcile_expired_jobs()
        if reconciled:
            logging.info("Startup reconciliation: recovered %d expired render job(s)", reconciled)

        internal_app = create_render_api_app(job_store, config)
        internal_runner = web.AppRunner(internal_app, access_log=None, shutdown_timeout=2)
        await internal_runner.setup()
        internal_site = web.TCPSite(internal_runner, config.render_api_host, config.render_api_port)
        await internal_site.start()
        logging.info("Render API listening on http://%s:%d", config.render_api_host, config.render_api_port)
    elif config.render_backend == "ordr":
        ordr = OrdrClient(config, store, runtime, session)
    else:
        from danser_renderer import DanserRenderer

        danser = DanserRenderer(config, runtime, session)

    producer = Producer(config, store, runtime, source, ordr, submissions, danser, job_store)
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

    if config.run_mode == "controller" and job_store is not None:
        async def job_completion_watcher():
            ffprobe = shutil.which("ffprobe")
            while True:
                try:
                    await asyncio.sleep(5)
                    completed_jobs = await job_store.get_completed_jobs()
                    for job in completed_jobs:
                        job_id = job["id"]
                        raw_path = job.get("artifact_path")
                        if not raw_path:
                            continue
                        artifact = Path(raw_path)
                        if not artifact.is_file() or artifact.stat().st_size == 0:
                            logging.warning("Completed job %s artifact %s missing or empty", job_id, raw_path)
                            continue

                        # Verify via ffprobe (has streams, duration > 0)
                        duration = 0.0
                        if ffprobe:
                            try:
                                proc = await asyncio.create_subprocess_exec(
                                    ffprobe,
                                    "-v",
                                    "error",
                                    "-show_entries",
                                    "format=duration:stream=codec_type",
                                    "-of",
                                    "json",
                                    str(artifact),
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE,
                                )
                                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
                                if proc.returncode != 0:
                                    logging.warning("ffprobe validation failed for job %s artifact", job_id)
                                    continue
                                probe_data = json.loads(stdout)
                                streams = probe_data.get("streams", [])
                                codec_types = {s.get("codec_type") for s in streams}
                                if "video" not in codec_types:
                                    logging.warning("Artifact for job %s contains no video stream", job_id)
                                    continue
                                duration = float(probe_data.get("format", {}).get("duration", 0) or 0)
                                if duration <= 0:
                                    logging.warning("Artifact for job %s duration <= 0", job_id)
                                    continue
                            except Exception as probe_err:
                                logging.warning("ffprobe error on job %s: %s", job_id, probe_err)
                                continue

                        res_meta_raw = job.get("result_metadata")
                        res_meta = {}
                        if res_meta_raw:
                            try:
                                res_meta = json.loads(res_meta_raw) if isinstance(res_meta_raw, str) else res_meta_raw
                            except Exception:
                                res_meta = {}

                        final_desc = res_meta.get("description") or job.get("description") or f"Replay render {job_id}"
                        player_name = res_meta.get("player") or ""
                        star_val = res_meta.get("stars")
                        label_parts = [
                            final_desc,
                            f"{float(star_val):.2f}★" if star_val is not None else "",
                            player_name,
                            job.get("mods_label", "NM"),
                        ]
                        composite_label = " • ".join(p for p in label_parts if p)

                        metadata = {
                            "score_id": job.get("score_id"),
                            "submission_id": job.get("submission_id"),
                            "render_id": f"worker-{job_id[:8]}",
                            "renderer": "danser-worker",
                            "beatmap_set_id": res_meta.get("beatmap_set_id"),
                            "beatmap_id": res_meta.get("beatmap_id"),
                            "artist": res_meta.get("artist"),
                            "title": res_meta.get("title"),
                            "difficulty": res_meta.get("difficulty"),
                            "mods": job.get("mods_label", "NM"),
                            "mods_value": job.get("mods", 0),
                            "skin": res_meta.get("skin") or "default",
                            "description": final_desc,
                            "player": player_name,
                            "stars": star_val,
                            "label": composite_label,
                            "duration": duration,
                            "queued_at": int(time.time()),
                        }
                        queued_dest = await store.enqueue(artifact, metadata)
                        logging.info("Moved completed render %s into queue: %s (%s)", job_id, queued_dest.name, composite_label)

                        submission_id = job.get("submission_id")
                        if submission_id is not None:
                            await submissions.mark_ready(submission_id, queued_dest.name, res_meta.get("skin"))

                        await job_store.archive_job(job_id)

                        # Clean up source replay if it exists in work
                        source_replay = Path(job.get("source_replay", ""))
                        if source_replay.is_file():
                            source_replay.unlink(missing_ok=True)
                except asyncio.CancelledError:
                    break
                except Exception as loop_err:
                    logging.exception("Error in job_completion_watcher: %s", loop_err)

        completion_task = asyncio.create_task(job_completion_watcher(), name="job-completion-watcher")
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
        if completion_task:
            completion_task.cancel()
            await asyncio.gather(completion_task, return_exceptions=True)
            logging.info("Job completion watcher stopped")
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
        if job_store:
            await job_store.close()
        logging.info("Render backend stopped")
        await session.close()
        logging.info("HTTP client stopped")
        await submissions.close()
        logging.info("Submission store stopped")
        if internal_site:
            await internal_site.stop()
        if internal_runner:
            try:
                await asyncio.wait_for(internal_runner.cleanup(), timeout=3)
            except asyncio.TimeoutError:
                logging.warning("Internal Render API shutdown timed out")
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
