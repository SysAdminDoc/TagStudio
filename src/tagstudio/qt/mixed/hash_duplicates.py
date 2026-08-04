# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

"""Dedicated exact-duplicate results view."""

from typing import TYPE_CHECKING

from humanfriendly import format_size
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tagstudio.core.library.alchemy.hash_duplicates import (
    HashDuplicateGroup,
    HashDuplicateScanner,
)
from tagstudio.core.library.alchemy.library import Library
from tagstudio.qt.translations import Translations
from tagstudio.qt.utils.custom_runnable import CustomRunnable

if TYPE_CHECKING:
    from tagstudio.qt.ts_qt import QtDriver


class HashDuplicateModal(QWidget):
    """Display exact duplicate groups found by a streamed BLAKE3 pass."""

    def __init__(self, library: Library, driver: "QtDriver") -> None:
        super().__init__()
        self.lib = library
        self.driver = driver
        self._groups: tuple[HashDuplicateGroup, ...] = ()
        self._runnable: CustomRunnable | None = None

        self.setWindowTitle(Translations["file.hash_duplicates.title"])
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumSize(760, 480)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)

        description = QLabel(Translations["file.hash_duplicates.description"])
        description.setWordWrap(True)
        root_layout.addWidget(description)

        controls = QHBoxLayout()
        self.status_label = QLabel()
        self.scan_button = QPushButton(Translations["file.hash_duplicates.scan"])
        self.scan_button.clicked.connect(self.refresh)
        controls.addWidget(self.status_label, 1)
        controls.addWidget(self.scan_button)
        root_layout.addLayout(controls)

        self.results_tree = QTreeWidget()
        self.results_tree.setHeaderLabels(
            [
                Translations["file.hash_duplicates.path"],
                Translations["file.hash_duplicates.size"],
                Translations["file.hash_duplicates.digest"],
            ]
        )
        self.results_tree.setAlternatingRowColors(True)
        root_layout.addWidget(self.results_tree, 1)

        close_button = QPushButton(Translations["generic.close"])
        close_button.clicked.connect(self.close)
        root_layout.addWidget(close_button, 0, Qt.AlignmentFlag.AlignRight)

        self.refresh()

    def refresh(self) -> None:
        """Run a new hash pass in the shared thread pool."""
        if self._runnable is not None:
            return
        self.scan_button.setEnabled(False)
        self.status_label.setText(Translations["file.hash_duplicates.scanning"])
        self.results_tree.clear()

        def scan() -> None:
            self._groups = HashDuplicateScanner(self.lib).scan()

        self._runnable = CustomRunnable(scan)
        self._runnable.done.connect(self._finish_refresh)
        QThreadPool.globalInstance().start(self._runnable)

    def _finish_refresh(self) -> None:
        self._runnable = None
        self.scan_button.setEnabled(True)
        groups = self._groups
        total_entries = sum(group.count for group in groups)
        if not groups:
            self.status_label.setText(Translations["file.hash_duplicates.none"])
            return

        self.status_label.setText(
            Translations.format(
                "file.hash_duplicates.results",
                groups=len(groups),
                entries=total_entries,
            )
        )
        entries = {
            entry.id: entry
            for entry in self.lib.get_entries_full(
                [entry_id for group in groups for entry_id in group.entry_ids]
            )
        }
        for index, group in enumerate(groups, start=1):
            group_item = QTreeWidgetItem(
                [
                    Translations.format("file.hash_duplicates.group", index=index),
                    format_size(group.size),
                    group.digest,
                ]
            )
            self.results_tree.addTopLevelItem(group_item)
            group_item.setExpanded(True)
            for entry_id in group.entry_ids:
                entry = entries.get(entry_id)
                if entry is None:
                    continue
                QTreeWidgetItem(group_item, [str(self.lib.resolve_entry_path(entry)), "", ""])

        self.results_tree.resizeColumnToContents(0)
        self.results_tree.resizeColumnToContents(1)
