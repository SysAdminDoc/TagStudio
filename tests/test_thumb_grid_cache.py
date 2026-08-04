# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

from pathlib import Path
from types import SimpleNamespace

from tagstudio.core.constants import TAG_ARCHIVED
from tagstudio.qt.thumb_grid_layout import ThumbGridLayout


def test_thumb_grid_evicts_offscreen_entry_and_render_caches():
    layout = SimpleNamespace(
        _entries={entry_id: object() for entry_id in range(1, 6)},
        _entry_paths={
            Path(f"{entry_id}.jpg"): entry_id for entry_id in range(1, 6)
        },
        _tag_entries={TAG_ARCHIVED: set(range(1, 6))},
        _render_results={
            Path(): object(),
            **{Path(f"{entry_id}.jpg"): object() for entry_id in range(1, 6)},
        },
    )

    ThumbGridLayout._retain_window_cache(layout, {3, 4})

    assert set(layout._entries) == {3, 4}
    assert set(layout._entry_paths.values()) == {3, 4}
    assert layout._tag_entries[TAG_ARCHIVED] == {3, 4}
    assert set(layout._render_results) == {Path(), Path("3.jpg"), Path("4.jpg")}
