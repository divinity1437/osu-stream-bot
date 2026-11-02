import os
import shutil
import time
from pathlib import Path
import pytest
from aiohttp import web

from config import Config
from danser_renderer import BeatmapLibrary, DanserError


class DummyRuntime:
    phase = "idle"


def test_prune_cache_protects_current_and_sorts_lru(tmp_path):
    songs_dir = tmp_path / "songs"
    work_dir = tmp_path / "work"
    songs_dir.mkdir()
    work_dir.mkdir()

    cfg = Config.from_env(root=tmp_path)
    object.__setattr__(cfg, "danser_songs_dir", songs_dir)
    object.__setattr__(cfg, "danser_dir", tmp_path)
    # Set limit to 2.5 MB
    object.__setattr__(cfg, "danser_max_songs_bytes", int(2.5 * 1024 * 1024))

    lib = BeatmapLibrary(cfg, None, DummyRuntime())

    # Create 3 beatmap folders (1 MB each)
    folder1 = songs_dir / "101"
    folder2 = songs_dir / "102"
    folder3 = songs_dir / "103"

    folder1.mkdir()
    folder2.mkdir()
    folder3.mkdir()

    (folder1 / "101.osu").write_bytes(b"x" * (1024 * 1024))
    (folder2 / "102.osu").write_bytes(b"x" * (1024 * 1024))
    (folder3 / "103.osu").write_bytes(b"x" * (1024 * 1024))

    # Mark timestamps explicitly: 101 oldest, 102 middle, 103 newest
    now = time.time()
    os.utime(folder1, (now - 300, now - 300))
    os.utime(folder2, (now - 200, now - 200))
    os.utime(folder3, (now - 100, now - 100))

    # Scenario 1: folder1 is protected even though it is the oldest
    freed = lib.prune_cache(protected={folder1})
    assert freed > 0
    # folder1 should still exist because it was protected
    assert folder1.is_dir(), "folder1 must be preserved because it was protected"
    # folder2 (the next oldest) should have been pruned
    assert not folder2.exists(), "folder2 should be pruned as the oldest unprotected entry"
    # folder3 (newest) should still exist
    assert folder3.is_dir(), "folder3 should remain"


def test_mark_used_updates_top_level_mtime(tmp_path):
    songs_dir = tmp_path / "songs"
    songs_dir.mkdir()

    cfg = Config.from_env(root=tmp_path)
    object.__setattr__(cfg, "danser_songs_dir", songs_dir)
    object.__setattr__(cfg, "danser_dir", tmp_path)

    lib = BeatmapLibrary(cfg, None, DummyRuntime())

    folder = songs_dir / "set_555" / "nested"
    folder.mkdir(parents=True)
    osu_file = folder / "map.osu"
    osu_file.write_bytes(b"osu data")

    # Set old timestamp
    old_time = time.time() - 1000
    top_entry = songs_dir / "set_555"
    os.utime(top_entry, (old_time, old_time))
    assert top_entry.stat().st_mtime < time.time() - 500

    # Call _mark_used
    marked = lib._mark_used(osu_file)
    assert marked == top_entry
    assert top_entry.stat().st_mtime >= time.time() - 2
