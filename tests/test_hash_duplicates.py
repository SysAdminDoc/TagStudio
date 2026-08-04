# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

from pathlib import Path

from tagstudio.core.library.alchemy.hash_duplicates import HashDuplicateScanner
from tagstudio.core.library.alchemy.models import Entry
from tagstudio.core.utils.types import unwrap


def test_hash_file_streams_blake3_digest(tmp_path: Path):
    path = tmp_path / "sample.bin"
    path.write_bytes(b"TagStudio" * 1000)

    digest = HashDuplicateScanner.hash_file(path)

    assert len(digest) == 64
    assert digest == HashDuplicateScanner.hash_file(path)


def test_hash_duplicate_scan_groups_same_content_and_reports_progress(library):
    root = unwrap(library.library_dir)
    folder = unwrap(library.folder)
    first_path = root / "first.bin"
    second_path = root / "second.bin"
    different_path = root / "different.bin"
    first_path.write_bytes(b"same content")
    second_path.write_bytes(b"same content")
    different_path.write_bytes(b"other data!")

    entries = [
        Entry(path=Path(path.name), folder=folder, fields=[])
        for path in (first_path, second_path, different_path)
    ]
    assert library.add_entries(entries)
    progress: list[tuple[int, int]] = []

    groups = HashDuplicateScanner(library).scan(
        progress=lambda current, total: progress.append((current, total))
    )

    assert len(groups) == 1
    assert groups[0].entry_ids == (entries[0].id, entries[1].id)
    assert groups[0].size == len(b"same content")
    assert progress[-1] == (2, 2)
