# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

"""Persistent regular-expression rules for automatically tagging library files."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from tagstudio.core.constants import TS_FOLDER_NAME

if TYPE_CHECKING:
    from tagstudio.core.library.alchemy.library import Library


logger = structlog.get_logger(__name__)

WATCH_RULES_FILENAME = "watch_rules.json"
WATCH_RULES_SCHEMA = "tagstudio.watch-rules"
WATCH_RULES_VERSION = 1


class WatchRuleTarget(str, Enum):
    """The relative file value tested by a watch rule."""

    PATH = "path"
    FILENAME = "filename"


class WatchRuleConfigError(ValueError):
    """Raised when a watch-rule document cannot be represented safely."""


@dataclass(frozen=True, slots=True)
class WatchTagRule:
    """One regular-expression rule and the existing tags it should apply."""

    pattern: str
    tag_names: tuple[str, ...]
    target: WatchRuleTarget = WatchRuleTarget.PATH
    case_sensitive: bool = False
    enabled: bool = True
    _compiled_pattern: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.pattern, str):
            raise WatchRuleConfigError("Watch rule pattern must be a string")
        pattern = self.pattern.strip()
        if not pattern:
            raise WatchRuleConfigError("Watch rule pattern cannot be empty")

        try:
            target = (
                self.target
                if isinstance(self.target, WatchRuleTarget)
                else WatchRuleTarget(self.target)
            )
        except (TypeError, ValueError) as error:
            raise WatchRuleConfigError(f"Unknown watch rule target: {self.target!r}") from error

        normalized_tags: list[str] = []
        for tag_name in self.tag_names:
            if not isinstance(tag_name, str):
                raise WatchRuleConfigError("Watch rule tag names must be strings")
            normalized_name = tag_name.strip()
            if normalized_name and normalized_name not in normalized_tags:
                normalized_tags.append(normalized_name)
        if not normalized_tags:
            raise WatchRuleConfigError("Watch rule must contain at least one tag name")
        if not isinstance(self.case_sensitive, bool):
            raise WatchRuleConfigError("Watch rule case_sensitive must be a boolean")
        if not isinstance(self.enabled, bool):
            raise WatchRuleConfigError("Watch rule enabled must be a boolean")

        flags = 0 if self.case_sensitive else re.IGNORECASE
        try:
            compiled_pattern = re.compile(pattern, flags)
        except re.error as error:
            raise WatchRuleConfigError(f"Invalid watch rule regex {pattern!r}: {error}") from error

        object.__setattr__(self, "pattern", pattern)
        object.__setattr__(self, "tag_names", tuple(normalized_tags))
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "_compiled_pattern", compiled_pattern)

    def matches(self, relative_path: Path) -> bool:
        """Return whether this rule matches a relative library path."""
        path_text = relative_path.as_posix().replace("\\", "/")
        candidate = (
            path_text
            if self.target is WatchRuleTarget.PATH
            else path_text.rsplit("/", 1)[-1]
        )
        return self._compiled_pattern.search(candidate) is not None

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation of this rule."""
        return {
            "case_sensitive": self.case_sensitive,
            "enabled": self.enabled,
            "pattern": self.pattern,
            "tags": list(self.tag_names),
            "target": self.target.value,
        }


