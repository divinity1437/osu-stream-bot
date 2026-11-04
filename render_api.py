import asyncio
import hmac
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from config import Config
from render_job_store import RenderJobStore


class ArtifactTooLarge(Exception):
    pass


def unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logging.warning("Could not remove temporary artifact: %s", path, exc_info=True)


def is_valid_uuid(val: str) -> bool:
    try:
        uuid_obj = uuid.UUID(str(val))
        return str(uuid_obj) == str(val).lower()
    except (ValueError, AttributeError, TypeError):
        return False


async def read_json_object(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "Invalid JSON"}),
            content_type="application/json",
        ) from error

    if not isinstance(body, dict):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "JSON body must be an object"}),
            content_type="application/json",
        )
    return body


def create_auth_middleware(token: str) -> Callable:
    @web.middleware
    async def auth_middleware(request: web.Request, handler: Callable) -> web.StreamResponse:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return web.json_response(
                {"error": "Missing or invalid Authorization header"},
                status=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        provided_token = auth_header[len("Bearer ") :].strip()
        if not token or not hmac.compare_digest(provided_token.encode("utf-8"), token.encode("utf-8")):
            return web.json_response(
                {"error": "Unauthorized"},
                status=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await handler(request)

    return auth_middleware


class RenderApi:
    def __init__(self, job_store: RenderJobStore, config: Config):
        self.job_store = job_store
        self.config = config
        self.artifacts_dir = self.config.root / "work" / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.max_artifact_bytes = getattr(config, "max_artifact_bytes", 1024 * 1024 * 1024)

    async def handle_get_replay(self, request: web.Request) -> web.StreamResponse:
        job_id = request.match_info.get("job_id", "")
        if not is_valid_uuid(job_id):
            return web.json_response({"error": "Invalid job_id format"}, status=400)

        job = await self.job_store.get_job(job_id)
        if not job:
            return web.json_response({"error": "Job not found"}, status=404)

        source_replay = Path(job.get("source_replay", "")).resolve()
        if not source_replay.is_file():
            return web.json_response({"error": "Source replay file not found on disk"}, status=404)

        return web.FileResponse(source_replay)

    async def handle_claim(self, request: web.Request) -> web.Response:
        body = await read_json_object(request)

        worker_id = body.get("worker_id")
        if not worker_id or not isinstance(worker_id, str):
            return web.json_response({"error": "worker_id is required"}, status=400)

        remote_ip = request.remote or ""
        specs = body.get("specs") if isinstance(body.get("specs"), dict) else None
        await self.job_store.record_worker_heartbeat(
            worker_id=worker_id,
            ip=remote_ip,
            specs=specs,
            current_job_id=None,
            progress="idle",
        )

        job = await self.job_store.claim_job(worker_id)
        if job is None:
            return web.Response(status=204)

        await self.job_store.record_worker_heartbeat(
            worker_id=worker_id,
            ip=remote_ip,
            specs=specs,
            current_job_id=job["id"],
            progress="claimed",
        )

        job["lease_seconds"] = 60.0
        job["danser_settings"] = {
            "background_dim": getattr(self.config, "danser_background_dim_percent", 95),
            "parallax_enabled": getattr(self.config, "danser_parallax_enabled", True),
            "parallax_amount": getattr(self.config, "danser_parallax_amount_percent", 10),
            "parallax_speed": getattr(self.config, "danser_parallax_speed_percent", 50),
            "random_skin": getattr(self.config, "danser_random_skin", False),
            "skin": getattr(self.config, "danser_skin", "default"),
        }
        return web.json_response(job, status=200)

    async def handle_heartbeat(self, request: web.Request) -> web.Response:
        job_id = request.match_info.get("job_id", "")
        if not is_valid_uuid(job_id):
            return web.json_response({"error": "Invalid job_id format"}, status=400)

        body = await read_json_object(request)

        lease_token = body.get("lease_token")
        if not lease_token or not isinstance(lease_token, str):
            return web.json_response({"error": "lease_token is required"}, status=400)
        if not is_valid_uuid(lease_token):
            return web.json_response({"error": "Invalid lease token format"}, status=400)

        progress = str(body.get("progress") or "").strip()
        success = await self.job_store.heartbeat(job_id, lease_token, progress=progress)
        if not success:
            return web.json_response({"error": "Stale lease or unknown job"}, status=410)

        worker_id = body.get("worker_id")
        if worker_id and isinstance(worker_id, str):
            specs = body.get("specs") if isinstance(body.get("specs"), dict) else None
            await self.job_store.record_worker_heartbeat(
                worker_id=worker_id,
                ip=request.remote or "",
                specs=specs,
                current_job_id=job_id,
                progress=progress,
            )

        return web.json_response({"status": "ok"}, status=200)

    async def handle_upload_artifact(self, request: web.Request) -> web.Response:
        job_id = request.match_info.get("job_id", "")
        if not is_valid_uuid(job_id):
            return web.json_response({"error": "Invalid job_id format"}, status=400)

        lease_token = request.headers.get("X-Lease-Token", "").strip()
        if not lease_token:
            return web.json_response({"error": "X-Lease-Token is required"}, status=400)
        if not is_valid_uuid(lease_token):
            return web.json_response({"error": "Invalid lease token format"}, status=400)

        job = await self.job_store.get_job(job_id)
        if not job:
            return web.json_response({"error": "Job not found"}, status=404)

        # Preflight check: active lease required
        if not await self.job_store.has_active_lease(job_id, lease_token):
            return web.json_response({"error": "Invalid or stale lease token"}, status=410)

        content_length = request.content_length
        if content_length is not None and content_length > self.max_artifact_bytes:
            return web.json_response(
                {"error": "Artifact exceeds maximum allowed size"},
                status=413,
            )

        # Different leases and concurrent requests must never share a path
        upload_id = uuid.uuid4().hex
        part_file = self.artifacts_dir / f".{job_id}-{upload_id}.part"
        final_file = self.artifacts_dir / f"{job_id}-{upload_id}.mp4"

        total_bytes = 0
        promoted = False
        committed = False
        try:
            with part_file.open("xb") as output:
                async for chunk in request.content.iter_chunked(1024 * 1024):
                    total_bytes += len(chunk)
                    if total_bytes > self.max_artifact_bytes:
                        raise ArtifactTooLarge
                    await asyncio.to_thread(output.write, chunk)

            if total_bytes == 0:
                return web.json_response({"error": "Artifact is empty"}, status=400)

            part_file.replace(final_file)
            promoted = True

            artifact_path = str(final_file.resolve())
            committed = await self.job_store.update_artifact_path(
                job_id,
                lease_token,
                artifact_path,
            )
            if not committed:
                return web.json_response(
                    {"error": "Lease expired while uploading artifact"},
                    status=410,
                )

            return web.json_response(
                {
                    "status": "uploaded",
                    "artifact_path": artifact_path,
                    "size": total_bytes,
                },
                status=200,
            )
        except ArtifactTooLarge:
            return web.json_response(
                {"error": "Artifact exceeds maximum allowed size"},
                status=413,
            )
        except Exception:
            logging.exception("Failed uploading artifact for job %s", job_id)
            return web.json_response({"error": "Failed writing artifact"}, status=500)
        finally:
            unlink_quietly(part_file)
            if promoted and not committed:
                unlink_quietly(final_file)

    async def handle_complete(self, request: web.Request) -> web.Response:
        job_id = request.match_info.get("job_id", "")
        if not is_valid_uuid(job_id):
            return web.json_response({"error": "Invalid job_id format"}, status=400)

        body = await read_json_object(request)

        lease_token = body.get("lease_token")
        if not lease_token or not isinstance(lease_token, str):
            return web.json_response({"error": "lease_token is required"}, status=400)
        if not is_valid_uuid(lease_token):
            return web.json_response({"error": "Invalid lease token format"}, status=400)

        job = await self.job_store.get_job(job_id)
        if not job:
            return web.json_response({"error": "Job not found"}, status=404)

        stored_path = job.get("artifact_path")
        if not isinstance(stored_path, str):
            return web.json_response({"error": "Artifact has not been uploaded"}, status=409)

        artifact_path = Path(stored_path).resolve()
        artifacts_root = self.artifacts_dir.resolve()
        if not artifact_path.is_relative_to(artifacts_root) or not artifact_path.is_file():
            return web.json_response({"error": "Stored artifact is invalid"}, status=409)

        result_metadata = body.get("metadata")
        metadata_json = json.dumps(result_metadata) if isinstance(result_metadata, dict) else None

        success = await self.job_store.complete_job(job_id, lease_token, result_metadata=metadata_json)
        if not success:
            return web.json_response({"error": "Stale lease or unknown job"}, status=410)

        return web.json_response({"status": "completed"}, status=200)

    async def handle_fail(self, request: web.Request) -> web.Response:
        job_id = request.match_info.get("job_id", "")
        if not is_valid_uuid(job_id):
            return web.json_response({"error": "Invalid job_id format"}, status=400)

        body = await read_json_object(request)

        lease_token = body.get("lease_token")
        if not lease_token or not isinstance(lease_token, str):
            return web.json_response({"error": "lease_token is required"}, status=400)
        if not is_valid_uuid(lease_token):
            return web.json_response({"error": "Invalid lease token format"}, status=400)

        error_message = body.get("error", "Worker reported error")
        success = await self.job_store.fail_job(job_id, lease_token, error_message)
        if not success:
            return web.json_response({"error": "Stale lease or unknown job"}, status=410)

        return web.json_response({"status": "failed"}, status=200)


def create_render_api_app(job_store: RenderJobStore, config: Config) -> web.Application:
    token = config.render_api_token
    if not token:
        raise RuntimeError("RENDER_API_TOKEN is required for the Render API")

    app = web.Application(middlewares=[create_auth_middleware(token)])
    api = RenderApi(job_store, config)

    app.router.add_post("/internal/v1/render/jobs/claim", api.handle_claim)
    app.router.add_get("/internal/v1/render/jobs/{job_id}/replay", api.handle_get_replay)
    app.router.add_post("/internal/v1/render/jobs/{job_id}/heartbeat", api.handle_heartbeat)
    app.router.add_put("/internal/v1/render/jobs/{job_id}/artifact", api.handle_upload_artifact)
    app.router.add_post("/internal/v1/render/jobs/{job_id}/complete", api.handle_complete)
    app.router.add_post("/internal/v1/render/jobs/{job_id}/fail", api.handle_fail)

    return app
