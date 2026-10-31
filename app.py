#!/usr/bin/env python3
import argparse
import asyncio
import html
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import signal
import struct
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from aiohttp import web
import socketio

from submission_store import SubmissionStore


ROOT = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value not in {None, ""} else default


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value not in {None, ""} else default


@dataclass(frozen=True)
class Config:
    root: Path
    render_backend: str
    developer_mode: bool
    api_key: str
    skin: str
    custom_skin: bool
    dt_skin: str
    dt_custom_skin: bool
    resolution: str
    render_timeout: int
    submit_interval: float
    replay_url: str
    scores_url: str
    score_id_min: int
    score_id_max: int
    replay_attempts: int
    buffer_size: int
    delete_after_play: bool
    producer_enabled: bool
    web_host: str
    web_port: int
    max_upload_bytes: int
    max_background_bytes: int
    initial_volume: int
    discord_enabled: bool
    discord_token: str
    discord_guild_id: int
    discord_channel_id: int
    discord_queue_limit: int
    discord_user_cooldown_seconds: int
    discord_max_file_bytes: int
    danser_dir: Path
    danser_executable: Path
    danser_songs_dir: Path
    danser_output_dir: Path
    danser_settings: str
    danser_skin: str
    danser_render_timeout: int
    danser_max_map_bytes: int

    @classmethod
    def from_env(cls) -> "Config":
        render_backend = os.getenv("RENDER_BACKEND", "ordr").strip().lower()
        developer_mode = env_bool("ORDR_DEVELOPER_MODE", True)
        api_key = os.getenv("ORDR_API_KEY", "").strip()
        configured_interval = env_float("ORDR_MIN_SUBMIT_INTERVAL_SECONDS", 0)
        if configured_interval > 0:
            submit_interval = configured_interval
        elif developer_mode:
            submit_interval = 2
        elif api_key:
            submit_interval = 5
        else:
            submit_interval = 315
        danser_dir = Path(os.getenv("DANSER_DIR", "tools/danser")).expanduser()
        if not danser_dir.is_absolute():
            danser_dir = ROOT / danser_dir
        config = cls(
            root=ROOT,
            render_backend=render_backend,
            developer_mode=developer_mode,
            api_key=api_key,
            skin=os.getenv("ORDR_SKIN", "whitecatCK1.0").strip(),
            custom_skin=env_bool("ORDR_CUSTOM_SKIN", False),
            dt_skin=os.getenv("ORDR_DT_SKIN", "RafisHDDT2018-03-26").strip(),
            dt_custom_skin=env_bool("ORDR_DT_CUSTOM_SKIN", False),
            resolution=os.getenv("ORDR_RESOLUTION", "1280x720").strip(),
            render_timeout=env_int("ORDR_RENDER_TIMEOUT_SECONDS", 1800),
            submit_interval=submit_interval,
            replay_url=os.getenv("OSU_REPLAY_URL", "https://api.osuokayu.pw/v1/get_replay").strip(),
            scores_url=os.getenv("OSU_SCORES_URL", "https://api.osuokayu.pw/v2/scores").strip(),
            score_id_min=env_int("SCORE_ID_MIN", 1),
            score_id_max=env_int("SCORE_ID_MAX", 0),
            replay_attempts=env_int("RANDOM_REPLAY_ATTEMPTS", 150),
            buffer_size=env_int("BUFFER_SIZE", 2),
            delete_after_play=env_bool("DELETE_AFTER_PLAY", True),
            producer_enabled=env_bool("PRODUCER_ENABLED", True),
            web_host=os.getenv("WEB_HOST", "127.0.0.1").strip(),
            web_port=env_int("WEB_PORT", 8787),
            max_upload_bytes=env_int("MAX_UPLOAD_MIB", 20) * 1024 * 1024,
            max_background_bytes=env_int("MAX_BACKGROUND_MIB", 20) * 1024 * 1024,
            initial_volume=env_int("PLAYER_VOLUME", 20),
            discord_enabled=env_bool("DISCORD_ENABLED", False),
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            discord_guild_id=env_int("DISCORD_GUILD_ID", 0),
            discord_channel_id=env_int("DISCORD_CHANNEL_ID", 0),
            discord_queue_limit=env_int("DISCORD_QUEUE_LIMIT", 50),
            discord_user_cooldown_seconds=env_int("DISCORD_USER_COOLDOWN_SECONDS", 300),
            discord_max_file_bytes=env_int("DISCORD_MAX_FILE_MIB", 10) * 1024 * 1024,
            danser_dir=danser_dir,
            danser_executable=danser_dir / "danser-cli",
            danser_songs_dir=danser_dir / "songs",
            danser_output_dir=danser_dir / "videos",
            danser_settings=os.getenv("DANSER_SETTINGS", "stream").strip(),
            danser_skin=os.getenv("DANSER_SKIN", "default").strip(),
            danser_render_timeout=env_int("DANSER_RENDER_TIMEOUT_SECONDS", 3600),
            danser_max_map_bytes=env_int("DANSER_MAX_MAP_MIB", 300) * 1024 * 1024,
        )
        if config.render_backend not in {"ordr", "danser"}:
            raise ValueError("RENDER_BACKEND must be either 'ordr' or 'danser'")
        if config.resolution not in {"960x540", "1280x720", "1920x1080", "3840x2160"}:
            raise ValueError(f"Unsupported ORDR_RESOLUTION: {config.resolution}")
        if config.buffer_size < 1:
            raise ValueError("BUFFER_SIZE must be at least 1")
        if config.score_id_min < 1:
            raise ValueError("SCORE_ID_MIN must be positive")
        if not 0 <= config.initial_volume <= 100:
            raise ValueError("PLAYER_VOLUME must be between 0 and 100")
        if config.max_background_bytes < 1024 * 1024:
            raise ValueError("MAX_BACKGROUND_MIB must be at least 1")
        if config.discord_enabled and not all(
            (config.discord_token, config.discord_guild_id, config.discord_channel_id)
        ):
            raise ValueError("DISCORD_TOKEN, DISCORD_GUILD_ID and DISCORD_CHANNEL_ID are required when Discord is enabled")
        if config.discord_queue_limit < 1:
            raise ValueError("DISCORD_QUEUE_LIMIT must be at least 1")
        if not config.danser_settings or "/" in config.danser_settings or "\\" in config.danser_settings:
            raise ValueError("DANSER_SETTINGS must be a simple settings profile name")
        if config.danser_render_timeout < 60:
            raise ValueError("DANSER_RENDER_TIMEOUT_SECONDS must be at least 60")
        if config.danser_max_map_bytes < 10 * 1024 * 1024:
            raise ValueError("DANSER_MAX_MAP_MIB must be at least 10")
        return config


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


