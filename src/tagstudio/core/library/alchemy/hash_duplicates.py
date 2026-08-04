# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

"""Exact duplicate detection using streamed BLAKE3 hashes."""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from blake3 import blake3

from tagstudio.core.library.alchemy.library import Library


@dataclass(frozen=True, slots=True)
class HashDuplicateGroup:
    """A set of entries with the same size and BLAKE3 digest."""

    digest: str
    size: int
    entry_ids: tuple[int, ...]

    @property
    def count(self) -> int:
        """Return the number of duplicate entries in this group."""
        return len(self.entry_ids)


class HashDuplicateScanner:
    """Find exact duplicate files without loading entire files into memory."""

    CHUNK_SIZE = 1024 * 1024

    def __init__(self, library: Library) -> None:
        self.library = library

    @staticmethod
    def hash_file(path: Path) -> str:
        """Return the hexadecimal BLAKE3 digest for a file."""
        digest = blake3()
        with path.open("rb") as file:
            while chunk := file.read(HashDuplicateScanner.CHUNK_SIZE):
                digest.update(chunk)
        return digest.hexdigest()

    def scan(
        self,
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> tuple[HashDuplicateGroup, ...]:
        """Hash only same-size candidates and return deterministic duplicate groups.

        ``progress`` receives ``(processed_candidates, total_candidates)`` after each file.
        Files that disappear or become unreadable during the pass are skipped.
        """
        by_size: dict[int, list[tuple[int, Path]]] = defaultdict(list)
        for entry in self.library.all_entries():
            path = self.library.resolve_entry_path(entry)
            if not path.is_file() or self.library.is_path_ignored(path):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            by_size[size].append((entry.id, path))

        candidates = [
            (size, entry_id, path)
            for size in sorted(by_size)
            if len(by_size[size]) > 1
            for entry_id, path in by_size[size]
        ]
        total = len(candidates)
        hashed = 0
        groups: dict[tuple[int, str], list[int]] = defaultdict(list)
        for size, entry_id, path in candidates:
            try:
                digest = self.hash_file(path)
            except OSError:
                continue
            groups[(size, digest)].append(entry_id)
            hashed += 1
            if progress is not None:
                progress(hashed, total)

        return tuple(
            HashDuplicateGroup(digest=digest, size=size, entry_ids=tuple(entry_ids))
            for (size, digest), entry_ids in sorted(groups.items(), key=lambda item: item[0])
            if len(entry_ids) > 1
        )
