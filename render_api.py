import asyncio
import hmac
import logging
import uuid
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from config import Config
from render_job_store import RenderJobStore


def is_valid_uuid(val: str) -> bool:
    try:
        uuid_obj = uuid.UUID(str(val))
        return str(uuid_obj) == str(val).lower()
    except (ValueError, AttributeError, TypeError):
        return False


def create_auth_middleware(token: str) -> Callable:
    @web.middleware
    async def auth_middleware(request: web.Request, handler: Callable) -> web.StreamResponse:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return web.json_response({"error": "Missing or invalid Authorization header"}, status=401)

        provided_token = auth_header[len("Bearer ") :].strip()
        if not token or not hmac.compare_digest(provided_token.encode("utf-8"), token.encode("utf-8")):
            return web.json_response({"error": "Unauthorized"}, status=401)

        return await handler(request)

    return auth_middleware


class RenderApi:
    def __init__(self, job_store: RenderJobStore, config: Config):
        self.job_store = job_store
        self.config = config
        self.artifacts_dir = self.config.root / "work" / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        # 1 GB default max artifact size or from config
        self.max_artifact_bytes = getattr(config, "max_artifact_bytes", 1024 * 1024 * 1024)

    async def handle_claim(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        worker_id = body.get("worker_id")
        if not worker_id or not isinstance(worker_id, str):
            return web.json_response({"error": "worker_id is required"}, status=400)

        job = await self.job_store.claim_job(worker_id)
        if job is None:
            return web.Response(status=204)

        return web.json_response(job, status=200)

    async def handle_heartbeat(self, request: web.Request) -> web.Response:
        job_id = request.match_info.get("job_id", "")
        if not is_valid_uuid(job_id):
            return web.json_response({"error": "Invalid job_id format"}, status=400)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        lease_token = body.get("lease_token")
        if not lease_token or not isinstance(lease_token, str):
            return web.json_response({"error": "lease_token is required"}, status=400)

        success = await self.job_store.heartbeat(job_id, lease_token)
        if not success:
            return web.json_response({"error": "Stale lease or unknown job"}, status=410)

        return web.json_response({"status": "ok"}, status=200)

    async def handle_upload_artifact(self, request: web.Request) -> web.Response:
        job_id = request.match_info.get("job_id", "")
        if not is_valid_uuid(job_id):
            return web.json_response({"error": "Invalid job_id format"}, status=400)

        lease_token = request.headers.get("X-Lease-Token", "").strip()
        job = await self.job_store.get_job(job_id)
        if not job:
            return web.json_response({"error": "Job not found"}, status=404)

        # Validate lease_token if job is leased/rendering
        if lease_token and job.get("lease_token") and lease_token != job.get("lease_token"):
            return web.json_response({"error": "Invalid or stale lease token"}, status=410)

        part_file = self.artifacts_dir / f"{job_id}.mp4.part"
        final_file = self.artifacts_dir / f"{job_id}.mp4"

        total_bytes = 0
        try:
            with part_file.open("wb") as f:
                async for chunk in request.content.iter_chunked(1024 * 1024):
                    total_bytes += len(chunk)
                    if total_bytes > self.max_artifact_bytes:
                        part_file.unlink(missing_ok=True)
                        return web.json_response({"error": "Artifact exceeds maximum allowed size"}, status=413)
                    await asyncio.to_thread(f.write, chunk)

            part_file.replace(final_file)
        except Exception as error:
            part_file.unlink(missing_ok=True)
            logging.error("Failed uploading artifact for job %s: %s", job_id, error)
            return web.json_response({"error": "Failed writing artifact"}, status=500)

        if job.get("lease_token"):
            await self.job_store.update_artifact_path(job_id, job["lease_token"], str(final_file.resolve()))

        return web.json_response(
            {"status": "uploaded", "artifact_path": str(final_file.resolve()), "size": total_bytes},
            status=200,
        )

    async def handle_complete(self, request: web.Request) -> web.Response:
        job_id = request.match_info.get("job_id", "")
        if not is_valid_uuid(job_id):
            return web.json_response({"error": "Invalid job_id format"}, status=400)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        lease_token = body.get("lease_token")
        if not lease_token or not isinstance(lease_token, str):
            return web.json_response({"error": "lease_token is required"}, status=400)

        artifact_path = body.get("artifact_path")
        success = await self.job_store.complete_job(job_id, lease_token, artifact_path)
        if not success:
            return web.json_response({"error": "Stale lease or unknown job"}, status=410)

        return web.json_response({"status": "completed"}, status=200)

    async def handle_fail(self, request: web.Request) -> web.Response:
        job_id = request.match_info.get("job_id", "")
        if not is_valid_uuid(job_id):
            return web.json_response({"error": "Invalid job_id format"}, status=400)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        lease_token = body.get("lease_token")
        if not lease_token or not isinstance(lease_token, str):
            return web.json_response({"error": "lease_token is required"}, status=400)

        error_message = body.get("error", "Worker reported error")
        success = await self.job_store.fail_job(job_id, lease_token, error_message)
        if not success:
            return web.json_response({"error": "Stale lease or unknown job"}, status=410)

        return web.json_response({"status": "failed"}, status=200)


def create_render_api_app(job_store: RenderJobStore, config: Config) -> web.Application:
    token = config.render_api_token
    middlewares = []
    if token:
        middlewares.append(create_auth_middleware(token))
    else:
        logging.warning("RENDER_API_TOKEN is not set; Render API is running WITHOUT authentication!")

    app = web.Application(middlewares=middlewares)
    api = RenderApi(job_store, config)

    app.router.add_post("/internal/v1/render/jobs/claim", api.handle_claim)
    app.router.add_post("/internal/v1/render/jobs/{job_id}/heartbeat", api.handle_heartbeat)
    app.router.add_put("/internal/v1/render/jobs/{job_id}/artifact", api.handle_upload_artifact)
    app.router.add_post("/internal/v1/render/jobs/{job_id}/complete", api.handle_complete)
    app.router.add_post("/internal/v1/render/jobs/{job_id}/fail", api.handle_fail)

    return app
