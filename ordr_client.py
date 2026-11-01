import asyncio
import json
import logging
from collections import OrderedDict
from typing import Any

import aiohttp
import socketio

from config import Config
from queue_store import QueueStore
from replay_source import ReplayCandidate, replay_has_double_time


class RenderRequestError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class OrdrClient:
    api_base = "https://apis.issou.best"

    def __init__(
        self,
        config: Config,
        store: QueueStore,
        runtime: Any,
        session: aiohttp.ClientSession,
    ):
        self.config = config
        self.store = store
        self.runtime = runtime
        self.session = session
        self.sio = socketio.AsyncClient(reconnection=True, logger=False, engineio_logger=False)
        self.outcomes: OrderedDict[int, tuple[str, dict[str, Any]]] = OrderedDict()
        self.waiters: dict[int, asyncio.Event] = {}
        self._register_socket_handlers()

    def _remember_outcome(self, render_id: int, kind: str, data: dict[str, Any]) -> None:
        self.outcomes[render_id] = (kind, data)
        self.outcomes.move_to_end(render_id)
        while len(self.outcomes) > 100:
            self.outcomes.popitem(last=False)
        waiter = self.waiters.get(render_id)
        if waiter:
            waiter.set()

    def _register_socket_handlers(self) -> None:
        @self.sio.event
        async def connect() -> None:
            self.runtime.websocket_connected = True
            logging.info("Connected to o!rdr WebSocket")
            if self.config.api_key and not self.config.developer_mode:
                await self.sio.emit("bot_auth", self.config.api_key)

        @self.sio.event
        async def disconnect(reason: Any = None) -> None:
            self.runtime.websocket_connected = False
            logging.warning("o!rdr WebSocket disconnected: %s", reason)

        @self.sio.event
        async def connect_error(data: Any) -> None:
            self.runtime.websocket_connected = False
            logging.warning("o!rdr WebSocket connection error: %s", data)

        @self.sio.on("bot_auth")
        async def bot_auth(data: Any) -> None:
            logging.info("o!rdr authentication: %s", data)

        @self.sio.on("render_progress_json")
        async def render_progress(data: dict[str, Any]) -> None:
            render_id = int(data.get("renderID", 0) or 0)
            if render_id == self.runtime.current_render_id:
                progress = data.get("progress") or data.get("description") or "Rendering"
                self.runtime.render_progress = str(progress)

        @self.sio.on("render_done_json")
        async def render_done(data: dict[str, Any]) -> None:
            render_id = int(data.get("renderID", 0) or 0)
            if render_id and (render_id == self.runtime.current_render_id or render_id in self.waiters):
                self._remember_outcome(render_id, "done", data)

        @self.sio.on("render_failed_json")
        async def render_failed(data: dict[str, Any]) -> None:
            render_id = int(data.get("renderID", 0) or 0)
            if render_id and (render_id == self.runtime.current_render_id or render_id in self.waiters):
                self._remember_outcome(render_id, "failed", data)

    async def connect(self) -> None:
        if self.sio.connected:
            return
        try:
            await self.sio.connect(
                self.api_base,
                socketio_path="ordr/ws",
                wait_timeout=25,
            )
        except Exception as error:
            self.runtime.websocket_connected = False
            logging.warning("WebSocket unavailable; REST polling will be used: %s", error)

    async def close(self) -> None:
        if self.sio.connected:
            await self.sio.disconnect()

    async def _rate_limit(self) -> None:
        delay = self.store.seconds_until_submit()
        if delay > 0:
            self.runtime.phase = f"rate limit: waiting {int(delay) + 1}s"
            logging.info("o!rdr rate limit: waiting %d seconds before the next submission", int(delay) + 1)
            await asyncio.sleep(delay)

    async def submit(self, candidate: ReplayCandidate) -> int:
        use_dt_skin = replay_has_double_time(candidate.mods)
        selected_skin = self.config.dt_skin if use_dt_skin else self.config.skin
        selected_custom_skin = self.config.dt_custom_skin if use_dt_skin else self.config.custom_skin
        logging.info("Selected o!rdr skin %s for mods %s", selected_skin, candidate.mods_label)
        fields = {
            "skin": selected_skin,
            "resolution": self.config.resolution,
            "customSkin": str(selected_custom_skin).lower(),
            "globalVolume": "50",
            "musicVolume": "50",
            "hitsoundVolume": "50",
            "showHitErrorMeter": "true",
            "showUnstableRate": "true",
            "showScore": "true",
            "showHPBar": "true",
            "showComboCounter": "true",
            "showPPCounter": "true",
            "showKeyOverlay": "true",
            "showScoreboard": "false",
            "showMods": "true",
            "showReplayAudio": "false",
        }
        if self.config.api_key:
            fields["verificationKey"] = self.config.api_key
        headers = {}
        if self.config.developer_mode:
            headers["ordr-dev"] = "true"
        await self._rate_limit()
        with candidate.path.open("rb") as handle:
            form = aiohttp.FormData()
            form.add_field("replayFile", handle, filename=candidate.path.name, content_type="application/octet-stream")
            for key, value in fields.items():
                form.add_field(key, value)
            self.runtime.phase = "submitting to o!rdr"
            try:
                async with self.session.post(
                    f"{self.api_base}/ordr/renders",
                    data=form,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as response:
                    response_text = await response.text()
                    response_status = response.status
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                raise RenderRequestError(f"o!rdr request failed: {error}") from error
        await self.store.set_last_submit(time.time())
        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError:
            payload = {"message": response_text.strip()}
        if response_status not in {200, 201}:
            message = payload.get("message") or f"o!rdr submission rejected with status {response_status}"
            code = payload.get("errorCode") or payload.get("code")
            raise RenderRequestError(message, int(code) if isinstance(code, int) else None, response_status)
        render_id = int(payload.get("renderID") or payload.get("renderId") or payload.get("id"))
        self.runtime.current_render_id = render_id
        self.runtime.render_progress = "Queued"
        logging.info("Created o!rdr render %d (%s)", render_id, payload.get("message", "ok"))
        return render_id

    async def _poll(self, render_id: int) -> tuple[str, dict[str, Any]] | None:
        try:
            async with self.session.get(
                f"{self.api_base}/ordr/renders",
                params={"renderId": render_id},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
            return None
        renders = payload.get("renders")
        if not isinstance(renders, list) or not renders:
            return None
        info = renders[0]
        progress = info.get("progress") or info.get("description")
        if progress:
            self.runtime.render_progress = str(progress)
        status = int(info.get("status", 0) or 0)
        if status == 2:
            return "done", info
        if status in {3, 4}:
            return "failed", info
        return None

    async def wait(self, render_id: int) -> dict[str, Any]:
        deadline = time.time() + self.config.render_timeout
        event = asyncio.Event()
        self.waiters[render_id] = event
        try:
            while time.time() < deadline:
                if render_id in self.outcomes:
                    kind, payload = self.outcomes[render_id]
                    if kind == "done":
                        return payload
                    message = payload.get("errorMessage") or payload.get("description") or "Render failed"
                    code = payload.get("errorCode")
                    raise RenderRequestError(str(message), int(code) if isinstance(code, int) else None)
                polled = await self._poll(render_id)
                if polled:
                    kind, payload = polled
                    if kind == "done":
                        return payload
                    message = payload.get("errorMessage") or payload.get("description") or "Render failed"
                    code = payload.get("errorCode")
                    raise RenderRequestError(str(message), int(code) if isinstance(code, int) else None)
                try:
                    await asyncio.wait_for(event.wait(), timeout=5.0)
                    event.clear()
                except asyncio.TimeoutError:
                    pass
            raise TimeoutError(f"Render {render_id} timed out after {self.config.render_timeout} seconds")
        finally:
            self.waiters.pop(render_id, None)

    async def _dynamic_link(self, render_id: int) -> str | None:
        try:
            async with self.session.get(
                f"{self.api_base}/ordr/renders",
                params={"renderId": render_id},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
            renders = payload.get("renders")
            if isinstance(renders, list) and renders:
                return renders[0].get("videoUrl")
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
            pass
        return None

    async def download(self, render_id: int, payload: dict[str, Any]) -> tuple[Any, float]:
        from pathlib import Path
        destination = self.store.work_dir / f"render-{render_id}.mp4"
        candidates = []
        for key in ("videoUrl", "downloadUrl"):
            value = payload.get(key)
            if value and value not in candidates:
                candidates.append(value)
        if not candidates:
            dynamic = await self._dynamic_link(render_id)
            if dynamic:
                candidates.append(dynamic)
        if not candidates:
            raise RuntimeError(f"Render {render_id} did not provide a downloadable video URL")
        for attempt, url in enumerate(candidates, start=1):
            logging.info("Downloading render %d video from %s", render_id, url)
            self.runtime.phase = f"downloading video ({attempt}/{len(candidates)})"
            self.runtime.render_progress = "Downloading"
            try:
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=1800, sock_read=180)) as response:
                    if response.status != 200:
                        logging.warning("HTTP %d when downloading %s", response.status, url)
                        continue
                    with destination.open("wb") as handle:
                        while chunk := await response.content.read(1024 * 1024):
                            handle.write(chunk)
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                logging.warning("Download failed for render %d (%s): %s", render_id, url, error)
                destination.unlink(missing_ok=True)
                continue
            if destination.is_file() and destination.stat().st_size > 1024 * 1024:
                duration = float(payload.get("videoDuration") or payload.get("duration") or 0.0)
                return destination, duration
            else:
                logging.warning("Downloaded file for render %d is not a valid video", render_id)
            if attempt < 3:
                await asyncio.sleep(10 * attempt)
                refreshed = await self._dynamic_link(render_id)
                if refreshed:
                    candidates.insert(0, refreshed)
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"Could not download a valid video for render {render_id}")
