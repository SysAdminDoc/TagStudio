# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

from pytestqt.qtbot import QtBot

from tagstudio.core.library.alchemy.library import Library
from tagstudio.qt.mixed.bulk_tag import BulkTagEditorPanel


def test_bulk_tag_editor_applies_rename(qtbot: QtBot, library: Library):
    panel = BulkTagEditorPanel(library)
    qtbot.addWidget(panel)
    panel.rename_editor.setPlainText("foo => renamed-foo")

    panel.apply_rename()

    assert library.get_tag_by_name("renamed-foo") is not None
    assert panel.status_label.text() == "Tags renamed."


def test_bulk_tag_editor_reports_invalid_split(qtbot: QtBot, library: Library):
    panel = BulkTagEditorPanel(library)
    qtbot.addWidget(panel)
    panel.split_source_field.setText("foo")
    panel.split_editor.setPlainText("new-tag => not-an-entry")

    panel.apply_split()

    assert "invalid literal" in panel.status_label.text()