def read_uleb128(stream: Any) -> int:
    value = 0
    shift = 0
    for _ in range(10):
        raw = stream.read(1)
        if len(raw) != 1:
            raise ValueError("Unexpected end of replay string length")
        byte = raw[0]
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value
        shift += 7
    raise ValueError("Invalid replay string length")


def skip_osu_string(stream: Any) -> None:
    marker = stream.read(1)
    if marker == b"\x00":
        return
    if marker != b"\x0b":
        raise ValueError("Invalid replay string marker")
    length = read_uleb128(stream)
    if length > 16 * 1024 * 1024 or len(stream.read(length)) != length:
        raise ValueError("Invalid replay string")


def parse_replay_header(path: Path) -> tuple[int, int]:
    with path.open("rb") as replay:
        raw_mode = replay.read(1)
        if len(raw_mode) != 1 or len(replay.read(4)) != 4:
            raise ValueError("Replay header is truncated")
        mode = raw_mode[0]
        for _ in range(3):
            skip_osu_string(replay)
        fixed = replay.read(12 + 4 + 2 + 1 + 4)
        if len(fixed) != 23:
            raise ValueError("Replay statistics are truncated")
        mods = struct.unpack_from("<I", fixed, 19)[0]
        return mode, mods


def replay_has_double_time(mods: int) -> bool:
    return bool(mods & ((1 << 6) | (1 << 9)))


def format_mods(mods: int) -> str:
    if mods == 0:
        return "NM"
    perfect = bool(mods & (1 << 14))
    nightcore = bool(mods & (1 << 9))
    definitions = [
        (1 << 0, "NF"),
        (1 << 1, "EZ"),
        (1 << 2, "TD"),
        (1 << 3, "HD"),
        (1 << 4, "HR"),
        (1 << 5, "SD"),
        (1 << 6, "DT"),
        (1 << 7, "RX"),
        (1 << 8, "HT"),
        (1 << 9, "NC"),
        (1 << 10, "FL"),
        (1 << 11, "AT"),
        (1 << 12, "SO"),
        (1 << 13, "AP"),
        (1 << 14, "PF"),
        (1 << 15, "4K"),
        (1 << 16, "5K"),
        (1 << 17, "6K"),
        (1 << 18, "7K"),
        (1 << 19, "8K"),
        (1 << 20, "FI"),
        (1 << 24, "9K"),
        (1 << 30, "MR"),
    ]
    names = []
    for bit, name in definitions:
        if name == "SD" and perfect:
            continue
        if name == "DT" and nightcore:
            continue
        if mods & bit:
            names.append(name)
    return "".join(names) or f"MODS({mods})"


