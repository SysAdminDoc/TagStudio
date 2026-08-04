# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

from pytestqt.qtbot import QtBot

from tagstudio.core.library.interop import InteropSource
from tagstudio.qt.mixed.interop_import import InteropImportModal


def test_interop_import_modal_exposes_all_sources(qtbot: QtBot, qt_driver) -> None:
    modal = InteropImportModal(qt_driver.lib, qt_driver)
    qtbot.addWidget(modal)

    assert [modal.source_combo.itemData(index) for index in range(4)] == list(InteropSource)
    assert modal.create_tags_checkbox.isChecked()
    assert not modal.replace_tags_checkbox.isChecked()
