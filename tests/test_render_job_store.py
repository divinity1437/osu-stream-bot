import asyncio
import time
import pytest
from render_job_store import RenderJobStore


def test_claim_job_atomicity_and_heartbeat(tmp_path):
    async def _test():
        store = RenderJobStore(tmp_path)
        try:
            now = time.time()
            job_id = "job-test-pytest-1"
            store.connection.execute(
                """
                INSERT INTO render_jobs (
                    id, status, source_replay, created_at, updated_at
                ) VALUES (?, 'queued', ?, ?, ?)
                """,
                (job_id, "/path/to/test.osr", now, now),
            )
            store.connection.commit()

            # 10 concurrent claim_job requests
            tasks = [store.claim_job("worker-1") for _ in range(10)]
            results = await asyncio.gather(*tasks)

            claimed = [r for r in results if r is not None]
            none_results = [r for r in results if r is None]

            assert len(claimed) == 1
            assert len(none_results) == 9

            claimed_job = claimed[0]
            assert claimed_job["id"] == job_id
            assert claimed_job["status"] == "leased"
            assert claimed_job["worker_id"] == "worker-1"
            valid_token = claimed_job["lease_token"]

            # Valid heartbeat
            assert await store.heartbeat(job_id, valid_token) is True

            # Invalid heartbeat
            assert await store.heartbeat(job_id, "wrong-token") is False
        finally:
            await store.close()

    asyncio.run(_test())
