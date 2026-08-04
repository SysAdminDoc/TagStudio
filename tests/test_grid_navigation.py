# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio


from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import Mock

from tagstudio.qt.ts_qt import QtDriver


class NavigationDriver:
    def __init__(self, entry_ids: list[int], columns: int):
        self.frame_content = entry_ids
        self._selected: OrderedDict[int, None] = OrderedDict()
        self.layout = SimpleNamespace(
            _entry_ids=entry_ids,
            column_count=Mock(return_value=columns),
            scroll_to=Mock(),
            update=Mock(),
        )
        self.main_window = SimpleNamespace(
            thumb_layout=self.layout,
            preview_panel=SimpleNamespace(set_selection=Mock()),
        )

    @property
    def selected(self) -> list[int]:
        return list(self._selected)

    @property
    def last_selected(self) -> int | None:
        return next(reversed(self._selected), None)

    def select_entry(self, entry_id: int):
        if entry_id in self._selected:
            self._selected.pop(entry_id)
        else:
            self._selected[entry_id] = None

    def select_to_entry(self, entry_id: int):
        if not self._selected:
            self.select_entry(entry_id)
            return
        start = self.frame_content.index(self.last_selected)
        end = self.frame_content.index(entry_id)
        if start > end:
            start, end = end, start
        for index in range(start, end + 1):
            self._selected[self.frame_content[index]] = None

    def clear_selected(self):
        self._selected.clear()

    def set_clipboard_menu_viability(self):
        pass

    def set_select_actions_visibility(self):
        pass


def test_grid_navigation_moves_by_columns():
    driver = NavigationDriver([1, 2, 3, 4, 5, 6], columns=3)

    QtDriver.move_grid_selection(driver, "right")
    assert driver.selected == [1]

    QtDriver.move_grid_selection(driver, "down")
    assert driver.selected == [4]

    QtDriver.move_grid_selection(driver, "left", extend=True)
    assert driver.selected == [4, 3]


def test_grid_navigation_home_and_end():
    driver = NavigationDriver([1, 2, 3, 4], columns=2)
    driver._selected = OrderedDict.fromkeys([2, 3])

    QtDriver.move_grid_selection(driver, "home")
    assert driver.selected == [1]

    QtDriver.move_grid_selection(driver, "end")
    assert driver.selected == [4]
