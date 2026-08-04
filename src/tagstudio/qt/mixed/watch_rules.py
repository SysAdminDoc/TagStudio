# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

"""Editor for library-local regular-expression tagging rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, override

from PySide6 import QtGui
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from tagstudio.core.library.watch_rules import (
    WatchRuleConfigError,
    WatchRuleSet,
    WatchRuleTarget,
    WatchTagRule,
)
from tagstudio.qt.translations import Translations

if TYPE_CHECKING:
    from tagstudio.core.library.alchemy.library import Library
    from tagstudio.qt.ts_qt import QtDriver


@dataclass(slots=True)
class _RuleRow:
    enabled: QCheckBox
    target: QComboBox
    pattern: QLineEdit
    tags: QLineEdit
    case_sensitive: QCheckBox


class WatchRulesModal(QWidget):
    """Manage regex-to-tag rules stored in the current library's .TagStudio folder."""

    def __init__(self, library: Library, driver: QtDriver):
        super().__init__()
        self.library = library
        self.driver = driver
        self._rows: list[_RuleRow] = []

        self.setWindowTitle(Translations["watch_rules.title"])
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumSize(900, 500)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)

        description = QLabel(Translations["watch_rules.description"])
        description.setWordWrap(True)
        root_layout.addWidget(description)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            [
                Translations["watch_rules.column.enabled"],
                Translations["watch_rules.column.match"],
                Translations["watch_rules.column.regex"],
                Translations["watch_rules.column.tags"],
                Translations["watch_rules.column.case_sensitive"],
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        root_layout.addWidget(self.table, stretch=1)

        edit_layout = QHBoxLayout()
        add_button = QPushButton(Translations["watch_rules.add"])
        add_button.clicked.connect(lambda: self._add_rule())
        remove_button = QPushButton(Translations["watch_rules.remove"])
        remove_button.clicked.connect(self._remove_selected)
        edit_layout.addWidget(add_button)
        edit_layout.addWidget(remove_button)
        edit_layout.addStretch(1)
        root_layout.addLayout(edit_layout)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        cancel_button = QPushButton(Translations["generic.cancel"])
        cancel_button.clicked.connect(self.hide)
        save_button = QPushButton(Translations["generic.save"])
        save_button.setAutoDefault(True)
        save_button.clicked.connect(self._save)
        button_layout.addWidget(cancel_button)
        button_layout.addWidget(save_button)
        root_layout.addLayout(button_layout)

        self._populate(WatchRuleSet())

    @override
    def showEvent(self, event: QtGui.QShowEvent) -> None:
        engine = getattr(self.driver, "watch_rule_engine", None)
        ruleset = engine.ruleset if engine is not None else WatchRuleSet()
        self._populate(ruleset)
        super().showEvent(event)

    def _populate(self, ruleset: WatchRuleSet) -> None:
        self.table.setRowCount(0)
        self._rows.clear()
        for rule in ruleset.rules:
            self._add_rule(rule)

    def _add_rule(self, rule: WatchTagRule | None = None) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)

        enabled = QCheckBox()
        enabled.setChecked(rule.enabled if rule is not None else True)
        enabled.setToolTip(Translations["watch_rules.enabled_help"])

        target = QComboBox()
        target.addItem(Translations["watch_rules.match.path"], WatchRuleTarget.PATH)
        target.addItem(Translations["watch_rules.match.filename"], WatchRuleTarget.FILENAME)
        if rule is not None:
            target.setCurrentIndex(target.findData(rule.target))

        pattern = QLineEdit(rule.pattern if rule is not None else "")
        pattern.setPlaceholderText(Translations["watch_rules.regex_placeholder"])

        tags = QLineEdit(", ".join(rule.tag_names) if rule is not None else "")
        tags.setPlaceholderText(Translations["watch_rules.tags_placeholder"])

        case_sensitive = QCheckBox()
        case_sensitive.setChecked(rule.case_sensitive if rule is not None else False)

        self.table.setCellWidget(row, 0, enabled)
        self.table.setCellWidget(row, 1, target)
        self.table.setCellWidget(row, 2, pattern)
        self.table.setCellWidget(row, 3, tags)
        self.table.setCellWidget(row, 4, case_sensitive)
        self._rows.append(_RuleRow(enabled, target, pattern, tags, case_sensitive))
        self.table.selectRow(row)

    def _remove_selected(self) -> None:
        selected_rows = sorted(
            {index.row() for index in self.table.selectionModel().selectedRows()}, reverse=True
        )
        for row in selected_rows:
            self.table.removeRow(row)
            self._rows.pop(row)

    def _rules_from_editor(self) -> WatchRuleSet:
        rules: list[WatchTagRule] = []
        for index, row in enumerate(self._rows, start=1):
            pattern = row.pattern.text().strip()
            tag_names = tuple(name.strip() for name in row.tags.text().split(",") if name.strip())
            if not pattern and not tag_names:
                continue
            if not pattern:
                raise WatchRuleConfigError(f"Rule {index} needs a regular expression")
            if not tag_names:
                raise WatchRuleConfigError(f"Rule {index} needs at least one tag")

            rule = WatchTagRule(
                pattern=pattern,
                tag_names=tag_names,
                target=row.target.currentData(),
                case_sensitive=row.case_sensitive.isChecked(),
                enabled=row.enabled.isChecked(),
            )
            unknown = [
                name for name in rule.tag_names if self.library.get_tag_by_name(name) is None
            ]
            if unknown:
                raise WatchRuleConfigError(
                    f"Rule {index} references unknown tag(s): {', '.join(unknown)}"
                )
            rules.append(rule)
        return WatchRuleSet(tuple(rules))

    def _save(self) -> None:
        try:
            ruleset = self._rules_from_editor()
            result = self.driver.save_watch_rules(ruleset)
        except (OSError, WatchRuleConfigError, ValueError) as error:
            QMessageBox.warning(
                self,
                Translations["watch_rules.validation_title"],
                str(error),
            )
            return

        if result.unknown_tag_names:
            message = Translations.format(
                "watch_rules.saved_with_unknown_tags",
                tags=", ".join(result.unknown_tag_names),
            )
        else:
            message = Translations.format(
                "watch_rules.saved",
                matched=result.matched_entries,
                added=result.added_tags,
            )
        self.driver.main_window.status_bar.showMessage(message, 8)
        self.hide()
