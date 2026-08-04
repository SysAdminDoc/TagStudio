# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio


from pathlib import Path

import pytest

from tagstudio.core.library.alchemy.enums import BrowsingState
from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.library.alchemy.models import Entry, Tag
from tagstudio.core.utils.types import unwrap


def test_saved_searches_validate_materialize_and_persist(tmp_path: Path):
    library = Library()
    assert library.open_library(tmp_path).success
    folder = unwrap(library.folder)
    tag = Tag(name="cat", color_namespace="tagstudio-standard", color_slug="red")
    assert library.add_tag(tag)
    entry = Entry(folder=folder, path=Path("cat.jpg"), fields=library.default_fields)
    assert library.add_entries([entry]) == [entry.id]
    assert library.add_tags_to_entries(entry.id, tag.id)

    saved = library.create_saved_search("Cats", 'tag:"cat"')
    assert saved.name == "Cats"
    assert saved.query == 'tag:"cat"'
    assert library.saved_search_state(saved.id) == BrowsingState.from_search_query('tag:"cat"')
    result = library.search_library(unwrap(library.saved_search_state(saved.id)), page_size=0)
    assert result.ids == [entry.id]

    with pytest.raises(ValueError, match="Invalid saved search query"):
        library.create_saved_search("Broken", "tag:(")
    with pytest.raises(ValueError, match="already exists"):
        library.create_saved_search("Cats", "")

    updated = library.update_saved_search(saved.id, name="All Cats", is_pinned=False)
    assert updated is not None
    assert updated.name == "All Cats"
    assert not updated.is_pinned
    assert library.get_saved_searches(pinned_only=True) == []

    library.close()
    reopened = Library()
    assert reopened.open_library(tmp_path).success
    persisted = unwrap(reopened.get_saved_search(saved.id))
    assert persisted.name == "All Cats"
    assert persisted.query == 'tag:"cat"'
    assert reopened.saved_search_state(saved.id) is not None


def test_saved_search_sidebar_order_and_delete(library: Library):
    first = library.create_saved_search("First", "")
    second = library.create_saved_search("Second", "tag:foo")
    third = library.create_saved_search("Third", "tag:bar", is_pinned=False)

    library.reorder_saved_searches([second.id, first.id, third.id])
    assert [saved.name for saved in library.get_saved_searches()] == [
        "Second",
        "First",
        "Third",
    ]
    assert [saved.name for saved in library.get_saved_searches(pinned_only=True)] == [
        "Second",
        "First",
    ]
    assert library.delete_saved_search(first.id)
    assert not library.delete_saved_search(first.id)
    assert [saved.name for saved in library.get_saved_searches()] == ["Second", "Third"]
