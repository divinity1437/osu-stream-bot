import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import time
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import aiohttp


REPLAY_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
PROGRESS_RE = re.compile(r"Progress:\s*(\d+)%", re.IGNORECASE)


class DanserError(RuntimeError):
    pass


@dataclass(frozen=True)
class BeatmapInfo:
    checksum: str
    set_id: int | None
    beatmap_id: int | None
    artist: str
    title: str
    difficulty: str
    path: Path | None = None

    @property
    def description(self) -> str:
        name = " - ".join(part for part in (self.artist, self.title) if part)
        if self.difficulty:
            name = f"{name} [{self.difficulty}]" if name else f"[{self.difficulty}]"
        return name or f"osu! replay {self.checksum[:8]}"


@dataclass(frozen=True)
class DanserResult:
    path: Path
    duration: float
    description: str
    skin: str
    beatmap_set_id: int | None
    beatmap_id: int | None


def _read_uleb128(stream: Any) -> int:
    value = 0
    shift = 0
    for _ in range(10):
        raw = stream.read(1)
        if len(raw) != 1:
            raise DanserError("Replay header ends before the beatmap checksum")
        byte = raw[0]
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value
        shift += 7
    raise DanserError("Replay contains an invalid string length")


def _read_osu_string(stream: Any) -> str:
    marker = stream.read(1)
    if marker == b"\x00":
        return ""
    if marker != b"\x0b":
        raise DanserError("Replay contains an invalid string marker")
    length = _read_uleb128(stream)
    if length > 1024:
        raise DanserError("Replay beatmap checksum is unexpectedly large")
    value = stream.read(length)
    if len(value) != length:
        raise DanserError("Replay header ends inside the beatmap checksum")
    return value.decode("utf-8", "replace")


def replay_beatmap_checksum(path: Path) -> str:
    try:
        with path.open("rb") as replay:
            if len(replay.read(5)) != 5:
                raise DanserError("Replay header is truncated")
            checksum = _read_osu_string(replay).strip().lower()
    except OSError as error:
        raise DanserError(f"Could not read replay: {error}") from error
    if not REPLAY_HASH_RE.fullmatch(checksum):
        raise DanserError("Replay does not contain a valid beatmap MD5 checksum")
    return checksum


def _file_md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _osu_metadata(path: Path, checksum: str) -> BeatmapInfo:
    values: dict[str, str] = {}
    section = ""
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as beatmap:
            for raw_line in beatmap:
                line = raw_line.strip()
                if line.startswith("[") and line.endswith("]"):
                    section = line
                    continue
                if section != "[Metadata]" or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                values[key.strip()] = value.strip()
    except OSError:
        pass

    def integer(name: str) -> int | None:
        try:
            value = int(values.get(name, ""))
            return value if value > 0 else None
        except ValueError:
            return None

    return BeatmapInfo(
        checksum=checksum,
        set_id=integer("BeatmapSetID"),
        beatmap_id=integer("BeatmapID"),
        artist=values.get("ArtistUnicode") or values.get("Artist", ""),
        title=values.get("TitleUnicode") or values.get("Title", ""),
        difficulty=values.get("Version", ""),
        path=path,
    )


