# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

"""Read-only filesystem mirror export panel."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, override

from PySide6 import QtGui
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from tagstudio.core.library.mirror import (
    MirrorExportResult,
    MirrorTarget,
    export_read_only_mirror,
)
from tagstudio.qt.translations import Translations
from tagstudio.qt.utils.custom_runnable import CustomRunnable

if TYPE_CHECKING:
    from tagstudio.core.library.alchemy.library import Library
    from tagstudio.qt.ts_qt import QtDriver


class MirrorExportModal(QWidget):
    """Export all or the current search as a local mirror for another photo manager."""

    def __init__(self, library: Library, driver: QtDriver) -> None:
        super().__init__()
        self.lib = library
        self.driver = driver
        self._runnable: CustomRunnable | None = None
        self._result: MirrorExportResult | None = None
        self._error: Exception | None = None

        self.setWindowTitle(Translations["mirror.title"])
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumSize(640, 360)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)

        description = QLabel(Translations["mirror.description"])
        description.setWordWrap(True)
        root_layout.addWidget(description)

        form = QFormLayout()
        self.target_combo = QComboBox()
        self.target_combo.addItem(Translations["mirror.target.immich"], MirrorTarget.IMMICH)
        self.target_combo.addItem(
            Translations["mirror.target.photoprism"], MirrorTarget.PHOTOPRISM
        )
        self.target_combo.addItem(Translations["mirror.target.nextcloud"], MirrorTarget.NEXTCLOUD)
        form.addRow(Translations["mirror.target.label"], self.target_combo)

        destination_container = QWidget()
        destination_layout = QHBoxLayout(destination_container)
        destination_layout.setContentsMargins(0, 0, 0, 0)
        self.destination_edit = QLineEdit()
        self.destination_edit.setPlaceholderText(Translations["mirror.destination.placeholder"])
        browse_button = QPushButton(Translations["mirror.browse"])
        browse_button.clicked.connect(self._browse)
        destination_layout.addWidget(self.destination_edit, 1)
        destination_layout.addWidget(browse_button)
        form.addRow(Translations["mirror.destination.label"], destination_container)

        self.scope_combo = QComboBox()
        self.scope_combo.addItem(Translations["mirror.scope.library"], "library")
        self.scope_combo.addItem(Translations["mirror.scope.search"], "search")
        form.addRow(Translations["mirror.scope.label"], self.scope_combo)
        root_layout.addLayout(form)

        self.overwrite_checkbox = QCheckBox(Translations["mirror.overwrite"])
        self.overwrite_checkbox.setChecked(True)
        root_layout.addWidget(self.overwrite_checkbox)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        root_layout.addWidget(self.status_label)
        root_layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.close_button = QPushButton(Translations["generic.close"])
        self.close_button.clicked.connect(self.close)
        self.export_button = QPushButton(Translations["mirror.export"])
        self.export_button.setDefault(True)
        self.export_button.clicked.connect(self._start_export)
        buttons.addWidget(self.close_button)
        buttons.addWidget(self.export_button)
        root_layout.addLayout(buttons)

        self.status_label.setText(Translations["mirror.ready"])

    def _browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            Translations["mirror.choose_destination"],
            str(self.lib.library_dir or ""),
        )
        if selected:
            self.destination_edit.setText(selected)

    def _start_export(self) -> None:
        if self._runnable is not None:
            return
        raw_destination = self.destination_edit.text().strip()
        if not raw_destination:
            QMessageBox.warning(
                self,
                Translations["mirror.validation_title"],
                Translations["mirror.destination.required"],
            )
            return

        destination = Path(raw_destination).expanduser()
        target = self.target_combo.currentData()
        entry_ids = None
        if self.scope_combo.currentData() == "search":
            try:
                entry_ids = list(
                    self.lib.search_library(self.driver.browsing_history.current, page_size=0).ids
                )
            except Exception as error:  # noqa: BLE001
                QMessageBox.warning(
                    self,
                    Translations["mirror.validation_title"],
                    str(error),
                )
                return

        overwrite = self.overwrite_checkbox.isChecked()
        self.export_button.setEnabled(False)
        self.close_button.setEnabled(False)
        self.status_label.setText(Translations["mirror.exporting"])
        self._result = None
        self._error = None

        def run() -> None:
            try:
                self._result = export_read_only_mirror(
                    self.lib,
                    target,
                    destination,
                    entry_ids=entry_ids,
                    overwrite=overwrite,
                )
            except Exception as error:  # noqa: BLE001
                self._error = error

        self._runnable = CustomRunnable(run)
        self._runnable.done.connect(self._finish_export)
        QThreadPool.globalInstance().start(self._runnable)

    def _finish_export(self) -> None:
        self._runnable = None
        self.export_button.setEnabled(True)
        self.close_button.setEnabled(True)
        if self._error is not None:
            error = self._error
            self._error = None
            QMessageBox.warning(
                self,
                Translations["mirror.export_failed"],
                str(error),
            )
            self.status_label.setText(Translations["mirror.ready"])
            return

        result = self._result
        if result is None:
            return
        message = Translations.format(
            "mirror.completed",
            copied=result.files_copied,
            sidecars=result.sidecars_written,
            skipped=result.skipped_entries,
        )
        self.status_label.setText(message)

    @override
    def showEvent(self, event: QtGui.QShowEvent) -> None:
        self.status_label.setText(Translations["mirror.ready"])
        super().showEvent(event)

    @override
    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self._runnable is not None:
            event.ignore()
            return
        event.accept()
