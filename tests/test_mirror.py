# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

import json
from pathlib import Path

import pytest

from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.library.alchemy.sidecars import parse_xmp
from tagstudio.core.library.mirror import (
    MirrorExportError,
    MirrorTarget,
    export_read_only_mirror,
)
from tagstudio.core.utils.types import unwrap


@pytest.mark.parametrize("target", list(MirrorTarget))
def test_mirror_copies_media_tags_and_manifest(library: Library, target: MirrorTarget) -> None:
    library_dir = unwrap(library.library_dir)
    source = library_dir / "foo.txt"
    source.write_text("source remains unchanged", encoding="utf-8")
    destination = library_dir.parent / f"mirror-{target.value}"

    result = export_read_only_mirror(library, target, destination, entry_ids=[1])

    assert result.files_copied == 1
    assert result.sidecars_written == 1
    assert result.skipped_entries == 0
    assert (destination / "foo.txt").read_text(encoding="utf-8") == "source remains unchanged"
    sidecar = destination / "foo.xmp"
    assert parse_xmp(sidecar.read_text(encoding="utf-8")).tags == ("foo",)
    assert json.loads((destination / ".tagstudio-mirror.json").read_text(encoding="utf-8")) == {
        "entries": [
            {"path": "foo.txt", "sidecar": "foo.xmp", "tags": ["foo"]},
        ],
        "schema": "tagstudio.read-only-mirror",
        "target": target.value,
        "version": 1,
    }
    assert source.read_text(encoding="utf-8") == "source remains unchanged"


def test_mirror_dry_run_does_not_write_files(library: Library, tmp_path: Path) -> None:
    source = unwrap(library.library_dir) / "foo.txt"
    source.write_text("source", encoding="utf-8")
    destination = tmp_path / "mirror"

    result = export_read_only_mirror(
        library,
        MirrorTarget.NEXTCLOUD,
        destination,
        entry_ids=[1],
        dry_run=True,
    )

    assert result.files_copied == 1
    assert result.manifest_path == destination / ".tagstudio-mirror.json"
    assert not destination.exists()


def test_mirror_rejects_destination_inside_source_library(library: Library) -> None:
    library_dir = unwrap(library.library_dir)

    with pytest.raises(MirrorExportError, match="outside every source"):
        export_read_only_mirror(library, MirrorTarget.IMMICH, library_dir / "mirror")
