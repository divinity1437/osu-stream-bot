import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value not in {None, ""} else default


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value not in {None, ""} else default


@dataclass(frozen=True)
class Config:
    root: Path
    run_mode: str
    render_backend: str
    developer_mode: bool
    api_key: str
    skin: str
    custom_skin: bool
    dt_skin: str
    dt_custom_skin: bool
    resolution: str
    render_timeout: int
    submit_interval: float
    replay_url: str
    scores_url: str
    score_id_min: int
    score_id_max: int
    replay_attempts: int
    buffer_size: int
    delete_after_play: bool
    producer_enabled: bool
    web_host: str
    web_port: int
    max_upload_bytes: int
    max_background_bytes: int
    initial_volume: int
    discord_enabled: bool
    discord_token: str
    discord_guild_id: int
    discord_channel_id: int
    discord_queue_limit: int
    discord_user_cooldown_seconds: int
    discord_max_file_bytes: int
    danser_dir: Path
    danser_executable: Path
    danser_songs_dir: Path
    danser_output_dir: Path
    danser_settings: str
    danser_skins_dir: str
    danser_skin: str
    danser_random_skin: bool
    danser_use_skin_cursor: bool
    danser_replay_overlay: bool
    danser_background_dim_percent: int
    danser_parallax_enabled: bool
    danser_parallax_amount_percent: int
    danser_parallax_speed_percent: int
    danser_render_timeout: int
    danser_max_map_bytes: int
    controller_url: str = ""

    @classmethod
    def from_env(cls, root: Path | None = None) -> "Config":
        base_root = (root or ROOT).resolve()
        run_mode = os.getenv("RUN_MODE", "standalone").strip().lower()
        if run_mode not in {"standalone", "controller", "render-worker"}:
            raise ValueError(f"Unsupported RUN_MODE: {run_mode}. Must be 'standalone', 'controller', or 'render-worker'")

        render_backend = os.getenv("RENDER_BACKEND", "ordr").strip().lower()
        developer_mode = env_bool("ORDR_DEVELOPER_MODE", True)
        api_key = os.getenv("ORDR_API_KEY", "").strip()
        configured_interval = env_float("ORDR_MIN_SUBMIT_INTERVAL_SECONDS", 0)
        if configured_interval > 0:
            submit_interval = configured_interval
        elif developer_mode:
            submit_interval = 2
        elif api_key:
            submit_interval = 5
        else:
            submit_interval = 315

        danser_dir = Path(os.getenv("DANSER_DIR", "tools/danser")).expanduser()
        if not danser_dir.is_absolute():
            danser_dir = base_root / danser_dir
        else:
            danser_dir = danser_dir.resolve()

        controller_url = os.getenv("CONTROLLER_URL", "").strip()

        config = cls(
            root=base_root,
            run_mode=run_mode,
            render_backend=render_backend,
            developer_mode=developer_mode,
            api_key=api_key,
            skin=os.getenv("ORDR_SKIN", "whitecatCK1.0").strip(),
            custom_skin=env_bool("ORDR_CUSTOM_SKIN", False),
            dt_skin=os.getenv("ORDR_DT_SKIN", "RafisHDDT2018-03-26").strip(),
            dt_custom_skin=env_bool("ORDR_DT_CUSTOM_SKIN", False),
            resolution=os.getenv("ORDR_RESOLUTION", "1280x720").strip(),
            render_timeout=env_int("ORDR_RENDER_TIMEOUT_SECONDS", 1800),
            submit_interval=submit_interval,
            replay_url=os.getenv("OSU_REPLAY_URL", "https://api.osuokayu.pw/v1/get_replay").strip(),
            scores_url=os.getenv("OSU_SCORES_URL", "https://api.osuokayu.pw/v2/scores").strip(),
            score_id_min=env_int("SCORE_ID_MIN", 1),
            score_id_max=env_int("SCORE_ID_MAX", 0),
            replay_attempts=env_int("RANDOM_REPLAY_ATTEMPTS", 150),
            buffer_size=env_int("BUFFER_SIZE", 2),
            delete_after_play=env_bool("DELETE_AFTER_PLAY", True),
            producer_enabled=env_bool("PRODUCER_ENABLED", True),
            web_host=os.getenv("WEB_HOST", "127.0.0.1").strip(),
            web_port=env_int("WEB_PORT", 8787),
            max_upload_bytes=env_int("MAX_UPLOAD_MIB", 20) * 1024 * 1024,
            max_background_bytes=env_int("MAX_BACKGROUND_MIB", 20) * 1024 * 1024,
            initial_volume=env_int("PLAYER_VOLUME", 20),
            discord_enabled=env_bool("DISCORD_ENABLED", False),
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            discord_guild_id=env_int("DISCORD_GUILD_ID", 0),
            discord_channel_id=env_int("DISCORD_CHANNEL_ID", 0),
            discord_queue_limit=env_int("DISCORD_QUEUE_LIMIT", 50),
            discord_user_cooldown_seconds=env_int("DISCORD_USER_COOLDOWN_SECONDS", 300),
            discord_max_file_bytes=env_int("DISCORD_MAX_FILE_MIB", 10) * 1024 * 1024,
            danser_dir=danser_dir,
            danser_executable=(
                danser_dir / ("danser-cli.exe" if sys.platform.startswith("win") else "danser-cli")
            ),
            danser_songs_dir=danser_dir / "songs",
            danser_output_dir=danser_dir / "videos",
            danser_settings=os.getenv("DANSER_SETTINGS", "stream-cpu").strip(),
            danser_skins_dir=os.getenv("DANSER_SKINS_DIR", "auto").strip(),
            danser_skin=os.getenv("DANSER_SKIN", "default").strip(),
            danser_random_skin=env_bool("DANSER_RANDOM_SKIN", False),
            danser_use_skin_cursor=env_bool("DANSER_USE_SKIN_CURSOR", True),
            danser_replay_overlay=env_bool("DANSER_REPLAY_OVERLAY", True),
            danser_background_dim_percent=env_int("DANSER_BACKGROUND_DIM_PERCENT", 95),
            danser_parallax_enabled=env_bool("DANSER_PARALLAX_ENABLED", True),
            danser_parallax_amount_percent=env_int("DANSER_PARALLAX_AMOUNT_PERCENT", 10),
            danser_parallax_speed_percent=env_int("DANSER_PARALLAX_SPEED_PERCENT", 50),
            danser_render_timeout=env_int("DANSER_RENDER_TIMEOUT_SECONDS", 3600),
            danser_max_map_bytes=env_int("DANSER_MAX_MAP_MIB", 300) * 1024 * 1024,
            controller_url=controller_url,
        )

        config._validate()
        return config

    def _validate(self) -> None:
        if self.run_mode == "render-worker":
            self._validate_render_worker()
            return

        if self.run_mode == "controller":
            self._validate_controller()
            return

        self._validate_standalone()

    def _validate_standalone(self) -> None:
        if self.render_backend not in {"ordr", "danser"}:
            raise ValueError("RENDER_BACKEND must be either 'ordr' or 'danser'")
        if self.resolution not in {"960x540", "1280x720", "1920x1080", "3840x2160"}:
            raise ValueError(f"Unsupported ORDR_RESOLUTION: {self.resolution}")
        if self.buffer_size < 1:
            raise ValueError("BUFFER_SIZE must be at least 1")
        if self.score_id_min < 1:
            raise ValueError("SCORE_ID_MIN must be positive")
        if not 0 <= self.initial_volume <= 100:
            raise ValueError("PLAYER_VOLUME must be between 0 and 100")
        if self.max_background_bytes < 1024 * 1024:
            raise ValueError("MAX_BACKGROUND_MIB must be at least 1")
        if self.discord_enabled and not all(
            (self.discord_token, self.discord_guild_id, self.discord_channel_id)
        ):
            raise ValueError("DISCORD_TOKEN, DISCORD_GUILD_ID and DISCORD_CHANNEL_ID are required when Discord is enabled")
        if self.discord_queue_limit < 1:
            raise ValueError("DISCORD_QUEUE_LIMIT must be at least 1")
        if not self.danser_settings or "/" in self.danser_settings or "\\" in self.danser_settings:
            raise ValueError("DANSER_SETTINGS must be a simple settings profile name")
        if not self.danser_skins_dir:
            raise ValueError("DANSER_SKINS_DIR must be 'auto' or a directory path")
        if not self.danser_skin or "/" in self.danser_skin or "\\" in self.danser_skin:
            raise ValueError("DANSER_SKIN must be a simple skin directory name")
        if not 0 <= self.danser_background_dim_percent <= 100:
            raise ValueError("DANSER_BACKGROUND_DIM_PERCENT must be between 0 and 100")
        if not 0 <= self.danser_parallax_amount_percent <= 100:
            raise ValueError("DANSER_PARALLAX_AMOUNT_PERCENT must be between 0 and 100")
        if not 0 <= self.danser_parallax_speed_percent <= 100:
            raise ValueError("DANSER_PARALLAX_SPEED_PERCENT must be between 0 and 100")
        if self.danser_render_timeout < 60:
            raise ValueError("DANSER_RENDER_TIMEOUT_SECONDS must be at least 60")
        if self.danser_max_map_bytes < 10 * 1024 * 1024:
            raise ValueError("DANSER_MAX_MAP_MIB must be at least 10")

    def _validate_controller(self) -> None:
        if self.buffer_size < 1:
            raise ValueError("BUFFER_SIZE must be at least 1")
        if self.score_id_min < 1:
            raise ValueError("SCORE_ID_MIN must be positive")
        if not 0 <= self.initial_volume <= 100:
            raise ValueError("PLAYER_VOLUME must be between 0 and 100")
        if self.max_background_bytes < 1024 * 1024:
            raise ValueError("MAX_BACKGROUND_MIB must be at least 1")
        if self.discord_enabled and not all(
            (self.discord_token, self.discord_guild_id, self.discord_channel_id)
        ):
            raise ValueError("DISCORD_TOKEN, DISCORD_GUILD_ID and DISCORD_CHANNEL_ID are required when Discord is enabled")
        if self.discord_queue_limit < 1:
            raise ValueError("DISCORD_QUEUE_LIMIT must be at least 1")

    def _validate_render_worker(self) -> None:
        if not self.controller_url:
            raise ValueError("CONTROLLER_URL is required when RUN_MODE is 'render-worker'")
        if not self.danser_settings or "/" in self.danser_settings or "\\" in self.danser_settings:
            raise ValueError("DANSER_SETTINGS must be a simple settings profile name")
        if not self.danser_skins_dir:
            raise ValueError("DANSER_SKINS_DIR must be 'auto' or a directory path")
        if not self.danser_skin or "/" in self.danser_skin or "\\" in self.danser_skin:
            raise ValueError("DANSER_SKIN must be a simple skin directory name")
        if not 0 <= self.danser_background_dim_percent <= 100:
            raise ValueError("DANSER_BACKGROUND_DIM_PERCENT must be between 0 and 100")
        if not 0 <= self.danser_parallax_amount_percent <= 100:
            raise ValueError("DANSER_PARALLAX_AMOUNT_PERCENT must be between 0 and 100")
        if not 0 <= self.danser_parallax_speed_percent <= 100:
            raise ValueError("DANSER_PARALLAX_SPEED_PERCENT must be between 0 and 100")
        if self.danser_render_timeout < 60:
            raise ValueError("DANSER_RENDER_TIMEOUT_SECONDS must be at least 60")
        if self.danser_max_map_bytes < 10 * 1024 * 1024:
            raise ValueError("DANSER_MAX_MAP_MIB must be at least 10")


