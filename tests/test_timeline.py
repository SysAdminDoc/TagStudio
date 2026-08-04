# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio


from datetime import datetime

from PySide6.QtCore import Qt

from tagstudio.core.timeline import (
    TimelineEvent,
    TimelineZoom,
    group_timeline_events,
    parse_exif_datetime,
)
from tagstudio.qt.mixed.timeline_pane import TimelinePane


def _event(entry_id: int, captured_at: str, color: str = "#ff0000") -> TimelineEvent:
    parsed = parse_exif_datetime(captured_at)
    assert parsed is not None
    return TimelineEvent(entry_id, f"photo-{entry_id}.jpg", parsed, color)


def test_parse_exif_datetime_supports_exif_and_iso_values():
    assert parse_exif_datetime("2024:01:02 03:04:05") == datetime(2024, 1, 2, 3, 4, 5)
    assert parse_exif_datetime("2024-01-02T03:04:05Z") == datetime(2024, 1, 2, 3, 4, 5)
    assert parse_exif_datetime("not a date") is None


def test_group_timeline_events_changes_bucket_size_with_zoom():
    events = [
        _event(3, "2024:02:03 00:00:00", "#00ff00"),
        _event(1, "2024:01:02 00:00:00"),
        _event(2, "2024:01:02 01:00:00", "#0000ff"),
        _event(4, "2023:12:31 00:00:00"),
    ]

    years = group_timeline_events(events, TimelineZoom.YEAR)
    assert [(group.label, group.entry_ids) for group in years] == [
        ("2023", (4,)),
        ("2024", (1, 2, 3)),
    ]

    months = group_timeline_events(events, TimelineZoom.MONTH)
    assert [(group.label, group.entry_ids) for group in months] == [
        ("December 2023", (4,)),
        ("January 2024", (1, 2)),
        ("February 2024", (3,)),
    ]
    assert months[1].colors == ("#ff0000", "#0000ff")

    days = group_timeline_events(events, "day")
    assert [group.label for group in days] == [
        "December 31, 2023",
        "January 2, 2024",
        "February 3, 2024",
    ]


def test_timeline_pane_renders_the_selected_zoom(qtbot):
    pane = TimelinePane()
    qtbot.addWidget(pane)
    pane.set_events(
        [
            _event(1, "2024:01:02 00:00:00"),
            _event(2, "2024:01:03 00:00:00"),
        ]
    )

    assert pane.zoom is TimelineZoom.MONTH
    assert pane.groups_list.count() == 1

    pane.set_zoom(TimelineZoom.DAY)

    assert pane.groups_list.count() == 2
    pane.set_selected([2])
    assert pane.groups_list.item(1).data(Qt.ItemDataRole.UserRole) == [2]