class BeatmapLibrary:
    def __init__(self, config: Any, session: aiohttp.ClientSession, runtime: Any):
        self.session = session
        self.runtime = runtime
        self.songs_dir = config.danser_songs_dir
        self.work_dir = config.danser_dir / "map-downloads"
        self.max_archive_bytes = config.danser_max_map_bytes
        self.songs_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self._index: dict[str, Path] = {}
        self._index_loaded = False

    def _scan_library(self) -> None:
        index: dict[str, Path] = {}
        for path in self.songs_dir.rglob("*.osu"):
            try:
                index[_file_md5(path)] = path
            except OSError:
                continue
        self._index = index
        self._index_loaded = True

    async def _local(self, checksum: str) -> BeatmapInfo | None:
        if not self._index_loaded:
            await asyncio.to_thread(self._scan_library)
        path = self._index.get(checksum)
        if path and path.is_file():
            return await asyncio.to_thread(_osu_metadata, path, checksum)
        return None

    @staticmethod
    def _from_hinai(payload: Any, checksum: str) -> BeatmapInfo | None:
        if not isinstance(payload, dict):
            return None
        children = payload.get("ChildrenBeatmaps")
        if not isinstance(children, list):
            return None
        child = next(
            (
                item
                for item in children
                if isinstance(item, dict) and str(item.get("FileMD5", "")).lower() == checksum
            ),
            None,
        )
        if not child:
            return None
        try:
            set_id = int(payload["SetID"])
        except (KeyError, TypeError, ValueError):
            return None
        try:
            beatmap_id = int(child.get("BeatmapID"))
        except (TypeError, ValueError):
            beatmap_id = None
        return BeatmapInfo(
            checksum=checksum,
            set_id=set_id,
            beatmap_id=beatmap_id,
            artist=str(payload.get("Artist") or ""),
            title=str(payload.get("Title") or ""),
            difficulty=str(child.get("DiffName") or ""),
        )

    @staticmethod
    def _from_mirria(payload: Any, checksum: str) -> BeatmapInfo | None:
        if not isinstance(payload, dict):
            return None
        children = payload.get("beatmaps")
        if not isinstance(children, list):
            return None
        child = next(
            (
                item
                for item in children
                if isinstance(item, dict) and str(item.get("checksum", "")).lower() == checksum
            ),
            None,
        )
        if not child:
            return None
        try:
            set_id = int(payload["id"])
        except (KeyError, TypeError, ValueError):
            return None
        try:
            beatmap_id = int(child.get("id"))
        except (TypeError, ValueError):
            beatmap_id = None
        return BeatmapInfo(
            checksum=checksum,
            set_id=set_id,
            beatmap_id=beatmap_id,
            artist=str(payload.get("artist") or ""),
            title=str(payload.get("title") or ""),
            difficulty=str(child.get("version") or ""),
        )

    async def _lookup(self, checksum: str) -> BeatmapInfo:
        lookups = (
            (
                f"https://mirror.hinamizawa.ai/api/md5/{checksum}/set",
                self._from_hinai,
                "hinai",
            ),
            (
                f"https://mirror.lisek.cc/api/v1/beatmaps/md5/{checksum}",
                self._from_mirria,
                "Mirria",
            ),
        )
        errors: list[str] = []
        timeout = aiohttp.ClientTimeout(total=45, sock_connect=15)
        for url, parser, name in lookups:
            try:
                async with self.session.get(url, timeout=timeout) as response:
                    if response.status == 404:
                        errors.append(f"{name}: not found")
                        continue
                    if response.status != 200:
                        errors.append(f"{name}: HTTP {response.status}")
                        continue
                    payload = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                errors.append(f"{name}: {error}")
                continue
            info = parser(payload, checksum)
            if info and info.set_id:
                logging.info(
                    "Resolved beatmap %s to set %d through %s (%s)",
                    checksum[:8],
                    info.set_id,
                    name,
                    info.description,
                )
                return info
            errors.append(f"{name}: invalid metadata")
        raise DanserError(
            f"Could not find the beatmap for replay checksum {checksum}. "
            f"Mirror responses: {'; '.join(errors)}"
        )

    async def _download(self, url: str, destination: Path, source_name: str) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
        size = 0
        try:
            async with self.session.get(url, timeout=timeout, allow_redirects=True) as response:
                if response.status != 200:
                    raise DanserError(f"{source_name} returned HTTP {response.status}")
                declared = int(response.headers.get("Content-Length", "0") or 0)
                if declared > self.max_archive_bytes:
                    raise DanserError(f"{source_name} archive exceeds the configured size limit")
                with destination.open("wb") as output:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        size += len(chunk)
                        if size > self.max_archive_bytes:
                            raise DanserError(f"{source_name} archive exceeds the configured size limit")
                        output.write(chunk)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            raise DanserError(f"{source_name} download failed: {error}") from error
        if size < 1024 or not zipfile.is_zipfile(destination):
            raise DanserError(f"{source_name} did not return a valid .osz archive")

    def _extract(self, archive: Path, set_id: int, checksum: str) -> Path:
        temporary = self.songs_dir / f".import-{set_id}-{uuid.uuid4().hex}"
        temporary.mkdir(parents=False)
        try:
            with zipfile.ZipFile(archive) as source:
                members = source.infolist()
                if len(members) > 10000:
                    raise DanserError("Beatmap archive contains too many files")
                unpacked_limit = min(max(self.max_archive_bytes * 3, 512 * 1024 * 1024), 1024 * 1024 * 1024)
                if sum(member.file_size for member in members) > unpacked_limit:
                    raise DanserError("Beatmap archive expands beyond the configured safety limit")
                root = temporary.resolve()
                for member in members:
                    normalized = member.filename.replace("\\", "/")
                    relative = PurePosixPath(normalized)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise DanserError("Beatmap archive contains an unsafe path")
                    if stat.S_ISLNK(member.external_attr >> 16):
                        raise DanserError("Beatmap archive contains a symbolic link")
                    destination = temporary.joinpath(*relative.parts)
                    if root not in (destination.resolve(), *destination.resolve().parents):
                        raise DanserError("Beatmap archive attempts to escape the songs directory")
                    if member.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(member) as incoming, destination.open("wb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing, 1024 * 1024)

            matched: Path | None = None
            for beatmap in temporary.rglob("*.osu"):
                current_hash = _file_md5(beatmap)
                if current_hash == checksum:
                    matched = beatmap
            if matched is None:
                raise DanserError("Downloaded beatmapset does not contain the replay's exact map revision")

            destination = self.songs_dir / str(set_id)
            if destination.exists():
                destination = self.songs_dir / f"{set_id}-{checksum[:8]}"
            if destination.exists():
                shutil.rmtree(temporary)
                existing = next(
                    (path for path in destination.rglob("*.osu") if _file_md5(path) == checksum),
                    None,
                )
                if existing is None:
                    raise DanserError("Beatmap destination already exists but contains a different revision")
                return existing

            relative_match = matched.relative_to(temporary)
            temporary.replace(destination)
            return destination / relative_match
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    async def ensure(self, checksum: str) -> BeatmapInfo:
        async with self.lock:
            local = await self._local(checksum)
            if local:
                logging.info("Beatmap %s is already available locally: %s", checksum[:8], local.path)
                return local

            info = await self._lookup(checksum)
            assert info.set_id is not None
            self.runtime.phase = f"downloading beatmap set {info.set_id}"
            candidates = (
                (f"https://mirror.lisek.cc/d/{info.set_id}?video=false", "Mirria"),
                (f"https://mirror.hinamizawa.ai/api/v1/hinai/d/{info.set_id}", "hinai"),
                (f"https://catboy.best/d/{info.set_id}?nv=1", "Mino"),
            )
            errors: list[str] = []
            for url, source_name in candidates:
                archive = self.work_dir / f"{info.set_id}-{uuid.uuid4().hex}.osz.part"
                try:
                    await self._download(url, archive, source_name)
                    path = await asyncio.to_thread(self._extract, archive, info.set_id, checksum)
                    await asyncio.to_thread(self._scan_library)
                    logging.info(
                        "Downloaded beatmap set %d through %s (%.1f MiB)",
                        info.set_id,
                        source_name,
                        archive.stat().st_size / 1048576,
                    )
                    return BeatmapInfo(
                        checksum=info.checksum,
                        set_id=info.set_id,
                        beatmap_id=info.beatmap_id,
                        artist=info.artist,
                        title=info.title,
                        difficulty=info.difficulty,
                        path=path,
                    )
                except (DanserError, OSError, zipfile.BadZipFile) as error:
                    logging.warning("Beatmap download from %s failed: %s", source_name, error)
                    errors.append(f"{source_name}: {error}")
                finally:
                    archive.unlink(missing_ok=True)
            raise DanserError(f"Could not download beatmap set {info.set_id}: {'; '.join(errors)}")


async def probe_video(path: Path) -> tuple[bool, float]:
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-select_streams",
            "v:0",
            "-of",
            "json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, 0
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        return False, 0
    try:
        payload = json.loads(stdout)
        duration = float(payload.get("format", {}).get("duration", 0) or 0)
        has_video = any(stream.get("codec_type") == "video" for stream in payload.get("streams", []))
    except (json.JSONDecodeError, TypeError, ValueError):
        return False, 0
    return has_video and duration > 0, duration


class DanserRenderer:
    def __init__(self, config: Any, runtime: Any, session: aiohttp.ClientSession):
        self.config = config
        self.runtime = runtime
        self.library = BeatmapLibrary(config, session, runtime)
        self.log_path = config.root / "logs" / "danser.log"
        self.log_path.parent.mkdir(exist_ok=True)

    async def prepare(self, candidate: Any) -> BeatmapInfo:
        checksum = replay_beatmap_checksum(candidate.path)
        self.runtime.phase = f"checking beatmap {checksum[:8]}"
        return await self.library.ensure(checksum)

    async def render(self, candidate: Any, beatmap: BeatmapInfo) -> DanserResult:
        output_name = f"stream-{int(time.time())}-{uuid.uuid4().hex[:10]}"
        output_path = self.config.danser_output_dir / f"{output_name}.mp4"
        temporary_dir = self.config.danser_output_dir / f"{output_name}_temp"
        output_path.unlink(missing_ok=True)
        shutil.rmtree(temporary_dir, ignore_errors=True)
        command = (
            str(self.config.danser_executable),
            f"-settings={self.config.danser_settings}",
            f"-replay={candidate.path.resolve()}",
            f"-out={output_name}",
            f"-skin={self.config.danser_skin}",
            "-nodbcheck",
            "-noupdatecheck",
            "-preciseprogress",
            "-quickstart",
        )
        self.runtime.phase = "rendering locally with danser"
        self.runtime.render_progress = "Starting local renderer"
        logging.info("Starting local danser render for %s", beatmap.description)
        env = os.environ.copy()
        library_path = str(self.config.danser_dir)
        if env.get("LD_LIBRARY_PATH"):
            library_path += os.pathsep + env["LD_LIBRARY_PATH"]
        env["LD_LIBRARY_PATH"] = library_path

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.config.danser_dir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as error:
            raise DanserError(f"Could not start danser: {error}") from error

        tail: deque[str] = deque(maxlen=40)

        async def consume_output() -> None:
            assert process.stdout is not None
            with self.log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} | {beatmap.description} =====\n")
                while raw_line := await process.stdout.readline():
                    line = raw_line.decode("utf-8", "replace").rstrip()
                    log_file.write(line + "\n")
                    log_file.flush()
                    if line:
                        tail.append(line)
                    match = PROGRESS_RE.search(line)
                    if match:
                        progress = int(match.group(1))
                        self.runtime.render_progress = f"danser {progress}%"

        reader_task = asyncio.create_task(consume_output(), name="danser-output")
        try:
            await asyncio.wait_for(process.wait(), timeout=self.config.danser_render_timeout)
        except asyncio.CancelledError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            output_path.unlink(missing_ok=True)
            shutil.rmtree(temporary_dir, ignore_errors=True)
            raise
        except asyncio.TimeoutError as error:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            raise DanserError(
                f"danser exceeded the {self.config.danser_render_timeout}-second render timeout"
            ) from error
        finally:
            await asyncio.gather(reader_task, return_exceptions=True)

        if process.returncode != 0:
            output_path.unlink(missing_ok=True)
            detail = next(
                (line for line in reversed(tail) if "panic:" in line.lower() or "error" in line.lower()),
                tail[-1] if tail else "no diagnostic output",
            )
            raise DanserError(f"danser exited with code {process.returncode}: {detail[:700]}")
        valid, duration = await probe_video(output_path)
        if not valid:
            output_path.unlink(missing_ok=True)
            raise DanserError("danser finished but did not create a valid MP4 video")
        self.runtime.render_progress = "Local render complete"
        logging.info(
            "Local danser render completed (%.1fs, %.1f MiB)",
            duration,
            output_path.stat().st_size / 1048576,
        )
        return DanserResult(
            path=output_path,
            duration=duration,
            description=beatmap.description,
            skin=self.config.danser_skin,
            beatmap_set_id=beatmap.set_id,
            beatmap_id=beatmap.beatmap_id,
        )

    async def close(self) -> None:
        return
