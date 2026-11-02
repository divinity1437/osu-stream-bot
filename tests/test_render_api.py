import asyncio
import io
import time
import uuid
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from config import Config
from render_api import create_render_api_app
from render_job_store import RenderJobStore


def test_render_api_full_flow(tmp_path):
    async def _test():
        store = RenderJobStore(tmp_path)
        config = Config.from_env(root=tmp_path)
        object.__setattr__(config, "render_api_token", "secret-test-token")
        object.__setattr__(config, "max_artifact_bytes", 1024 * 1024)

        app = create_render_api_app(store, config)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()

        try:
            headers = {"Authorization": "Bearer secret-test-token"}

            # 1. Test Auth: no token -> 401 with WWW-Authenticate, bad token -> 401
            resp = await client.post("/internal/v1/render/jobs/claim", json={"worker_id": "w1"})
            assert resp.status == 401
            assert resp.headers.get("WWW-Authenticate") == "Bearer"

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

            # 3. Seed 1 job
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

            # 4. Test Claim with job -> 200
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

            # 5. Heartbeat validation
            # Invalid lease token format -> 400
            resp = await client.post(
                f"/internal/v1/render/jobs/{job_uuid}/heartbeat",
                json={"lease_token": "not-a-uuid"},
                headers=headers,
            )
            assert resp.status == 400

            # Valid heartbeat -> 200
            resp = await client.post(
                f"/internal/v1/render/jobs/{job_uuid}/heartbeat",
                json={"lease_token": lease_token},
                headers=headers,
            )
            assert resp.status == 200

            # Stale lease token -> 410
            other_uuid = str(uuid.uuid4())
            resp = await client.post(
                f"/internal/v1/render/jobs/{job_uuid}/heartbeat",
                json={"lease_token": other_uuid},
                headers=headers,
            )
            assert resp.status == 410

            # 6. Upload artifact validation
            # Missing X-Lease-Token -> 400
            resp = await client.put(
                f"/internal/v1/render/jobs/{job_uuid}/artifact",
                data=b"dummy",
                headers=headers,
            )
            assert resp.status == 400

            # Stale X-Lease-Token -> 410
            resp = await client.put(
                f"/internal/v1/render/jobs/{job_uuid}/artifact",
                data=b"dummy",
                headers={"Authorization": "Bearer secret-test-token", "X-Lease-Token": other_uuid},
            )
            assert resp.status == 410

            # Empty artifact -> 400
            resp = await client.put(
                f"/internal/v1/render/jobs/{job_uuid}/artifact",
                data=b"",
                headers={"Authorization": "Bearer secret-test-token", "X-Lease-Token": lease_token},
            )
            assert resp.status == 400

            # Too large artifact via content-length -> 413
            resp = await client.put(
                f"/internal/v1/render/jobs/{job_uuid}/artifact",
                data=b"a" * 10,
                headers={
                    "Authorization": "Bearer secret-test-token",
                    "X-Lease-Token": lease_token,
                    "Content-Length": str(2 * 1024 * 1024),
                    "Connection": "close",
                },
            )
            assert resp.status == 413

            # Complete before artifact uploaded -> 409
            resp = await client.post(
                f"/internal/v1/render/jobs/{job_uuid}/complete",
                json={"lease_token": lease_token},
                headers=headers,
            )
            assert resp.status == 409

            # Valid artifact upload -> 200
            dummy_mp4 = b"RIFF" + (b"\x00" * 2048)
            resp = await client.put(
                f"/internal/v1/render/jobs/{job_uuid}/artifact",
                data=dummy_mp4,
                headers={"Authorization": "Bearer secret-test-token", "X-Lease-Token": lease_token},
            )
            assert resp.status == 200
            res_json = await resp.json()
            assert res_json["status"] == "uploaded"
            uploaded_path = res_json["artifact_path"]

            # 7. Complete after artifact uploaded -> 200
            resp = await client.post(
                f"/internal/v1/render/jobs/{job_uuid}/complete",
                json={"lease_token": lease_token},
                headers=headers,
            )
            assert resp.status == 200

            # Second complete or heartbeat after complete -> 410
            resp = await client.post(
                f"/internal/v1/render/jobs/{job_uuid}/complete",
                json={"lease_token": lease_token},
                headers=headers,
            )
            assert resp.status == 410

            # 8. Fail endpoint test
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

            # 9. Fail-closed test: create_render_api_app without token raises RuntimeError
            no_token_config = Config.from_env(root=tmp_path)
            object.__setattr__(no_token_config, "render_api_token", "")
            with pytest.raises(RuntimeError, match="RENDER_API_TOKEN is required"):
                create_render_api_app(store, no_token_config)

        finally:
            await client.close()
            await store.close()

    asyncio.run(_test())
