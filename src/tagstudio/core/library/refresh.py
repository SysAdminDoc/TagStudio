# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio


import shutil
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime as dt
from pathlib import Path
from time import time

import structlog
from wcmatch import pathlib

from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.library.alchemy.models import Entry
from tagstudio.core.library.ignore import PATH_GLOB_FLAGS, ignore_to_glob
from tagstudio.core.library.watch_rules import WatchRuleEngine
from tagstudio.core.library.watcher import FileSystemEvent, FileSystemEventKind
from tagstudio.core.utils.silent_subprocess import silent_run  # pyright: ignore
from tagstudio.core.utils.types import unwrap

logger = structlog.get_logger(__name__)


@dataclass
class RefreshTracker:
    library: Library
    files_not_in_library: list[Path] = field(default_factory=list)
    _pending_files: list[tuple[int, Path]] = field(default_factory=list, init=False, repr=False)
    watch_rule_engine: WatchRuleEngine = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.watch_rule_engine = WatchRuleEngine(self.library)

    @property
    def files_count(self) -> int:
        return len(self.files_not_in_library)

    def save_new_files(self) -> Iterator[int]:
        """Save the list of files that are not in the library."""
        batch_size = 200
        pending_files = self._pending_files or [
            (unwrap(self.library.folder).id, entry_path) for entry_path in self.files_not_in_library
        ]

        index = 0
        while index < len(pending_files):
            yield index
            end = min(len(pending_files), index + batch_size)
            entries = [
                Entry(
                    path=entry_path,
                    folder_id=folder_id,
                    fields=self.library.default_fields_for_path(entry_path, folder=folder_id),
                    date_added=dt.now(),
                )
                for folder_id, entry_path in pending_files[index:end]
            ]
            new_ids = self.library.add_entries(entries)
            for entry_id, entry in zip(new_ids, entries, strict=False):
                auto_tag_ids = self.library.auto_tag_ids_for_path(
                    entry.path, folder=entry.folder_id
                )
                if auto_tag_ids:
                    self.library.add_tags_to_entries(entry_id, auto_tag_ids)
                self.watch_rule_engine.apply_to_entry(entry_id, entry.path)
            index = end
        self._pending_files = []
        self.files_not_in_library = []
        self.watch_rule_engine.apply_to_all()

    def refresh_dir(self, library_dir: Path, force_internal_tools: bool = False) -> Iterator[int]:
        """Scan a directory for files, and add those relative filenames to internal variables.

        Args:
            library_dir (Path): The library directory.
            force_internal_tools (bool): Option to force the use of internal tools for scanning
                (i.e. wcmatch) instead of using tools found on the system (i.e. ripgrep).
        """
        return self.refresh_dirs((library_dir,), force_internal_tools=force_internal_tools)

    def refresh_dirs(
        self,
        library_dirs: Iterable[Path],
        force_internal_tools: bool = False,
    ) -> Iterator[int]:
        """Scan all configured roots and retain each new file's owning folder."""
        if self.library.library_dir is None:
            raise ValueError("No library directory set.")

        roots = tuple(Path(root) for root in library_dirs)
        if not roots:
            raise ValueError("At least one library directory is required.")

        self.files_not_in_library = []
        self._pending_files = []
        return self.__refresh_roots(roots, force_internal_tools)

    def __refresh_roots(self, roots: tuple[Path, ...], force_internal_tools: bool) -> Iterator[int]:
        total_count = 0
        for root in roots:
            folder = self.library.add_root(root)
            ignore_patterns = self.library.get_scan_ignore_patterns(root)

            if force_internal_tools:
                scanner = self.__wc_add(root, ignore_to_glob(ignore_patterns), folder.id)
            else:
                dir_list = self.__get_dir_list(root, ignore_patterns)
                # Use ripgrep if it was found and working, else fallback to wcmatch.
                if dir_list is not None:
                    scanner = self.__rg_add(root, dir_list, folder.id)
                else:
                    scanner = self.__wc_add(root, ignore_to_glob(ignore_patterns), folder.id)

            root_count = 0
            for count in scanner:
                root_count = count
                yield total_count + count
            total_count += root_count

    def __get_dir_list(self, library_dir: Path, ignore_patterns: list[str]) -> list[str] | None:
        """Use ripgrep to return a list of matched directories and files.

        Return `None` if ripgrep not found on system.
        """
        rg_path = shutil.which("rg")
        # Use ripgrep if found on system
        if rg_path is not None:
            logger.info("[Refresh: Using ripgrep for scanning]")

            # Secondary roots do not have their own .TagStudio directory, so keep the compiled
            # ignore file in the OS temp directory and remove it after ripgrep exits.
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix="tagstudio-ignore-",
                suffix=".txt",
                delete=False,
            ) as pattern_file:
                pattern_file.write("\n".join(ignore_patterns))
                compiled_ignore_path = Path(pattern_file.name)

            try:
                result = silent_run(
                    " ".join(
                        [
                            "rg",
                            "--files",
                            "--follow",
                            "--hidden",
                            "--ignore-file",
                            f'"{str(compiled_ignore_path)}"',
                        ]
                    ),
                    cwd=library_dir,
                    capture_output=True,
                    shell=True,
                    encoding="UTF-8",
                )
            finally:
                compiled_ignore_path.unlink(missing_ok=True)

            if result.stderr:
                logger.error(result.stderr)

            return result.stdout.splitlines()  # pyright: ignore [reportReturnType]

        logger.warning("[Refresh: ripgrep not found on system]")
        return None

    def __rg_add(self, library_dir: Path, dir_list: list[str], folder_id: int) -> Iterator[int]:
        start_time_total = time()
        start_time_loop = time()
        dir_file_count = 0
        for r in dir_list:
            relative_path = pathlib.Path(r)
            absolute_path = (library_dir / relative_path).resolve(strict=False)

            end_time_loop = time()
            # Yield output every 1/30 of a second
            if (end_time_loop - start_time_loop) > 0.034:
                yield dir_file_count
                start_time_loop = time()

            # Skip if the file/path is already mapped in the Library
            if absolute_path in self.library.included_files:
                dir_file_count += 1
                continue

            # Ignore if the file is a directory
            if absolute_path.is_dir():
                continue

            dir_file_count += 1
            self.library.included_files.add(absolute_path)

            if not self.library.has_path_entry(relative_path, folder=folder_id):
                self.files_not_in_library.append(relative_path)
                self._pending_files.append((folder_id, relative_path))

        end_time_total = time()
        yield dir_file_count
        logger.info(
            "[Refresh]: Directory scan time",
            path=library_dir,
            duration=(end_time_total - start_time_total),
            files_scanned=dir_file_count,
            tool_used="ripgrep (system)",
        )

    def __wc_add(
        self, library_dir: Path, ignore_patterns: list[str], folder_id: int
    ) -> Iterator[int]:
        start_time_total = time()
        start_time_loop = time()
        dir_file_count = 0
        logger.info("[Refresh]: Falling back to wcmatch for scanning")

        try:
            root_path = Path(library_dir).resolve(strict=False)
            for f in pathlib.Path(str(root_path)).glob(
                "***/*", flags=PATH_GLOB_FLAGS, exclude=ignore_patterns
            ):
                absolute_path = Path(f).resolve(strict=False)
                end_time_loop = time()
                # Yield output every 1/30 of a second
                if (end_time_loop - start_time_loop) > 0.034:
                    yield dir_file_count
                    start_time_loop = time()

                # Skip if the file/path is already mapped in the Library
                if absolute_path in self.library.included_files:
                    dir_file_count += 1
                    continue

                # Ignore if the file is a directory
                if absolute_path.is_dir():
                    continue

                dir_file_count += 1
                self.library.included_files.add(absolute_path)

                relative_path = absolute_path.relative_to(root_path)

                if not self.library.has_path_entry(relative_path, folder=folder_id):
                    self.files_not_in_library.append(relative_path)
                    self._pending_files.append((folder_id, relative_path))
        except ValueError:
            logger.info("[Refresh]: ValueError when refreshing directory with wcmatch!")

        end_time_total = time()
        yield dir_file_count
        logger.info(
            "[Refresh]: Directory scan time",
            path=library_dir,
            duration=(end_time_total - start_time_total),
            files_scanned=dir_file_count,
            tool_used="wcmatch (internal)",
        )


