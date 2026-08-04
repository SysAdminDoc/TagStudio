# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

"""External catalog and sidecar import panel."""

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

from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.library.interop import (
    InteropImportResult,
    InteropSource,
    import_external_tags,
)
from tagstudio.qt.translations import Translations
from tagstudio.qt.utils.custom_runnable import CustomRunnable

if TYPE_CHECKING:
    from tagstudio.qt.ts_qt import QtDriver


class InteropImportModal(QWidget):
    """Import tags from one external catalog or a directory of ExifTool sidecars."""

    def __init__(self, library: Library, driver: QtDriver) -> None:
        super().__init__()
        self.lib = library
        self.driver = driver
        self._runnable: CustomRunnable | None = None
        self._result: InteropImportResult | None = None
        self._error: Exception | None = None

        self.setWindowTitle(Translations["interop.title"])
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumSize(640, 360)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)

        description = QLabel(Translations["interop.description"])
        description.setWordWrap(True)
        root_layout.addWidget(description)

        form = QFormLayout()
        self.source_combo = QComboBox()
        self.source_combo.addItem(Translations["interop.source.lightroom"], InteropSource.LIGHTROOM)
        self.source_combo.addItem(Translations["interop.source.digikam"], InteropSource.DIGIKAM)
        self.source_combo.addItem(Translations["interop.source.hydrus"], InteropSource.HYDRUS)
        self.source_combo.addItem(Translations["interop.source.exiftool"], InteropSource.EXIFTOOL)
        form.addRow(Translations["interop.source.label"], self.source_combo)

        path_container = QWidget()
        path_layout = QHBoxLayout(path_container)
        path_layout.setContentsMargins(0, 0, 0, 0)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText(Translations["interop.path.placeholder"])
        browse_button = QPushButton(Translations["interop.browse"])
        browse_button.clicked.connect(self._browse)
        path_layout.addWidget(self.path_edit, 1)
        path_layout.addWidget(browse_button)
        form.addRow(Translations["interop.path.label"], path_container)
        root_layout.addLayout(form)

        self.create_tags_checkbox = QCheckBox(Translations["interop.create_tags"])
        self.create_tags_checkbox.setChecked(True)
        self.replace_tags_checkbox = QCheckBox(Translations["interop.replace_tags"])
        root_layout.addWidget(self.create_tags_checkbox)
        root_layout.addWidget(self.replace_tags_checkbox)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        root_layout.addWidget(self.status_label)
        root_layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.close_button = QPushButton(Translations["generic.close"])
        self.close_button.clicked.connect(self.close)
        self.import_button = QPushButton(Translations["interop.import"])
        self.import_button.setDefault(True)
        self.import_button.clicked.connect(self._start_import)
        buttons.addWidget(self.close_button)
        buttons.addWidget(self.import_button)
        root_layout.addLayout(buttons)

    def _browse(self) -> None:
        source = self.source_combo.currentData()
        if source is InteropSource.EXIFTOOL:
            selected = QFileDialog.getExistingDirectory(
                self,
                Translations["interop.choose_sidecar_directory"],
                str(self.lib.library_dir or ""),
            )
        else:
            selected, _ = QFileDialog.getOpenFileName(
                self,
                Translations["interop.choose_catalog"],
                str(self.lib.library_dir or ""),
                Translations["interop.catalog_filter"],
            )
        if selected:
            self.path_edit.setText(selected)

    def _start_import(self) -> None:
        if self._runnable is not None:
            return
        raw_path = self.path_edit.text().strip()
        if not raw_path:
            QMessageBox.warning(
                self,
                Translations["interop.validation_title"],
                Translations["interop.path.required"],
            )
            return
        path = Path(raw_path).expanduser()
        if not path.exists():
            QMessageBox.warning(
                self,
                Translations["interop.validation_title"],
                Translations.format("interop.path.missing", path=path),
            )
            return

        source = self.source_combo.currentData()
        create_tags = self.create_tags_checkbox.isChecked()
        replace_tags = self.replace_tags_checkbox.isChecked()
        self.import_button.setEnabled(False)
        self.close_button.setEnabled(False)
        self.status_label.setText(Translations["interop.importing"])
        self._result = None
        self._error = None

        def run() -> None:
            try:
                self._result = import_external_tags(
                    self.lib,
                    source,
                    path,
                    create_tags=create_tags,
                    replace_tags=replace_tags,
                )
            except Exception as error:  # noqa: BLE001
                self._error = error

        self._runnable = CustomRunnable(run)
        self._runnable.done.connect(self._finish_import)
        QThreadPool.globalInstance().start(self._runnable)

    def _finish_import(self) -> None:
        self._runnable = None
        self.import_button.setEnabled(True)
        self.close_button.setEnabled(True)
        if self._error is not None:
            error = self._error
            self._error = None
            QMessageBox.warning(
                self,
                Translations["interop.import_failed"],
                str(error),
            )
            self.status_label.setText(Translations["interop.ready"])
            return

        result = self._result
        if result is None:
            return
        self.driver.update_browsing_state()
        message = Translations.format(
            "interop.completed",
            matched=result.matched_files,
            added=result.added_tags,
            created=result.created_tags,
        )
        if result.warnings:
            message += " " + Translations.format(
                "interop.warnings", count=len(result.warnings)
            )
        self.status_label.setText(message)

    @override
    def showEvent(self, event: QtGui.QShowEvent) -> None:
        self.status_label.setText(Translations["interop.ready"])
        super().showEvent(event)

    @override
    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self._runnable is not None:
            event.ignore()
            return
        event.accept()
