# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

import re
from collections.abc import Iterable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
)

from tagstudio.core.library.alchemy.library import BulkTagError, Library
from tagstudio.qt.translations import Translations
from tagstudio.qt.views.panel_modal import PanelWidget


class BulkTagEditorPanel(PanelWidget):
    """Small, name-oriented front end for the library's atomic bulk tag APIs."""

    changed = Signal()

    def __init__(self, library: Library):
        super().__init__()
        self.lib = library
        self.root_layout = QVBoxLayout(self)

        self.tabs = QTabWidget()
        self.root_layout.addWidget(self.tabs)

        self.rename_editor = QPlainTextEdit()
        self.rename_editor.setPlaceholderText("old name => new name\none per line")
        self.tabs.addTab(self.__make_operation_tab(
            self.rename_editor,
            Translations["bulk_tags.rename_help"],
            self.apply_rename,
        ), Translations["bulk_tags.rename"])

        self.merge_source_field = QLineEdit()
        self.merge_source_field.setPlaceholderText("source tags, separated by commas")
        self.merge_target_field = QLineEdit()
        self.merge_target_field.setPlaceholderText("target tag")
        merge_form = QFormLayout()
        merge_form.addRow(Translations["bulk_tags.sources"], self.merge_source_field)
        merge_form.addRow(Translations["bulk_tags.target"], self.merge_target_field)
        self.tabs.addTab(
            self.__make_operation_tab(
                merge_form, Translations["bulk_tags.merge_help"], self.apply_merge
            ),
            Translations["bulk_tags.merge"],
        )

        self.split_source_field = QLineEdit()
        self.split_source_field.setPlaceholderText("source tag")
        self.split_editor = QPlainTextEdit()
        self.split_editor.setPlaceholderText(
            "new tag => entry IDs, separated by commas\none per line"
        )
        self.remove_original_checkbox = QCheckBox(Translations["bulk_tags.remove_original"])
        self.remove_original_checkbox.setChecked(True)
        split_form = QFormLayout()
        split_form.addRow(Translations["bulk_tags.source"], self.split_source_field)
        split_form.addRow(Translations["bulk_tags.assignments"], self.split_editor)
        split_layout = QVBoxLayout()
        split_layout.addLayout(split_form)
        split_layout.addWidget(self.remove_original_checkbox)
        split_layout.addWidget(self.__make_apply_button(self.apply_split))
        split_layout.addWidget(QLabel(Translations["bulk_tags.split_help"]))
        split_tab = QVBoxLayout()
        split_tab.addLayout(split_layout)
        split_tab.addStretch(1)
        split_widget = self.__layout_widget(split_tab)
        self.tabs.addTab(split_widget, Translations["bulk_tags.split"])

        self.reparent_children_field = QLineEdit()
        self.reparent_children_field.setPlaceholderText("child tags, separated by commas")
        self.reparent_parent_field = QLineEdit()
        self.reparent_parent_field.setPlaceholderText("parent tag; leave empty to clear")
        reparent_form = QFormLayout()
        reparent_form.addRow(Translations["bulk_tags.children"], self.reparent_children_field)
        reparent_form.addRow(Translations["bulk_tags.parent"], self.reparent_parent_field)
        self.tabs.addTab(
            self.__make_operation_tab(
                reparent_form,
                Translations["bulk_tags.reparent_help"],
                self.apply_reparent,
            ),
            Translations["bulk_tags.reparent"],
        )

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.root_layout.addWidget(self.status_label)

    @staticmethod
    def __layout_widget(layout: QVBoxLayout) -> QLabel:
        widget = QLabel()
        widget.setLayout(layout)
        return widget

    def __make_apply_button(self, callback) -> QPushButton:
        button = QPushButton(Translations["bulk_tags.apply"])
        button.clicked.connect(callback)
        return button

    def __make_operation_tab(self, content, help_text: str, callback) -> QLabel:
        layout = QVBoxLayout()
        if isinstance(content, QFormLayout):
            layout.addLayout(content)
        else:
            layout.addWidget(content)
        layout.addWidget(self.__make_apply_button(callback))
        layout.addWidget(QLabel(help_text))
        layout.addStretch(1)
        return self.__layout_widget(layout)

    @staticmethod
    def __split_names(text: str) -> list[str]:
        return [value.strip() for value in re.split(r"[,\n]", text) if value.strip()]

    def __resolve_tag(self, name: str) -> int:
        tag = self.lib.get_tag_by_name(name.strip())
        if tag is None:
            tag = next(
                (
                    candidate
                    for candidate in self.lib.tags
                    if candidate.name.casefold() == name.casefold()
                ),
                None,
            )
        if tag is None:
            raise BulkTagError(f"Unknown tag: {name.strip()!r}")
        return tag.id

    def __resolve_tags(self, names: Iterable[str]) -> list[int]:
        ids = [self.__resolve_tag(name) for name in names]
        if len(ids) != len(set(ids)):
            raise BulkTagError("Each tag may appear only once in an operation")
        return ids

    def __success(self, message: str) -> None:
        self.status_label.setStyleSheet("color: palette(highlight)")
        self.status_label.setText(message)
        self.changed.emit()

    def __failure(self, error: Exception) -> None:
        self.status_label.setStyleSheet("color: palette(darkRed)")
        self.status_label.setText(str(error))

    def apply_rename(self) -> None:
        try:
            renames: dict[int, str] = {}
            for line in self.rename_editor.toPlainText().splitlines():
                if not line.strip():
                    continue
                if "=>" not in line:
                    raise BulkTagError("Rename lines must use: old name => new name")
                old_name, new_name = line.split("=>", maxsplit=1)
                renames[self.__resolve_tag(old_name)] = new_name.strip()
            self.lib.rename_tags(renames)
            self.__success(Translations["bulk_tags.rename_success"])
        except (BulkTagError, ValueError) as error:
            self.__failure(error)

    def apply_merge(self) -> None:
        try:
            source_ids = self.__resolve_tags(self.__split_names(self.merge_source_field.text()))
            target_id = self.__resolve_tag(self.merge_target_field.text())
            self.lib.merge_tags(source_ids, target_id)
            self.__success(Translations["bulk_tags.merge_success"])
        except (BulkTagError, ValueError) as error:
            self.__failure(error)

    def apply_split(self) -> None:
        try:
            source_id = self.__resolve_tag(self.split_source_field.text())
            splits: dict[str, set[int]] = {}
            for line in self.split_editor.toPlainText().splitlines():
                if not line.strip():
                    continue
                if "=>" not in line:
                    raise BulkTagError("Split lines must use: new tag => entry IDs")
                name, raw_ids = line.split("=>", maxsplit=1)
                entry_ids = {
                    int(value) for value in re.split(r"[,\s]+", raw_ids.strip()) if value
                }
                splits[name.strip()] = entry_ids
            self.lib.split_tag(
                source_id,
                splits,
                remove_original=self.remove_original_checkbox.isChecked(),
            )
            self.__success(Translations["bulk_tags.split_success"])
        except (BulkTagError, ValueError) as error:
            self.__failure(error)

    def apply_reparent(self) -> None:
        try:
            child_ids = self.__resolve_tags(
                self.__split_names(self.reparent_children_field.text())
            )
            parent_text = self.reparent_parent_field.text().strip()
            parent_ids = [] if not parent_text else [self.__resolve_tag(parent_text)]
            self.lib.reparent_tags(child_ids, parent_ids)
            self.__success(Translations["bulk_tags.reparent_success"])
        except (BulkTagError, ValueError) as error:
            self.__failure(error)
