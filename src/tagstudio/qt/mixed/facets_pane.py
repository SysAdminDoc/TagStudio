# Copyright (C) 2025 Travis Abendshien (CyanVoxel).
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio


"""EXIF histogram facets pane."""

from collections.abc import Iterable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from tagstudio.core.facets import FacetBucket, FacetEvent, FacetField, build_facet_buckets
from tagstudio.qt.translations import Translations


class FacetsPane(QWidget):
    """Display selectable camera, focal-length, and rating histograms."""

    bucket_selected = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._events: list[FacetEvent] = []
        self._selected: set[int] = set()
        self._lists: dict[FacetField, QListWidget] = {}

        self.setObjectName("facets_panel")
        self.setMinimumWidth(260)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 0, 0, 0)
        layout.setSpacing(6)

        title = QLabel(Translations["facets.title"])
        title.setObjectName("facets_title")
        self.status_label = QLabel(Translations["facets.loading"])
        self.status_label.setObjectName("facets_status")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addWidget(self.status_label)

        scroll = QScrollArea()
        scroll.setObjectName("facets_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 6, 0)
        for field in FacetField:
            group = QGroupBox(Translations[f"facets.{field.value}"])
            group_layout = QVBoxLayout(group)
            bucket_list = QListWidget()
            bucket_list.setObjectName(f"facet_{field.value}")
            bucket_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
            bucket_list.itemClicked.connect(self._bucket_clicked)
            group_layout.addWidget(bucket_list)
            content_layout.addWidget(group)
            self._lists[field] = bucket_list
        content_layout.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

    def set_events(self, events: Iterable[FacetEvent]) -> None:
        self._events = list(events)
        self._render()

    def set_selected(self, entry_ids: Iterable[int]) -> None:
        self._selected = set(entry_ids)
        for bucket_list in self._lists.values():
            for index in range(bucket_list.count()):
                item = bucket_list.item(index)
                bucket_ids = item.data(Qt.ItemDataRole.UserRole) or []
                item.setBackground(
                    QColor("#3b4d73") if self._selected.intersection(bucket_ids) else QColor()
                )

    def clear(self) -> None:
        self._events.clear()
        self._selected.clear()
        self._render()

    def _bucket_clicked(self, item: QListWidgetItem) -> None:
        entry_ids = item.data(Qt.ItemDataRole.UserRole) or []
        self.bucket_selected.emit(entry_ids)

    def _render(self) -> None:
        for bucket_list in self._lists.values():
            bucket_list.clear()

        if not self._events:
            self.status_label.setText(Translations["facets.no_values"])
            return

        self.status_label.setText(
            Translations.format(
                "facets.entries",
                count=sum(
                    bool(
                        event.camera_model
                        or event.focal_length_mm is not None
                        or event.rating is not None
                    )
                    for event in self._events
                ),
            )
        )
        for field, bucket_list in self._lists.items():
            for bucket in build_facet_buckets(self._events, field):
                self._add_bucket(bucket_list, bucket)

    def _add_bucket(self, bucket_list: QListWidget, bucket: FacetBucket) -> None:
        item = QListWidgetItem(
            Translations.format("facets.bucket", value=bucket.value, count=bucket.count)
        )
        item.setData(Qt.ItemDataRole.UserRole, list(bucket.entry_ids))
        if self._selected.intersection(bucket.entry_ids):
            item.setBackground(QColor("#3b4d73"))
        bucket_list.addItem(item)