@dataclass(frozen=True, slots=True)
class WatchRuleSet:
    """A versioned collection of watch rules and non-fatal load diagnostics."""

    rules: tuple[WatchTagRule, ...] = ()
    errors: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> WatchRuleSet:
        """Parse a JSON object, retaining valid rules when one row is malformed."""
        schema = payload.get("schema", WATCH_RULES_SCHEMA)
        version = payload.get("version", WATCH_RULES_VERSION)
        if schema != WATCH_RULES_SCHEMA:
            raise WatchRuleConfigError(f"Unsupported watch rule schema: {schema!r}")
        if version != WATCH_RULES_VERSION:
            raise WatchRuleConfigError(f"Unsupported watch rule version: {version!r}")

        raw_rules = payload.get("rules", [])
        if not isinstance(raw_rules, list):
            raise WatchRuleConfigError("Watch rule 'rules' must be a list")

        rules: list[WatchTagRule] = []
        errors: list[str] = []
        for index, raw_rule in enumerate(raw_rules, start=1):
            if not isinstance(raw_rule, Mapping):
                errors.append(f"Rule {index} is not an object")
                continue
            raw_tags = raw_rule.get("tags", raw_rule.get("tag_names", ()))
            if not isinstance(raw_tags, list | tuple):
                errors.append(f"Rule {index} has invalid tags")
                continue
            try:
                rules.append(
                    WatchTagRule(
                        pattern=raw_rule.get("pattern", ""),
                        tag_names=tuple(raw_tags),
                        target=raw_rule.get("target", WatchRuleTarget.PATH.value),
                        case_sensitive=raw_rule.get("case_sensitive", False),
                        enabled=raw_rule.get("enabled", True),
                    )
                )
            except (TypeError, WatchRuleConfigError) as error:
                errors.append(f"Rule {index}: {error}")

        return cls(tuple(rules), tuple(errors))

    @classmethod
    def load(cls, path: Path) -> WatchRuleSet:
        """Load rules from ``path`` without allowing malformed config to break a library."""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            logger.warning("[WatchRules] Could not read watch rules", path=path, error=error)
            return cls(errors=(f"Could not read watch rules: {error}",))

        if not isinstance(payload, Mapping):
            logger.warning("[WatchRules] Ignoring non-object watch rules", path=path)
            return cls(errors=("Watch rule document must be an object",))

        try:
            ruleset = cls.from_mapping(payload)
        except WatchRuleConfigError as error:
            logger.warning("[WatchRules] Ignoring unsupported watch rules", path=path, error=error)
            return cls(errors=(str(error),))

        for diagnostic in ruleset.errors:
            logger.warning(
                "[WatchRules] Ignoring malformed rule", path=path, error=diagnostic
            )
        return ruleset

    def save(self, path: Path) -> None:
        """Atomically write this rule set to the library-local config path."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "rules": [rule.to_dict() for rule in self.rules],
            "schema": WATCH_RULES_SCHEMA,
            "version": WATCH_RULES_VERSION,
        }
        temporary_path = path.with_name(f".{path.name}.tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary_path.replace(path)


@dataclass(frozen=True, slots=True)
class WatchRuleApplyResult:
    """Summary of one automatic-tagging pass."""

    matched_entries: int = 0
    added_tags: int = 0
    unknown_tag_names: tuple[str, ...] = ()


class WatchRuleEngine:
    """Match persisted rules and add their known tags to library entries."""

    def __init__(self, library: Library, ruleset: WatchRuleSet | None = None) -> None:
        self.library = library
        self.path = self._path_for_library(library)
        self.ruleset = ruleset if ruleset is not None else self._load_rules()
        self._tag_id_cache: dict[str, int | None] = {}
        self._unknown_tag_names: set[str] = set()

    @staticmethod
    def _path_for_library(library: Library) -> Path | None:
        if library.library_dir is None:
            return None
        return Path(library.library_dir) / TS_FOLDER_NAME / WATCH_RULES_FILENAME

    def _load_rules(self) -> WatchRuleSet:
        if self.path is None:
            return WatchRuleSet()
        return WatchRuleSet.load(self.path)

    @property
    def has_rules(self) -> bool:
        """Return whether at least one enabled rule is available."""
        return any(rule.enabled for rule in self.ruleset.rules)

    def replace(self, ruleset: WatchRuleSet, *, save: bool = False) -> None:
        """Replace active rules and optionally persist them."""
        if save:
            if self.path is None:
                raise WatchRuleConfigError("Cannot save watch rules without a library path")
            ruleset.save(self.path)
        self.ruleset = ruleset
        self._tag_id_cache.clear()
        self._unknown_tag_names.clear()

    def reload(self) -> None:
        """Reload rules from disk after an external configuration change."""
        self.replace(self._load_rules())

    def _tag_ids_for_path(self, relative_path: Path) -> tuple[int, ...]:
        tag_ids: list[int] = []
        seen_ids: set[int] = set()
        for rule in self.ruleset.rules:
            if not rule.enabled or not rule.matches(relative_path):
                continue
            for tag_name in rule.tag_names:
                if tag_name not in self._tag_id_cache:
                    tag = self.library.get_tag_by_name(tag_name)
                    self._tag_id_cache[tag_name] = tag.id if tag is not None else None
                tag_id = self._tag_id_cache[tag_name]
                if tag_id is None:
                    self._unknown_tag_names.add(tag_name)
                elif tag_id not in seen_ids:
                    seen_ids.add(tag_id)
                    tag_ids.append(tag_id)
        return tuple(tag_ids)

    def apply_to_entry(self, entry_id: int, relative_path: Path) -> int:
        """Apply matching rule tags to one entry and return the number newly added."""
        tag_ids = self._tag_ids_for_path(Path(relative_path))
        if not tag_ids:
            return 0
        return self._add_missing_tags(entry_id, tag_ids)

    def apply_to_entries(self, entries: Iterable[tuple[int, Path]]) -> WatchRuleApplyResult:
        """Apply rules to explicit entry/path pairs."""
        matched_entries = 0
        added_tags = 0
        for entry_id, relative_path in entries:
            tag_ids = self._tag_ids_for_path(Path(relative_path))
            if not tag_ids:
                continue
            matched_entries += 1
            added_tags += self._add_missing_tags(entry_id, tag_ids)
        return self._result(matched_entries, added_tags)

    def apply_to_all(self) -> WatchRuleApplyResult:
        """Apply rules to every indexed entry, retaining existing manual tags."""
        if not self.has_rules:
            return self._result()
        return self.apply_to_entries((entry.id, entry.path) for entry in self.library.all_entries())

    def _result(self, matched_entries: int = 0, added_tags: int = 0) -> WatchRuleApplyResult:
        return WatchRuleApplyResult(
            matched_entries=matched_entries,
            added_tags=added_tags,
            unknown_tag_names=tuple(sorted(self._unknown_tag_names)),
        )

    def _add_missing_tags(self, entry_id: int, tag_ids: tuple[int, ...]) -> int:
        existing = self.library.get_tag_entries(tag_ids, (entry_id,))
        missing_ids = tuple(tag_id for tag_id in tag_ids if entry_id not in existing[tag_id])
        if not missing_ids:
            return 0
        return self.library.add_tags_to_entries(entry_id, missing_ids)
