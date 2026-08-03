# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio


import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.library.alchemy.models import Entry, Tag
from tagstudio.core.library.alchemy.sidecars import SidecarFormat
from tagstudio.core.utils.types import unwrap


@pytest.mark.parametrize("library", [TemporaryDirectory()], indirect=True)
def test_json_and_xmp_sidecars_round_trip_tags(library: Library):
    root = unwrap(library.folder)
    library_path = unwrap(library.library_dir)
    media_path = library_path / "photo.jpg"
    media_path.touch()

    entry = Entry(path=Path("photo.jpg"), folder=root, fields=[])
    assert library.add_entries([entry])
    animal = unwrap(library.add_tag(Tag(name="animal")))
    cat = unwrap(library.add_tag(Tag(name="cat")))
    library.add_tags_to_entries(entry.id, [animal.id, cat.id])

    json_path = library.export_sidecar(entry.id)
    assert json_path == media_path.with_name("photo.jpg.tagstudio.json")
    assert json.loads(json_path.read_text(encoding="utf-8")) == {
        "file": "photo.jpg",
        "schema": "tagstudio.sidecar",
        "tags": ["animal", "cat"],
        "version": 1,
    }

    xmp_path = library.export_sidecar(entry.id, SidecarFormat.XMP)
    assert xmp_path == media_path.with_suffix(".xmp")
    xmp_text = xmp_path.read_text(encoding="utf-8")
    assert "dc:subject" in xmp_text
    assert "lr:hierarchicalSubject" in xmp_text
    assert "animal" in xmp_text and "cat" in xmp_text

    library.remove_tags_from_entries(entry.id, [animal.id, cat.id])
    assert library.import_sidecar(entry.id, json_path) == 2
    imported = unwrap(library.get_entry_full(entry.id))
    assert {tag.name for tag in imported.tags} == {"animal", "cat"}

    library.remove_tags_from_entries(entry.id, [animal.id, cat.id])
    assert library.import_sidecar(entry.id, xmp_path) == 2
    imported = unwrap(library.get_entry_full(entry.id))
    assert {tag.name for tag in imported.tags} == {"animal", "cat"}
