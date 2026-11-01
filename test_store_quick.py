import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path

from render_job_store import RenderJobStore


async def run_tests() -> None:
    temp_dir = Path(tempfile.mkdtemp(prefix="test_store_"))
    try:
        print(f"1. Initializing RenderJobStore in temp directory: {temp_dir}")
        store = RenderJobStore(temp_dir)

        # Seed 1 job in status 'queued'
        now = time.time()
        job_id = "job-atomic-test-1"
        store.connection.execute(
            """
            INSERT INTO render_jobs (
                id, status, source_replay, created_at, updated_at
            ) VALUES (?, 'queued', ?, ?, ?)
            """,
            (job_id, "/path/to/test.osr", now, now),
        )
        store.connection.commit()
        print("   Seeded 1 queued job.")

        # Test concurrency with 10 coroutines
        print("2. Spawning 10 concurrent claim_job('worker-1') coroutines...")
        tasks = [store.claim_job("worker-1") for _ in range(10)]
        results = await asyncio.gather(*tasks)

        claimed = [r for r in results if r is not None]
        none_results = [r for r in results if r is None]

        print(f"   Claimed count: {len(claimed)}, None count: {len(none_results)}")
        assert len(claimed) == 1, f"Expected exactly 1 claim, got {len(claimed)}"
        assert len(none_results) == 9, f"Expected 9 None results, got {len(none_results)}"

        claimed_job = claimed[0]
        assert claimed_job["id"] == job_id
        assert claimed_job["status"] == "leased"
        assert claimed_job["worker_id"] == "worker-1"
        assert claimed_job["lease_token"] is not None
        valid_token = claimed_job["lease_token"]
        print(f"   [PASS] Atomicity verified! Claimed job: id={job_id}, token={valid_token[:8]}...")

        # Test heartbeat with valid token
        print("3. Testing heartbeat with valid token...")
        ok = await store.heartbeat(job_id, valid_token)
        print(f"   heartbeat returned: {ok}")
        assert ok is True, "Expected heartbeat with valid token to return True"
        print("   [PASS] Heartbeat with valid token succeeded.")

        # Test heartbeat with invalid token
        print("4. Testing heartbeat with invalid token...")
        bad_token = "invalid-token-12345"
        ok_bad = await store.heartbeat(job_id, bad_token)
        print(f"   heartbeat returned: {ok_bad}")
        assert ok_bad is False, "Expected heartbeat with invalid token to return False"
        print("   [PASS] Heartbeat with invalid token failed as expected.")

        await store.close()
        print("\nALL QUICK TESTS PASSED SUCCESSFULLY!")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(run_tests())