@dataclass(frozen=True, slots=True)
class IncrementalScanResult:
    """IDs affected by one batch of native file-system events."""

    added_ids: tuple[int, ...] = ()
    updated_ids: tuple[int, ...] = ()
    moved_ids: tuple[int, ...] = ()
    deleted_ids: tuple[int, ...] = ()

    @property
    def changed_ids(self) -> tuple[int, ...]:
        """Return affected IDs in stable, duplicate-free order."""
        return tuple(
            dict.fromkeys(
                (*self.added_ids, *self.updated_ids, *self.moved_ids, *self.deleted_ids)
            )
        )


class IncrementalScanner:
    """Apply native watcher events without walking every configured root."""

    def __init__(self, library: Library, watch_rule_engine: WatchRuleEngine | None = None) -> None:
        self.library = library
        self.watch_rule_engine = watch_rule_engine or WatchRuleEngine(library)

    def apply(self, events: Iterable[FileSystemEvent]) -> IncrementalScanResult:
        """Apply a batch of events and return the entries that need a UI refresh."""
        added_ids: list[int] = []
        updated_ids: list[int] = []
        moved_ids: list[int] = []
        deleted_ids: list[int] = []

        for event in events:
            path = Path(event.path).resolve(strict=False)
            if event.kind is FileSystemEventKind.CREATED:
                entry_id = self._add_path(path)
                if entry_id is not None:
                    added_ids.append(entry_id)
            elif event.kind is FileSystemEventKind.MODIFIED:
                entry = self.library.get_entry_full_by_file_path(path)
                if entry is None:
                    entry_id = self._add_path(path)
                    if entry_id is not None:
                        added_ids.append(entry_id)
                else:
                    self.library.included_files.add(path)
                    self.watch_rule_engine.apply_to_entry(entry.id, entry.path)
                    updated_ids.append(entry.id)
            elif event.kind is FileSystemEventKind.DELETED:
                self.library.included_files.discard(path)
                entry = self.library.get_entry_full_by_file_path(path)
                if entry is not None:
                    deleted_ids.append(entry.id)
            elif event.kind is FileSystemEventKind.MOVED:
                old_path = (
                    Path(event.old_path).resolve(strict=False)
                    if event.old_path is not None
                    else None
                )
                if old_path is not None:
                    entry = self.library.get_entry_full_by_file_path(old_path)
                else:
                    entry = None

                if entry is not None and self._move_entry(entry.id, path):
                    self.library.included_files.discard(old_path)
                    self.library.included_files.add(path)
                    moved_ids.append(entry.id)
                else:
                    entry_id = self._add_path(path)
                    if entry_id is not None:
                        added_ids.append(entry_id)

        return IncrementalScanResult(
            added_ids=tuple(dict.fromkeys(added_ids)),
            updated_ids=tuple(dict.fromkeys(updated_ids)),
            moved_ids=tuple(dict.fromkeys(moved_ids)),
            deleted_ids=tuple(dict.fromkeys(deleted_ids)),
        )

    def _add_path(self, path: Path) -> int | None:
        if not path.is_file() or self.library.is_path_ignored(path):
            return None

        root = self.library.root_for_path(path)
        if root is None:
            return None
        folder = self.library.get_folder_by_path(root)
        if folder is None:
            return None

        relative_path = self.library.relative_path(path)
        existing = self.library.get_entry_full_by_path(relative_path, folder=folder.id)
        if existing is not None:
            self.library.included_files.add(path)
            return None

        entry = Entry(
            path=relative_path,
            folder_id=folder.id,
            fields=self.library.default_fields_for_path(relative_path, folder=folder.id),
            date_added=dt.now(),
        )
        new_ids = self.library.add_entries([entry])
        if not new_ids:
            return None

        entry_id = new_ids[0]
        auto_tag_ids = self.library.auto_tag_ids_for_path(relative_path, folder=folder.id)
        if auto_tag_ids:
            self.library.add_tags_to_entries(entry_id, auto_tag_ids)
        self.watch_rule_engine.apply_to_entry(entry_id, relative_path)
        self.library.included_files.add(path)
        return entry_id

    def _move_entry(self, entry_id: int, path: Path) -> bool:
        if not path.is_file() or self.library.is_path_ignored(path):
            return False
        root = self.library.root_for_path(path)
        if root is None:
            return False
        folder = self.library.get_folder_by_path(root)
        if folder is None:
            return False
        relative_path = self.library.relative_path(path)
        moved = self.library.move_entry(
            entry_id,
            relative_path,
            folder=folder.id,
        )
        if moved:
            self.watch_rule_engine.apply_to_entry(entry_id, relative_path)
        return moved