class QueueStore:
    def __init__(self, config: Config):
        self.config = config
        self.queue_dir = ROOT / "video_queue"
        self.inbox_dir = ROOT / "inbox"
        self.work_dir = ROOT / "work"
        self.rejected_dir = ROOT / "rejected"
        self.archive_dir = ROOT / "archive"
        self.state_path = ROOT / "state.json"
        for directory in (
            self.queue_dir,
            self.inbox_dir,
            self.work_dir,
            self.rejected_dir,
            self.archive_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.state = read_json(
            self.state_path,
            {
                "sequence": 0,
                "current_video": None,
                "played_count": 0,
                "skipped_count": 0,
                "deleted_count": 0,
                "player_volume": config.initial_volume,
                "playback_revision": 0,
                "last_submit_unix": 0,
                "used_score_ids": [],
            },
        )
        self.lock = asyncio.Lock()
        self._repair_state()

    def _repair_state(self) -> None:
        current = self.state.get("current_video")
        if current and not (self.queue_dir / current).is_file():
            self.state["current_video"] = None
        file_sequences = []
        for video in self.video_files():
            prefix = video.name.split("-", 1)[0]
            if prefix.isdigit():
                file_sequences.append(int(prefix))
        self.state["sequence"] = max(int(self.state.get("sequence", 0)), *file_sequences, 0)
        self.state.setdefault("played_count", 0)
        self.state.setdefault("skipped_count", 0)
        self.state.setdefault("deleted_count", 0)
        self.state.setdefault("player_volume", self.config.initial_volume)
        self.state.setdefault("playback_revision", 0)
        self.state.setdefault("last_submit_unix", 0)
        self.state.setdefault("used_score_ids", [])
        self._save()

    def _save(self) -> None:
        write_json_atomic(self.state_path, self.state)

    def video_files(self) -> list[Path]:
        return sorted(self.queue_dir.glob("*.mp4"))

    def buffered_count(self) -> int:
        return len(self.video_files())

    def is_score_used(self, score_id: int) -> bool:
        return score_id in self.state.get("used_score_ids", [])

    async def remember_score(self, score_id: int) -> None:
        async with self.lock:
            used = self.state.setdefault("used_score_ids", [])
            if score_id not in used:
                used.append(score_id)
                del used[:-5000]
                self._save()

    async def set_last_submit(self, timestamp: float) -> None:
        async with self.lock:
            self.state["last_submit_unix"] = timestamp
            self._save()

    def seconds_until_submit(self) -> float:
        elapsed = time.time() - float(self.state.get("last_submit_unix", 0))
        return max(0.0, self.config.submit_interval - elapsed)

    async def enqueue(self, source: Path, metadata: dict[str, Any]) -> Path:
        async with self.lock:
            sequence = int(self.state.get("sequence", 0)) + 1
            self.state["sequence"] = sequence
            score = metadata.get("score_id") or "manual"
            render = metadata.get("render_id") or metadata.get("renderer") or "unknown"
            name = f"{sequence:06d}-score-{score}-render-{render}.mp4"
            destination = self.queue_dir / name
            source.replace(destination)
            write_json_atomic(destination.with_suffix(".json"), {**metadata, "name": name})
            self._save()
            return destination

    def _metadata_for(self, video: Path) -> dict[str, Any]:
        return read_json(video.with_suffix(".json"), {})

    async def get_next(self) -> dict[str, Any] | None:
        async with self.lock:
            current_name = self.state.get("current_video")
            current_path = self.queue_dir / current_name if current_name else None
            if not current_path or not current_path.is_file():
                videos = self.video_files()
                if not videos:
                    self.state["current_video"] = None
                    self._save()
                    return None
                current_path = videos[0]
                self.state["current_video"] = current_path.name
                self.state["playback_revision"] = int(self.state.get("playback_revision", 0)) + 1
                self._save()
            metadata = self._metadata_for(current_path)
            label = metadata.get("description") or metadata.get("label") or current_path.name
            return {
                "name": current_path.name,
                "label": label,
                "url": f"/video/{quote(current_path.name)}",
                "volume": int(self.state.get("player_volume", self.config.initial_volume)),
            }

    async def complete(self, name: str, reason: str) -> bool:
        async with self.lock:
            if self.state.get("current_video") != name:
                return False
            video = self.queue_dir / Path(name).name
            metadata = video.with_suffix(".json")
            if self.config.delete_after_play:
                video.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
            else:
                if video.exists():
                    video.replace(self.archive_dir / video.name)
                if metadata.exists():
                    metadata.replace(self.archive_dir / metadata.name)
            self.state["current_video"] = None
            if reason == "ended":
                self.state["played_count"] = int(self.state.get("played_count", 0)) + 1
            else:
                self.state["skipped_count"] = int(self.state.get("skipped_count", 0)) + 1
            self.state["last_completion_reason"] = reason
            self.state["playback_revision"] = int(self.state.get("playback_revision", 0)) + 1
            self._save()
            return True

    async def set_volume(self, volume: int) -> int:
        volume = max(0, min(100, int(volume)))
        async with self.lock:
            self.state["player_volume"] = volume
            self._save()
        return volume

    async def player_state(self) -> dict[str, Any]:
        async with self.lock:
            return {
                "current_video": self.state.get("current_video"),
                "volume": int(self.state.get("player_volume", self.config.initial_volume)),
                "revision": int(self.state.get("playback_revision", 0)),
            }

    async def remove_video(self, name: str, reason: str = "deleted") -> tuple[bool, bool]:
        safe_name = Path(name).name
        if safe_name != name or not safe_name.lower().endswith(".mp4"):
            return False, False
        async with self.lock:
            video = self.queue_dir / safe_name
            if not video.is_file():
                return False, False
            was_current = self.state.get("current_video") == safe_name
            video.unlink(missing_ok=True)
            video.with_suffix(".json").unlink(missing_ok=True)
            self.state["deleted_count"] = int(self.state.get("deleted_count", 0)) + 1
            self.state["last_completion_reason"] = reason
            if was_current:
                self.state["current_video"] = None
                self.state["skipped_count"] = int(self.state.get("skipped_count", 0)) + 1
                self.state["playback_revision"] = int(self.state.get("playback_revision", 0)) + 1
            self._save()
            return True, was_current

    async def skip_current(self) -> tuple[bool, str | None]:
        async with self.lock:
            current = self.state.get("current_video")
            if not current:
                return False, None
            video = self.queue_dir / Path(current).name
            video.unlink(missing_ok=True)
            video.with_suffix(".json").unlink(missing_ok=True)
            self.state["current_video"] = None
            self.state["skipped_count"] = int(self.state.get("skipped_count", 0)) + 1
            self.state["deleted_count"] = int(self.state.get("deleted_count", 0)) + 1
            self.state["last_completion_reason"] = "control-skip"
            self.state["playback_revision"] = int(self.state.get("playback_revision", 0)) + 1
            self._save()
            return True, current

    async def snapshot(self) -> list[dict[str, Any]]:
        async with self.lock:
            current = self.state.get("current_video")
            items = []
            for video in self.video_files():
                metadata = self._metadata_for(video)
                items.append(
                    {
                        "name": video.name,
                        "label": metadata.get("description") or metadata.get("label") or video.name,
                        "current": video.name == current,
                        "score_id": metadata.get("score_id"),
                        "render_id": metadata.get("render_id"),
                        "mods": metadata.get("mods"),
                        "skin": metadata.get("skin"),
                    }
                )
            return items


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


@dataclass
class ReplayCandidate:
    path: Path
    score_id: int | None
    manual: bool
    original_name: str
    mods: int = 0
    mods_label: str = "NM"
    submission_id: int | None = None


class ReplaySource:
    def __init__(
        self,
        config: Config,
        store: QueueStore,
        session: aiohttp.ClientSession,
        submissions: SubmissionStore,
    ):
        self.config = config
        self.store = store
        self.session = session
        self.submissions = submissions
        self.score_pool: list[int] = []
        self.discovered_max_id = config.score_id_max

    async def _take_submission(self) -> ReplayCandidate | None:
        while row := await self.submissions.claim_next():
            original = Path(row["stored_path"])
            if not original.is_file():
                await self.submissions.mark_failed(int(row["id"]), "Stored replay file is missing")
                continue
            destination = self.store.work_dir / f"submission-{row['id']}-{uuid.uuid4().hex[:8]}.osr"
            try:
                shutil.copy2(original, destination)
            except OSError as error:
                await self.submissions.mark_failed(int(row["id"]), f"Could not prepare replay: {error}")
                continue
            logging.info(
                "Using Discord submission #%d from %s (%s)",
                row["id"],
                row["external_user_name"],
                row["mods_label"],
            )
            return ReplayCandidate(
                path=destination,
                score_id=None,
                manual=False,
                original_name=row["original_name"],
                mods=int(row["mods"]),
                mods_label=row["mods_label"],
                submission_id=int(row["id"]),
            )
        return None

    def _take_manual(self) -> ReplayCandidate | None:
        files = sorted(self.store.inbox_dir.glob("*.osr"), key=lambda item: item.stat().st_mtime)
        if not files:
            return None
        source = files[0]
        destination = self.store.work_dir / f"manual-{uuid.uuid4().hex[:10]}-{source.name}"
        source.replace(destination)
        try:
            mode, mods = parse_replay_header(destination)
            if mode != 0:
                logging.warning("Manual replay %s uses unsupported mode %d", source.name, mode)
        except (OSError, ValueError) as error:
            logging.warning("Could not parse mods from manual replay %s: %s", source.name, error)
            mods = 0
        return ReplayCandidate(destination, None, True, source.name, mods, format_mods(mods))

    @staticmethod
    def _score_ids(payload: Any) -> list[int]:
        collections: list[Any] = []
        if isinstance(payload, list):
            collections.append(payload)
        elif isinstance(payload, dict):
            for key in ("scores", "data", "items", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    collections.append(value)
                elif isinstance(value, dict):
                    for nested_key in ("scores", "data", "items", "results"):
                        nested = value.get(nested_key)
                        if isinstance(nested, list):
                            collections.append(nested)
        result: list[int] = []
        for collection in collections:
            for record in collection:
                if not isinstance(record, dict):
                    continue
                value = record.get("id", record.get("score_id"))
                if isinstance(value, int) and value > 0:
                    result.append(value)
        return list(dict.fromkeys(result))

    @staticmethod
    def _total_scores(payload: Any) -> int | None:
        if not isinstance(payload, dict):
            return None
        containers = [payload]
        for key in ("pagination", "meta", "metadata"):
            value = payload.get(key)
            if isinstance(value, dict):
                containers.append(value)
        for container in containers:
            for key in ("total", "total_count", "max_scores", "count"):
                value = container.get(key)
                if isinstance(value, int) and value > 0:
                    return value
        return None

    async def _scores_page(self, page: int, page_size: int) -> Any:
        timeout = aiohttp.ClientTimeout(total=30)
        async with self.session.get(
            self.config.scores_url,
            params={"mode": "0", "status": "2", "page": str(page), "page_size": str(page_size)},
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            return await response.json(content_type=None)

    async def _refill_score_pool(self) -> None:
        page_size = 100
        first = await self._scores_page(1, page_size)
        first_ids = self._score_ids(first)
        if first_ids:
            self.discovered_max_id = max(self.discovered_max_id, max(first_ids))
        total = self._total_scores(first)
        payload = first
        if total and total > page_size:
            page = random.randint(1, max(1, (total + page_size - 1) // page_size))
            if page != 1:
                payload = await self._scores_page(page, page_size)
        ids = self._score_ids(payload) or first_ids
        random.shuffle(ids)
        self.score_pool.extend(score_id for score_id in ids if not self.store.is_score_used(score_id))
        logging.info("Loaded %d score IDs from osuokayu", len(ids))

    async def _download_replay(self, score_id: int) -> Path | None:
        timeout = aiohttp.ClientTimeout(total=45)
        try:
            async with self.session.get(
                self.config.replay_url,
                params={"id": str(score_id), "include_headers": "true"},
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    return None
                if int(response.headers.get("Content-Length", "0") or 0) > self.config.max_upload_bytes:
                    return None
                body = await response.read()
                content_type = response.headers.get("Content-Type", "").lower()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise
        if len(body) < 32 or "json" in content_type or body.lstrip().startswith((b"{", b"[")):
            return None
        destination = self.store.work_dir / f"score-{score_id}-{uuid.uuid4().hex[:8]}.osr"
        destination.write_bytes(body)
        return destination

    async def next(self) -> ReplayCandidate:
        submission = await self._take_submission()
        if submission:
            return submission
        manual = self._take_manual()
        if manual:
            logging.info("Using manually supplied replay: %s", manual.original_name)
            return manual
        if not self.score_pool:
            try:
                await self._refill_score_pool()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                detail = str(error) or type(error).__name__
                logging.warning("Could not query the score list: %s", detail)
        seen: set[int] = set()
        for attempt in range(self.config.replay_attempts):
            if not self.score_pool and attempt > 0 and attempt % 25 == 0:
                try:
                    await self._refill_score_pool()
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    pass
            if self.score_pool:
                score_id = self.score_pool.pop()
            else:
                maximum = self.config.score_id_max or self.discovered_max_id
                if maximum < self.config.score_id_min:
                    raise RuntimeError("Cannot discover the maximum score ID while the osuokayu API is unavailable")
                score_id = random.randint(self.config.score_id_min, maximum)
            if score_id in seen or self.store.is_score_used(score_id):
                continue
            seen.add(score_id)
            path = await self._download_replay(score_id)
            if not path:
                continue
            try:
                mode, mods = parse_replay_header(path)
            except (OSError, ValueError) as error:
                logging.warning("Could not parse replay %d: %s", score_id, error)
                path.unlink(missing_ok=True)
                continue
            if mode != 0:
                path.unlink(missing_ok=True)
                continue
            await self.store.remember_score(score_id)
            mods_label = format_mods(mods)
            logging.info("Selected osuokayu score %d (%s)", score_id, mods_label)
            return ReplayCandidate(path, score_id, False, path.name, mods, mods_label)
        raise RuntimeError(f"No downloadable standard replay found after {self.config.replay_attempts} attempts")


class RenderRequestError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class OrdrClient:
    api_base = "https://apis.issou.best"

    def __init__(
        self,
        config: Config,
        store: QueueStore,
        runtime: RuntimeState,
        session: aiohttp.ClientSession,
    ):
        self.config = config
        self.store = store
        self.runtime = runtime
        self.session = session
        self.sio = socketio.AsyncClient(reconnection=True, logger=False, engineio_logger=False)
        self.outcomes: OrderedDict[int, tuple[str, dict[str, Any]]] = OrderedDict()
        self.waiters: dict[int, asyncio.Event] = {}
        self._register_socket_handlers()

    def _remember_outcome(self, render_id: int, kind: str, data: dict[str, Any]) -> None:
        self.outcomes[render_id] = (kind, data)
        self.outcomes.move_to_end(render_id)
        while len(self.outcomes) > 100:
            self.outcomes.popitem(last=False)
        waiter = self.waiters.get(render_id)
        if waiter:
            waiter.set()

    def _register_socket_handlers(self) -> None:
        @self.sio.event
        async def connect() -> None:
            self.runtime.websocket_connected = True
            logging.info("Connected to o!rdr WebSocket")
            if self.config.api_key and not self.config.developer_mode:
                await self.sio.emit("bot_auth", self.config.api_key)

        @self.sio.event
        async def disconnect(reason: Any = None) -> None:
            self.runtime.websocket_connected = False
            logging.warning("o!rdr WebSocket disconnected: %s", reason)

        @self.sio.event
        async def connect_error(data: Any) -> None:
            self.runtime.websocket_connected = False
            logging.warning("o!rdr WebSocket connection error: %s", data)

        @self.sio.on("bot_auth")
        async def bot_auth(data: Any) -> None:
            logging.info("o!rdr authentication: %s", data)

        @self.sio.on("render_progress_json")
        async def render_progress(data: dict[str, Any]) -> None:
            render_id = int(data.get("renderID", 0) or 0)
            if render_id == self.runtime.current_render_id:
                progress = data.get("progress") or data.get("description") or "Rendering"
                self.runtime.render_progress = str(progress)

        @self.sio.on("render_done_json")
        async def render_done(data: dict[str, Any]) -> None:
            render_id = int(data.get("renderID", 0) or 0)
            if render_id and (render_id == self.runtime.current_render_id or render_id in self.waiters):
                self._remember_outcome(render_id, "done", data)

        @self.sio.on("render_failed_json")
        async def render_failed(data: dict[str, Any]) -> None:
            render_id = int(data.get("renderID", 0) or 0)
            if render_id and (render_id == self.runtime.current_render_id or render_id in self.waiters):
                self._remember_outcome(render_id, "failed", data)

    async def connect(self) -> None:
        if self.sio.connected:
            return
        try:
            await self.sio.connect(
                self.api_base,
                socketio_path="ordr/ws",
                wait_timeout=25,
            )
        except Exception as error:
            self.runtime.websocket_connected = False
            logging.warning("WebSocket unavailable; REST polling will be used: %s", error)

    async def close(self) -> None:
        if self.sio.connected:
            await self.sio.disconnect()

    async def _rate_limit(self) -> None:
        delay = self.store.seconds_until_submit()
        if delay > 0:
            self.runtime.phase = f"rate limit: waiting {int(delay) + 1}s"
            logging.info("o!rdr rate limit: waiting %d seconds before the next submission", int(delay) + 1)
            await asyncio.sleep(delay)

    async def submit(self, candidate: ReplayCandidate) -> int:
        use_dt_skin = replay_has_double_time(candidate.mods)
        selected_skin = self.config.dt_skin if use_dt_skin else self.config.skin
        selected_custom_skin = self.config.dt_custom_skin if use_dt_skin else self.config.custom_skin
        logging.info("Selected o!rdr skin %s for mods %s", selected_skin, candidate.mods_label)
        fields = {
            "skin": selected_skin,
            "resolution": self.config.resolution,
            "customSkin": str(selected_custom_skin).lower(),
            "globalVolume": "50",
            "musicVolume": "50",
            "hitsoundVolume": "50",
            "showHitErrorMeter": "true",
            "showUnstableRate": "true",
            "showScore": "true",
            "showHPBar": "true",
            "showComboCounter": "true",
            "showPPCounter": "true",
            "showMods": "true",
            "showResultScreen": "true",
            "showKeyOverlay": "true",
            "useSkinCursor": "true",
            "useSkinHitsounds": "true",
            "useBeatmapColors": "true",
            "loadStoryboard": "true",
            "loadVideo": "true",
            "skip": "true",
            "cursorTrail": "true",
            "sliderSnakingIn": "true",
            "sliderSnakingOut": "false",
            "playNightcoreSamples": "true",
            "ignoreFail": "false",
        }
        verification_key = "devmode_success" if self.config.developer_mode else self.config.api_key
        if verification_key:
            fields["verificationKey"] = verification_key
        while True:
            await self._rate_limit()
            form = aiohttp.FormData()
            for key, value in fields.items():
                form.add_field(key, value)
            self.runtime.phase = "submitting replay to o!rdr"
            try:
                with candidate.path.open("rb") as replay:
                    form.add_field(
                        "replayFile",
                        replay,
                        filename=candidate.original_name,
                        content_type="application/octet-stream",
                    )
                    timeout = aiohttp.ClientTimeout(total=90)
                    async with self.session.post(
                        f"{self.api_base}/ordr/renders",
                        data=form,
                        timeout=timeout,
                    ) as response:
                        text = await response.text()
                        retry_after_header = response.headers.get("Retry-After")
                        try:
                            payload = json.loads(text)
                        except json.JSONDecodeError:
                            payload = {"message": text[:500]}
                        response_status = response.status
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                raise RenderRequestError(f"o!rdr request failed: {error}") from error
            code = payload.get("errorCode")
            message = str(payload.get("message") or payload.get("errorMessage") or payload)
            rate_limited = response_status == 429 or "too many requests" in message.lower()
            if rate_limited:
                try:
                    retry_delay = max(60, int(float(retry_after_header)) + 2)
                except (TypeError, ValueError):
                    retry_delay = 60
                self.runtime.phase = f"o!rdr rate limited: retrying in {retry_delay}s"
                logging.warning("o!rdr rate limited the request; retrying the same replay in %d seconds", retry_delay)
                await asyncio.sleep(retry_delay)
                continue
            if response_status != 201 or code not in {0, None}:
                raise RenderRequestError(message, int(code) if isinstance(code, int) else None, response_status)
            render_id = int(payload["renderID"])
            await self.store.set_last_submit(time.time())
            self.runtime.current_render_id = render_id
            self.runtime.render_progress = "Queued"
            logging.info("o!rdr accepted render %d%s", render_id, " (developer mode)" if self.config.developer_mode else "")
            return render_id

    async def _poll(self, render_id: int) -> tuple[str, dict[str, Any]] | None:
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with self.session.get(
                f"{self.api_base}/ordr/renders",
                params={"renderID": str(render_id), "pageSize": "1"},
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None
        renders = payload.get("renders", []) if isinstance(payload, dict) else []
        if not renders:
            return None
        record = renders[0]
        error_code = int(record.get("errorCode", 0) or 0)
        if error_code:
            return "failed", {
                "renderID": render_id,
                "errorCode": error_code,
                "errorMessage": record.get("progress") or "Render failed",
            }
        progress = str(record.get("progress", ""))
        self.runtime.render_progress = progress
        if record.get("videoUrl") and progress.lower().startswith("done"):
            return "done", {
                "renderID": render_id,
                "videoUrl": record.get("videoUrl"),
                "description": record.get("description"),
                "title": record.get("title"),
                "replayMods": record.get("replayMods"),
                "skin": record.get("skin"),
            }
        return None

    async def wait(self, render_id: int) -> dict[str, Any]:
        event = self.waiters.setdefault(render_id, asyncio.Event())
        deadline = asyncio.get_running_loop().time() + self.config.render_timeout
        self.runtime.phase = f"waiting for render {render_id}"
        try:
            while True:
                outcome = self.outcomes.pop(render_id, None)
                if outcome:
                    kind, data = outcome
                    if kind == "failed":
                        code = data.get("errorCode")
                        message = data.get("errorMessage") or "Render failed"
                        raise RenderRequestError(str(message), int(code) if isinstance(code, int) else None)
                    details = await self._poll(render_id)
                    if details and details[0] == "done":
                        return {**data, **details[1]}
                    return data
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"Render {render_id} did not finish in time")
                try:
                    await asyncio.wait_for(event.wait(), timeout=min(15, remaining))
                    event.clear()
                except asyncio.TimeoutError:
                    polled = await self._poll(render_id)
                    if polled:
                        self._remember_outcome(render_id, polled[0], polled[1])
        finally:
            self.waiters.pop(render_id, None)

    async def _dynamic_link(self, render_id: int) -> str | None:
        headers = {}
        if self.config.api_key and not self.config.developer_mode:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with self.session.get(
                f"{self.api_base}/dynlink/ordr/gen",
                params={"id": str(render_id)},
                headers=headers,
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    logging.warning("Dynamic-link endpoint returned HTTP %d", response.status)
                    return None
                payload = await response.json(content_type=None)
                return payload.get("url")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            logging.warning("Could not generate dynamic download link: %s", error)
            return None

    async def _download_url(self, url: str, destination: Path, depth: int = 0) -> bool:
        if depth > 2:
            return False
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=180)
        try:
            async with self.session.get(url, timeout=timeout, allow_redirects=True) as response:
                if response.status != 200:
                    logging.warning("Video URL returned HTTP %d: %s", response.status, url)
                    return False
                content_type = response.headers.get("Content-Type", "").lower()
                length = int(response.headers.get("Content-Length", "0") or 0)
                final_url = str(response.url)
                looks_binary = (
                    "video/" in content_type
                    or "application/octet-stream" in content_type
                    or final_url.lower().split("?", 1)[0].endswith(".mp4")
                    or (length > 1_000_000 and "text/" not in content_type and "json" not in content_type)
                )
                if looks_binary:
                    with destination.open("wb") as output:
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            output.write(chunk)
                    return destination.stat().st_size > 1_000_000
                page = await response.text(errors="replace")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            logging.warning("Video download failed: %s", error)
            return False
        matches = re.findall(r"https?://[^\"'<>\s]+\.mp4(?:\?[^\"'<>\s]*)?", html.unescape(page), re.I)
        for match in matches:
            if await self._download_url(match, destination, depth + 1):
                return True
        return False

    async def _ffprobe(self, path: Path) -> tuple[bool, float]:
        try:
            process = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-select_streams",
                "v:0",
                "-of",
                "json",
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return True, 0
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            return False, 0
        try:
            payload = json.loads(stdout)
            duration = float(payload.get("format", {}).get("duration", 0) or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            return False, 0
        return duration > 0, duration

    async def download(self, render_id: int, outcome: dict[str, Any]) -> tuple[Path, float]:
        destination = self.store.work_dir / f"render-{render_id}-{uuid.uuid4().hex[:8]}.mp4.part"
        dynamic_url = await self._dynamic_link(render_id)
        candidates = [url for url in (dynamic_url, outcome.get("videoUrl")) if url]
        if not candidates:
            polled = await self._poll(render_id)
            if polled and polled[0] == "done" and polled[1].get("videoUrl"):
                candidates.append(polled[1]["videoUrl"])
        for attempt in range(1, 4):
            for url in dict.fromkeys(candidates):
                destination.unlink(missing_ok=True)
                self.runtime.phase = f"downloading render {render_id}"
                if not await self._download_url(str(url), destination):
                    continue
                valid, duration = await self._ffprobe(destination)
                if valid:
                    logging.info("Downloaded render %d (%.1fs, %.1f MiB)", render_id, duration, destination.stat().st_size / 1048576)
                    return destination, duration
                logging.warning("Downloaded file for render %d is not a valid video", render_id)
            if attempt < 3:
                await asyncio.sleep(10 * attempt)
                refreshed = await self._dynamic_link(render_id)
                if refreshed:
                    candidates.insert(0, refreshed)
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"Could not download a valid video for render {render_id}")


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
                    await self.submissions.mark_ready(candidate.submission_id, queued.name)
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
                message = str(error)
                logging.error("Producer error: %s", message)
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
                skin = config.danser_skin
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
    required = [
        ROOT / "static" / "player.html",
        ROOT / "static" / "control.html",
        ROOT / "static" / "fallback.svg",
    ]
    if config.render_backend == "danser":
        required.extend(
            [
                config.danser_executable,
                config.danser_dir / "settings" / f"{config.danser_settings}.json",
                config.danser_dir / "assets.dpak",
            ]
        )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise SystemExit("Missing files: " + ", ".join(missing))
    ffprobe = shutil.which("ffprobe")
    print(f"Project:        {ROOT}")
    print(f"Python:         {os.sys.version.split()[0]}")
    print(f"ffprobe:        {ffprobe or 'missing'}")
    print(f"Player URL:     http://{config.web_host}:{config.web_port}/")
    print(f"Control URL:    http://{config.web_host}:{config.web_port}/control")
    print(f"Render backend: {config.render_backend}")
    if config.render_backend == "ordr":
        print(f"o!rdr mode:     {'developer simulation' if config.developer_mode else 'real renders'}")
        print(f"o!rdr API key:  {'configured' if config.api_key else 'not configured'}")
    else:
        print(f"danser:         {config.danser_executable}")
        print("danser output:  1280x720 @ 60 FPS")
    print(f"Buffer target:  {config.buffer_size}")
    print(f"Discord intake: {'enabled' if config.discord_enabled else 'disabled'}")
    print("Configuration check passed")


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
