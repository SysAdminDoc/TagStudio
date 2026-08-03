# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tagstudio.core.enums import LibraryPrefs
from tagstudio.core.library.alchemy.fields import FieldID
from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.library.alchemy.models import Tag
from tagstudio.core.library.refresh import RefreshTracker
from tagstudio.core.utils.types import unwrap

CWD = Path(__file__).parent


@pytest.mark.parametrize("exclude_mode", [True, False])
@pytest.mark.parametrize("library", [TemporaryDirectory()], indirect=True)
def test_refresh_new_files(library: Library, exclude_mode: bool):
    library_dir = unwrap(library.library_dir)
    # Given
    library.set_prefs(LibraryPrefs.IS_EXCLUDE_LIST, exclude_mode)
    library.set_prefs(LibraryPrefs.EXTENSION_LIST, [".md"])
    registry = RefreshTracker(library=library)
    library.included_files.clear()
    (library_dir / "FOO.MD").touch()

    # Test if the single file was added
    list(registry.refresh_dir(library_dir, force_internal_tools=True))
    assert registry.files_not_in_library == [Path("FOO.MD")]


@pytest.mark.parametrize("library", [TemporaryDirectory()], indirect=True)
def test_refresh_multi_byte_filenames(library: Library):
    library_dir = unwrap(library.library_dir)
    # Given
    registry = RefreshTracker(library=library)
    library.included_files.clear()
    (library_dir / ".TagStudio").mkdir()
    (library_dir / "こんにちは.txt").touch()
    (library_dir / "em–dash.txt").touch()
    (library_dir / "apostrophe’.txt").touch()
    (library_dir / "umlaute äöü.txt").touch()

    # Test if all files were added with their correct names and without exceptions
    list(registry.refresh_dir(library_dir))
    assert Path("こんにちは.txt") in registry.files_not_in_library
    assert Path("em–dash.txt") in registry.files_not_in_library
    assert Path("apostrophe’.txt") in registry.files_not_in_library
    assert Path("umlaute äöü.txt") in registry.files_not_in_library


@pytest.mark.parametrize("library", [TemporaryDirectory()], indirect=True)
def test_refresh_multiple_roots_keeps_duplicate_relative_paths_separate(library: Library):
    primary = unwrap(library.library_dir)
    with TemporaryDirectory() as secondary_name:
        secondary = Path(secondary_name)
        library.add_root(secondary)
        (primary / "same.txt").touch()
        (secondary / "same.txt").touch()

        tracker = RefreshTracker(library=library)
        library.included_files.clear()
        list(tracker.refresh_dirs((primary, secondary), force_internal_tools=True))
        assert tracker.files_not_in_library == [Path("same.txt"), Path("same.txt")]

        list(tracker.save_new_files())

    entries = [entry for entry in library.all_entries() if entry.path == Path("same.txt")]
    assert len(entries) == 2
    assert {entry.folder_id for entry in entries} == {folder.id for folder in library.folders}


@pytest.mark.parametrize("library", [TemporaryDirectory()], indirect=True)
def test_refresh_applies_folder_field_defaults_and_auto_tags(library: Library):
    root = unwrap(library.folder)
    library_path = unwrap(library.library_dir)
    photos_path = library_path / "photos"
    photos_path.mkdir()
    image_path = photos_path / "image.jpg"
    image_path.touch()

    auto_tag = library.add_tag(Tag(name="photo-auto-tag"))
    assert auto_tag is not None
    library.set_folder_override(
        Path("photos"),
        folder=root,
        field_defaults=[FieldID.AUTHOR],
        auto_tag_ids=[auto_tag.id],
    )

    tracker = RefreshTracker(library=library)
    library.included_files.clear()
    list(tracker.refresh_dir(library_path, force_internal_tools=True))
    list(tracker.save_new_files())

    entry = library.get_entry_full_by_path(Path("photos/image.jpg"), folder=root)
    assert entry is not None
    assert [field.type_key for field in entry.fields] == [FieldID.AUTHOR.name]
    assert auto_tag.id in {tag.id for tag in entry.tags}
