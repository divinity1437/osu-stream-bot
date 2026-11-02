import asyncio
import io
import time
import uuid
import pytest
from pathlib import Path
from aiohttp import web

from app import Producer, RuntimeState
from config import Config
from queue_store import QueueStore
from render_job_store import RenderJobStore
from replay_source import ReplayCandidate


class DummyReplaySource:
    def __init__(self, candidates: list[ReplayCandidate]):
        self.candidates = list(candidates)

    async def next(self) -> ReplayCandidate:
        if self.candidates:
            return self.candidates.pop(0)
        raise RuntimeError("No more replays")


class DummySubmissions:
    def __init__(self):
        self.marked_rendering = []

    async def mark_rendering(self, submission_id: int, render_id: Any) -> None:
        self.marked_rendering.append((submission_id, render_id))

    async def mark_failed(self, submission_id: int, reason: str) -> None:
        pass


def test_controller_producer_flow(tmp_path):
    async def _test():
        config = Config.from_env(root=tmp_path)
        object.__setattr__(config, "run_mode", "controller")
        object.__setattr__(config, "render_api_token", "test-token")
        object.__setattr__(config, "buffer_size", 2)

        store = QueueStore(config)
        runtime = RuntimeState(config, store)
        job_store = RenderJobStore(tmp_path)
        submissions = DummySubmissions()

        dummy_replay_file = tmp_path / "test.osr"
        dummy_replay_file.write_bytes(b"dummy-replay-data")

        candidate = ReplayCandidate(
            path=dummy_replay_file,
            score_id=12345,
            manual=False,
            original_name="test.osr",
            mods=64,
            mods_label="DT",
            submission_id=42,
        )

        source = DummyReplaySource([candidate])
        producer = Producer(
            config=config,
            store=store,
            runtime=runtime,
            source=source,
            ordr=None,
            submissions=submissions,
            danser=None,
            job_store=job_store,
        )

        # Run producer briefly
        task = asyncio.create_task(producer.run())
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        # Verify job was created in RenderJobStore
        assert await job_store.count_active_jobs() == 1
        claim = await job_store.claim_job("worker-test")
        assert claim is not None
        assert claim["score_id"] == 12345
        assert claim["submission_id"] == 42
        assert claim["mods_label"] == "DT"
        assert (42, None) in submissions.marked_rendering

        await job_store.close()

    asyncio.run(_test())
