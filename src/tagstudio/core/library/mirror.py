# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

"""Credential-free, read-only filesystem mirrors for external photo managers."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from tagstudio.core.library.alchemy.enums import BrowsingState
from tagstudio.core.library.alchemy.sidecars import (
    SidecarDocument,
    SidecarFormat,
    sidecar_path,
    write_sidecar,
)

if TYPE_CHECKING:
    from tagstudio.core.library.alchemy.library import Library
    from tagstudio.core.library.alchemy.models import Entry, Folder

MIRROR_SCHEMA = "tagstudio.read-only-mirror"
MIRROR_VERSION = 1
MIRROR_MANIFEST_NAME = ".tagstudio-mirror.json"


class MirrorTarget(str, Enum):
    """External application profile for a filesystem mirror."""

    IMMICH = "immich"
    PHOTOPRISM = "photoprism"
    NEXTCLOUD = "nextcloud"


class MirrorExportError(ValueError):
    """Raised when a mirror cannot be safely generated."""


@dataclass(frozen=True, slots=True)
class MirrorExportResult:
    """Summary of one mirror export."""

    target: MirrorTarget
    destination: Path
    entries_seen: int
    files_copied: int
    sidecars_written: int
    skipped_entries: int
    warnings: tuple[str, ...]
    manifest_path: Path


def normalize_target(target: MirrorTarget | str) -> MirrorTarget:
    """Normalize a target enum or its command-friendly string value."""
    if isinstance(target, MirrorTarget):
        return target
    try:
        return MirrorTarget(target.lower())
    except (AttributeError, ValueError) as error:
        raise MirrorExportError(f"Unsupported mirror target: {target}") from error


def _canonical(path: Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _library_root(library: Library, folder: Folder) -> Path:
    root = Path(folder.path)
    if not root.is_absolute():
        if library.library_dir is None:
            raise MirrorExportError("The library has no directory for resolving its roots.")
        root = Path(library.library_dir) / root
    return _canonical(root)


def _root_labels(library: Library) -> dict[int, str]:
    """Return stable, readable directory labels for multi-root exports."""
    labels: dict[int, str] = {}
    used: set[str] = set()
    for folder in library.folders:
        name = _library_root(library, folder).name or f"root-{folder.id}"
        if name in used:
            name = f"{name}-{folder.id}"
        labels[folder.id] = name
        used.add(name)
    return labels


def _entry_ids(library: Library, entry_ids: Iterable[int] | None) -> list[int]:
    if entry_ids is not None:
        return list(dict.fromkeys(entry_ids))
    return list(library.search_library(BrowsingState(), page_size=0).ids)


def _relative_export_path(entry: Entry, labels: dict[int, str], multi_root: bool) -> Path:
    relative = Path(entry.path)
    if relative.is_absolute() or ".." in relative.parts:
        raise MirrorExportError(f"Entry has an unsafe relative path: {entry.path}")
    if multi_root:
        try:
            return Path(labels[entry.folder_id]) / relative
        except KeyError as error:
            raise MirrorExportError(
                f"Entry references an unknown library root: {entry.id}"
            ) from error
    return relative


def _assert_destination_is_external(library: Library, destination: Path) -> None:
    for folder in library.folders:
        root = _library_root(library, folder)
        if destination == root or destination.is_relative_to(root):
            raise MirrorExportError(
                "Mirror destination must be outside every source library root "
                "to keep the export read-only."
            )


def export_read_only_mirror(
    library: Library,
    target: MirrorTarget | str,
    destination: Path,
    *,
    entry_ids: Iterable[int] | None = None,
    overwrite: bool = True,
    include_sidecars: bool = True,
    dry_run: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> MirrorExportResult:
    """Copy indexed files and XMP tags into a target-specific mirror directory.

    The source library and its media files are only read. All generated files live below
    ``destination``. Multi-root libraries receive one top-level directory per configured
    root so files with the same relative path cannot overwrite one another.
    """
    normalized_target = normalize_target(target)
    destination = _canonical(Path(destination))
    _assert_destination_is_external(library, destination)

    selected_ids = _entry_ids(library, entry_ids)
    labels = _root_labels(library)
    multi_root = len(labels) > 1
    warnings: list[str] = []
    manifest_entries: list[dict[str, object]] = []
    files_copied = 0
    sidecars_written = 0
    skipped_entries = 0

    for index, entry_id in enumerate(selected_ids, start=1):
        if progress is not None:
            progress(index - 1, len(selected_ids))
        entry = library.get_entry_full(entry_id, with_fields=False, with_tags=True)
        if entry is None:
            skipped_entries += 1
            warnings.append(f"Entry {entry_id} no longer exists.")
            continue

        try:
            relative = _relative_export_path(entry, labels, multi_root)
        except MirrorExportError as error:
            skipped_entries += 1
            warnings.append(str(error))
            continue

        source_path = _canonical(library.resolve_entry_path(entry))
        media_path = destination / relative
        if not source_path.is_file():
            skipped_entries += 1
            warnings.append(f"Source file is missing: {source_path}")
            continue
        if media_path.exists() and not overwrite:
            skipped_entries += 1
            warnings.append(f"Destination already exists: {media_path}")
            continue

        tags = tuple(
            sorted((tag.name for tag in entry.tags), key=lambda name: (name.casefold(), name))
        )
        sidecar = sidecar_path(media_path, SidecarFormat.XMP)
        if not dry_run:
            media_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, media_path)
            files_copied += 1
            if include_sidecars:
                write_sidecar(
                    sidecar,
                    SidecarDocument(tags=tags, file=relative.as_posix()),
                    SidecarFormat.XMP,
                )
                sidecars_written += 1
        else:
            files_copied += 1
            if include_sidecars:
                sidecars_written += 1

        manifest_entries.append(
            {
                "path": relative.as_posix(),
                "sidecar": sidecar.relative_to(destination).as_posix()
                if include_sidecars
                else None,
                "tags": list(tags),
            }
        )

    if progress is not None:
        progress(len(selected_ids), len(selected_ids))

    manifest_path = destination / MIRROR_MANIFEST_NAME
    if not dry_run:
        destination.mkdir(parents=True, exist_ok=True)
        manifest = {
            "entries": manifest_entries,
            "schema": MIRROR_SCHEMA,
            "target": normalized_target.value,
            "version": MIRROR_VERSION,
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    return MirrorExportResult(
        target=normalized_target,
        destination=destination,
        entries_seen=len(selected_ids),
        files_copied=files_copied,
        sidecars_written=sidecars_written,
        skipped_entries=skipped_entries,
        warnings=tuple(warnings),
        manifest_path=manifest_path,
    )
