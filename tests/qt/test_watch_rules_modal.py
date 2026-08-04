# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

import pytest
from pytestqt.qtbot import QtBot

from tagstudio.core.library.watch_rules import WatchRuleConfigError, WatchRuleTarget
from tagstudio.qt.mixed.watch_rules import WatchRulesModal


def test_watch_rules_modal_reads_editor_rows(qtbot: QtBot, qt_driver) -> None:  # pyright: ignore[reportMissingTypeArgument]
    modal = WatchRulesModal(qt_driver.lib, qt_driver)
    qtbot.addWidget(modal)

    modal._add_rule()  # pyright: ignore[reportPrivateUsage]
    row = modal._rows[0]  # pyright: ignore[reportPrivateUsage]
    row.target.setCurrentIndex(row.target.findData(WatchRuleTarget.FILENAME))
    row.pattern.setText(r"\.jpg$")
    row.tags.setText("foo, bar")

    ruleset = modal._rules_from_editor()  # pyright: ignore[reportPrivateUsage]

    assert len(ruleset.rules) == 1
    assert ruleset.rules[0].target is WatchRuleTarget.FILENAME
    assert ruleset.rules[0].tag_names == ("foo", "bar")


def test_watch_rules_modal_rejects_unknown_tags(qtbot: QtBot, qt_driver) -> None:
    modal = WatchRulesModal(qt_driver.lib, qt_driver)
    qtbot.addWidget(modal)

    modal._add_rule()  # pyright: ignore[reportPrivateUsage]
    row = modal._rows[0]  # pyright: ignore[reportPrivateUsage]
    row.pattern.setText("photo")
    row.tags.setText("does-not-exist")

    with pytest.raises(WatchRuleConfigError, match="unknown tag"):
        modal._rules_from_editor()  # pyright: ignore[reportPrivateUsage]
