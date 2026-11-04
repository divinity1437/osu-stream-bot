import asyncio
from pathlib import Path
from aiohttp import web
import pytest

from stream_broadcaster import BroadcasterConfig, StreamBroadcaster


def test_broadcaster_config_from_env(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "STREAM_TWITCH_KEY=test_twitch_123\n"
        "STREAM_YOUTUBE_KEY=test_yt_456\n"
        "STREAM_DRY_RUN=true\n"
        "STREAM_API_URL=http://127.0.0.1:8787\n",
        encoding="utf-8",
    )
    config = BroadcasterConfig.from_env(tmp_path)
    assert config.twitch_key == "test_twitch_123"
    assert config.youtube_key == "test_yt_456"
    assert config.dry_run is True
    assert config.api_url == "http://127.0.0.1:8787"

    broadcaster = StreamBroadcaster(config)
    dests = broadcaster.get_destinations()
    assert any("test_twitch_123" in d for d in dests)
    assert any("test_yt_456" in d for d in dests)


def test_broadcaster_api_flow(tmp_path: Path):
    async def _test():
        calls = []

        async def mock_next(request):
            calls.append("next")
            if len(calls) == 1:
                return web.json_response({"available": False, "phase": "standby"})
            return web.json_response({
                "available": True,
                "name": "000001-replay.mp4",
                "url": "/video/000001-replay.mp4",
                "label": "Player - Song [Diff]",
            })

        async def mock_complete(request):
            data = await request.json()
            calls.append(f"complete:{data.get('name')}")
            return web.json_response({"completed": True})

        async def mock_player_state(request):
            return web.json_response({"current_video": "000001-replay.mp4", "paused": False})

        app = web.Application()
        app.router.add_get("/api/next", mock_next)
        app.router.add_post("/api/complete", mock_complete)
        app.router.add_get("/api/player-state", mock_player_state)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 18787)
        await site.start()

        try:
            config = BroadcasterConfig(
                api_url="http://127.0.0.1:18787",
                twitch_key="",
                twitch_url="rtmp://live.twitch.tv/app",
                youtube_key="",
                youtube_url="rtmp://a.rtmp.youtube.com/live2",
                custom_rtmp_url="",
                standby_path=tmp_path / "standby.mp4",
                queue_dir=tmp_path,
                poll_interval=0.1,
                dry_run=True,
            )
            broadcaster = StreamBroadcaster(config)

            import aiohttp
            async with aiohttp.ClientSession() as session:
                item1 = await broadcaster._fetch_next(session)
                assert item1["available"] is False

                item2 = await broadcaster._fetch_next(session)
                assert item2["available"] is True
                assert item2["name"] == "000001-replay.mp4"

                completed = await broadcaster._complete_replay(session, "000001-replay.mp4", "ended")
                assert completed is True
                assert "complete:000001-replay.mp4" in calls
        finally:
            await runner.cleanup()

    asyncio.run(_test())
