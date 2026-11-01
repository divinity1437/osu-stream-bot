import os
from pathlib import Path

import pytest

from config import Config
from replay_source import format_mods, parse_replay_header


ROOT = Path(__file__).resolve().parent.parent


def test_standalone_mode_initialization(monkeypatch):
    monkeypatch.delenv("RUN_MODE", raising=False)
    monkeypatch.setenv("RENDER_BACKEND", "danser")

    config = Config.from_env(root=ROOT)
    assert config.run_mode == "standalone"
    assert config.render_backend == "danser"
    assert config.root == ROOT
    assert config.buffer_size >= 1
    assert config.initial_volume >= 0


def test_controller_mode_validation(monkeypatch):
    monkeypatch.setenv("RUN_MODE", "controller")

    config = Config.from_env(root=ROOT)
    assert config.run_mode == "controller"


def test_render_worker_mode_validation(monkeypatch):
    monkeypatch.setenv("RUN_MODE", "render-worker")
    monkeypatch.delenv("CONTROLLER_URL", raising=False)

    with pytest.raises(ValueError, match="CONTROLLER_URL is required"):
        Config.from_env(root=ROOT)

    monkeypatch.setenv("CONTROLLER_URL", "http://127.0.0.1:8787")
    config = Config.from_env(root=ROOT)
    assert config.run_mode == "render-worker"
    assert config.controller_url == "http://127.0.0.1:8787"


def test_existing_replay_header_parsing():
    # Test on existing sample replay files in submissions/originals
    sample_hddtrx = ROOT / "submissions" / "originals" / "1793434616-discord-273750512615292930-02ef1634fdb5.osr"
    sample_hr = ROOT / "submissions" / "originals" / "1793435341-discord-273750512615292930-fd399155e40b.osr"

    if sample_hddtrx.is_file():
        mode, mods = parse_replay_header(sample_hddtrx)
        assert mode == 0
        assert mods == 200
        assert format_mods(mods) == "HDDTRX"

    if sample_hr.is_file():
        mode, mods = parse_replay_header(sample_hr)
        assert mode == 0
        assert mods == 16
        assert format_mods(mods) == "HR"


def test_synthetic_replay_header_parsing(tmp_path):
    # Construct a valid standard osu replay header
    # mode (1 byte: 0) + game_version (4 bytes) + 3 osu strings (beatmap md5, player, replay md5)
    # followed by fixed statistics (23 bytes, where mods is at offset 19: unpack '<I')
    def encode_uleb128(n: int) -> bytes:
        res = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            if n != 0:
                res.append(b | 0x80)
            else:
                res.append(b)
                break
        return bytes(res)

    def encode_osu_string(s: str) -> bytes:
        raw = s.encode("utf-8")
        return b"\x0b" + encode_uleb128(len(raw)) + raw

    content = bytearray()
    content.append(0)  # mode 0 (osu standard)
    content.extend((20201102).to_bytes(4, "little"))  # version
    content.extend(encode_osu_string("beatmap_hash"))
    content.extend(encode_osu_string("player_name"))
    content.extend(encode_osu_string("replay_hash"))

    # Fixed statistics length = 23 bytes (12 + 4 + 2 + 1 + 4)
    # The first 19 bytes can be zeroes or stats
    # Offset 19 is a 4-byte unsigned int for mods (e.g. 72 for HD+DT => (1<<3) | (1<<6) = 8 + 64 = 72)
    stats = bytearray(19)
    mods_value = 72
    stats.extend(mods_value.to_bytes(4, "little"))
    content.extend(stats)

    dummy_replay = tmp_path / "test.osr"
    dummy_replay.write_bytes(bytes(content))

    mode, mods = parse_replay_header(dummy_replay)
    assert mode == 0
    assert mods == 72
    assert format_mods(mods) == "HDDT"
