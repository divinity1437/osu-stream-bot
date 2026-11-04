#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp
import platform
import subprocess

from config import Config, ROOT, load_env
from danser_renderer import (
    BeatmapLibrary,
    PROGRESS_RE,
    STAR_TOTAL_RE,
    configure_danser_profile,
    discover_danser_skins,
    replay_beatmap_checksum,
    replay_player_name,
    resolve_danser_skins_dir,
)


class StaleLeaseError(RuntimeError):
    """Raised when heartbeat returns 410 Gone (lease expired or reclaimed)."""
    pass


class RetryableUploadError(RuntimeError):
    pass


RETRYABLE_UPLOAD_STATUSES = {408, 425, 429, 500, 502, 503, 504}


def is_valid_uuid(val: str) -> bool:
    try:
        uuid_obj = uuid.UUID(str(val))
        return str(uuid_obj) == str(val).lower()
    except (ValueError, AttributeError, TypeError):
        return False


class RenderWorker:
    def __init__(self, config: Config):
        self.config = config
        self.worker_id = config.worker_id or f"worker-{socket.gethostname()}"
        self.controller_url = config.controller_url.rstrip("/")
        self.api_token = config.render_api_token
        self.heartbeat_interval = config.render_heartbeat_seconds
        self.work_dir = config.root / "work" / "worker"
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self.current_process: asyncio.subprocess.Process | None = None
        self.current_job_task: asyncio.Task | None = None
        self.stop_event = asyncio.Event()

        self.current_progress: str = ""
        self.current_star_rating: float | None = None

        self.library: BeatmapLibrary | None = None
        self.skins_directory = resolve_danser_skins_dir(config)
        self.settings_path = configure_danser_profile(config, self.skins_directory)
        self.last_skin: str | None = None
        self.specs = self._collect_system_specs()
        available_skins = discover_danser_skins(self.skins_directory)
        logging.info(
            "Worker danser skins: %s (%d custom skin%s found)",
            self.skins_directory,
            len(available_skins),
            "" if len(available_skins) == 1 else "s",
        )

    def _select_skin(self, job_settings: dict[str, Any] | None = None) -> str:
        available = discover_danser_skins(self.skins_directory)
        job_settings = job_settings or {}
        random_skin = job_settings.get("random_skin", self.config.danser_random_skin)
        target_skin = job_settings.get("skin", self.config.danser_skin)

        if random_skin and available:
            candidates = [skin for skin in available if skin != self.last_skin] or available
            selected = secrets.choice(candidates)
        elif target_skin == "default" or target_skin in available:
            selected = target_skin
        else:
            logging.warning(
                "Configured danser skin %r was not found in %s; using default",
                target_skin,
                self.skins_directory,
            )
            selected = "default"
        self.last_skin = selected
        return selected

    def _collect_system_specs(self) -> dict[str, Any]:
        specs: dict[str, Any] = {
            "hostname": socket.gethostname(),
            "os": f"{platform.system()} {platform.release()}",
            "arch": platform.machine(),
            "cpu_cores": os.cpu_count() or 1,
        }
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
                for line in f:
                    if "model name" in line:
                        specs["cpu_model"] = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass
        if "cpu_model" not in specs:
            specs["cpu_model"] = platform.processor() or "Unknown CPU"

        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                mem = {}
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        k = parts[0].strip()
                        v = parts[1].strip().split()[0]
                        if k in ("MemTotal", "MemAvailable"):
                            mem[k] = int(v) * 1024
                if "MemTotal" in mem:
                    specs["ram_total_gb"] = round(mem["MemTotal"] / (1024**3), 1)
                if "MemAvailable" in mem:
                    specs["ram_avail_gb"] = round(mem["MemAvailable"] / (1024**3), 1)
        except Exception:
            pass

        try:
            virt = subprocess.check_output(
                ["systemd-detect-virt"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            specs["virt"] = virt
        except Exception:
            specs["virt"] = "none"

        try:
            lspci_out = subprocess.check_output(
                ["lspci"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            gpus = [
                line.split(":", 2)[-1].strip()
                for line in lspci_out.splitlines()
                if any(k in line.lower() for k in ("vga compatible", "3d controller", "display controller"))
            ]
            if gpus:
                specs["gpu"] = gpus[0]
        except Exception:
            pass

        return specs

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        if extra:
            headers.update(extra)
        return headers

    async def _kill_current_process(self) -> None:
        process = self.current_process
        if process is None:
            return
        await self._terminate_process(process)
        if self.current_process is process:
            self.current_process = None

    async def _terminate_process(
        self,
        process: asyncio.subprocess.Process,
    ) -> None:
        if process.returncode is not None:
            await process.wait()
            return

        def signal_process(force: bool) -> None:
            try:
                if os.name == "posix":
                    signum = signal.SIGKILL if force else signal.SIGTERM
                    os.killpg(process.pid, signum)
                elif force:
                    process.kill()
                else:
                    process.terminate()
            except ProcessLookupError:
                pass

        logging.warning("Terminating active danser process tree...")
        signal_process(force=False)
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logging.warning("danser did not exit in 5s; killing process tree...")
            signal_process(force=True)
            await process.wait()

    async def _heartbeat_loop(
        self,
        session: aiohttp.ClientSession,
        job_id: str,
        lease_token: str,
        job_task: asyncio.Task,
        lease_seconds: float,
    ) -> None:
        url = f"{self.controller_url}/internal/v1/render/jobs/{job_id}/heartbeat"
        lease_deadline = time.monotonic() + lease_seconds

        while True:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                payload = {
                    "lease_token": lease_token,
                    "worker_id": self.worker_id,
                    "progress": self.current_progress,
                    "specs": self.specs,
                }
                request_started = time.monotonic()
                async with session.post(
                    url,
                    json=payload,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        # Controller renewed the lease while this request was
                        # in flight, so request_started is conservative.
                        lease_deadline = request_started + lease_seconds
                        logging.debug("Heartbeat sent for job %s", job_id)
                        continue
                    elif resp.status in {400, 401, 403, 404, 409, 410}:
                        logging.error(
                            "Heartbeat permanently rejected for job %s: HTTP %d",
                            job_id,
                            resp.status,
                        )
                        job_task.cancel()
                        return
                    else:
                        logging.warning(
                            "Heartbeat returned transient HTTP %d: %s",
                            resp.status,
                            await resp.text(),
                        )
            except asyncio.CancelledError:
                return
            except Exception as error:
                logging.warning("Heartbeat request failed for job %s: %s", job_id, error)

            if time.monotonic() >= lease_deadline:
                logging.error(
                    "Controller could not renew lease for job %s before its deadline",
                    job_id,
                )
                job_task.cancel()
                return

    async def _download_replay(
        self,
        session: aiohttp.ClientSession,
        job_id: str,
        destination: Path,
    ) -> None:
        part_path = destination.with_suffix(".part")
        url = f"{self.controller_url}/internal/v1/render/jobs/{job_id}/replay"
        logging.info("Downloading replay from %s...", url)

        async with session.get(
            url,
            headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to download replay: HTTP {resp.status} - {await resp.text()}")

            with part_path.open("wb") as f:
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    await asyncio.to_thread(f.write, chunk)

        if not part_path.is_file() or part_path.stat().st_size == 0:
            part_path.unlink(missing_ok=True)
            raise RuntimeError("Downloaded replay file is empty")

        part_path.replace(destination)
        logging.info("Replay saved to %s (%d bytes)", destination, destination.stat().st_size)

    def _apply_visual_settings(self, visual_settings: dict[str, Any] | None) -> None:
        if not visual_settings:
            return
        path = self.config.danser_dir / "settings" / f"{self.config.danser_settings}.json"
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            playfield = payload.setdefault("Playfield", {})
            background = playfield.setdefault("Background", {})
            dim = background.setdefault("Dim", {})
            parallax = background.setdefault("Parallax", {})

            if "background_dim" in visual_settings:
                dim["Normal"] = int(visual_settings["background_dim"]) / 100.0
            if "parallax_enabled" in visual_settings:
                parallax["Enabled"] = bool(visual_settings["parallax_enabled"])
            if "parallax_amount" in visual_settings:
                parallax["Amount"] = int(visual_settings["parallax_amount"]) / 100.0
            if "parallax_speed" in visual_settings:
                parallax["Speed"] = int(visual_settings["parallax_speed"]) / 100.0

            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as err:
            logging.warning("Could not apply controller danser visual settings: %s", err)

    async def _run_danser(
        self,
        replay_path: Path,
        output_mp4: Path,
        job_settings: dict[str, Any] | None = None,
    ) -> str:
        self._apply_visual_settings(job_settings)
        output_name = output_mp4.stem
        selected_skin = self._select_skin(job_settings)
        command = [
            str(self.config.danser_executable),
            f"-settings={self.config.danser_settings}",
            f"-replay={replay_path.resolve()}",
            f"-out={output_name}",
            f"-skin={selected_skin}",
            "-nodbcheck",
            "-noupdatecheck",
            "-preciseprogress",
            "-quickstart",
        ]
        logging.info("Running danser command with skin %r: %s", selected_skin, " ".join(command))
        self.current_progress = "Starting danser"
        self.current_star_rating = None

        env = os.environ.copy()
        library_path = str(self.config.danser_dir)
        if env.get("LD_LIBRARY_PATH"):
            library_path += os.pathsep + env["LD_LIBRARY_PATH"]
        env["LD_LIBRARY_PATH"] = library_path

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.config.danser_dir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=(os.name == "posix"),
        )
        self.current_process = process

        async def consume_output() -> None:
            assert process.stdout is not None
            reading_stars = False
            while line := await process.stdout.readline():
                decoded = line.decode("utf-8", "replace").rstrip()
                if decoded.rstrip().endswith("Stars:"):
                    reading_stars = True
                elif reading_stars:
                    star_match = STAR_TOTAL_RE.search(decoded)
                    if star_match:
                        self.current_star_rating = float(star_match.group(1))
                        reading_stars = False
                if "Progress:" in decoded:
                    logging.info("danser: %s", decoded)
                    prog_match = PROGRESS_RE.search(decoded)
                    if prog_match:
                        # Extract e.g. "Progress: 45%, Speed: 2.34x, ETA: 10s" if available
                        idx = decoded.find("Progress:")
                        self.current_progress = decoded[idx:]
                    else:
                        self.current_progress = decoded
                elif "Error" in decoded or "panic" in decoded.lower():
                    logging.warning("danser: %s", decoded)

        reader_task = asyncio.create_task(
            consume_output(),
            name=f"danser-output-{output_name}",
        )
        timed_out = False
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=self.config.danser_render_timeout,
            )
        except asyncio.TimeoutError:
            timed_out = True
        finally:
            if process.returncode is None:
                await asyncio.shield(self._terminate_process(process))

            try:
                await asyncio.wait_for(reader_task, timeout=5.0)
            except asyncio.TimeoutError:
                reader_task.cancel()
                await asyncio.gather(reader_task, return_exceptions=True)

            if self.current_process is process:
                self.current_process = None

        if timed_out:
            raise RuntimeError(
                f"danser render timed out after {self.config.danser_render_timeout}s"
            )
        if process.returncode != 0:
            raise RuntimeError(
                f"danser process exited with code {process.returncode}"
            )
        self.current_progress = "Render complete, uploading video"
        return selected_skin

    async def _validate_mp4(self, path: Path) -> None:
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("Rendered video file is missing or empty")

        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return

        process = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=60,
            )
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        except asyncio.TimeoutError as error:
            process.kill()
            await process.wait()
            raise RuntimeError("ffprobe validation timed out") from error

        if process.returncode != 0:
            raise RuntimeError(f"ffprobe validation failed: {stderr.decode('utf-8', 'replace')}")

        try:
            duration = float(stdout.strip())
            if duration <= 0:
                raise ValueError("duration <= 0")
        except Exception as error:
            raise RuntimeError(f"Invalid video duration: {error}")

    async def _upload_artifact(
        self,
        session: aiohttp.ClientSession,
        job_id: str,
        lease_token: str,
        video_path: Path,
    ) -> None:
        url = f"{self.controller_url}/internal/v1/render/jobs/{job_id}/artifact"
        headers = self._headers({"X-Lease-Token": lease_token})

        async def file_sender():
            with video_path.open("rb") as f:
                while chunk := await asyncio.to_thread(f.read, 1024 * 1024):
                    yield chunk

        file_size = video_path.stat().st_size
        minimum_bytes_per_second = 128 * 1024
        total_timeout = max(
            1800.0,
            file_size / minimum_bytes_per_second + 300.0,
        )

        max_retries = 5
        for attempt in range(1, max_retries + 1):
            logging.info("Uploading artifact to controller (attempt %d/%d)...", attempt, max_retries)
            try:
                async with session.put(
                    url,
                    data=file_sender(),
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(
                        total=total_timeout,
                        sock_connect=30,
                        sock_read=120,
                    ),
                ) as resp:
                    if resp.status == 200:
                        logging.info("Artifact successfully uploaded for job %s", job_id)
                        return
                    if resp.status == 410:
                        raise StaleLeaseError("Lease expired during artifact upload")

                    text = await resp.text()
                    if resp.status in RETRYABLE_UPLOAD_STATUSES:
                        raise RetryableUploadError(
                            f"Transient HTTP {resp.status}: {text}"
                        )
                    raise RuntimeError(
                        f"Artifact upload rejected with HTTP {resp.status}: {text}"
                    )
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                RetryableUploadError,
            ) as error:
                if attempt == max_retries:
                    raise RuntimeError(f"Artifact upload failed after {max_retries} attempts: {error}") from error
                backoff = min(30, 2 ** attempt)
                logging.warning("Upload network error: %s. Retrying in %ds...", error, backoff)
                await asyncio.sleep(backoff)

    async def _send_complete(
        self,
        session: aiohttp.ClientSession,
        job_id: str,
        lease_token: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        url = f"{self.controller_url}/internal/v1/render/jobs/{job_id}/complete"
        payload: dict[str, Any] = {"lease_token": lease_token}
        if metadata:
            payload["metadata"] = metadata
        async with session.post(
            url,
            json=payload,
            headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status == 200:
                logging.info("Job %s marked completed on controller.", job_id)
            elif resp.status == 410:
                raise StaleLeaseError("Lease expired before complete call")
            else:
                raise RuntimeError(f"Complete failed: HTTP {resp.status} - {await resp.text()}")

    async def _send_fail(
        self,
        session: aiohttp.ClientSession,
        job_id: str,
        lease_token: str,
        error: str,
    ) -> None:
        url = f"{self.controller_url}/internal/v1/render/jobs/{job_id}/fail"
        payload = {"lease_token": lease_token, "error": error}
        try:
            async with session.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                logging.info("Job %s reported failed: status=%d", job_id, resp.status)
        except Exception as err:
            logging.warning("Could not report fail to controller: %s", err)

    async def _process_job(self, session: aiohttp.ClientSession, job: dict[str, Any]) -> None:
        job_id = job["id"]
        lease_token = job["lease_token"]
        if not is_valid_uuid(job_id) or not is_valid_uuid(lease_token):
            logging.error("Invalid job ID or lease token format: job_id=%s, lease_token=%s", job_id, lease_token)
            return

        logging.info("Claimed job %s (attempts=%d)", job_id, job.get("attempts", 0))

        replay_path = self.work_dir / f"{job_id}.osr"
        output_mp4 = self.config.danser_output_dir / f"{job_id}.mp4"

        current_task = asyncio.current_task()
        assert current_task is not None

        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(
                session,
                job_id,
                lease_token,
                current_task,
                float(job.get("lease_seconds", 60.0)),
            ),
            name=f"heartbeat-{job_id}",
        )

        async def stop_heartbeat() -> None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

        try:
            # 1. Download replay
            await self._download_replay(session, job_id, replay_path)

            # 2. Ensure beatmap is downloaded and indexed
            if self.library is None:
                class DummyRuntime:
                    phase = "idle"
                self.library = BeatmapLibrary(self.config, session, DummyRuntime())

            checksum = replay_beatmap_checksum(replay_path)
            logging.info("Checking beatmap with checksum %s for job %s...", checksum, job_id)
            self.current_progress = f"Checking beatmap {checksum[:8]}"
            beatmap = await self.library.ensure(checksum)
            logging.info("Beatmap ready: %s (%s)", beatmap.description, beatmap.path)

            try:
                player_name = replay_player_name(replay_path)
            except Exception as player_err:
                logging.warning("Could not read player name from %s: %s", replay_path.name, player_err)
                player_name = ""

            # 3. Render locally via danser
            output_mp4.unlink(missing_ok=True)
            danser_settings = job.get("danser_settings")
            selected_skin = await self._run_danser(replay_path, output_mp4, danser_settings)

            # 4. Validate video
            await self._validate_mp4(output_mp4)

            # 5. Upload artifact
            self.current_progress = "Uploading video to controller"
            await self._upload_artifact(session, job_id, lease_token, output_mp4)

            # 6. Complete
            await stop_heartbeat()
            self.current_progress = "Complete"

            result_metadata = {
                "beatmap_set_id": beatmap.set_id,
                "beatmap_id": beatmap.beatmap_id,
                "artist": beatmap.artist,
                "title": beatmap.title,
                "difficulty": beatmap.difficulty,
                "description": beatmap.description,
                "player": player_name,
                "stars": self.current_star_rating,
                "skin": selected_skin,
            }
            await self._send_complete(session, job_id, lease_token, metadata=result_metadata)
        except asyncio.CancelledError:
            logging.warning("Job %s was cancelled (e.g. shutdown or lease expired)", job_id)
            raise
        except StaleLeaseError as error:
            logging.error("Job %s aborted due to stale lease: %s", job_id, error)
        except Exception as error:
            logging.exception("Error processing job %s: %s", job_id, error)
            await stop_heartbeat()
            await self._send_fail(session, job_id, lease_token, str(error))
        finally:
            self.current_progress = ""
            await stop_heartbeat()
            await self._kill_current_process()

            replay_part = replay_path.with_suffix(".part")
            for path in (replay_path, replay_part, output_mp4):
                try:
                    await asyncio.to_thread(path.unlink, missing_ok=True)
                except OSError:
                    logging.warning(
                        "Could not remove worker file %s",
                        path,
                        exc_info=True,
                    )

            danser_temp = (
                self.config.danser_output_dir
                / f"{output_mp4.stem}_temp"
            )
            await asyncio.to_thread(
                shutil.rmtree,
                danser_temp,
                ignore_errors=True,
            )

    async def run(self) -> None:
        logging.info("Starting Render Worker %s (controller=%s)", self.worker_id, self.controller_url)
        claim_url = f"{self.controller_url}/internal/v1/render/jobs/claim"
        claim_payload = {
            "worker_id": self.worker_id,
            "specs": self.specs,
        }

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while not self.stop_event.is_set():
                try:
                    async with session.post(
                        claim_url,
                        json=claim_payload,
                        headers=self._headers(),
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status == 204:
                            logging.debug("No jobs available. Waiting 5s...")
                            await asyncio.sleep(5)
                            continue
                        elif resp.status == 200:
                            job = await resp.json()
                        elif resp.status == 401:
                            logging.error("Unauthorized: check RENDER_API_TOKEN. Retrying in 10s...")
                            await asyncio.sleep(10)
                            continue
                        else:
                            logging.warning("Claim returned unexpected HTTP %d: %s", resp.status, await resp.text())
                            await asyncio.sleep(5)
                            continue

                    self.current_job_task = asyncio.create_task(
                        self._process_job(session, job),
                        name=f"job-{job['id']}",
                    )
                    try:
                        await self.current_job_task
                    except asyncio.CancelledError:
                        logging.info("Current job task was cancelled.")
                    finally:
                        self.current_job_task = None

                except asyncio.CancelledError:
                    break
                except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                    logging.warning("Controller connection error: %s. Retrying in 5s...", error)
                    await asyncio.sleep(5)
                except Exception as error:
                    logging.exception("Unexpected error in worker loop: %s", error)
                    await asyncio.sleep(5)

        logging.info("Render Worker stopped.")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    load_env(ROOT / ".env")
    config = Config.from_env()

    worker = RenderWorker(config)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def handle_signal():
        logging.info("Shutdown signal received.")
        worker.stop_event.set()
        if worker.current_job_task and not worker.current_job_task.done():
            worker.current_job_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(worker.run())
    finally:
        loop.run_until_complete(worker._kill_current_process())
        loop.close()


if __name__ == "__main__":
    main()
