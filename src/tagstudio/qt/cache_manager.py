# Copyright (C) 2025 Travis Abendshien (CyanVoxel).
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

"""Persistent, size-bounded LRU management for rendered thumbnails."""

import json
import math
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

import structlog
from PIL import Image

from tagstudio.core.constants import THUMB_CACHE_NAME, TS_FOLDER_NAME
from tagstudio.qt.global_settings import (
    DEFAULT_CACHED_IMAGE_QUALITY,
    DEFAULT_THUMB_CACHE_SIZE,
    MIN_THUMB_CACHE_SIZE,
)

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class CacheFolder:
    """Bookkeeping for one on-disk cache folder."""

    path: Path
    size: int


class CacheManager:
    """Manage thumbnail files with a persistent least-recently-used policy.

    Cache entries remain in the existing ``thumbs/<timestamp>`` layout for
    compatibility. File modification times record access order, so the LRU
    ordering survives application restarts without a second metadata database.
    """

    MAX_FOLDER_SIZE = 10  # Number in MiB
    STAT_MULTIPLIER = 1_000_000  # Multiplier to apply to file stats (bytes) to get user units (MiB)
    SETTINGS_NAME = "cache_settings.json"

    def __init__(
        self,
        library_dir: Path,
        max_size: int | float = DEFAULT_THUMB_CACHE_SIZE,
        img_quality: int = DEFAULT_CACHED_IMAGE_QUALITY,
    ):
        """Create a thumbnail cache manager for one library.

        Args:
            library_dir: The folder containing the ``.TagStudio`` library folder.
            max_size: The fallback maximum cache size in MiB. A saved
                per-library override takes precedence when present.
            img_quality: The image quality used when saving PIL images (0-100).
        """
        self._lock = RLock()
        self.cache_path = library_dir / TS_FOLDER_NAME / THUMB_CACHE_NAME
        self.settings_path = self.cache_path.parent / self.SETTINGS_NAME
        configured_size = self._read_configured_size()
        self.max_size = self._size_to_bytes(
            configured_size if configured_size is not None else max_size
        )
        self.img_quality = (
            img_quality if 0 <= img_quality <= 100 else DEFAULT_CACHED_IMAGE_QUALITY
        )

        self.folders: list[CacheFolder] = []
        self._entries: OrderedDict[Path, int] = OrderedDict()
        self.current_size = 0
        self._load_entries()
        self._cull_entries()

    @classmethod
    def _size_to_bytes(cls, max_size: int | float) -> int:
        try:
            size = float(max_size)
        except (TypeError, ValueError):
            size = float(DEFAULT_THUMB_CACHE_SIZE)
        if not math.isfinite(size):
            size = float(DEFAULT_THUMB_CACHE_SIZE)
        return max(
            math.floor(size * cls.STAT_MULTIPLIER),
            math.floor(MIN_THUMB_CACHE_SIZE * cls.STAT_MULTIPLIER),
        )

    @property
    def max_size_mib(self) -> float:
        """Return the effective cache cap in MiB."""
        return self.max_size / self.STAT_MULTIPLIER

    def _read_configured_size(self) -> float | None:
        if not self.settings_path.is_file():
            return None
        try:
            settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
            value = settings.get("thumbnail_cache_size_mib")
            if isinstance(value, int | float) and not isinstance(value, bool):
                value = float(value)
                if math.isfinite(value) and value >= MIN_THUMB_CACHE_SIZE:
                    return value
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            logger.warning("[CacheManager] Could not read cache settings", error=error)
        return None

    def set_max_size_mib(self, max_size: int | float) -> None:
        """Set and persist the current library's cache cap, then enforce it."""
        with self._lock:
            self.max_size = self._size_to_bytes(max_size)
            self._cull_entries()
            try:
                self.settings_path.parent.mkdir(parents=True, exist_ok=True)
                self.settings_path.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "thumbnail_cache_size_mib": self.max_size_mib,
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            except OSError as error:
                logger.warning("[CacheManager] Could not save cache settings", error=error)

    def _load_entries(self) -> None:
        """Load cached files in oldest-to-newest access order."""
        if not self.cache_path.is_dir():
            return

        files: list[tuple[int, str, Path, int]] = []
        try:
            folders = [folder for folder in self.cache_path.iterdir() if folder.is_dir()]
        except OSError as error:
            logger.warning("[CacheManager] Could not enumerate cache folders", error=error)
            return

        for folder in folders:
            try:
                for path in folder.iterdir():
                    if not path.is_file() or path.suffix.lower() != ".webp":
                        continue
                    stat = path.stat()
                    files.append((stat.st_mtime_ns, str(path), path, stat.st_size))
            except OSError as error:
                logger.warning(
                    "[CacheManager] Could not read cache folder", folder=folder, error=error
                )

        for _, _, path, size in sorted(files):
            self._entries[path] = size
            self.current_size += size
            self._adjust_folder_size(path.parent, size)

    def _find_folder(self, path: Path) -> CacheFolder | None:
        for folder in self.folders:
            if folder.path == path:
                return folder
        return None

    def _adjust_folder_size(self, path: Path, delta: int) -> None:
        folder = self._find_folder(path)
        if folder is None:
            if delta <= 0:
                return
            folder = CacheFolder(path, 0)
            self.folders.append(folder)
        folder.size += delta
        if folder.size <= 0:
            self.folders.remove(folder)

    def _mark_folder_recent(self, folder: CacheFolder) -> None:
        try:
            index = self.folders.index(folder)
        except ValueError:
            return
        if index != len(self.folders) - 1:
            self.folders.append(self.folders.pop(index))

    def _create_folder(self) -> CacheFolder:
        self.cache_path.mkdir(parents=True, exist_ok=True)
        stem = str(math.floor(time.time()))
        suffix = 0
        while True:
            folder_path = self.cache_path / (stem if suffix == 0 else f"{stem}-{suffix}")
            try:
                folder_path.mkdir()
            except FileExistsError:
                existing = self._find_folder(folder_path)
                if (
                    existing is not None
                    and existing.size < self.MAX_FOLDER_SIZE * self.STAT_MULTIPLIER
                ):
                    self._mark_folder_recent(existing)
                    return existing
                suffix += 1
                continue
            folder = CacheFolder(folder_path, 0)
            self.folders.append(folder)
            return folder

    def _get_current_folder(self) -> CacheFolder:
        for folder in reversed(self.folders):
            if folder.size < self.MAX_FOLDER_SIZE * self.STAT_MULTIPLIER:
                self._mark_folder_recent(folder)
                return folder
        return self._create_folder()

    def _drop_entry(self, path: Path) -> None:
        size = self._entries.pop(path, None)
        if size is None:
            return
        self.current_size -= size
        self._adjust_folder_size(path.parent, -size)

    def _remove_entry(self, path: Path) -> bool:
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            logger.warning("[CacheManager] Failed to remove cached file", file=path, error=error)
            return False
        self._drop_entry(path)
        self._remove_empty_folder(path.parent)
        return True

    def _remove_empty_folder(self, path: Path) -> None:
        if path == self.cache_path or not path.is_dir():
            return
        try:
            path.rmdir()
        except OSError:
            return
        self.folders = [folder for folder in self.folders if folder.path != path]

    def _cull_entries(self) -> None:
        """Evict least-recently-used files until the configured cap is met."""
        attempts = 0
        while self.current_size > self.max_size and self._entries:
            oldest = next(iter(self._entries))
            if self._remove_entry(oldest):
                attempts = 0
                continue
            self._entries.move_to_end(oldest)
            attempts += 1
            if attempts >= len(self._entries):
                logger.warning(
                    "[CacheManager] Cache remains over size limit because files could not be "
                    "removed",
                    current_size=self.current_size,
                    max_size=self.max_size,
                )
                break

    def clear_cache(self) -> None:
        """Clear all managed thumbnail files and empty cache folders."""
        with self._lock:
            for path in list(self._entries):
                self._remove_entry(path)
            if self.cache_path.is_dir():
                try:
                    for folder in self.cache_path.iterdir():
                        if folder.is_dir():
                            self._remove_empty_folder(folder)
                except OSError as error:
                    logger.warning("[CacheManager] Could not finish clearing cache", error=error)
        logger.info("[CacheManager] Cleared cache!")

    def get_file_path(self, file_name: Path) -> Path | None:
        """Return a cached file and mark it as recently used."""
        with self._lock:
            name = Path(file_name).name
            for path in reversed(list(self._entries)):
                if path.name != name:
                    continue
                if not path.is_file():
                    self._drop_entry(path)
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    self._drop_entry(path)
                    continue
                old_size = self._entries[path]
                if size != old_size:
                    self._entries[path] = size
                    self.current_size += size - old_size
                    self._adjust_folder_size(path.parent, size - old_size)
                self._entries.move_to_end(path)
                try:
                    os.utime(path, None)
                except OSError as error:
                    logger.debug(
                        "[CacheManager] Could not update cache access time", file=path, error=error
                    )
                return path
        return None

    def save_image(self, image: Image.Image, file_name: Path, mode: str = "RGBA") -> None:
        """Save an image to the cache and evict old files if necessary."""
        with self._lock:
            cache_folder = self._get_current_folder()
            cache_file = Path(file_name)
            if cache_file.is_absolute() or ".." in cache_file.parts:
                raise ValueError("Cache file names must be relative to the cache folder")
            file_path = cache_folder.path / cache_file
            file_path.parent.mkdir(parents=True, exist_ok=True)
            image.save(file_path, mode=mode, quality=self.img_quality)

            old_size = self._entries.get(file_path, 0)
            size = file_path.stat().st_size
            if old_size:
                self.current_size -= old_size
                self._adjust_folder_size(file_path.parent, -old_size)
            self._entries[file_path] = size
            self._entries.move_to_end(file_path)
            self.current_size += size
            self._adjust_folder_size(file_path.parent, size)
            self._cull_entries()
