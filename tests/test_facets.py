# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio


from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy

from tagstudio.core.facets import FacetEvent, FacetField, build_facet_buckets
from tagstudio.qt.mixed.facets_pane import FacetsPane


def _event(
    entry_id: int,
    *,
    camera_model: str | None = None,
    focal_length_mm: float | None = None,
    rating: float | None = None,
) -> FacetEvent:
    return FacetEvent(entry_id, camera_model, focal_length_mm, rating)


def test_build_facet_buckets_sorts_and_groups_each_dimension():
    events = [
        _event(3, camera_model="Canon EOS R5", focal_length_mm=50, rating=4),
        _event(1, camera_model="Nikon Z8", focal_length_mm=35, rating=5),
        _event(2, camera_model="canon EOS R5", focal_length_mm=50, rating=4),
        _event(4),
    ]

    camera_buckets = build_facet_buckets(events, FacetField.CAMERA_MODEL)
    assert [(bucket.value, bucket.entry_ids) for bucket in camera_buckets] == [
        ("Canon EOS R5", (3,)),
        ("canon EOS R5", (2,)),
        ("Nikon Z8", (1,)),
    ]
    focal_buckets = build_facet_buckets(events, FacetField.FOCAL_LENGTH)
    assert [(bucket.value, bucket.entry_ids) for bucket in focal_buckets] == [
        ("35 mm", (1,)),
        ("50 mm", (3, 2)),
    ]
    rating_buckets = build_facet_buckets(events, FacetField.RATING)
    assert [(bucket.value, bucket.entry_ids) for bucket in rating_buckets] == [
        ("4 / 5", (3, 2)),
        ("5 / 5", (1,)),
    ]


def test_facets_pane_renders_buckets_and_emits_selected_entries(qtbot):
    pane = FacetsPane()
    qtbot.addWidget(pane)
    pane.set_events(
        [
            _event(1, camera_model="Nikon Z8", focal_length_mm=35, rating=5),
            _event(2, camera_model="Canon EOS R5", focal_length_mm=50, rating=4),
        ]
    )

    camera_list = pane._lists[FacetField.CAMERA_MODEL]
    assert camera_list.count() == 2
    assert camera_list.item(0).text() == "Canon EOS R5 · 1 entries"
    assert pane._lists[FacetField.FOCAL_LENGTH].count() == 2
    assert pane._lists[FacetField.RATING].count() == 2

    pane.set_selected([2])
    assert camera_list.item(0).background().color().name() == "#3b4d73"

    signal_spy = QSignalSpy(pane.bucket_selected)
    pane._bucket_clicked(camera_list.item(0))
    assert signal_spy.count() == 1
    assert signal_spy.at(0) == [[2]]

    assert camera_list.item(0).data(Qt.ItemDataRole.UserRole) == [2]
