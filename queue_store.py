import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from config import Config


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json_atomic(path: Path, data: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class QueueStore:
    def __init__(self, config: Config):
        self.config = config
        self.queue_dir = config.root / "video_queue"
        self.inbox_dir = config.root / "inbox"
        self.work_dir = config.root / "work"
        self.rejected_dir = config.root / "rejected"
        self.archive_dir = config.root / "archive"
        self.state_path = config.root / "state.json"
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

    @property
    def ready_videos(self) -> list[Path]:
        return self.video_files()

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
            label = metadata.get("label") or metadata.get("description") or current_path.name
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
            try:
                video.unlink(missing_ok=True)
                video.with_suffix(".json").unlink(missing_ok=True)
            except OSError as error:
                logging.warning("Could not immediately remove deleted video file %s: %s", video.name, error)
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
            try:
                video.unlink(missing_ok=True)
                video.with_suffix(".json").unlink(missing_ok=True)
            except OSError as error:
                logging.warning("Could not immediately remove skipped video file %s: %s", video.name, error)
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
                        "label": metadata.get("label") or metadata.get("description") or video.name,
                        "current": video.name == current,
                        "score_id": metadata.get("score_id"),
                        "render_id": metadata.get("render_id"),
                        "mods": metadata.get("mods"),
                        "skin": metadata.get("skin"),
                    }
                )
            return items
