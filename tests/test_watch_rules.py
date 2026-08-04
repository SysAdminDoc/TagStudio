# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

import json
from pathlib import Path

import pytest

from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.library.alchemy.models import Entry, Tag
from tagstudio.core.library.refresh import IncrementalScanner
from tagstudio.core.library.watch_rules import (
    WATCH_RULES_SCHEMA,
    WATCH_RULES_VERSION,
    WatchRuleConfigError,
    WatchRuleEngine,
    WatchRuleSet,
    WatchRuleTarget,
    WatchTagRule,
)
from tagstudio.core.library.watcher import FileSystemEvent, FileSystemEventKind
from tagstudio.core.utils.types import unwrap


def _library(tmp_path: Path) -> Library:
    library = Library()
    assert library.open_library(tmp_path, ":memory:").success
    return library


def test_watch_rule_matches_relative_path_and_filename() -> None:
    path_rule = WatchTagRule(r"^photos/(2025|2026)/", ("photo",))
    filename_rule = WatchTagRule(
        r"\.raw$", ("raw",), target=WatchRuleTarget.FILENAME, case_sensitive=True
    )

    assert path_rule.matches(Path("photos/2026/IMG_001.JPG"))
    assert not path_rule.matches(Path("documents/2026/IMG_001.JPG"))
    assert filename_rule.matches(Path("photos/IMG_001.raw"))
    assert not filename_rule.matches(Path("photos/IMG_001.RAW"))


def test_watch_rule_validates_and_normalizes_values() -> None:
    rule = WatchTagRule("  IMG  ", (" photo ", "photo", ""))

    assert rule.pattern == "IMG"
    assert rule.tag_names == ("photo",)
    assert rule.target is WatchRuleTarget.PATH

    with pytest.raises(WatchRuleConfigError, match="Invalid watch rule regex"):
        WatchTagRule("[", ("photo",))
    with pytest.raises(WatchRuleConfigError, match="at least one tag"):
        WatchTagRule("photo", ())


def test_watch_rule_set_round_trips_and_retains_valid_rows(tmp_path: Path) -> None:
    path = tmp_path / ".TagStudio" / "watch_rules.json"
    ruleset = WatchRuleSet(
        (
            WatchTagRule(
                r"^incoming/", ("incoming",), target=WatchRuleTarget.PATH, case_sensitive=True
            ),
        )
    )
    ruleset.save(path)

    loaded = WatchRuleSet.load(path)
    assert loaded.rules == ruleset.rules
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "rules": [
            {
                "case_sensitive": True,
                "enabled": True,
                "pattern": "^incoming/",
                "tags": ["incoming"],
                "target": "path",
            }
        ],
        "schema": WATCH_RULES_SCHEMA,
        "version": WATCH_RULES_VERSION,
    }

    path.write_text(
        json.dumps(
            {
                "schema": WATCH_RULES_SCHEMA,
                "version": WATCH_RULES_VERSION,
                "rules": [
                    {"pattern": "[", "tags": ["broken"]},
                    {"pattern": r"\.jpg$", "tags": ["photo"]},
                ],
            }
        ),
        encoding="utf-8",
    )
    loaded = WatchRuleSet.load(path)
    assert len(loaded.rules) == 1
    assert loaded.rules[0].pattern == r"\.jpg$"
    assert len(loaded.errors) == 1


def test_watch_rule_engine_applies_existing_tags_idempotently(tmp_path: Path) -> None:
    library = _library(tmp_path)
    folder = unwrap(library.folder)
    photo_tag = library.add_tag(Tag(name="Photo"))
    raw_tag = library.add_tag(Tag(name="Raw"))
    assert photo_tag is not None and raw_tag is not None

    entry = Entry(folder=folder, path=Path("photos/IMG_001.raw"), fields=library.default_fields)
    assert library.add_entries([entry]) == [entry.id]

    engine = WatchRuleEngine(
        library,
        WatchRuleSet(
            (
                WatchTagRule(r"^photos/", ("Photo",)),
                WatchTagRule(r"\.raw$", ("Raw",), target=WatchRuleTarget.FILENAME),
            )
        ),
    )
    first = engine.apply_to_all()
    second = engine.apply_to_all()

    assert first.matched_entries == 1
    assert first.added_tags == 2
    assert second.matched_entries == 1
    assert second.added_tags == 0
    tagged_entry = library.get_entry_full(entry.id)
    assert tagged_entry is not None
    assert {tag.name for tag in tagged_entry.tags} == {"Photo", "Raw"}


def test_incremental_scanner_applies_rules_to_created_files(tmp_path: Path) -> None:
    library = _library(tmp_path)
    photo_tag = library.add_tag(Tag(name="Photo"))
    assert photo_tag is not None
    new_file = tmp_path / "photos" / "new-photo.jpg"
    new_file.parent.mkdir()
    new_file.touch()

    engine = WatchRuleEngine(
        library,
        WatchRuleSet((WatchTagRule(r"^photos/", ("Photo",)),)),
    )
    scanner = IncrementalScanner(library, engine)
    result = scanner.apply(
        [FileSystemEvent(FileSystemEventKind.CREATED, new_file)]
    )

    assert len(result.added_ids) == 1
    entry = library.get_entry_full(result.added_ids[0])
    assert entry is not None
    assert {tag.name for tag in entry.tags} == {"Photo"}
