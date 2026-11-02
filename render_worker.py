#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import shutil
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from config import Config, ROOT, load_env


class StaleLeaseError(RuntimeError):
    """Raised when heartbeat returns 410 Gone (lease expired or reclaimed)."""
    pass


class RenderWorker:
    def __init__(self, config: Config):
        self.config = config
        self.worker_id = config.worker_id or f"worker-{os.uname().nodename}"
        self.controller_url = config.controller_url.rstrip("/")
        self.api_token = config.render_api_token
        self.heartbeat_interval = config.render_heartbeat_seconds
        self.work_dir = config.root / "work" / "worker"
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self.current_process: asyncio.subprocess.Process | None = None
        self.current_job_task: asyncio.Task | None = None
        self.stop_event = asyncio.Event()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        if extra:
            headers.update(extra)
        return headers

    async def _kill_current_process(self) -> None:
        if self.current_process and self.current_process.returncode is None:
            logging.warning("Terminating active danser process...")
            self.current_process.terminate()
            try:
                await asyncio.wait_for(self.current_process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logging.warning("danser did not exit in 5s; killing...")
                self.current_process.kill()
                await self.current_process.wait()
            logging.info("danser process killed.")

    async def _heartbeat_loop(
        self,
        session: aiohttp.ClientSession,
        job_id: str,
        lease_token: str,
        job_task: asyncio.Task,
    ) -> None:
        url = f"{self.controller_url}/internal/v1/render/jobs/{job_id}/heartbeat"
        payload = {"lease_token": lease_token}

        while True:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                async with session.post(
                    url,
                    json=payload,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        logging.debug("Heartbeat sent for job %s", job_id)
                    elif resp.status == 410:
                        logging.error("Heartbeat returned 410 Gone for job %s! Lease is stale.", job_id)
                        await self._kill_current_process()
                        job_task.cancel()
                        return
                    else:
                        logging.warning("Heartbeat returned unexpected HTTP %d: %s", resp.status, await resp.text())
            except asyncio.CancelledError:
                break
            except Exception as error:
                logging.warning("Heartbeat request failed for job %s: %s", job_id, error)

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

    async def _run_danser(self, replay_path: Path, output_mp4: Path) -> None:
        output_name = output_mp4.stem
        command = [
            str(self.config.danser_executable),
            f"-settings={self.config.danser_settings}",
            f"-replay={replay_path.resolve()}",
            f"-out={output_name}",
            f"-skin={self.config.danser_skin}",
            "-nodbcheck",
            "-noupdatecheck",
            "-preciseprogress",
            "-quickstart",
        ]
        logging.info("Running danser command: %s", " ".join(command))

        env = os.environ.copy()
        library_path = str(self.config.danser_dir)
        if env.get("LD_LIBRARY_PATH"):
            library_path += os.pathsep + env["LD_LIBRARY_PATH"]
        env["LD_LIBRARY_PATH"] = library_path

        self.current_process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.config.danser_dir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            assert self.current_process.stdout is not None
            while line := await self.current_process.stdout.readline():
                decoded = line.decode("utf-8", "replace").rstrip()
                if "Progress:" in decoded:
                    logging.info("danser: %s", decoded)
                elif "Error" in decoded or "panic" in decoded.lower():
                    logging.warning("danser: %s", decoded)

            await asyncio.wait_for(
                self.current_process.wait(),
                timeout=self.config.danser_render_timeout,
            )
        except asyncio.TimeoutError:
            await self._kill_current_process()
            raise RuntimeError(f"danser render timed out after {self.config.danser_render_timeout}s")
        finally:
            process = self.current_process
            self.current_process = None
            if process and process.returncode is not None and process.returncode != 0:
                raise RuntimeError(f"danser process exited with code {process.returncode}")

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
        stdout, stderr = await process.communicate()
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

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            logging.info("Uploading artifact to controller (attempt %d/%d)...", attempt, max_retries)
            try:
                async with session.put(
                    url,
                    data=file_sender(),
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=1800, sock_read=120),
                ) as resp:
                    if resp.status == 200:
                        logging.info("Artifact successfully uploaded for job %s", job_id)
                        return
                    elif resp.status == 410:
                        raise StaleLeaseError("Lease expired during artifact upload")
                    else:
                        text = await resp.text()
                        raise RuntimeError(f"Artifact upload rejected with HTTP {resp.status}: {text}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                if attempt == max_retries:
                    raise RuntimeError(f"Artifact upload failed after {max_retries} attempts: {error}") from error
                backoff = 2 ** attempt
                logging.warning("Upload network error: %s. Retrying in %ds...", error, backoff)
                await asyncio.sleep(backoff)

    async def _send_complete(
        self,
        session: aiohttp.ClientSession,
        job_id: str,
        lease_token: str,
    ) -> None:
        url = f"{self.controller_url}/internal/v1/render/jobs/{job_id}/complete"
        payload = {"lease_token": lease_token}
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
        logging.info("Claimed job %s (attempts=%d)", job_id, job.get("attempts", 0))

        replay_path = self.work_dir / f"{job_id}.osr"
        output_mp4 = self.config.danser_output_dir / f"{job_id}.mp4"

        current_task = asyncio.current_task()
        assert current_task is not None

        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(session, job_id, lease_token, current_task),
            name=f"heartbeat-{job_id}",
        )

        completed_or_failed = False
        try:
            # 1. Download replay
            await self._download_replay(session, job_id, replay_path)

            # 2. Render locally via danser
            output_mp4.unlink(missing_ok=True)
            await self._run_danser(replay_path, output_mp4)

            # 3. Validate video
            await self._validate_mp4(output_mp4)

            # 4. Upload artifact
            await self._upload_artifact(session, job_id, lease_token, output_mp4)

            # 5. Complete
            await self._send_complete(session, job_id, lease_token)
            completed_or_failed = True
        except asyncio.CancelledError:
            logging.warning("Job %s was cancelled (e.g. shutdown or lease expired)", job_id)
            raise
        except StaleLeaseError as error:
            logging.error("Job %s aborted due to stale lease: %s", job_id, error)
        except Exception as error:
            logging.exception("Error processing job %s: %s", job_id, error)
            await self._send_fail(session, job_id, lease_token, str(error))
            completed_or_failed = True
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            await self._kill_current_process()

            if completed_or_failed:
                replay_path.unlink(missing_ok=True)
                output_mp4.unlink(missing_ok=True)
                replay_part = replay_path.with_suffix(".part")
                replay_part.unlink(missing_ok=True)

    async def run(self) -> None:
        logging.info("Starting Render Worker %s (controller=%s)", self.worker_id, self.controller_url)
        claim_url = f"{self.controller_url}/internal/v1/render/jobs/claim"
        claim_payload = {"worker_id": self.worker_id}

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
