#!/usr/bin/env python3
import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

from config import Config
from render_api import create_render_api_app
from render_job_store import RenderJobStore
from render_worker import RenderWorker

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("smoke_test_e2e")


def create_dummy_replay(path: Path) -> None:
    def encode_uleb128(n: int) -> bytes:
        res = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            if n != 0:
                res.append(b | 0x80)
            else:
                res.append(b)
                break
        return bytes(res)

    def encode_osu_string(s: str) -> bytes:
        raw = s.encode("utf-8")
        return b"\x0b" + encode_uleb128(len(raw)) + raw

    content = bytearray()
    content.append(0)  # mode 0 (osu standard)
    content.extend((20201102).to_bytes(4, "little"))  # version
    content.extend(encode_osu_string("beatmap_hash"))
    content.extend(encode_osu_string("player_name"))
    content.extend(encode_osu_string("replay_hash"))

    stats = bytearray(19)
    mods_value = 72
    stats.extend(mods_value.to_bytes(4, "little"))
    content.extend(stats)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(content))


async def run_e2e_test():
    test_dir = Path("/tmp/e2e_test")
    if test_dir.exists():
        shutil.rmtree(test_dir, ignore_errors=True)
    test_dir.mkdir(parents=True, exist_ok=True)

    token = "smoke-test-secret-token"
    api_host = "127.0.0.1"
    api_port = 8790
    controller_url = f"http://{api_host}:{api_port}"

    logger.info("Initializing environments in %s", test_dir)

    # 1. Controller Config & Store
    controller_env = {
        "RUN_MODE": "controller",
        "RENDER_API_HOST": api_host,
        "RENDER_API_PORT": str(api_port),
        "RENDER_API_TOKEN": token,
    }
    with patch.dict(os.environ, controller_env, clear=False):
        controller_config = Config.from_env(root=test_dir / "controller")

    job_store = RenderJobStore(controller_config.root)

    # 2. Worker Config
    worker_env = {
        "RUN_MODE": "render-worker",
        "CONTROLLER_RENDER_URL": controller_url,
        "RENDER_API_TOKEN": token,
        "WORKER_ID": "smoke-worker-1",
        "RENDER_HEARTBEAT_SECONDS": "2.0",
    }
    with patch.dict(os.environ, worker_env, clear=False):
        worker_config = Config.from_env(root=test_dir / "worker")

    # 3. Create dummy replay & 1 fake job in database
    replay_file = test_dir / "controller" / "replays" / "dummy.osr"
    create_dummy_replay(replay_file)
    assert replay_file.is_file() and replay_file.stat().st_size > 0

    job_id = await job_store.create_job(
        source_replay=str(replay_file.resolve()),
        score_id=123456,
        submission_id=789,
        mods=72,
        mods_label="HDDT",
        description="Smoke test fake replay",
    )
    logger.info("Created fake job in database: %s", job_id)

    # 4. Start Render API server
    app = create_render_api_app(job_store, controller_config)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, api_host, api_port)
    await site.start()
    logger.info("Render API server started at %s", controller_url)

    # 5. Mocks for RenderWorker
    async def mock_run_danser(self, replay_path: Path, output_mp4: Path):
        logger.info("[MOCK] danser running for %s -> %s", replay_path.name, output_mp4.name)
        output_mp4.parent.mkdir(parents=True, exist_ok=True)
        # Create 1 KB dummy mp4
        output_mp4.write_bytes(b"\x00" * 1024)
        logger.info("[MOCK] danser generated 1KB dummy mp4")

    async def mock_validate_mp4(self, path: Path):
        logger.info("[MOCK] _validate_mp4 bypassed for %s", path.name)
        return True

    worker = RenderWorker(worker_config)
    worker_task = None

    try:
        with patch.object(RenderWorker, "_run_danser", new=mock_run_danser), \
             patch.object(RenderWorker, "_validate_mp4", new=mock_validate_mp4):

            logger.info("Starting RenderWorker background task...")
            worker_task = asyncio.create_task(worker.run(), name="smoke-render-worker")

            # 6. Wait for job to reach 'completed'
            logger.info("Waiting for job %s to transition to 'completed'...", job_id)
            deadline = asyncio.get_event_loop().time() + 15.0
            completed = False

            while asyncio.get_event_loop().time() < deadline:
                job_data = await job_store.get_job(job_id)
                current_status = job_data.get("status") if job_data else None
                logger.info("Job %s status: %s", job_id, current_status)

                if current_status == "completed":
                    completed = True
                    artifact_path = job_data.get("artifact_path")
                    logger.info("SUCCESS: Job completed! Artifact path in DB: %s", artifact_path)
                    assert artifact_path is not None, "artifact_path must not be None"
                    assert Path(artifact_path).is_file(), f"Artifact file {artifact_path} must exist on disk"
                    assert Path(artifact_path).stat().st_size == 1024, "Artifact file must be 1024 bytes"
                    break

                await asyncio.sleep(0.5)

            assert completed, f"Job {job_id} did not complete within timeout!"
            print("\n==========================================")
            print("SMOKE TEST PASSED: E2E pipeline verified!")
            print("==========================================\n")

    finally:
        logger.info("Cleaning up tasks and resources...")
        # Stop worker
        worker.stop_event.set()
        if worker_task:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)

        # Stop HTTP server
        await site.stop()
        await runner.cleanup()

        # Close database connection
        await job_store.close()

        # Clean temporary test dir
        if test_dir.exists():
            shutil.rmtree(test_dir, ignore_errors=True)
        logger.info("Cleanup completed.")


if __name__ == "__main__":
    asyncio.run(run_e2e_test())
