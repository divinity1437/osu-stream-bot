import logging
import random
import shutil
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from config import Config
from queue_store import QueueStore
from submission_store import SubmissionStore


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
        self.score_total: int | None = None
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
                    break
        collected: list[int] = []
        for collection in collections:
            for item in collection:
                if isinstance(item, dict):
                    candidate = item.get("id") or item.get("score_id")
                    if isinstance(candidate, int) and candidate > 0:
                        collected.append(candidate)
                elif isinstance(item, int) and item > 0:
                    collected.append(item)
        return collected

    async def _refill_score_pool(self) -> None:
        async with self.session.get(self.config.scores_url, timeout=aiohttp.ClientTimeout(total=20)) as response:
            if response.status != 200:
                raise ValueError(f"scores endpoint HTTP {response.status}")
            payload = await response.json(content_type=None)
        ids = self._score_ids(payload)
        if not ids:
            raise ValueError("No score entries found in the API response")
        highest = max(ids)
        self.discovered_max_id = max(self.discovered_max_id, highest)
        fresh = [score_id for score_id in ids if not self.store.is_score_used(score_id)]
        random.shuffle(fresh)
        self.score_pool = fresh
        logging.info("Discovered %d scores from osuokayu (highest seen: %d)", len(ids), self.discovered_max_id)

    async def _download_replay(self, score_id: int) -> Path | None:
        destination = self.store.work_dir / f"score-{score_id}-{uuid.uuid4().hex[:8]}.osr"
        try:
            async with self.session.get(
                self.config.replay_url,
                params={"id": score_id},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    return None
                data = await response.read()
                if len(data) < 64:
                    return None
                destination.write_bytes(data)
                return destination
        except (aiohttp.ClientError, OSError):
            destination.unlink(missing_ok=True)
            return None

    async def next(self) -> ReplayCandidate:
        submission = await self._take_submission()
        if submission:
            return submission
        manual = self._take_manual()
        if manual:
            logging.info("Using manually supplied replay: %s", manual.original_name)
            return manual
        seen: set[int] = set()
        for attempt in range(self.config.replay_attempts):
            if not self.score_pool:
                try:
                    await self._refill_score_pool()
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                    if attempt == 0:
                        detail = str(error) or type(error).__name__
                        logging.warning("Could not query the score list: %s", detail)
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
