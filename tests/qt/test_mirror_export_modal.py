# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

from pytestqt.qtbot import QtBot

from tagstudio.core.library.mirror import MirrorTarget
from tagstudio.qt.mixed.mirror_export import MirrorExportModal


def test_mirror_export_modal_exposes_all_targets(qtbot: QtBot, qt_driver) -> None:
    modal = MirrorExportModal(qt_driver.lib, qt_driver)
    qtbot.addWidget(modal)

    assert [modal.target_combo.itemData(index) for index in range(3)] == list(MirrorTarget)
    assert modal.scope_combo.itemData(0) == "library"
    assert modal.scope_combo.itemData(1) == "search"
    assert modal.overwrite_checkbox.isChecked()
