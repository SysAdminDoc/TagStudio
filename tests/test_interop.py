# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

import hashlib
import json
import sqlite3
from pathlib import Path

from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.library.alchemy.models import Entry
from tagstudio.core.library.interop import (
    ExternalTagRecord,
    InteropSource,
    import_external_tags,
    load_external_records,
)
from tagstudio.core.utils.types import unwrap


def _library(tmp_path: Path, relative_path: str = "photos/image.jpg") -> Library:
    library = Library()
    assert library.open_library(tmp_path, ":memory:").success
    folder = unwrap(library.folder)
    entry = Entry(folder=folder, path=Path(relative_path), fields=library.default_fields)
    assert library.add_entries([entry]) == [entry.id]
    return library


def test_exiftool_json_sidecar_imports_tags_and_is_idempotent(tmp_path: Path) -> None:
    media = tmp_path / "photos" / "image.jpg"
    media.parent.mkdir()
    media.write_bytes(b"image")
    sidecar = media.with_name(f"{media.name}.json")
    sidecar.write_text(
        json.dumps(
            [
                {
                    "SourceFile": str(media),
                    "IPTC:Keywords": ["Vacation", "Beach"],
                    "XMP:Subject": ["Beach"],
                }
            ]
        ),
        encoding="utf-8",
    )
    library = _library(tmp_path)

    records = load_external_records(InteropSource.EXIFTOOL, sidecar)
    assert records[0].source_path == media
    assert records[0].tags == ("Vacation", "Beach")

    first = import_external_tags(library, InteropSource.EXIFTOOL, sidecar)
    second = import_external_tags(library, InteropSource.EXIFTOOL, sidecar)

    assert first.matched_files == 1
    assert first.created_tags == 2
    assert first.added_tags == 2
    assert second.added_tags == 0
    entry = library.get_entry_full(1)
    assert entry is not None
    assert {tag.name for tag in entry.tags} == {"Vacation", "Beach"}


def test_lightroom_catalog_imports_relative_paths(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.lrcat"
    connection = sqlite3.connect(catalog)
    connection.executescript(
        """
        CREATE TABLE AgLibraryFile (id_local INTEGER, idx_filename TEXT, folder INTEGER);
        CREATE TABLE AgLibraryFolder (id_local INTEGER, pathFromRoot TEXT, parent INTEGER);
        CREATE TABLE AgLibraryKeyword (id_local INTEGER, name TEXT);
        CREATE TABLE AgLibraryKeywordImage (image INTEGER, tag INTEGER);
        CREATE TABLE Adobe_images (id_local INTEGER, rootFile INTEGER);
        INSERT INTO AgLibraryFolder VALUES (10, 'photos', NULL);
        INSERT INTO AgLibraryFile VALUES (20, 'image.jpg', 10);
        INSERT INTO AgLibraryKeyword VALUES (30, 'Vacation');
        INSERT INTO AgLibraryKeywordImage VALUES (40, 30);
        INSERT INTO Adobe_images VALUES (40, 20);
        """
    )
    connection.commit()
    connection.close()

    records = load_external_records(InteropSource.LIGHTROOM, catalog)
    assert records == [ExternalTagRecord(Path("photos/image.jpg"), ("Vacation",))]

    library = _library(tmp_path)
    result = import_external_tags(library, InteropSource.LIGHTROOM, catalog)
    assert result.matched_files == 1
    assert result.added_tags == 1


def test_digikam_database_imports_absolute_album_paths(tmp_path: Path) -> None:
    database = tmp_path / "digikam.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE AlbumRoots (id INTEGER, specificPath TEXT);
        CREATE TABLE Albums (id INTEGER, albumRoot INTEGER, relativePath TEXT);
        CREATE TABLE Images (id INTEGER, name TEXT, album INTEGER);
        CREATE TABLE Tags (id INTEGER, name TEXT, pid INTEGER);
        CREATE TABLE ImageTags (imageid INTEGER, tagid INTEGER);
        """
    )
    connection.execute("INSERT INTO AlbumRoots VALUES (?, ?)", (1, str(tmp_path)))
    connection.execute("INSERT INTO Albums VALUES (2, 1, 'photos')")
    connection.execute("INSERT INTO Images VALUES (3, 'image.jpg', 2)")
    connection.execute("INSERT INTO Tags VALUES (4, 'Digikam', NULL)")
    connection.execute("INSERT INTO ImageTags VALUES (3, 4)")
    connection.commit()
    connection.close()

    records = load_external_records(InteropSource.DIGIKAM, database)
    assert records[0].source_path == tmp_path / "photos" / "image.jpg"
    assert records[0].tags == ("Digikam",)

    library = _library(tmp_path)
    result = import_external_tags(library, InteropSource.DIGIKAM, database)
    assert result.matched_files == 1
    assert result.created_tags == 1


def test_hydrus_database_matches_sha256_without_paths(tmp_path: Path) -> None:
    media = tmp_path / "photos" / "image.jpg"
    media.parent.mkdir()
    media.write_bytes(b"hydrus image")
    digest = hashlib.sha256(media.read_bytes()).digest()

    database = tmp_path / "client.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE hashes (hash_id INTEGER, hash BLOB);
        CREATE TABLE tags (tag_id INTEGER, tag TEXT);
        CREATE TABLE current_mappings (service_id INTEGER, tag_id INTEGER, hash_id INTEGER);
        """
    )
    connection.execute("INSERT INTO hashes VALUES (1, ?)", (digest,))
    connection.execute("INSERT INTO tags VALUES (2, 'hydrus:blue')")
    connection.execute("INSERT INTO current_mappings VALUES (3, 2, 1)")
    connection.commit()
    connection.close()

    library = _library(tmp_path)
    result = import_external_tags(library, InteropSource.HYDRUS, database)
    assert result.records_read == 1
    assert result.matched_files == 1
    assert result.added_tags == 1
    entry = library.get_entry_full(1)
    assert entry is not None
    assert {tag.name for tag in entry.tags} == {"hydrus:blue"}
