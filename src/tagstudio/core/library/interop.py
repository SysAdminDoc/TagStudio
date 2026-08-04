# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

"""Read-only import adapters for common catalog and metadata formats."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Any

import structlog

from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.library.alchemy.models import Entry, Tag
from tagstudio.core.library.alchemy.sidecars import SidecarError, parse_xmp

logger = structlog.get_logger(__name__)


class InteropSource(str, Enum):
    """External metadata formats supported by the importer."""

    LIGHTROOM = "lightroom"
    DIGIKAM = "digikam"
    HYDRUS = "hydrus"
    EXIFTOOL = "exiftool"


class InteropImportError(ValueError):
    """Raised when an external catalog cannot be read safely."""


@dataclass(frozen=True, slots=True)
class ExternalTagRecord:
    """Tags associated with an external path or content hash."""

    source_path: Path | None = None
    tags: tuple[str, ...] = ()
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class InteropImportResult:
    """Summary of a non-destructive external-tag import."""

    source: InteropSource
    records_read: int = 0
    matched_files: int = 0
    added_tags: int = 0
    created_tags: int = 0
    skipped_records: int = 0
    warnings: tuple[str, ...] = ()


ProgressCallback = Callable[[int, int], None]


def _clean_tags(values: Iterable[str]) -> tuple[str, ...]:
    tags: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        tag = value.strip()
        if "|" in tag:
            tag = tag.rsplit("|", maxsplit=1)[-1].strip()
        if tag and tag not in tags:
            tags.append(tag)
    return tuple(tags)


def _path_key(value: Path | str) -> str:
    return Path(value).as_posix().replace("\\", "/").rstrip("/").casefold()


def _is_absolute_path(value: Path | str) -> bool:
    return Path(value).is_absolute() or PureWindowsPath(str(value)).is_absolute()


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _connect_catalog(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as error:
        raise InteropImportError(f"Could not open catalog {path}: {error}") from error


def _table_name(connection: sqlite3.Connection, expected: str) -> str | None:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND lower(name) = ?",
        (expected.casefold(),),
    ).fetchone()
    return str(row[0]) if row else None


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, str]:
    return {
        str(row[1]).casefold(): str(row[1])
        for row in connection.execute(f"PRAGMA table_info({_quoted_identifier(table)})")
    }


def _pick(columns: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        if name.casefold() in columns:
            return columns[name.casefold()]
    return None


def _rows(
    connection: sqlite3.Connection,
    table: str,
    selected_columns: Iterable[str],
) -> list[sqlite3.Row]:
    selected = ", ".join(_quoted_identifier(column) for column in selected_columns)
    return list(connection.execute(f"SELECT {selected} FROM {_quoted_identifier(table)}"))


def _require_table(connection: sqlite3.Connection, expected: str) -> str:
    table = _table_name(connection, expected)
    if table is None:
        raise InteropImportError(f"Catalog is missing the {expected} table")
    return table


def _folder_path_map(
    connection: sqlite3.Connection,
    table: str,
    id_names: tuple[str, ...] = ("id_local", "id"),
) -> dict[Any, Path]:
    table_columns = _columns(connection, table)
    id_column = _pick(table_columns, *id_names)
    path_column = _pick(
        table_columns, "absolutePath", "specificPath", "pathFromRoot", "relativePath", "name"
    )
    parent_column = _pick(table_columns, "parent", "parentId", "pid")
    if id_column is None or path_column is None:
        return {}

    raw_rows = _rows(
        connection,
        table,
        [column for column in (id_column, path_column, parent_column) if column is not None],
    )
    rows: dict[Any, tuple[Path, Any | None]] = {}
    for row in raw_rows:
        raw_path = str(row[path_column] or "")
        parent_id = row[parent_column] if parent_column is not None else None
        rows[row[id_column]] = (Path(raw_path), parent_id)

    resolved: dict[Any, Path] = {}

    def resolve(folder_id: Any, active: set[Any]) -> Path:
        if folder_id in resolved:
            return resolved[folder_id]
        if folder_id in active or folder_id not in rows:
            return Path()
        raw_path, parent_id = rows[folder_id]
        if raw_path.is_absolute() or PureWindowsPath(str(raw_path)).is_absolute():
            result = raw_path
        elif parent_id is not None and parent_id in rows:
            result = resolve(parent_id, active | {folder_id}) / raw_path
        else:
            result = raw_path
        resolved[folder_id] = result
        return result

    for folder_id in rows:
        resolve(folder_id, set())
    return resolved


def _parse_lightroom_catalog(path: Path) -> list[ExternalTagRecord]:
    connection = _connect_catalog(path)
    try:
        files_table = _require_table(connection, "AgLibraryFile")
        folders_table = _require_table(connection, "AgLibraryFolder")
        keywords_table = _require_table(connection, "AgLibraryKeyword")
        links_table = _require_table(connection, "AgLibraryKeywordImage")

        file_columns = _columns(connection, files_table)
        file_id = _pick(file_columns, "id_local", "id")
        filename_column = _pick(file_columns, "idx_filename", "filename", "name")
        folder_column = _pick(file_columns, "folder", "folder_id")
        if file_id is None or filename_column is None or folder_column is None:
            raise InteropImportError("Lightroom catalog has an unsupported AgLibraryFile schema")

        keyword_columns = _columns(connection, keywords_table)
        keyword_id = _pick(keyword_columns, "id_local", "id")
        keyword_name = _pick(keyword_columns, "name", "lc_name")
        if keyword_id is None or keyword_name is None:
            raise InteropImportError("Lightroom catalog has an unsupported keyword schema")

        link_columns = _columns(connection, links_table)
        link_image = _pick(link_columns, "image", "image_id", "file", "file_id")
        link_tag = _pick(link_columns, "tag", "tag_id", "keyword", "keyword_id")
        if link_image is None or link_tag is None:
            raise InteropImportError("Lightroom catalog has an unsupported keyword-link schema")

        keyword_names = {
            row[keyword_id]: str(row[keyword_name])
            for row in _rows(connection, keywords_table, [keyword_id, keyword_name])
            if row[keyword_name]
        }
        image_links: dict[Any, list[Any]] = defaultdict(list)
        for row in _rows(connection, links_table, [link_image, link_tag]):
            image_links[row[link_image]].append(row[link_tag])

        image_to_files: dict[Any, list[Any]] = defaultdict(list)
        images_table = _table_name(connection, "Adobe_images")
        if images_table is not None:
            image_columns = _columns(connection, images_table)
            image_id = _pick(image_columns, "id_local", "id")
            root_file = _pick(image_columns, "rootFile", "root_file", "file")
            if image_id is not None and root_file is not None:
                for row in _rows(connection, images_table, [image_id, root_file]):
                    image_to_files[row[root_file]].append(row[image_id])

        folder_paths = _folder_path_map(connection, folders_table)
        records: list[ExternalTagRecord] = []
        for row in _rows(connection, files_table, [file_id, filename_column, folder_column]):
            file_id_value = row[file_id]
            image_ids = image_to_files.get(file_id_value, [file_id_value])
            tag_ids = [
                tag_id
                for image_id_value in image_ids
                for tag_id in image_links.get(image_id_value, ())
            ]
            tags = _clean_tags(
                keyword_names[tag_id] for tag_id in tag_ids if tag_id in keyword_names
            )
            if not tags:
                continue
            filename = Path(str(row[filename_column]))
            folder_path = folder_paths.get(row[folder_column], Path())
            records.append(ExternalTagRecord(folder_path / filename, tags))
        return records
    except sqlite3.Error as error:
        raise InteropImportError(f"Could not read Lightroom catalog {path}: {error}") from error
    finally:
        connection.close()


def _parse_digikam_database(path: Path) -> list[ExternalTagRecord]:
    connection = _connect_catalog(path)
    try:
        images_table = _require_table(connection, "Images")
        albums_table = _require_table(connection, "Albums")
        tags_table = _require_table(connection, "Tags")
        links_table = _require_table(connection, "ImageTags")

        image_columns = _columns(connection, images_table)
        image_id = _pick(image_columns, "id", "id_local")
        image_name = _pick(image_columns, "name", "filename")
        image_album = _pick(image_columns, "album", "album_id")
        if image_id is None or image_name is None or image_album is None:
            raise InteropImportError("digiKam database has an unsupported Images schema")

        album_columns = _columns(connection, albums_table)
        album_id = _pick(album_columns, "id", "id_local")
        album_root = _pick(album_columns, "albumRoot", "album_root", "root")
        album_relative_path = _pick(album_columns, "relativePath", "relative_path", "path")
        if album_id is None or album_relative_path is None:
            raise InteropImportError("digiKam database has an unsupported Albums schema")

        tag_columns = _columns(connection, tags_table)
        tag_id = _pick(tag_columns, "id", "id_local")
        tag_name = _pick(tag_columns, "name", "tag")
        if tag_id is None or tag_name is None:
            raise InteropImportError("digiKam database has an unsupported Tags schema")

        link_columns = _columns(connection, links_table)
        link_image = _pick(link_columns, "imageid", "image_id", "image")
        link_tag = _pick(link_columns, "tagid", "tag_id", "tag")
        if link_image is None or link_tag is None:
            raise InteropImportError("digiKam database has an unsupported ImageTags schema")

        root_paths: dict[Any, Path] = {}
        if album_root is not None:
            roots_table = _table_name(connection, "AlbumRoots")
            if roots_table is not None:
                root_columns = _columns(connection, roots_table)
                root_id = _pick(root_columns, "id", "id_local")
                root_path = _pick(root_columns, "specificPath", "specific_path", "path")
                if root_id is not None and root_path is not None:
                    root_paths = {
                        row[root_id]: Path(str(row[root_path] or ""))
                        for row in _rows(connection, roots_table, [root_id, root_path])
                    }

        album_paths: dict[Any, Path] = {}
        album_columns_to_read = [
            column
            for column in (album_id, album_root, album_relative_path)
            if column is not None
        ]
        for row in _rows(connection, albums_table, album_columns_to_read):
            album_root_path = (
                root_paths.get(row[album_root], Path()) if album_root is not None else Path()
            )
            album_paths[row[album_id]] = album_root_path / Path(
                str(row[album_relative_path] or "")
            )
        tag_names = {
            row[tag_id]: str(row[tag_name])
            for row in _rows(connection, tags_table, [tag_id, tag_name])
            if row[tag_name]
        }
        image_tags: dict[Any, list[Any]] = defaultdict(list)
        for row in _rows(connection, links_table, [link_image, link_tag]):
            image_tags[row[link_image]].append(row[link_tag])

        records: list[ExternalTagRecord] = []
        for row in _rows(connection, images_table, [image_id, image_name, image_album]):
            tags = _clean_tags(
                tag_names[tag_id_value]
                for tag_id_value in image_tags.get(row[image_id], ())
                if tag_id_value in tag_names
            )
            if tags:
                records.append(
                    ExternalTagRecord(
                        album_paths.get(row[image_album], Path()) / Path(str(row[image_name])),
                        tags,
                    )
                )
        return records
    except sqlite3.Error as error:
        raise InteropImportError(f"Could not read digiKam database {path}: {error}") from error
    finally:
        connection.close()


def _hash_value(value: object) -> str | None:
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex()
    if isinstance(value, str):
        return value.strip().casefold()
    return None


def _parse_hydrus_database(path: Path) -> list[ExternalTagRecord]:
    connection = _connect_catalog(path)
    try:
        mappings_table = _require_table(connection, "current_mappings")
        tags_table = _require_table(connection, "tags")
        hashes_table = _require_table(connection, "hashes")

        mapping_columns = _columns(connection, mappings_table)
        mapping_hash = _pick(mapping_columns, "hash_id", "hashid")
        mapping_tag = _pick(mapping_columns, "tag_id", "tagid")
        tag_columns = _columns(connection, tags_table)
        tag_id = _pick(tag_columns, "tag_id", "id")
        tag_name = _pick(tag_columns, "tag", "name")
        hash_columns = _columns(connection, hashes_table)
        hash_id = _pick(hash_columns, "hash_id", "id")
        hash_value = _pick(hash_columns, "hash")
        if None in (mapping_hash, mapping_tag, tag_id, tag_name, hash_id, hash_value):
            raise InteropImportError("Hydrus database has an unsupported mapping schema")
        assert mapping_hash is not None
        assert mapping_tag is not None
        assert tag_id is not None
        assert tag_name is not None
        assert hash_id is not None
        assert hash_value is not None

        names = {
            row[tag_id]: str(row[tag_name])
            for row in _rows(connection, tags_table, [tag_id, tag_name])
            if row[tag_name]
        }
        hashes = {
            row[hash_id]: _hash_value(row[hash_value])
            for row in _rows(connection, hashes_table, [hash_id, hash_value])
        }
        grouped: dict[str, list[str]] = defaultdict(list)
        for row in _rows(connection, mappings_table, [mapping_hash, mapping_tag]):
            digest = hashes.get(row[mapping_hash])
            name = names.get(row[mapping_tag])
            if digest and name:
                grouped[digest].append(name)
        return [
            ExternalTagRecord(tags=_clean_tags(tags), sha256=digest)
            for digest, tags in grouped.items()
        ]
    except sqlite3.Error as error:
        raise InteropImportError(f"Could not read Hydrus database {path}: {error}") from error
    finally:
        connection.close()


_EXIFTOOL_TAG_KEYS = {
    "keywords",
    "iptc:keywords",
    "xmp:subject",
    "xmp-dc:subject",
    "subject",
    "tags",
    "hierarchicalsubject",
    "xmp:hierarchicalsubject",
    "lr:hierarchicalsubject",
}


def _exiftool_values(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _exiftool_values(item)


def _exiftool_tags(payload: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key, value in payload.items():
        normalized_key = str(key).casefold()
        if normalized_key not in _EXIFTOOL_TAG_KEYS and not normalized_key.endswith(":keywords"):
            continue
        values.extend(_exiftool_values(value))
    return _clean_tags(values)


def _inferred_sidecar_source(path: Path) -> Path:
    return path.with_suffix("")


def _parse_exiftool_json(path: Path) -> list[ExternalTagRecord]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("[Interop] Skipping invalid ExifTool JSON", path=path, error=error)
        return []
    documents = payload if isinstance(payload, list) else [payload]
    records: list[ExternalTagRecord] = []
    for document in documents:
        if not isinstance(document, Mapping):
            continue
        tags = _exiftool_tags(document)
        if not tags:
            continue
        source_file = document.get("SourceFile")
        source_path = (
            Path(source_file)
            if isinstance(source_file, str)
            else _inferred_sidecar_source(path)
        )
        records.append(ExternalTagRecord(source_path, tags))
    return records


def _parse_exiftool_xmp(path: Path) -> list[ExternalTagRecord]:
    try:
        document = parse_xmp(path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError, SidecarError) as error:
        logger.warning("[Interop] Skipping invalid ExifTool XMP", path=path, error=error)
        return []
    if not document.tags:
        return []
    return [ExternalTagRecord(_inferred_sidecar_source(path), document.tags)]


def _parse_exiftool_sidecars(path: Path) -> list[ExternalTagRecord]:
    paths = (
        [path]
        if path.is_file()
        else sorted(
            candidate
            for candidate in path.rglob("*")
            if candidate.is_file()
            and candidate.suffix.casefold() in {".json", ".xmp"}
            and not candidate.name.casefold().endswith(".tagstudio.json")
        )
    )
    records: list[ExternalTagRecord] = []
    for candidate in paths:
        if candidate.suffix.casefold() == ".json":
            records.extend(_parse_exiftool_json(candidate))
        elif candidate.suffix.casefold() == ".xmp":
            records.extend(_parse_exiftool_xmp(candidate))
    return records


def load_external_records(source: InteropSource | str, path: Path) -> list[ExternalTagRecord]:
    """Read external metadata into normalized, path/hash-based records."""
    try:
        normalized_source = source if isinstance(source, InteropSource) else InteropSource(source)
    except ValueError as error:
        raise InteropImportError(f"Unsupported interop source: {source!r}") from error

    if normalized_source is InteropSource.LIGHTROOM:
        return _parse_lightroom_catalog(Path(path))
    if normalized_source is InteropSource.DIGIKAM:
        return _parse_digikam_database(Path(path))
    if normalized_source is InteropSource.HYDRUS:
        return _parse_hydrus_database(Path(path))
    return _parse_exiftool_sidecars(Path(path))


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for block in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def _entry_indexes(
    library: Library,
) -> tuple[dict[str, list[int]], dict[str, list[int]], list[Entry]]:
    absolute: dict[str, list[int]] = defaultdict(list)
    suffixes: dict[str, list[int]] = defaultdict(list)
    entries = list(library.all_entries())
    for entry in entries:
        try:
            absolute_key = _path_key(library.resolve_entry_path(entry))
        except (OSError, ValueError):
            continue
        absolute[absolute_key].append(entry.id)
        relative_parts = entry.path.as_posix().replace("\\", "/").split("/")
        for index in range(len(relative_parts)):
            suffixes["/".join(relative_parts[index:]).casefold()].append(entry.id)
    return absolute, suffixes, entries


def _matching_entry_ids(
    record: ExternalTagRecord,
    absolute: Mapping[str, list[int]],
    suffixes: Mapping[str, list[int]],
    hashes: Mapping[str, list[int]],
) -> tuple[int, ...]:
    if record.sha256:
        return tuple(hashes.get(record.sha256.casefold(), ()))
    if record.source_path is None:
        return ()
    source_key = _path_key(record.source_path)
    if _is_absolute_path(record.source_path):
        exact = absolute.get(source_key)
        if exact:
            return tuple(exact)
    matches = suffixes.get(source_key)
    if matches:
        return tuple(matches)
    matching_keys = [key for key in suffixes if source_key.endswith("/" + key)]
    if not matching_keys:
        return ()
    longest_key = max(matching_keys, key=len)
    return tuple(suffixes[longest_key])


def import_external_tags(
    library: Library,
    source: InteropSource | str,
    path: Path,
    *,
    replace_tags: bool = False,
    create_tags: bool = True,
    progress: ProgressCallback | None = None,
) -> InteropImportResult:
    """Import external tags onto matching entries without changing files or catalogs."""
    try:
        normalized_source = source if isinstance(source, InteropSource) else InteropSource(source)
    except ValueError as error:
        raise InteropImportError(f"Unsupported interop source: {source!r}") from error

    records = load_external_records(normalized_source, Path(path))
    absolute, suffixes, entries = _entry_indexes(library)

    hash_indexes: dict[str, list[int]] = defaultdict(list)
    if normalized_source is InteropSource.HYDRUS:
        for entry in entries:
            try:
                entry_path = library.resolve_entry_path(entry)
            except (OSError, ValueError):
                continue
            digest = _sha256(entry_path)
            if digest:
                hash_indexes[digest].append(entry.id)

    tag_id_cache: dict[str, int | None] = {}
    created_tag_names: set[str] = set()
    warnings: set[str] = set()
    matched_entry_ids: set[int] = set()
    added_tags = 0
    skipped_records = 0

    for index, record in enumerate(records, start=1):
        entry_ids = _matching_entry_ids(record, absolute, suffixes, hash_indexes)
        if not entry_ids:
            skipped_records += 1
            if record.source_path is not None:
                warnings.add(f"No library entry matched {record.source_path}")
            elif record.sha256 is not None:
                warnings.add(f"No library entry matched SHA-256 {record.sha256}")
            if progress is not None:
                progress(index, len(records))
            continue
        matched_entry_ids.update(entry_ids)

        tag_ids: list[int] = []
        for tag_name in record.tags:
            if tag_name not in tag_id_cache:
                tag = library.get_tag_by_name(tag_name)
                if tag is None and create_tags:
                    tag = library.add_tag(Tag(name=tag_name))
                    if tag is not None:
                        created_tag_names.add(tag_name)
                tag_id_cache[tag_name] = tag.id if tag is not None else None
            tag_id = tag_id_cache[tag_name]
            if tag_id is None:
                warnings.add(f"Could not create or find tag {tag_name!r}")
            elif tag_id not in tag_ids:
                tag_ids.append(tag_id)

        if tag_ids:
            if replace_tags:
                for entry_id in entry_ids:
                    entry = library.get_entry_full(entry_id, with_fields=False, with_tags=True)
                    if entry is not None and entry.tags:
                        library.remove_tags_from_entries(entry_id, [tag.id for tag in entry.tags])
            existing = library.get_tag_entries(tag_ids, entry_ids)
            for entry_id in entry_ids:
                missing = tuple(tag_id for tag_id in tag_ids if entry_id not in existing[tag_id])
                if missing:
                    added_tags += library.add_tags_to_entries(entry_id, missing)
        if progress is not None:
            progress(index, len(records))

    return InteropImportResult(
        source=normalized_source,
        records_read=len(records),
        matched_files=len(matched_entry_ids),
        added_tags=added_tags,
        created_tags=len(created_tag_names),
        skipped_records=skipped_records,
        warnings=tuple(sorted(warnings)),
    )
