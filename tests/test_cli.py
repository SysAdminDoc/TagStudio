# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

import json
from pathlib import Path

from tagstudio.cli import main as cli_main
from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.library.alchemy.models import Entry, Tag
from tagstudio.core.utils.types import unwrap


def _create_disk_library(tmp_path: Path) -> tuple[Path, Path]:
    library_path = tmp_path / "library"
    media_path = library_path / "photo.jpg"
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(b"cli media")

    library = Library()
    status = library.open_library(library_path)
    assert status.success
    folder = unwrap(library.folder)
    existing = unwrap(library.add_tag(Tag(name="existing")))
    entry = Entry(path=Path("photo.jpg"), folder=folder, fields=[])
    assert library.add_entries([entry])
    assert library.add_tags_to_entries(entry.id, existing.id) == 1
    library.close()
    return library_path, media_path


def test_query_emits_structured_records(tmp_path: Path, capsys) -> None:
    library_path, media_path = _create_disk_library(tmp_path)
    capsys.readouterr()

    assert cli_main(["query", str(library_path), 'tag:"existing"']) == 0

    records = json.loads(capsys.readouterr().out)
    assert records == [
        {
            "id": 1,
            "path": media_path.resolve().as_posix(),
            "relative_path": "photo.jpg",
            "tags": ["existing"],
        }
    ]


def test_tag_adds_and_removes_by_path(tmp_path: Path, capsys) -> None:
    library_path, media_path = _create_disk_library(tmp_path)
    capsys.readouterr()

    assert cli_main(["tag", str(library_path), "new", "--path", str(media_path)]) == 0
    added = json.loads(capsys.readouterr().out)
    assert added["created_tags"] == ["new"]
    assert added["added_assignments"] == 1

    assert cli_main(["tag", str(library_path), "new", "--path", "photo.jpg", "--remove"]) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["removed_assignments"] == 1

    library = Library()
    assert library.open_library(library_path).success
    entry = library.get_entry_full(1, with_fields=False, with_tags=True)
    assert entry is not None
    assert {tag.name for tag in entry.tags} == {"existing"}
    library.close()


def test_export_command_writes_target_mirror(tmp_path: Path, capsys) -> None:
    library_path, _ = _create_disk_library(tmp_path)
    capsys.readouterr()
    destination = tmp_path / "immich-mirror"

    assert (
        cli_main(
            [
                "export",
                str(library_path),
                str(destination),
                "--target",
                "immich",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)

    assert result["target"] == "immich"
    assert result["files_copied"] == 1
    assert (destination / "photo.jpg").read_bytes() == b"cli media"
    assert (destination / "photo.xmp").exists()
