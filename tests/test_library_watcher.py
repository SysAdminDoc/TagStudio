# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

import sys
import time
from pathlib import Path

import pytest

from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.library.refresh import IncrementalScanner
from tagstudio.core.library.watcher import (
    FileSystemEvent,
    FileSystemEventKind,
    LibraryWatcher,
)
from tagstudio.core.utils.types import unwrap


def test_incremental_scanner_adds_moves_and_tracks_deleted_files(library: Library):
    root = unwrap(library.library_dir)
    original_path = root / "live.txt"
    moved_path = root / "renamed.txt"
    original_path.write_text("first", encoding="utf-8")

    scanner = IncrementalScanner(library)
    added = scanner.apply(
        [FileSystemEvent(FileSystemEventKind.CREATED, original_path)]
    )
    assert len(added.added_ids) == 1
    entry_id = added.added_ids[0]

    moved_path.write_text("first", encoding="utf-8")
    original_path.unlink()
    move_result = scanner.apply(
        [
            FileSystemEvent(
                FileSystemEventKind.MOVED,
                moved_path,
                old_path=original_path,
            )
        ]
    )
    assert move_result.moved_ids == (entry_id,)
    assert library.get_entry_full_by_file_path(original_path) is None
    assert library.get_entry_full_by_file_path(moved_path) is not None

    moved_path.unlink()
    delete_result = scanner.apply([FileSystemEvent(FileSystemEventKind.DELETED, moved_path)])
    assert delete_result.deleted_ids == (entry_id,)
    assert library.get_entry(entry_id) is not None


@pytest.mark.skipif(sys.platform != "win32", reason="ReadDirectoryChangesW is Windows-only")
def test_windows_watcher_reports_created_files(tmp_path: Path):
    events: list[FileSystemEvent] = []
    watcher = LibraryWatcher((tmp_path,), events.append)
    watcher.start()
    try:
        time.sleep(0.1)
        target = tmp_path / "watched.txt"
        target.write_text("watched", encoding="utf-8")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not events:
            time.sleep(0.05)
    finally:
        watcher.stop()

    assert any(
        event.path == target
        and event.kind in (FileSystemEventKind.CREATED, FileSystemEventKind.MODIFIED)
        for event in events
    )