def check_role_requirements(config: Config) -> None:
    if config.run_mode == "controller":
        required = [
            config.root / "static" / "player.html",
            config.root / "static" / "control.html",
            config.root / "static" / "fallback.svg",
        ]
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise SystemExit("Missing files: " + ", ".join(missing))
        print(f"Role:           Controller")
        print(f"Project:        {config.root}")
        print(f"Python:         {sys.version.split()[0]}")
        print(f"Player URL:     http://{config.web_host}:{config.web_port}/")
        print(f"Control URL:    http://{config.web_host}:{config.web_port}/control")
        print(f"Buffer target:  {config.buffer_size}")
        print(f"Discord intake: {'enabled' if config.discord_enabled else 'disabled'}")
        print("Controller configuration check passed")
        return

    if config.run_mode == "render-worker":
        if not config.controller_url:
            raise SystemExit("Missing configuration: CONTROLLER_URL is required for render-worker")

        required = [
            config.danser_executable,
            config.danser_dir / "settings" / f"{config.danser_settings}.json",
            config.danser_dir / "assets.dpak",
        ]
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise SystemExit("Missing worker files: " + ", ".join(missing))

        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        missing_binaries = []
        if not ffmpeg:
            missing_binaries.append("ffmpeg")
        if not ffprobe:
            missing_binaries.append("ffprobe")
        if missing_binaries:
            raise SystemExit("Missing required binaries: " + ", ".join(missing_binaries))

        print(f"Role:           Render Worker")
        print(f"Controller:     {config.controller_url}")
        print(f"Python:         {sys.version.split()[0]}")
        print(f"danser:         {config.danser_executable}")
        print(f"settings:       {config.danser_settings}.json")
        print(f"ffmpeg:         {ffmpeg}")
        print(f"ffprobe:        {ffprobe}")
        print("Render-worker configuration check passed")
        return

    # Standalone mode
    required = [
        config.root / "static" / "player.html",
        config.root / "static" / "control.html",
        config.root / "static" / "fallback.svg",
    ]
    if config.render_backend == "danser":
        required.extend(
            [
                config.danser_executable,
                config.danser_dir / "settings" / f"{config.danser_settings}.json",
                config.danser_dir / "assets.dpak",
            ]
        )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise SystemExit("Missing files: " + ", ".join(missing))
    ffprobe = shutil.which("ffprobe")
    print(f"Project:        {config.root}")
    print(f"Python:         {sys.version.split()[0]}")
    print(f"ffprobe:        {ffprobe or 'missing'}")
    print(f"Player URL:     http://{config.web_host}:{config.web_port}/")
    print(f"Control URL:    http://{config.web_host}:{config.web_port}/control")
    print(f"Render backend: {config.render_backend}")
    if config.render_backend == "ordr":
        print(f"o!rdr mode:     {'developer simulation' if config.developer_mode else 'real renders'}")
        print(f"o!rdr API key:  {'configured' if config.api_key else 'not configured'}")
    else:
        print(f"danser:         {config.danser_executable}")
        print("danser output:  1280x720 @ 60 FPS")
    print(f"Buffer target:  {config.buffer_size}")
    print(f"Discord intake: {'enabled' if config.discord_enabled else 'disabled'}")
    print("Configuration check passed")
