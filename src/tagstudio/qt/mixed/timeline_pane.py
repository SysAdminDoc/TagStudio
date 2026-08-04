# Copyright (C) 2025 Travis Abendshien (CyanVoxel).
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio


"""Calendar timeline pane for EXIF capture dates."""

from collections.abc import Iterable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tagstudio.core.timeline import (
    TimelineEvent,
    TimelineZoom,
    group_timeline_events,
)
from tagstudio.qt.translations import Translations


class TimelinePane(QWidget):
    """Display dated entries grouped by year, month, or day."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._events: list[TimelineEvent] = []
        self._selected: set[int] = set()

        self.setObjectName("timeline_panel")
        self.setMinimumWidth(260)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        self.title_label = QLabel(Translations["timeline.title"])
        self.title_label.setObjectName("timeline_title")
        self.zoom_label = QLabel(Translations["timeline.zoom"])
        self.zoom_combo = QComboBox()
        self.zoom_combo.setObjectName("timeline_zoom")
        for zoom in TimelineZoom:
            self.zoom_combo.addItem(Translations[f"timeline.zoom.{zoom.value}"], zoom)
        self.zoom_combo.setCurrentIndex(self.zoom_combo.findData(TimelineZoom.MONTH))
        self.zoom_combo.currentIndexChanged.connect(self._zoom_changed)
        header.addWidget(self.title_label)
        header.addStretch(1)
        header.addWidget(self.zoom_label)
        header.addWidget(self.zoom_combo)

        self.status_label = QLabel(Translations["timeline.loading"])
        self.status_label.setObjectName("timeline_status")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.groups_list = QListWidget()
        self.groups_list.setObjectName("timeline_groups")
        self.groups_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)

        layout.addLayout(header)
        layout.addWidget(self.status_label)
        layout.addWidget(self.groups_list, 1)

    @property
    def zoom(self) -> TimelineZoom:
        return TimelineZoom(self.zoom_combo.currentData())

    def set_zoom(self, zoom: TimelineZoom) -> None:
        index = self.zoom_combo.findData(TimelineZoom(zoom))
        if index >= 0:
            self.zoom_combo.setCurrentIndex(index)

    def set_events(self, events: Iterable[TimelineEvent]) -> None:
        self._events = list(events)
        self._render()

    def set_selected(self, entry_ids: Iterable[int]) -> None:
        self._selected = set(entry_ids)
        for index in range(self.groups_list.count()):
            item = self.groups_list.item(index)
            group_ids = item.data(Qt.ItemDataRole.UserRole) or []
            item.setBackground(
                QColor("#3b4d73") if self._selected.intersection(group_ids) else QColor()
            )

    def clear(self) -> None:
        self._events.clear()
        self._selected.clear()
        self._render()

    def _zoom_changed(self, _index: int) -> None:
        self._render()

    def _render(self) -> None:
        groups = group_timeline_events(self._events, self.zoom)
        self.groups_list.clear()
        if not groups:
            self.status_label.setText(Translations["timeline.no_dates"])
            return

        self.status_label.setText(
            Translations.format("timeline.entries", count=sum(group.count for group in groups))
        )
        for group in groups:
            item = QListWidgetItem(
                Translations.format("timeline.group", label=group.label, count=group.count)
            )
            item.setData(Qt.ItemDataRole.UserRole, list(group.entry_ids))
            item.setForeground(QColor(group.primary_color))
            if self._selected.intersection(group.entry_ids):
                item.setBackground(QColor("#3b4d73"))
            self.groups_list.addItem(item)
