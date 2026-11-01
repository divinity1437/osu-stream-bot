import asyncio
import io
import time
import uuid
import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

from config import Config
from render_api import create_render_api_app
from render_job_store import RenderJobStore


def test_render_api_full_flow(tmp_path):
    async def _test():
        store = RenderJobStore(tmp_path)
        config = Config.from_env(root=tmp_path)
        # Set token in config
        object.__setattr__(config, "render_api_token", "secret-test-token")

        app = create_render_api_app(store, config)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()

        try:
            headers = {"Authorization": "Bearer secret-test-token"}

            # 1. Test Auth: no token -> 401, bad token -> 401
            resp = await client.post("/internal/v1/render/jobs/claim", json={"worker_id": "w1"})
            assert resp.status == 401

            resp = await client.post(
                "/internal/v1/render/jobs/claim",
                json={"worker_id": "w1"},
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert resp.status == 401

            # 2. Test Claim when empty -> 204
            resp = await client.post(
                "/internal/v1/render/jobs/claim",
                json={"worker_id": "w1"},
                headers=headers,
            )
            assert resp.status == 204

            # Seed 1 job
            job_uuid = str(uuid.uuid4())
            now = time.time()
            store.connection.execute(
                """
                INSERT INTO render_jobs (
                    id, status, source_replay, created_at, updated_at
                ) VALUES (?, 'queued', ?, ?, ?)
                """,
                (job_uuid, "/path/to/replay.osr", now, now),
            )
            store.connection.commit()

            # 3. Test Claim with job -> 200
            resp = await client.post(
                "/internal/v1/render/jobs/claim",
                json={"worker_id": "w1"},
                headers=headers,
            )
            assert resp.status == 200
            claimed_data = await resp.json()
            assert claimed_data["id"] == job_uuid
            assert claimed_data["status"] == "leased"
            lease_token = claimed_data["lease_token"]

            # 4. Test Heartbeat valid -> 200, invalid/stale -> 410
            resp = await client.post(
                f"/internal/v1/render/jobs/{job_uuid}/heartbeat",
                json={"lease_token": lease_token},
                headers=headers,
            )
            assert resp.status == 200

            resp = await client.post(
                f"/internal/v1/render/jobs/{job_uuid}/heartbeat",
                json={"lease_token": "stale-token"},
                headers=headers,
            )
            assert resp.status == 410

            # 5. Test Path traversal protection on job_id
            resp = await client.put(
                "/internal/v1/render/jobs/../../etc/passwd/artifact",
                data=b"dummy",
                headers=headers,
            )
            assert resp.status in {400, 404}

            # 6. Test PUT artifact streaming
            dummy_mp4 = b"RIFF" + (b"\x00" * 2048)
            resp = await client.put(
                f"/internal/v1/render/jobs/{job_uuid}/artifact",
                data=dummy_mp4,
                headers={"Authorization": "Bearer secret-test-token", "X-Lease-Token": lease_token},
            )
            assert resp.status == 200
            res_json = await resp.json()
            assert res_json["status"] == "uploaded"
            assert res_json["size"] == len(dummy_mp4)

            # Check artifact exists on disk
            expected_file = tmp_path / "work" / "artifacts" / f"{job_uuid}.mp4"
            assert expected_file.is_file()
            assert expected_file.read_bytes() == dummy_mp4

            # 7. Test Complete
            resp = await client.post(
                f"/internal/v1/render/jobs/{job_uuid}/complete",
                json={"lease_token": lease_token, "artifact_path": str(expected_file)},
                headers=headers,
            )
            assert resp.status == 200

            # Completed job cannot be heartbeated (stale)
            resp = await client.post(
                f"/internal/v1/render/jobs/{job_uuid}/heartbeat",
                json={"lease_token": lease_token},
                headers=headers,
            )
            assert resp.status == 410

            # 8. Test Fail endpoint on a new job
            fail_job_uuid = str(uuid.uuid4())
            store.connection.execute(
                """
                INSERT INTO render_jobs (
                    id, status, source_replay, created_at, updated_at
                ) VALUES (?, 'queued', ?, ?, ?)
                """,
                (fail_job_uuid, "/path/to/replay.osr", now, now),
            )
            store.connection.commit()

            claim_resp = await client.post(
                "/internal/v1/render/jobs/claim",
                json={"worker_id": "w2"},
                headers=headers,
            )
            fail_job_data = await claim_resp.json()
            fail_lease_token = fail_job_data["lease_token"]

            resp = await client.post(
                f"/internal/v1/render/jobs/{fail_job_uuid}/fail",
                json={"lease_token": fail_lease_token, "error": "Danser crashed"},
                headers=headers,
            )
            assert resp.status == 200

            db_job = await store.get_job(fail_job_uuid)
            assert db_job["status"] == "failed"
            assert db_job["error"] == "Danser crashed"

        finally:
            await client.close()
            await store.close()

    asyncio.run(_test())
