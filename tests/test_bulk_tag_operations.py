# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

import pytest

from tagstudio.core.library.alchemy.library import BulkTagError, Library
from tagstudio.core.library.alchemy.models import Tag
from tagstudio.core.utils.types import unwrap


def add_tag(library: Library, name: str) -> Tag:
    return unwrap(
        library.add_tag(
            Tag(name=name, color_namespace="tagstudio-standard", color_slug="red")
        )
    )


def test_bulk_rename_tags_preserves_identity(library: Library):
    tag = add_tag(library, "rename-me")

    assert library.rename_tags({tag.id: "renamed"}) == {tag.id: "renamed"}
    assert library.get_tag_by_name("rename-me") is None
    renamed = unwrap(library.get_tag(tag.id))
    assert renamed.name == "renamed"


def test_merge_tags_redirects_entries_aliases_and_hierarchy(library: Library):
    parent = add_tag(library, "merge-parent")
    target = add_tag(library, "merge-target")
    source_a = add_tag(library, "merge-source-a")
    source_b = add_tag(library, "merge-source-b")
    child = add_tag(library, "merge-child")

    library.reparent_tags({target.id, source_a.id, source_b.id}, {parent.id})
    library.add_parent_tag(source_a.id, child.id)
    source_a_model = unwrap(library.get_tag(source_a.id))
    library.update_tag(source_a_model, {parent.id}, {"source alias"}, set())
    library.add_tags_to_entries([1, 2], source_a.id)
    library.add_tags_to_entries(2, source_b.id)
    library.add_tags_to_entries(1, target.id)

    assert library.merge_tags({source_a.id, source_b.id}, target.id) == target.id

    assert library.get_tag(source_a.id) is None
    assert library.get_tag(source_b.id) is None
    assert library.get_tag_entries({target.id}, {1, 2}) == {target.id: {1, 2}}
    target_model = unwrap(library.get_tag(target.id))
    assert parent.id in target_model.parent_ids
    assert "merge-source-a" in target_model.alias_strings
    assert "merge-source-b" in target_model.alias_strings
    assert "source alias" in target_model.alias_strings
    child_model = unwrap(library.get_tag(child.id))
    assert target.id in child_model.parent_ids


def test_split_tag_creates_inherited_tags_and_moves_assignments(library: Library):
    parent = add_tag(library, "split-parent")
    source = add_tag(library, "split-source")
    library.reparent_tags(source.id, {parent.id})
    library.add_tags_to_entries([1, 2], source.id)

    created = library.split_tag(
        source.id,
        {
            "split-left": {1},
            "split-right": {2},
        },
    )

    left = created["split-left"]
    right = created["split-right"]
    assert library.get_tag_entries({source.id, left, right}, {1, 2}) == {
        source.id: set(),
        left: {1},
        right: {2},
    }
    assert parent.id in unwrap(library.get_tag(left)).parent_ids
    assert parent.id in unwrap(library.get_tag(right)).parent_ids


def test_split_tag_rejects_non_source_entries_atomically(library: Library):
    source = add_tag(library, "split-source")
    library.add_tags_to_entries(1, source.id)

    with pytest.raises(BulkTagError, match="already have the source tag"):
        library.split_tag(source.id, {"split-left": {1, 2}})

    assert library.get_tag_by_name("split-left") is None
    assert library.get_tag_entries({source.id}, {1, 2}) == {source.id: {1}}


def test_reparent_tags_supports_shared_and_per_tag_parents(library: Library):
    parent_a = add_tag(library, "parent-a")
    parent_b = add_tag(library, "parent-b")
    child_a = add_tag(library, "child-a")
    child_b = add_tag(library, "child-b")

    assert library.reparent_tags({child_a.id, child_b.id}, {parent_a.id}) == {
        child_a.id: {parent_a.id},
        child_b.id: {parent_a.id},
    }
    assert library.reparent_tags(
        {child_a.id: {parent_b.id}, child_b.id: set()}
    ) == {
        child_a.id: {parent_b.id},
        child_b.id: set(),
    }
    assert unwrap(library.get_tag(child_a.id)).parent_ids == [parent_b.id]
    assert unwrap(library.get_tag(child_b.id)).parent_ids == []


def test_reparent_tags_rejects_cycles_without_partial_updates(library: Library):
    parent = add_tag(library, "cycle-parent")
    child = add_tag(library, "cycle-child")
    library.reparent_tags(child.id, {parent.id})

    with pytest.raises(BulkTagError, match="cycle"):
        library.reparent_tags(parent.id, {child.id})

    assert unwrap(library.get_tag(child.id)).parent_ids == [parent.id]
    assert unwrap(library.get_tag(parent.id)).parent_ids == []
