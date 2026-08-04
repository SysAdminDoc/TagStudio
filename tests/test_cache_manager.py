# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

import json
import os
from pathlib import Path

from tagstudio.qt.cache_manager import CacheManager


class FakeImage:
    def __init__(self, size: int) -> None:
        self.size = size

    def save(self, path: Path, *, mode: str, quality: int) -> None:
        del mode, quality
        path.write_bytes(b"x" * self.size)


def _write_cached_file(path: Path, size: int, modified_ns: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, ns=(modified_ns, modified_ns))


def test_cache_manager_evicts_least_recently_used_files(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(CacheManager, "STAT_MULTIPLIER", 1)
    cache_path = tmp_path / ".TagStudio" / "thumbs"
    old_path = cache_path / "old" / "old.webp"
    recent_path = cache_path / "recent" / "recent.webp"
    _write_cached_file(old_path, 4, 1)
    _write_cached_file(recent_path, 4, 2)

    manager = CacheManager(tmp_path, max_size=10)
    assert manager.get_file_path(Path("old.webp")) == old_path

    manager.save_image(FakeImage(4), Path("new.webp"))

    assert old_path.is_file()
    assert not recent_path.exists()
    assert manager.current_size <= manager.max_size


def test_cache_manager_persists_library_limit_and_lru_order(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(CacheManager, "STAT_MULTIPLIER", 1)
    cache_path = tmp_path / ".TagStudio" / "thumbs"
    old_path = cache_path / "old" / "old.webp"
    recent_path = cache_path / "recent" / "recent.webp"
    _write_cached_file(old_path, 4, 1)
    _write_cached_file(recent_path, 4, 2)

    manager = CacheManager(tmp_path, max_size=10)
    manager.get_file_path(Path("old.webp"))
    manager.set_max_size_mib(25)

    restarted = CacheManager(tmp_path, max_size=500)
    assert restarted.max_size_mib == 25
    restarted.save_image(FakeImage(20), Path("new.webp"))

    assert old_path.is_file()
    assert not recent_path.exists()
    settings = json.loads((tmp_path / ".TagStudio" / "cache_settings.json").read_text())
    assert settings["thumbnail_cache_size_mib"] == 25.0


def test_cache_manager_clear_cache_removes_files_and_resets_size(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(CacheManager, "STAT_MULTIPLIER", 1)
    cache_path = tmp_path / ".TagStudio" / "thumbs" / "cached"
    cached_path = cache_path / "cached.webp"
    _write_cached_file(cached_path, 3, 1)

    manager = CacheManager(tmp_path, max_size=10)
    manager.clear_cache()

    assert manager.current_size == 0
    assert not cached_path.exists()
