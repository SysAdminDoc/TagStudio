# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

# NOTE: This file contains necessary use of deprecated first-party code until that
# code is removed in a future version (prefs).
# pyright: reportDeprecated=false


import re
import shutil
import time
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from os import makedirs
from os.path import relpath
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from warnings import catch_warnings

import sqlalchemy
import structlog
import wcmatch.fnmatch as fnmatch
from humanfriendly import format_timespan  # pyright: ignore[reportUnknownVariableType]
from sqlalchemy import (
    URL,
    ColumnExpressionArgument,
    Engine,
    NullPool,
    ScalarResult,
    and_,
    asc,
    create_engine,
    delete,
    desc,
    exists,
    func,
    inspect,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import (
    InstanceState,
    Session,
    contains_eager,
    joinedload,
    make_transient,
    noload,
    selectinload,
)
from sqlalchemy.orm.exc import DetachedInstanceError
from typing_extensions import deprecated

from tagstudio.core.constants import (
    BACKUP_FOLDER_NAME,
    IGNORE_NAME,
    LEGACY_TAG_FIELD_IDS,
    RESERVED_NAMESPACE_PREFIX,
    RESERVED_TAG_END,
    RESERVED_TAG_START,
    TAG_ARCHIVED,
    TAG_FAVORITE,
    TAG_META,
    TS_FOLDER_NAME,
)
from tagstudio.core.enums import LibraryPrefs
from tagstudio.core.library.alchemy import default_color_groups
from tagstudio.core.library.alchemy.constants import (
    DB_VERSION,
    DB_VERSION_CURRENT_KEY,
    DB_VERSION_INITIAL_KEY,
    DB_VERSION_LEGACY_KEY,
    JSON_FILENAME,
    SQL_FILENAME,
    TAG_CHILDREN_QUERY,
)
from tagstudio.core.library.alchemy.db import make_tables
from tagstudio.core.library.alchemy.enums import (
    MAX_SQL_VARIABLES,
    BrowsingState,
    FieldTypeEnum,
    SortingModeEnum,
)
from tagstudio.core.library.alchemy.fields import (
    BaseField,
    DatetimeField,
    FieldID,
    TextField,
)
from tagstudio.core.library.alchemy.joins import TagEntry, TagParent
from tagstudio.core.library.alchemy.migrations import Migration, MigrationRunner
from tagstudio.core.library.alchemy.models import (
    Entry,
    Folder,
    FolderOverride,
    Namespace,
    Preferences,
    SavedSearch,
    Tag,
    TagAlias,
    TagColorGroup,
    ValueType,
    Version,
)
from tagstudio.core.library.alchemy.sidecars import (
    SidecarDocument,
    SidecarError,
    SidecarFormat,
    normalize_format,
    read_sidecar,
    sidecar_path,
    write_sidecar,
)
from tagstudio.core.library.alchemy.visitors import SQLBoolExpressionBuilder
from tagstudio.core.library.ignore import PATH_GLOB_FLAGS, Ignore, ignore_to_glob
from tagstudio.core.library.json.library import Library as JsonLibrary
from tagstudio.core.query_lang.util import ParsingError
from tagstudio.core.utils.types import unwrap
from tagstudio.qt.translations import Translations

if TYPE_CHECKING:
    from sqlalchemy import Select


logger = structlog.get_logger(__name__)


class ReservedNamespaceError(Exception):
    """Raise during an unauthorized attempt to create or modify a reserved namespace value.

    Reserved namespace prefix: "tagstudio".
    """

    pass


class BulkTagError(ValueError):
    """Raise when a bulk tag operation cannot be applied safely."""

    pass


def slugify(input_string: str, allow_reserved: bool = False) -> str:
    # Convert to lowercase and normalize unicode characters
    slug = unicodedata.normalize("NFKD", input_string.lower())

    # Remove non-word characters (except hyphens and spaces)
    slug = re.sub(r"[^\w\s-]", "", slug).strip()

    # Replace spaces with hyphens
    slug = re.sub(r"[-\s]+", "-", slug)

    if not allow_reserved and slug.startswith(RESERVED_NAMESPACE_PREFIX):
        raise ReservedNamespaceError

    return slug


def get_default_tags() -> tuple[Tag, ...]:
    meta_tag = Tag(
        id=TAG_META,
        name="Meta Tags",
        aliases={TagAlias(name="Meta"), TagAlias(name="Meta Tag")},
        is_category=True,
    )
    archive_tag = Tag(
        id=TAG_ARCHIVED,
        name="Archived",
        aliases={TagAlias(name="Archive")},
        parent_tags={meta_tag},
        is_hidden=True,
        color_slug="red",
        color_namespace="tagstudio-standard",
    )
    favorite_tag = Tag(
        id=TAG_FAVORITE,
        name="Favorite",
        aliases={
            TagAlias(name="Favorited"),
            TagAlias(name="Favorites"),
        },
        parent_tags={meta_tag},
        color_slug="yellow",
        color_namespace="tagstudio-standard",
    )

    return archive_tag, favorite_tag, meta_tag


# The difference in the number of default JSON tags vs default tags in the current version.
DEFAULT_TAG_DIFF: int = len(get_default_tags()) - len([TAG_ARCHIVED, TAG_FAVORITE])


@dataclass(frozen=True)
class SearchResult:
    """Wrapper for search results.

    Attributes:
        total_count(int): total number of items for given query, might be different than len(items).
        ids(list[int]): for current page (size matches filter.page_size).
    """

    total_count: int
    ids: list[int]

    def __bool__(self) -> bool:
        """Boolean evaluation for the wrapper.

        :return: True if there are ids in the result.
        """
        return self.total_count > 0

    def __len__(self) -> int:
        """Return the total number of ids in the result."""
        return len(self.ids)

    def __getitem__(self, index: int) -> int:
        """Allow to access ids via index directly on the wrapper."""
        return self.ids[index]


@dataclass
class LibraryStatus:
    """Keep status of library opening operation."""

    success: bool
    library_path: Path | None = None
    message: str | None = None
    msg_description: str | None = None
    json_migration_req: bool = False


@dataclass(frozen=True)
class FolderOverrideConfig:
    """Effective rules for a path after applying matching parent overrides."""

    ignore_patterns: tuple[str, ...] = ()
    field_defaults: tuple[str, ...] | None = None
    auto_tag_ids: tuple[int, ...] = ()


class Library:
    """Class for the Library object, and all CRUD operations made upon it."""

    library_dir: Path | None = None
    storage_path: Path | str | None = None
    engine: Engine | None = None
    folder: Folder | None = None
    included_files: set[Path] = set()

    def __init__(self) -> None:
        self.dupe_entries_count: int = -1  # NOTE: For internal management.
        self.dupe_files_count: int = -1
        self.ignored_entries_count: int = -1
        self.unlinked_entries_count: int = -1
        self.included_files = set()

    def close(self):
        if self.engine:
            self.engine.dispose()
        self.library_dir = None
        self.storage_path = None
        self.folder = None
        self.included_files = set()

        self.dupe_entries_count = -1
        self.dupe_files_count = -1
        self.ignored_entries_count = -1
        self.unlinked_entries_count = -1

    def migrate_json_to_sqlite(self, json_lib: JsonLibrary):
        """Migrate JSON library data to the SQLite database."""
        logger.info("Starting Library Conversion...")
        start_time = time.time()
        folder = unwrap(self.folder)

        # Tags
        for tag in json_lib.tags:
            color_namespace, color_slug = default_color_groups.json_to_sql_color(tag.color)
            disambiguation_id: int | None = None
            if tag.subtag_ids and tag.subtag_ids[0] != tag.id:
                disambiguation_id = tag.subtag_ids[0]
            self.add_tag(
                Tag(
                    id=tag.id,
                    name=tag.name,
                    shorthand=tag.shorthand,
                    color_namespace=color_namespace,
                    color_slug=color_slug,
                    disambiguation_id=disambiguation_id,
                )
            )
            # Apply user edits to built-in JSON tags.
            if tag.id in range(RESERVED_TAG_START, RESERVED_TAG_END + 1):
                updated_tag = self.get_tag(tag.id)
                if not updated_tag:
                    continue
                updated_tag.name = tag.name
                updated_tag.shorthand = tag.shorthand
                updated_tag.color_namespace = color_namespace
                updated_tag.color_slug = color_slug
                self.update_tag(updated_tag)  # NOTE: This just calls add_tag?

        # Tag Aliases
        for tag in json_lib.tags:
            for alias in tag.aliases:
                if not alias:
                    break
                # Only add new (user-created) aliases to the default tags.
                # This prevents pre-existing built-in aliases from being added as duplicates.
                if tag.id in range(RESERVED_TAG_START, RESERVED_TAG_END + 1):
                    for dt in get_default_tags():
                        if dt.id == tag.id and alias not in dt.alias_strings:
                            self.add_alias(name=alias, tag_id=tag.id)
                else:
                    self.add_alias(name=alias, tag_id=tag.id)

        # Parent Tags (Previously known as "Subtags" in JSON)
        for tag in json_lib.tags:
            for parent_id in tag.subtag_ids:
                self.add_parent_tag(parent_id=parent_id, child_id=tag.id)

        # Entries
        self.add_entries(
            [
                Entry(
                    path=entry.path / entry.filename,
                    folder=folder,
                    fields=[],
                    id=entry.id + 1,  # JSON IDs start at 0 instead of 1
                    date_added=datetime.now(),
                )
                for entry in json_lib.entries
            ]
        )
        for entry in json_lib.entries:
            for field in entry.fields:  # pyright: ignore[reportUnknownVariableType]
                for k, v in field.items():  # pyright: ignore[reportUnknownVariableType]
                    # Old tag fields get added as tags
                    if k in LEGACY_TAG_FIELD_IDS:
                        self.add_tags_to_entries(entry_ids=entry.id + 1, tag_ids=v)
                    else:
                        self.add_field_to_entry(
                            entry_id=(entry.id + 1),  # JSON IDs start at 0 instead of 1
                            field_id=self.get_field_name_from_id(k),
                            value=v,
                        )

        # Preferences
        self.set_prefs(LibraryPrefs.EXTENSION_LIST, [x.strip(".") for x in json_lib.ext_list])
        self.set_prefs(LibraryPrefs.IS_EXCLUDE_LIST, json_lib.is_exclude_list)

        end_time = time.time()
        logger.info(f"Library Converted! ({format_timespan(end_time - start_time)})")

    def get_field_name_from_id(self, field_id: int) -> FieldID | None:
        for f in FieldID:
            if field_id == f.value.id:
                return f
        return None

    def tag_display_name(self, tag: Tag | None) -> str:
        if not tag:
            return "<NO TAG>"

        if tag.disambiguation_id:
            with Session(self.engine) as session:
                disam_tag = session.scalar(select(Tag).where(Tag.id == tag.disambiguation_id))
                if not disam_tag:
                    return "<NO DISAM TAG>"
                disam_name = disam_tag.shorthand
                if not disam_name:
                    disam_name = disam_tag.name
                return f"{tag.name} ({disam_name})"
        else:
            return tag.name

    def open_library(
        self, library_dir: Path, storage_path: Path | str | None = None
    ) -> LibraryStatus:
        is_new: bool = True
        if storage_path == ":memory:":
            self.storage_path = storage_path
            is_new = True
            return self.open_sqlite_library(library_dir, is_new)
        else:
            self.storage_path = library_dir / TS_FOLDER_NAME / SQL_FILENAME
            assert isinstance(self.storage_path, Path)
            if self.verify_ts_folder(library_dir) and (is_new := not self.storage_path.exists()):
                json_path = library_dir / TS_FOLDER_NAME / JSON_FILENAME
                if json_path.exists():
                    return LibraryStatus(
                        success=False,
                        library_path=library_dir,
                        message="[JSON] Legacy v9.4 library requires conversion to v9.5+",
                        json_migration_req=True,
                    )

        return self.open_sqlite_library(library_dir, is_new)

    @staticmethod
    def __canonical_root(root: Path) -> Path:
        return Path(root).expanduser().resolve(strict=False)

    def __resolve_folder_path(self, path: Path, base: Path | None = None) -> Path:
        """Resolve a persisted root path against the currently opened library."""
        path = Path(path)
        if path.is_absolute():
            return self.__canonical_root(path)
        if base is None:
            if self.library_dir is None:
                raise ValueError("No library directory is configured.")
            base = self.library_dir
        return self.__canonical_root(Path(base) / path)

    def __relative_folder_path(self, root: Path, base: Path | None = None) -> Path:
        """Convert a root to a path that can move with this library database."""
        if base is None:
            if self.library_dir is None:
                raise ValueError("No library directory is configured.")
            base = self.library_dir

        canonical_root = self.__canonical_root(root)
        canonical_base = self.__canonical_root(base)
        try:
            relative = canonical_root.relative_to(canonical_base)
        except ValueError:
            try:
                relative = Path(relpath(canonical_root, canonical_base))
            except ValueError as error:
                # A different volume has no meaningful relative path. Keep the absolute
                # location as an unavoidable external-root fallback so multi-root libraries
                # continue to work; roots on the library volume remain fully relocatable.
                logger.warning(
                    "[Library] Retaining cross-volume root as absolute path",
                    path=canonical_root,
                    error=error,
                )
                return canonical_root
        return relative if relative.parts else Path(".")

    def __get_or_create_folder(self, session: Session, root: Path) -> Folder:
        """Return the persisted folder for ``root``, matching equivalent path spellings."""
        canonical_root = self.__canonical_root(root)
        for folder in session.scalars(select(Folder).order_by(Folder.id)):
            if self.__resolve_folder_path(folder.path) == canonical_root:
                return folder

        folder = Folder(path=self.__relative_folder_path(canonical_root), uuid=str(uuid4()))
        session.add(folder)
        session.flush()
        return folder

    def add_root(self, root: Path) -> Folder:
        """Add a scan root to this library and return its persisted folder record.

        Roots may be offline when a portable or external-drive library is opened; they are
        normalized before being stored so relative paths remain stable across sessions.
        """
        if self.engine is None:
            raise ValueError("Library is not open.")

        with Session(self.engine, expire_on_commit=False) as session:
            folder = self.__get_or_create_folder(session, Path(root))
            session.commit()
            session.expunge(folder)
            folder.path = self.__canonical_root(root)
            return folder

    @property
    def folders(self) -> list[Folder]:
        """Return all configured roots in stable insertion order."""
        if self.engine is None:
            return []

        with Session(self.engine, expire_on_commit=False) as session:
            folders = list(session.scalars(select(Folder).order_by(Folder.id)))
            session.expunge_all()
            for folder in folders:
                folder.path = self.__resolve_folder_path(folder.path)
            return folders

    @property
    def roots(self) -> tuple[Path, ...]:
        """Return all configured root paths, including the primary library directory."""
        return tuple(folder.path for folder in self.folders)

    def root_for_path(self, file_path: Path) -> Path | None:
        """Return the configured root containing an absolute file path, if any."""
        absolute_path = self.__canonical_root(file_path)
        for root in self.roots:
            try:
                absolute_path.relative_to(self.__canonical_root(root))
                return root
            except ValueError:
                continue
        return None

    def relative_path(self, file_path: Path) -> Path:
        """Return a file path relative to its configured root when possible."""
        root = self.root_for_path(file_path)
        if root is None:
            return file_path
        return self.__canonical_root(file_path).relative_to(self.__canonical_root(root))

    @staticmethod
    def __normalize_override_path(path: Path) -> Path:
        path = Path(path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Folder override paths must stay within their library root")
        parts = tuple(part for part in path.parts if part not in ("", "."))
        return Path(*parts) if parts else Path(".")

    def __relative_path_for_folder(self, session: Session, folder_id: int, path: Path) -> Path:
        path = Path(path)
        if path.is_absolute():
            stored_root = session.scalar(select(Folder.path).where(Folder.id == folder_id))
            if stored_root is None:
                raise ValueError(f"Root is not configured: {folder_id}")
            root = self.__resolve_folder_path(stored_root)
            try:
                path = self.__canonical_root(path).relative_to(self.__canonical_root(root))
            except ValueError as error:
                raise ValueError(f"Path is outside root: {path}") from error
        return path

    def __matching_folder_overrides(
        self, session: Session, folder_id: int, path: Path
    ) -> list[FolderOverride]:
        relative_path = self.__relative_path_for_folder(session, folder_id, path)
        matches: list[FolderOverride] = []
        for override in session.scalars(
            select(FolderOverride)
            .where(FolderOverride.folder_id == folder_id)
            .order_by(FolderOverride.id)
        ):
            override_path = self.__normalize_override_path(override.path)
            if override_path == Path("."):
                matches.append(override)
                continue
            try:
                relative_path.relative_to(override_path)
            except ValueError:
                continue
            matches.append(override)

        return sorted(
            matches,
            key=lambda override: (
                len(self.__normalize_override_path(override.path).parts),
                override.id,
            ),
        )

    def folder_override_config(
        self, path: Path, folder: Folder | Path | int | None = None
    ) -> FolderOverrideConfig:
        """Return the effective override values for a file or directory path."""
        with Session(self.engine) as session:
            folder_id = self.__folder_id(session, folder)
            matches = self.__matching_folder_overrides(session, folder_id, path)

            ignore_patterns: list[str] = []
            field_defaults: tuple[str, ...] | None = None
            auto_tag_ids: tuple[int, ...] = ()
            for override in matches:
                if override.ignore_patterns is not None:
                    ignore_patterns.extend(override.ignore_patterns)
                if override.field_defaults is not None:
                    field_defaults = tuple(override.field_defaults)
                if override.auto_tag_ids is not None:
                    auto_tag_ids = tuple(override.auto_tag_ids)

            return FolderOverrideConfig(
                ignore_patterns=tuple(dict.fromkeys(ignore_patterns)),
                field_defaults=field_defaults,
                auto_tag_ids=auto_tag_ids,
            )

    def get_folder_overrides(
        self, folder: Folder | Path | int | None = None
    ) -> list[FolderOverride]:
        """Return all overrides for a configured root."""
        with Session(self.engine, expire_on_commit=False) as session:
            folder_id = self.__folder_id(session, folder)
            overrides = list(
                session.scalars(
                    select(FolderOverride)
                    .where(FolderOverride.folder_id == folder_id)
                    .order_by(FolderOverride.path)
                )
            )
            session.expunge_all()
            return overrides

    def set_folder_override(
        self,
        path: Path,
        *,
        folder: Folder | Path | int | None = None,
        ignore_patterns: Iterable[str] | None = None,
        field_defaults: Iterable[str | FieldID] | None = None,
        auto_tag_ids: Iterable[int] | None = None,
    ) -> FolderOverride:
        """Create or replace rules for a directory relative to one configured root.

        ``None`` means inherit the parent value; an empty iterable explicitly clears local
        rules.  Ignore patterns are additive across matching parent directories.
        """
        normalized_path = self.__normalize_override_path(path)
        normalized_fields = (
            None
            if field_defaults is None
            else [
                field.name if isinstance(field, FieldID) else str(field) for field in field_defaults
            ]
        )
        normalized_tags = None if auto_tag_ids is None else [int(tag_id) for tag_id in auto_tag_ids]
        normalized_ignores = (
            None
            if ignore_patterns is None
            else [pattern.strip() for pattern in ignore_patterns if pattern.strip()]
        )

        with Session(self.engine, expire_on_commit=False) as session:
            folder_id = self.__folder_id(session, folder)
            if normalized_fields is not None:
                known_fields = set(session.scalars(select(ValueType.key)))
                unknown_fields = set(normalized_fields).difference(known_fields)
                if unknown_fields:
                    raise ValueError(f"Unknown field defaults: {sorted(unknown_fields)}")

            override = session.scalar(
                select(FolderOverride).where(
                    and_(
                        FolderOverride.folder_id == folder_id,
                        FolderOverride.path == normalized_path,
                    )
                )
            )
            if override is None:
                override = FolderOverride(
                    folder_id=folder_id,
                    path=normalized_path,
                    ignore_patterns=normalized_ignores,
                    field_defaults=normalized_fields,
                    auto_tag_ids=normalized_tags,
                )
                session.add(override)
            else:
                override.ignore_patterns = normalized_ignores
                override.field_defaults = normalized_fields
                override.auto_tag_ids = normalized_tags

            session.commit()
            session.expunge(override)
            return override

    def clear_folder_override(self, path: Path, folder: Folder | Path | int | None = None) -> bool:
        """Remove the exact override for a directory, leaving parent rules intact."""
        normalized_path = self.__normalize_override_path(path)
        with Session(self.engine) as session:
            folder_id = self.__folder_id(session, folder)
            override = session.scalar(
                select(FolderOverride).where(
                    and_(
                        FolderOverride.folder_id == folder_id,
                        FolderOverride.path == normalized_path,
                    )
                )
            )
            if override is None:
                return False
            session.delete(override)
            session.commit()
            return True

    @staticmethod
    def __validate_saved_search_query(query: str) -> str:
        normalized_query = query.strip()
        try:
            _ = BrowsingState.from_search_query(normalized_query).ast
        except ParsingError as error:
            raise ValueError(f"Invalid saved search query: {error}") from error
        return normalized_query

    def get_saved_searches(self, pinned_only: bool = False) -> list[SavedSearch]:
        """Return saved searches in their sidebar order."""
        with Session(self.engine, expire_on_commit=False) as session:
            statement = select(SavedSearch).order_by(SavedSearch.position, SavedSearch.id)
            if pinned_only:
                statement = statement.where(SavedSearch.is_pinned.is_(True))
            saved_searches = list(session.scalars(statement))
            session.expunge_all()
            return saved_searches

    def get_saved_search(self, saved_search_id: int) -> SavedSearch | None:
        """Return one saved search without leaving it attached to a session."""
        with Session(self.engine, expire_on_commit=False) as session:
            saved_search = session.get(SavedSearch, saved_search_id)
            if saved_search is not None:
                session.expunge(saved_search)
            return saved_search

    def saved_search_state(self, saved_search_id: int) -> BrowsingState | None:
        """Materialize one saved query as a browsing state."""
        saved_search = self.get_saved_search(saved_search_id)
        if saved_search is None:
            return None
        return BrowsingState.from_search_query(saved_search.query)

    def create_saved_search(
        self,
        name: str,
        query: str,
        *,
        is_pinned: bool = True,
        position: int | None = None,
    ) -> SavedSearch:
        """Persist a validated named query for reuse as a smart collection."""
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("Saved search name cannot be empty")
        normalized_query = self.__validate_saved_search_query(query)
        if position is not None and position < 0:
            raise ValueError("Saved search position cannot be negative")

        with Session(self.engine, expire_on_commit=False) as session:
            if position is None:
                max_position = session.scalar(select(func.max(SavedSearch.position)))
                position = 0 if max_position is None else max_position + 1
            saved_search = SavedSearch(
                name=normalized_name,
                query=normalized_query,
                is_pinned=is_pinned,
                position=position,
            )
            session.add(saved_search)
            try:
                session.commit()
            except IntegrityError as error:
                session.rollback()
                raise ValueError(
                    f"A saved search named {normalized_name!r} already exists"
                ) from error
            session.expunge(saved_search)
            return saved_search

    def update_saved_search(
        self,
        saved_search_id: int,
        *,
        name: str | None = None,
        query: str | None = None,
        is_pinned: bool | None = None,
        position: int | None = None,
    ) -> SavedSearch | None:
        """Update a saved search and return its detached record."""
        normalized_name = name.strip() if name is not None else None
        if normalized_name == "":
            raise ValueError("Saved search name cannot be empty")
        normalized_query = self.__validate_saved_search_query(query) if query is not None else None
        if position is not None and position < 0:
            raise ValueError("Saved search position cannot be negative")

        with Session(self.engine, expire_on_commit=False) as session:
            saved_search = session.get(SavedSearch, saved_search_id)
            if saved_search is None:
                return None
            if normalized_name is not None:
                saved_search.name = normalized_name
            if normalized_query is not None:
                saved_search.query = normalized_query
            if is_pinned is not None:
                saved_search.is_pinned = is_pinned
            if position is not None:
                saved_search.position = position
            try:
                session.commit()
            except IntegrityError as error:
                session.rollback()
                raise ValueError(
                    f"A saved search named {normalized_name!r} already exists"
                ) from error
            session.expunge(saved_search)
            return saved_search

    def delete_saved_search(self, saved_search_id: int) -> bool:
        """Delete a saved search, returning whether a record was removed."""
        with Session(self.engine) as session:
            saved_search = session.get(SavedSearch, saved_search_id)
            if saved_search is None:
                return False
            session.delete(saved_search)
            session.commit()
            return True

    def reorder_saved_searches(self, saved_search_ids: Iterable[int]) -> None:
        """Set pinned sidebar order while retaining unmentioned searches afterward."""
        requested_ids = list(saved_search_ids)
        if len(requested_ids) != len(set(requested_ids)):
            raise ValueError("Saved search IDs must be unique")

        with Session(self.engine) as session:
            saved_searches = list(
                session.scalars(select(SavedSearch).order_by(SavedSearch.position, SavedSearch.id))
            )
            saved_by_id = {saved_search.id: saved_search for saved_search in saved_searches}
            if any(saved_search_id not in saved_by_id for saved_search_id in requested_ids):
                raise ValueError("Cannot reorder an unknown saved search")
            ordered = [saved_by_id[saved_search_id] for saved_search_id in requested_ids]
            ordered.extend(
                saved_search
                for saved_search in saved_searches
                if saved_search.id not in requested_ids
            )
            for position, saved_search in enumerate(ordered):
                saved_search.position = position
            session.commit()

    def get_scan_ignore_patterns(self, root: Path) -> list[str]:
        """Return root ignore rules plus path-prefixed folder override rules."""
        patterns = list(Ignore.get_patterns(root))
        with Session(self.engine) as session:
            folder_id = self.__folder_id(session, root)
            overrides = session.scalars(
                select(FolderOverride)
                .where(FolderOverride.folder_id == folder_id)
                .order_by(FolderOverride.path)
            )
            for override in overrides:
                if not override.ignore_patterns:
                    continue
                prefix = self.__normalize_override_path(override.path)
                for pattern in override.ignore_patterns:
                    if not pattern:
                        continue
                    negated = pattern.startswith("!")
                    raw_pattern = pattern[1:] if negated else pattern
                    pattern_path = Path(raw_pattern)
                    if pattern_path.is_absolute() or ".." in pattern_path.parts:
                        raise ValueError(
                            "Folder override ignore patterns must stay within their directory"
                        )
                    full_pattern = pattern_path if prefix == Path(".") else prefix / pattern_path
                    patterns.append(("!" if negated else "") + full_pattern.as_posix())
        return patterns

    def is_path_ignored(self, file_path: Path) -> bool:
        """Evaluate ignore rules, including the effective folder override, for a file."""
        root = self.root_for_path(file_path)
        if root is None:
            return False
        matcher = fnmatch.compile(
            ignore_to_glob(self.get_scan_ignore_patterns(root)), PATH_GLOB_FLAGS
        )
        return bool(matcher.match(root / self.relative_path(file_path)))

    def default_fields_for_path(
        self, path: Path, folder: Folder | Path | int | None = None
    ) -> list[BaseField]:
        """Return field defaults after applying the nearest matching folder override."""
        with Session(self.engine) as session:
            folder_id = self.__folder_id(session, folder)
            config = self.folder_override_config(path, folder=folder_id)
            if config.field_defaults is None:
                value_types = list(
                    session.scalars(
                        select(ValueType)
                        .where(ValueType.is_default.is_(True))
                        .order_by(ValueType.position)
                    )
                )
            else:
                value_types_by_key = {
                    value_type.key: value_type
                    for value_type in session.scalars(
                        select(ValueType).where(ValueType.key.in_(config.field_defaults))
                    )
                }
                value_types = [
                    value_types_by_key[key]
                    for key in config.field_defaults
                    if key in value_types_by_key
                ]
            return [value_type.as_field for value_type in value_types]

    def auto_tag_ids_for_path(
        self, path: Path, folder: Folder | Path | int | None = None
    ) -> tuple[int, ...]:
        """Return tag IDs configured for a new entry at ``path``."""
        return self.folder_override_config(path, folder=folder).auto_tag_ids

    def __folder_id(
        self,
        session: Session,
        folder: Folder | Path | int | None,
    ) -> int:
        if folder is None:
            if self.folder is None or self.folder.id is None:
                raise ValueError("No primary library root is configured.")
            return self.folder.id
        if isinstance(folder, int):
            return folder
        if isinstance(folder, Folder):
            return folder.id

        canonical_root = self.__canonical_root(folder)
        for candidate in session.scalars(select(Folder)):
            if self.__resolve_folder_path(candidate.path) == canonical_root:
                return candidate.id
        raise ValueError(f"Root is not configured: {folder}")

    def get_folder(self, folder_id: int) -> Folder | None:
        """Load a configured root by ID without leaving a live SQLAlchemy session attached."""
        if self.engine is None:
            return None
        with Session(self.engine, expire_on_commit=False) as session:
            folder = session.scalar(select(Folder).where(Folder.id == folder_id))
            if folder is not None:
                session.expunge(folder)
                folder.path = self.__resolve_folder_path(folder.path)
            return folder

    def resolve_entry_path(self, entry: Entry) -> Path:
        """Resolve an entry's relative path against the root that owns it."""
        try:
            return self.__resolve_folder_path(entry.folder.path) / entry.path
        except (AttributeError, DetachedInstanceError, ValueError):
            if self.engine is None:
                raise ValueError("Library is not open.") from None
            with Session(self.engine) as session:
                stored_root = session.scalar(
                    select(Folder.path).where(Folder.id == entry.folder_id)
                )
                if stored_root is None:
                    raise ValueError(f"Entry has no configured root: {entry.id}") from None
                return self.__resolve_folder_path(stored_root) / entry.path

    def sidecar_path(self, entry_id: int, format: SidecarFormat | str = SidecarFormat.JSON) -> Path:
        """Return the conventional sidecar path for one library entry."""
        entry = self.get_entry_full(entry_id, with_fields=False, with_tags=True)
        if entry is None:
            raise ValueError(f"Entry does not exist: {entry_id}")
        return sidecar_path(self.resolve_entry_path(entry), format)

    def export_sidecar(
        self,
        entry_id: int,
        format: SidecarFormat | str = SidecarFormat.JSON,
        *,
        destination: Path | None = None,
        overwrite: bool = True,
    ) -> Path:
        """Export an entry's tags to a JSON or XMP sidecar beside its file."""
        normalized_format = normalize_format(format)
        entry = self.get_entry_full(entry_id, with_fields=False, with_tags=True)
        if entry is None:
            raise ValueError(f"Entry does not exist: {entry_id}")

        file_path = self.resolve_entry_path(entry)
        target = Path(destination) if destination is not None else sidecar_path(file_path, format)
        if target.exists() and not overwrite:
            raise FileExistsError(target)

        tags = tuple(
            sorted((tag.name for tag in entry.tags), key=lambda name: (name.casefold(), name))
        )
        document = SidecarDocument(tags=tags, file=entry.path.as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        write_sidecar(target, document, normalized_format)
        return target

    def import_sidecar(
        self,
        entry_id: int,
        path: Path | None = None,
        *,
        format: SidecarFormat | str | None = None,
        replace_tags: bool = False,
    ) -> int:
        """Import tags from a sidecar, creating missing TagStudio tags as needed."""
        entry = self.get_entry_full(entry_id, with_fields=False, with_tags=True)
        if entry is None:
            raise ValueError(f"Entry does not exist: {entry_id}")

        normalized_format: SidecarFormat
        if path is None:
            if format is None:
                candidates = [
                    (
                        sidecar_path(self.resolve_entry_path(entry), SidecarFormat.JSON),
                        SidecarFormat.JSON,
                    ),
                    (
                        sidecar_path(self.resolve_entry_path(entry), SidecarFormat.XMP),
                        SidecarFormat.XMP,
                    ),
                ]
                path, normalized_format = next(
                    (
                        (candidate, candidate_format)
                        for candidate, candidate_format in candidates
                        if candidate.exists()
                    ),
                    (None, SidecarFormat.JSON),
                )
                if path is None:
                    raise FileNotFoundError(candidates[0][0])
            else:
                normalized_format = normalize_format(format)
                path = sidecar_path(self.resolve_entry_path(entry), normalized_format)
        else:
            path = Path(path)
            if format is None:
                if path.name.endswith(".tagstudio.json"):
                    normalized_format = SidecarFormat.JSON
                elif path.suffix.lower() == ".xmp":
                    normalized_format = SidecarFormat.XMP
                else:
                    raise SidecarError(f"Could not infer sidecar format from: {path}")
            else:
                normalized_format = normalize_format(format)

        document = read_sidecar(path, normalized_format)
        if replace_tags:
            current_tag_ids = [tag.id for tag in entry.tags]
            if current_tag_ids:
                self.remove_tags_from_entries(entry_id, current_tag_ids)

        tag_ids: list[int] = []
        for name in document.tags:
            tag = self.get_tag_by_name(name)
            if tag is None:
                tag = self.add_tag(Tag(name=name))
            if tag is not None:
                tag_ids.append(tag.id)

        if tag_ids:
            self.add_tags_to_entries(entry_id, tag_ids)
        return len(tag_ids)

    def get_entry_full_by_file_path(self, file_path: Path) -> Entry | None:
        """Load an entry by its absolute path across all configured roots."""
        if self.engine is None:
            return None

        absolute_path = self.__canonical_root(file_path)
        with Session(self.engine) as session:
            for folder in session.scalars(select(Folder).order_by(Folder.id)):
                root = self.__resolve_folder_path(folder.path)
                try:
                    relative_path = absolute_path.relative_to(root)
                except ValueError:
                    continue

                entry = session.scalar(
                    select(Entry)
                    .where(
                        and_(
                            Entry.folder_id == folder.id,
                            Entry.path == relative_path,
                        )
                    )
                    .options(joinedload(Entry.folder))
                )
                if entry is not None:
                    session.expunge(entry)
                    make_transient(entry)
                    return entry
        return None

    def open_sqlite_library(self, library_dir: Path, is_new: bool) -> LibraryStatus:
        connection_string = URL.create(
            drivername="sqlite",
            database=str(self.storage_path),
        )
        # NOTE: File-based databases should use NullPool to create new DB connection in order to
        # keep connections on separate threads, which prevents the DB files from being locked
        # even after a connection has been closed.
        # SingletonThreadPool (the default for :memory:) should still be used for in-memory DBs.
        # More info can be found on the SQLAlchemy docs:
        # https://docs.sqlalchemy.org/en/20/changelog/migration_07.html
        # Under -> sqlite-the-sqlite-dialect-now-uses-nullpool-for-file-based-databases
        poolclass = None if self.storage_path == ":memory:" else NullPool
        loaded_db_version: int = 0

        logger.info(
            "[Library] Opening SQLite Library",
            library_dir=library_dir,
            connection_string=connection_string,
        )
        self.engine = create_engine(connection_string, poolclass=poolclass)
        self.library_dir = Path(library_dir)
        with Session(self.engine, expire_on_commit=False) as session:
            # Don't check DB version when creating new library
            if not is_new:
                loaded_db_version = self.get_version(DB_VERSION_CURRENT_KEY)

                # ======================== Library Database Version Checking =======================
                # DB_VERSION 6 is the first supported SQLite DB version.
                # If the DB_VERSION is >= 100, that means it's a compound major + minor version.
                #   - Dividing by 100 and flooring gives the major (breaking changes) version.
                #   - If a DB has major version higher than the current program, don't load it.
                #   - If only the minor version is higher, it's still allowed to load.
                if loaded_db_version < 6 or (
                    loaded_db_version >= 100 and loaded_db_version // 100 > DB_VERSION // 100
                ):
                    mismatch_text = Translations["status.library_version_mismatch"]
                    found_text = Translations["status.library_version_found"]
                    expected_text = Translations["status.library_version_expected"]
                    return LibraryStatus(
                        success=False,
                        message=(
                            f"{mismatch_text}\n"
                            f"{found_text} v{loaded_db_version}, "
                            f"{expected_text} v{DB_VERSION}"
                        ),
                    )

            logger.info(f"[Library] DB_VERSION: {loaded_db_version}")
            make_tables(self.engine)

            # Add default tag color namespaces.
            if is_new:
                namespaces = default_color_groups.namespaces()
                try:
                    session.add_all(namespaces)
                    session.commit()
                except IntegrityError as e:
                    logger.error("[Library] Couldn't add default tag color namespaces", error=e)
                    session.rollback()

            # Add default tag colors.
            if is_new:
                tag_colors: list[TagColorGroup] = default_color_groups.standard()
                tag_colors += default_color_groups.pastels()
                tag_colors += default_color_groups.shades()
                tag_colors += default_color_groups.grayscale()
                tag_colors += default_color_groups.earth_tones()
                tag_colors += default_color_groups.neon()
                if is_new:
                    try:
                        session.add_all(tag_colors)
                        session.commit()
                    except IntegrityError as e:
                        logger.error("[Library] Couldn't add default tag colors", error=e)
                        session.rollback()

            # Add default tags.
            if is_new:
                tags = get_default_tags()
                try:
                    session.add_all(tags)
                    session.commit()
                except IntegrityError:
                    session.rollback()

            # Ensure version rows are present
            with catch_warnings(record=True):
                # NOTE: The "Preferences" table is depreciated and will be removed in the future.
                # The DB_VERSION is still being set to it in order to remain backwards-compatible
                # with existing TagStudio versions until it is removed.
                try:
                    session.add(Preferences(key=DB_VERSION_LEGACY_KEY, value=DB_VERSION))
                    session.commit()
                except IntegrityError:
                    session.rollback()

                try:
                    initial = DB_VERSION if is_new else 100
                    session.add(Version(key=DB_VERSION_INITIAL_KEY, value=initial))
                    session.commit()
                except IntegrityError:
                    session.rollback()

                try:
                    session.add(Version(key=DB_VERSION_CURRENT_KEY, value=DB_VERSION))
                    session.commit()
                except IntegrityError:
                    session.rollback()

            # TODO: Remove this "Preferences" system.
            for pref in LibraryPrefs:
                with catch_warnings(record=True):
                    try:
                        session.add(Preferences(key=pref.name, value=pref.default))
                        session.commit()
                    except IntegrityError:
                        session.rollback()

            for field in FieldID:
                try:
                    session.add(
                        ValueType(
                            key=field.name,
                            name=field.value.name,
                            type=field.value.type,
                            position=field.value.id,
                            is_default=field.value.is_default,
                        )
                    )
                    session.commit()
                except IntegrityError:
                    logger.debug("ValueType already exists", field=field)
                    session.rollback()

            # Generate default .ts_ignore file
            if is_new:
                try:
                    ts_ignore_template = (
                        Path(__file__).parents[3] / "resources/templates/ts_ignore_template.txt"
                    )
                    shutil.copy2(ts_ignore_template, library_dir / TS_FOLDER_NAME / IGNORE_NAME)
                except Exception as e:
                    logger.error("[ERROR][Library] Could not generate '.ts_ignore' file!", error=e)

            # Apply any post-SQL migration patches.
            if is_new:
                MigrationRunner(session, self.__migration_steps()).seed(DB_VERSION)
            else:
                # save backup if patches will be applied
                if loaded_db_version < DB_VERSION:
                    self.library_dir = library_dir
                    self.save_library_backup_to_disk()

                self.__prepare_legacy_schema(session, loaded_db_version)
                MigrationRunner(session, self.__migration_steps()).upgrade(
                    loaded_db_version, DB_VERSION
                )

                # Convert file extension list to ts_ignore file, if a .ts_ignore file does not exist
                self.migrate_sql_to_ts_ignore(library_dir)

            # The first folder is the metadata/primary root. Additional roots are added with
            # ``add_root`` and share this library database.
            self.folder = self.__get_or_create_folder(session, library_dir)
            session.commit()

            # Update DB_VERSION
            if loaded_db_version < DB_VERSION:
                self.set_version(DB_VERSION_CURRENT_KEY, DB_VERSION)

        # everything is fine, set the library path
        self.library_dir = library_dir
        if self.folder is not None:
            self.folder.path = self.__resolve_folder_path(self.folder.path)
        return LibraryStatus(success=True, library_path=library_dir)

    def __migration_steps(self) -> tuple[Migration, ...]:
        """Return the ordered migration chain for the current library format."""
        return (
            Migration(7, "repair DB_VERSION 6 data", self.__apply_repairs_for_db6),
            Migration(8, "add DB_VERSION 8 schema and defaults", self.__apply_db8_migration),
            Migration(9, "add DB_VERSION 9 filename data", self.__apply_db9_migration),
            Migration(
                100,
                "repair DB_VERSION 100 parent relationships",
                self.__apply_db100_parent_repairs,
            ),
            Migration(101, "create the schema version ledger", lambda _session: None),
            Migration(102, "repair DB_VERSION 102 parent references", self.__apply_db102_repairs),
            Migration(103, "add DB_VERSION 103 hidden tags", self.__apply_db103_migration),
            Migration(
                104,
                "allow duplicate relative paths across roots",
                self.__apply_db104_migration,
            ),
            Migration(105, "add per-folder override rules", lambda _session: None),
            Migration(
                106,
                "store configured roots relative to the library",
                self.__apply_db106_migration,
            ),
            Migration(107, "add saved searches", lambda _session: None),
        )

    def __prepare_legacy_schema(self, session: Session, loaded_db_version: int) -> None:
        """Add columns needed by ORM-backed data repairs before they run."""
        if loaded_db_version < 8:
            self.__apply_db8_schema_changes(session)
        if loaded_db_version < 9:
            self.__apply_db9_schema_changes(session)
        if loaded_db_version < 103:
            self.__apply_db103_schema_changes(session)

    def __apply_db8_migration(self, session: Session) -> None:
        self.__apply_db8_schema_changes(session)
        self.__apply_db8_default_data(session)

    def __apply_db9_migration(self, session: Session) -> None:
        self.__apply_db9_schema_changes(session)
        self.__apply_db9_filename_population(session)

    def __apply_db103_migration(self, session: Session) -> None:
        self.__apply_db103_schema_changes(session)
        self.__apply_db103_default_data(session)

    def __apply_db104_migration(self, session: Session) -> None:
        """Replace the legacy global entry-path constraint with a per-root constraint."""
        indexes = session.execute(text("PRAGMA index_list('entries')")).all()
        unique_columns: list[list[str]] = []
        for index in indexes:
            if not index[2]:
                continue
            index_name = str(index[1]).replace("'", "''")
            columns = session.execute(text(f"PRAGMA index_info('{index_name}')")).all()
            unique_columns.append([str(column[2]) for column in columns])

        if ["folder_id", "path"] in unique_columns:
            return

        if ["path"] not in unique_columns:
            raise RuntimeError("entries table has no supported path uniqueness constraint")

        session.execute(text("PRAGMA foreign_keys=OFF"))
        try:
            session.execute(
                text(
                    """
                    CREATE TABLE entries_new (
                        id INTEGER NOT NULL,
                        folder_id INTEGER NOT NULL,
                        path VARCHAR NOT NULL,
                        filename VARCHAR NOT NULL,
                        suffix VARCHAR NOT NULL,
                        date_created DATETIME,
                        date_modified DATETIME,
                        date_added DATETIME,
                        PRIMARY KEY (id),
                        FOREIGN KEY(folder_id) REFERENCES folders (id),
                        CONSTRAINT uq_entries_folder_path UNIQUE (folder_id, path)
                    )
                    """
                )
            )
            session.execute(
                text(
                    """
                    INSERT INTO entries_new
                        (
                            id, folder_id, path, filename, suffix, date_created,
                            date_modified, date_added
                        )
                    SELECT
                        id, folder_id, path, filename, suffix, date_created,
                        date_modified, date_added
                    FROM entries
                    """
                )
            )
            session.execute(text("DROP TABLE entries"))
            session.execute(text("ALTER TABLE entries_new RENAME TO entries"))
        finally:
            session.execute(text("PRAGMA foreign_keys=ON"))

    def __apply_db106_migration(self, session: Session) -> None:
        """Convert legacy absolute folder roots to paths relative to the library."""
        if self.library_dir is None:
            raise RuntimeError("A library directory is required for root path migration")

        folders = list(session.scalars(select(Folder).order_by(Folder.id)))
        if not folders:
            return

        old_primary_root = self.__resolve_folder_path(folders[0].path)
        for folder in folders:
            stored_path = Path(folder.path)
            if not stored_path.is_absolute():
                continue

            try:
                relative_path = Path(relpath(stored_path, old_primary_root))
            except ValueError:
                # A root on another volume cannot be represented by a relative path. Preserve
                # it so the library remains usable; newly added roots use the same fallback.
                logger.warning(
                    "[Library][Migration] Retaining cross-volume root as absolute path",
                    path=stored_path,
                )
                continue

            folder.path = relative_path if relative_path.parts else Path(".")

    def __apply_repairs_for_db6(self, session: Session):
        """Apply database repairs introduced in DB_VERSION 7."""
        logger.info("[Library][Migration] Applying patches to DB_VERSION: 6 library...")
        with session:
            # Repair "Description" fields with a TEXT_LINE key instead of a TEXT_BOX key.
            desc_stmt = (
                update(ValueType)
                .where(ValueType.key == FieldID.DESCRIPTION.name)
                .values(type=FieldTypeEnum.TEXT_BOX.name)
            )
            session.execute(desc_stmt)
            session.flush()

            # Repair tags that may have a disambiguation_id pointing towards a deleted tag.
            all_tag_ids: set[int] = {tag.id for tag in self.tags}
            disam_stmt = (
                update(Tag)
                .where(Tag.disambiguation_id.not_in(all_tag_ids))
                .values(disambiguation_id=None)
            )
            session.execute(disam_stmt)
            session.commit()

    def __apply_db8_schema_changes(self, session: Session):
        """Apply database schema changes introduced in DB_VERSION 8."""
        # TODO: Use Alembic for this part instead
        # Add the missing color_border column to the TagColorGroups table.
        color_border_stmt = text(
            "ALTER TABLE tag_colors ADD COLUMN color_border BOOLEAN DEFAULT FALSE NOT NULL"
        )
        try:
            session.execute(color_border_stmt)
            session.commit()
            logger.info("[Library][Migration] Added color_border column to tag_colors table")
        except Exception as e:
            logger.error(
                "[Library][Migration] Could not create color_border column in tag_colors table!",
                error=e,
            )
            session.rollback()

    def __apply_db8_default_data(self, session: Session):
        """Apply default data changes introduced in DB_VERSION 8."""
        tag_colors: list[TagColorGroup] = default_color_groups.standard()
        tag_colors += default_color_groups.pastels()
        tag_colors += default_color_groups.shades()
        tag_colors += default_color_groups.grayscale()
        tag_colors += default_color_groups.earth_tones()
        # tag_colors += default_color_groups.neon() # NOTE: Neon is handled separately

        # Add any new default colors introduced in DB_VERSION 8
        for color in tag_colors:
            try:
                session.add(color)
                logger.info(
                    "[Library][Migration] Migrated tag color to DB_VERSION 8+",
                    color_name=color.name,
                )
                session.commit()
            except IntegrityError:
                session.rollback()

        # Update Neon colors to use the the color_border property
        for color in default_color_groups.neon():
            try:
                neon_stmt = (
                    update(TagColorGroup)
                    .where(
                        and_(
                            TagColorGroup.namespace == color.namespace,
                            TagColorGroup.slug == color.slug,
                        )
                    )
                    .values(
                        slug=color.slug,
                        namespace=color.namespace,
                        name=color.name,
                        primary=color.primary,
                        secondary=color.secondary,
                        color_border=color.color_border,
                    )
                )
                session.execute(neon_stmt)
                session.commit()
            except IntegrityError as e:
                logger.error(
                    "[Library] Could not migrate Neon colors to DB_VERSION 8+!",
                    error=e,
                )
                session.rollback()

    def __apply_db9_schema_changes(self, session: Session):
        """Apply database schema changes introduced in DB_VERSION 9."""
        add_filename_column = text(
            "ALTER TABLE entries ADD COLUMN filename TEXT NOT NULL DEFAULT ''"
        )
        try:
            session.execute(add_filename_column)
            session.commit()
            logger.info("[Library][Migration] Added filename column to entries table")
        except Exception as e:
            logger.error(
                "[Library][Migration] Could not create filename column in entries table!",
                error=e,
            )
            session.rollback()

    def __apply_db9_filename_population(self, session: Session):
        """Populate the filename column introduced in DB_VERSION 9."""
        for entry in self.all_entries():
            session.merge(entry).filename = entry.path.name
        session.commit()
        logger.info("[Library][Migration] Populated filename column in entries table")

    def __apply_db100_parent_repairs(self, session: Session):
        """Swap the child_id and parent_id values in the TagParent table."""
        with session:
            # Repair parent-child tag relationships that are the wrong way around.
            stmt = update(TagParent).values(
                parent_id=TagParent.child_id,
                child_id=TagParent.parent_id,
            )
            session.execute(stmt)
            session.commit()
            logger.info("[Library][Migration] Refactored TagParent table")

    def __apply_db102_repairs(self, session: Session):
        """Repair tag_parents rows with references to deleted tags."""
        with session:
            all_tag_ids: list[int] = [t.id for t in self.tags]
            stmt = delete(TagParent).where(TagParent.parent_id.not_in(all_tag_ids))
            session.execute(stmt)
            session.commit()
            logger.info("[Library][Migration] Verified TagParent table data")

    def __apply_db103_schema_changes(self, session: Session):
        """Apply database schema changes introduced in DB_VERSION 103."""
        add_is_hidden_column = text(
            "ALTER TABLE tags ADD COLUMN is_hidden BOOLEAN NOT NULL DEFAULT 0"
        )
        try:
            session.execute(add_is_hidden_column)
            session.commit()
            logger.info("[Library][Migration] Added is_hidden column to tags table")
        except Exception as e:
            logger.error(
                "[Library][Migration] Could not create is_hidden column in tags table!",
                error=e,
            )
            session.rollback()

    def __apply_db103_default_data(self, session: Session):
        """Apply default data changes introduced in DB_VERSION 103."""
        try:
            session.query(Tag).filter(Tag.id == TAG_ARCHIVED).update({"is_hidden": True})
            session.commit()
            logger.info("[Library][Migration] Updated archived tag to be hidden")
            session.commit()
        except Exception as e:
            logger.error(
                "[Library][Migration] Could not update archived tag to be hidden!",
                error=e,
            )
            session.rollback()

    def migrate_sql_to_ts_ignore(self, library_dir: Path):
        # Do not continue if existing '.ts_ignore' file is found
        if Path(library_dir / TS_FOLDER_NAME / IGNORE_NAME).exists():
            return

        # Create blank '.ts_ignore' file
        ts_ignore_template = (
            Path(__file__).parents[3] / "resources/templates/ts_ignore_template_blank.txt"
        )
        ts_ignore = library_dir / TS_FOLDER_NAME / IGNORE_NAME
        try:
            shutil.copy2(ts_ignore_template, ts_ignore)
        except Exception as e:
            logger.error("[ERROR][Library] Could not generate '.ts_ignore' file!", error=e)

        # Load legacy extension data
        extensions: list[str] = self.prefs(LibraryPrefs.EXTENSION_LIST)  # pyright: ignore
        is_exclude_list: bool = self.prefs(LibraryPrefs.IS_EXCLUDE_LIST)  # pyright: ignore

        # Copy extensions to '.ts_ignore' file
        if ts_ignore.exists():
            with open(ts_ignore, "a") as f:
                prefix = ""
                if not is_exclude_list:
                    prefix = "!"
                    f.write("*\n")
                f.writelines([f"{prefix}*.{x.lstrip('.')}\n" for x in extensions])

    @property
    def default_fields(self) -> list[BaseField]:
        with Session(self.engine) as session:
            types = session.scalars(
                select(ValueType).where(
                    # check if field is default
                    ValueType.is_default.is_(True)
                )
            )
            return [x.as_field for x in types]

    def get_entry(self, entry_id: int) -> Entry | None:
        """Load entry without joins."""
        with Session(self.engine) as session:
            entry = session.scalar(select(Entry).where(Entry.id == entry_id))
            if not entry:
                return None
            session.expunge(entry)
            make_transient(entry)
            return entry

    def get_entry_full(
        self, entry_id: int, with_fields: bool = True, with_tags: bool = True
    ) -> Entry | None:
        """Load entry and join with all joins and all tags."""
        # NOTE: TODO: Currently this method makes multiple separate queries to the db and combines
        # those into a final Entry object (if using "with" args). This was done due to it being
        # much more efficient than the existing join query, however there likely exists a single
        # query that can accomplish the same task without exhibiting the same slowdown.
        with Session(self.engine) as session:
            tags: set[Tag] | None = None
            tag_stmt: Select[tuple[Tag]]
            entry_stmt = select(Entry).where(Entry.id == entry_id).limit(1)
            if with_fields:
                entry_stmt = (
                    entry_stmt.outerjoin(Entry.text_fields)
                    .outerjoin(Entry.datetime_fields)
                    .options(
                        selectinload(Entry.text_fields),
                        selectinload(Entry.datetime_fields),
                    )
                )
            # if with_tags:
            #     entry_stmt = entry_stmt.outerjoin(Entry.tags).options(selectinload(Entry.tags))
            if with_tags:
                tag_stmt = select(Tag).where(
                    and_(
                        TagEntry.tag_id == Tag.id,
                        TagEntry.entry_id == entry_id,
                    )
                )

            start_time = time.time()
            entry = session.scalar(entry_stmt)
            if with_tags:
                tags = set(session.scalars(tag_stmt))  # pyright: ignore[reportPossiblyUnboundVariable]
            end_time = time.time()
            logger.info(
                f"[Library] Time it took to get entry: "
                f"{format_timespan(end_time - start_time, max_units=5)}",
                with_fields=with_fields,
                with_tags=with_tags,
            )
            if not entry:
                return None
            session.expunge(entry)
            make_transient(entry)

            # Recombine the separately queried tags with the base entry object.
            if with_tags and tags:
                entry.tags = tags
            return entry

    def get_entries(self, entry_ids: Iterable[int]) -> list[Entry]:
        with Session(self.engine) as session:
            statement = select(Entry).where(Entry.id.in_(entry_ids))
            entries = dict((e.id, e) for e in session.scalars(statement))
            return [entries[id] for id in entry_ids]

    def get_entries_full(self, entry_ids: list[int] | set[int]) -> Iterator[Entry]:
        """Load entry and join with all joins and all tags."""
        with Session(self.engine) as session:
            statement = select(Entry).where(Entry.id.in_(set(entry_ids)))
            statement = (
                statement.outerjoin(Entry.text_fields)
                .outerjoin(Entry.datetime_fields)
                .outerjoin(Entry.tags)
            )
            statement = statement.options(
                selectinload(Entry.text_fields),
                selectinload(Entry.datetime_fields),
                selectinload(Entry.tags).options(
                    selectinload(Tag.aliases),
                    selectinload(Tag.parent_tags),
                ),
            )
            statement = statement.distinct()
            entries: ScalarResult[Entry] | list[Entry] = session.execute(statement).scalars()
            entries = entries.unique()  # type: ignore

            entry_order_dict = {e_id: order for order, e_id in enumerate(entry_ids)}
            entries = sorted(entries, key=lambda e: entry_order_dict[e.id])

            for entry in entries:
                yield entry
                session.expunge(entry)

    def get_entry_full_by_path(
        self, path: Path, folder: Folder | Path | int | None = None
    ) -> Entry | None:
        """Get the entry with the corresponding path in a configured root.

        When no root is supplied, the primary library root is used for compatibility with the
        original single-root API.
        """
        with Session(self.engine) as session:
            folder_id = self.__folder_id(session, folder)
            stmt = select(Entry).where(and_(Entry.folder_id == folder_id, Entry.path == path))
            stmt = (
                stmt.outerjoin(Entry.text_fields)
                .outerjoin(Entry.datetime_fields)
                .options(selectinload(Entry.text_fields), selectinload(Entry.datetime_fields))
            )
            stmt = (
                stmt.outerjoin(Entry.tags)
                .outerjoin(TagAlias)
                .options(
                    selectinload(Entry.tags).options(
                        joinedload(Tag.aliases),
                        joinedload(Tag.parent_tags),
                    )
                )
            )
            entry = session.scalar(stmt)
            if not entry:
                return None
            session.expunge(entry)
            make_transient(entry)
            return entry

    def get_tag_entries(
        self, tag_ids: Iterable[int], entry_ids: Iterable[int]
    ) -> dict[int, set[int]]:
        """Returns a dict of tag_id->(entry_ids with tag_id)."""
        tag_entries: dict[int, set[int]] = dict((id, set()) for id in tag_ids)
        with Session(self.engine) as session:
            statement = select(TagEntry).where(
                and_(TagEntry.tag_id.in_(tag_ids), TagEntry.entry_id.in_(entry_ids))
            )
            for tag_entry in session.scalars(statement).fetchall():
                tag_entries[tag_entry.tag_id].add(tag_entry.entry_id)
        return tag_entries

    @property
    def entries_count(self) -> int:
        with Session(self.engine) as session:
            return unwrap(session.scalar(select(func.count(Entry.id))))

    def all_entries(self, with_joins: bool = False) -> Iterator[Entry]:
        """Load entries without joins."""
        with Session(self.engine) as session:
            stmt = select(Entry)
            if with_joins:
                # load Entry with all joins and all tags
                stmt = (
                    stmt.outerjoin(Entry.text_fields)
                    .outerjoin(Entry.datetime_fields)
                    .outerjoin(Entry.tags)
                )
                stmt = stmt.options(
                    contains_eager(Entry.text_fields),
                    contains_eager(Entry.datetime_fields),
                    contains_eager(Entry.tags),
                )

            stmt = stmt.distinct()

            entries = session.execute(stmt).scalars()
            if with_joins:
                entries = entries.unique()

            for entry in entries:
                yield entry
                session.expunge(entry)

    @property
    def tags(self) -> list[Tag]:
        with Session(self.engine) as session:
            # load all tags and join parent tags
            tags_query = select(Tag).options(selectinload(Tag.parent_tags))
            tags = session.scalars(tags_query).unique()
            tags_list = list(tags)

            for tag in tags_list:
                session.expunge(tag)

        return list(tags_list)

    def verify_ts_folder(self, library_dir: Path | None) -> bool:
        """Verify/create folders required by TagStudio.

        Returns:
            bool: True if path exists, False if it needed to be created.
        """
        if library_dir is None:
            raise ValueError("No path set.")

        if not library_dir.exists():
            raise ValueError("Invalid library directory.")

        full_ts_path = library_dir / TS_FOLDER_NAME
        if not full_ts_path.exists():
            logger.info("creating library directory", dir=full_ts_path)
            full_ts_path.mkdir(parents=True, exist_ok=True)
            return False
        return True

    def add_entries(self, items: list[Entry]) -> list[int]:
        """Add multiple Entry records to the Library."""
        assert items

        folder_paths_by_identity: dict[int, tuple[Folder, Path]] = {}
        for item in items:
            if item.folder is None:
                continue
            folder = item.folder
            if id(folder) in folder_paths_by_identity:
                continue
            original_path = Path(folder.path)
            folder.path = self.__relative_folder_path(self.__resolve_folder_path(original_path))
            folder_paths_by_identity[id(folder)] = (folder, original_path)

        with Session(self.engine) as session:
            # add all items

            try:
                session.add_all(items)
                session.commit()
            except IntegrityError:
                session.rollback()
                logger.error("IntegrityError")
                new_ids = []
            else:
                new_ids = [item.id for item in items]
            finally:
                session.expunge_all()

        for folder, original_path in folder_paths_by_identity.values():
            folder.path = original_path

        return new_ids

    def remove_entries(self, entry_ids: list[int]) -> None:
        """Remove Entry items matching supplied IDs from the Library."""
        with Session(self.engine) as session:
            for sub_list in [
                entry_ids[i : i + MAX_SQL_VARIABLES]
                for i in range(0, len(entry_ids), MAX_SQL_VARIABLES)
            ]:
                session.query(Entry).where(Entry.id.in_(sub_list)).delete()
            session.commit()

    def has_path_entry(self, path: Path, folder: Folder | Path | int | None = None) -> bool:
        """Check if an item with the given path exists in a configured root."""
        with Session(self.engine) as session:
            folder_id = self.__folder_id(session, folder)
            return session.query(
                exists().where(and_(Entry.folder_id == folder_id, Entry.path == path))
            ).scalar()

    def get_paths(self, limit: int = -1) -> list[str]:
        path_strings: list[str] = []
        with Session(self.engine) as session:
            if limit > 0:
                paths = session.scalars(select(Entry.path).limit(limit)).unique()
            else:
                paths = session.scalars(select(Entry.path)).unique()
            path_strings = list(map(lambda x: x.as_posix(), paths))
            return path_strings

    def search_library(
        self,
        search: BrowsingState,
        page_size: int | None,
    ) -> SearchResult:
        """Filter library by search query.

        :return: number of entries matching the query and one page of results.
        """
        assert isinstance(search, BrowsingState)
        assert self.library_dir

        with Session(unwrap(self.engine), expire_on_commit=False) as session:
            if page_size:
                statement = (
                    select(Entry.id, func.count().over())
                    .offset(search.page_index * page_size)
                    .limit(page_size)
                )
            else:
                statement = select(Entry.id)

            ast = search.ast

            if not search.show_hidden_entries:
                statement = statement.where(~Entry.tags.any(Tag.is_hidden))

            if ast:
                start_time = time.time()
                statement = statement.where(SQLBoolExpressionBuilder(self).visit(ast))
                end_time = time.time()
                logger.info(
                    f"SQL Expression Builder finished ({format_timespan(end_time - start_time)})"
                )

            statement = statement.distinct(Entry.id)

            sort_on: ColumnExpressionArgument = Entry.id
            match search.sorting_mode:
                case SortingModeEnum.DATE_ADDED:
                    sort_on = Entry.id
                case SortingModeEnum.FILE_NAME:
                    sort_on = func.lower(Entry.filename)
                case SortingModeEnum.PATH:
                    sort_on = func.lower(Entry.path)
                case SortingModeEnum.RANDOM:
                    sort_on = func.sin(Entry.id * search.random_seed)

            statement = statement.order_by(asc(sort_on) if search.ascending else desc(sort_on))

            logger.info(
                "searching library",
                filter=search,
                query_full=str(statement.compile(compile_kwargs={"literal_binds": True})),
            )

            start_time = time.time()
            if page_size:
                rows = session.execute(statement).fetchall()
                ids = []
                total_count = 0
                for row in rows:
                    ids.append(row[0])
                    total_count = row[1]
            else:
                ids = list(session.scalars(statement))
                total_count = len(ids)
            end_time = time.time()
            logger.info(f"SQL Execution finished ({format_timespan(end_time - start_time)})")

            res = SearchResult(
                total_count=total_count,
                ids=ids,
            )

            session.expunge_all()

            return res

    def search_tags(self, name: str | None, limit: int = 100) -> list[set[Tag]]:
        """Return a list of Tag records matching the query."""
        with Session(self.engine) as session:
            query = select(Tag).outerjoin(TagAlias).order_by(func.lower(Tag.name))
            query = query.options(
                selectinload(Tag.parent_tags),
                selectinload(Tag.aliases),
            )
            if limit > 0:
                query = query.limit(limit)

            if name:
                query = query.where(
                    or_(
                        Tag.name.icontains(name),
                        Tag.shorthand.icontains(name),
                        TagAlias.name.icontains(name),
                    )
                )

            direct_tags = set(session.scalars(query))
            ancestor_tag_ids: list[Tag] = []
            for tag in direct_tags:
                ancestor_tag_ids.extend(
                    list(session.scalars(TAG_CHILDREN_QUERY, {"tag_id": tag.id}))
                )

            ancestor_tags = session.scalars(
                select(Tag)
                .where(Tag.id.in_(ancestor_tag_ids))
                .options(selectinload(Tag.parent_tags), selectinload(Tag.aliases))
            )

            res = [
                direct_tags,
                {at for at in ancestor_tags if at not in direct_tags},
            ]

            logger.info(
                "searching tags",
                search=name,
                limit=limit,
                statement=str(query),
                results=len(res),
            )

            session.expunge_all()

            return res

    def update_entry_path(
        self,
        entry_id: int | Entry,
        path: Path,
        folder: Folder | Path | int | None = None,
    ) -> bool:
        """Set the path field of an entry.

        Returns True if the action succeeded and False if the path already exists.
        """
        entry_folder_id: int | None = None
        if isinstance(entry_id, Entry):
            entry_folder_id = entry_id.folder_id
            entry_id = entry_id.id

        with Session(self.engine) as session:
            if folder is not None:
                entry_folder_id = self.__folder_id(session, folder)
            elif entry_folder_id is None:
                entry_folder_id = session.scalar(
                    select(Entry.folder_id).where(Entry.id == entry_id)
                )
            if entry_folder_id is None:
                return False

            if session.scalar(
                select(
                    exists().where(
                        and_(
                            Entry.folder_id == entry_folder_id,
                            Entry.path == path,
                            Entry.id != entry_id,
                        )
                    )
                )
            ):
                return False

            update_stmt = (
                update(Entry)
                .where(
                    and_(
                        Entry.id == entry_id,
                        Entry.folder_id == entry_folder_id,
                    )
                )
                .values(path=path)
            )

            session.execute(update_stmt)
            session.commit()
        return True

    def remove_tag(self, tag_id: int) -> bool:
        with Session(self.engine, expire_on_commit=False) as session:
            try:
                session.execute(delete(TagAlias).where(TagAlias.tag_id == tag_id))
                session.execute(delete(TagEntry).where(TagEntry.tag_id == tag_id))
                session.execute(
                    delete(TagParent).where(
                        or_(TagParent.child_id == tag_id, TagParent.parent_id == tag_id)
                    )
                )
                session.execute(
                    update(Tag)
                    .where(Tag.disambiguation_id == tag_id)
                    .values(disambiguation_id=None)
                )
                session.execute(delete(Tag).where(Tag.id == tag_id))
                session.commit()

            except IntegrityError as e:
                logger.error(e)
                session.rollback()
                return False
        return True

    def update_field_position(
        self,
        field_class: type[BaseField],
        field_type: str,
        entry_ids: list[int] | int,
    ):
        if isinstance(entry_ids, int):
            entry_ids = [entry_ids]

        with Session(self.engine) as session:
            for entry_id in entry_ids:
                rows = list(
                    session.scalars(
                        select(field_class)
                        .where(
                            and_(
                                field_class.entry_id == entry_id,
                                field_class.type_key == field_type,
                            )
                        )
                        .order_by(field_class.id)
                    )
                )

                # Reassign `order` starting from 0
                for index, row in enumerate(rows):
                    row.position = index
                    session.add(row)
                    session.flush()
                if rows:
                    session.commit()

    def remove_entry_field(
        self,
        field: BaseField,
        entry_ids: list[int],
    ) -> None:
        FieldClass = type(field)  # noqa: N806

        logger.info(
            "remove_entry_field",
            field=field,
            entry_ids=entry_ids,
            field_type=field.type,
            cls=FieldClass,
            pos=field.position,
        )

        with Session(self.engine) as session:
            # remove all fields matching entry and field_type
            delete_stmt = delete(FieldClass).where(
                and_(
                    FieldClass.position == field.position,
                    FieldClass.type_key == field.type_key,
                    FieldClass.entry_id.in_(entry_ids),
                )
            )

            session.execute(delete_stmt)

            session.commit()

        # recalculate the remaining positions
        # self.update_field_position(type(field), field.type, entry_ids)

    def update_entry_field(
        self,
        entry_ids: list[int] | int,
        field: BaseField,
        content: str | datetime,
    ):
        if isinstance(entry_ids, int):
            entry_ids = [entry_ids]

        FieldClass = type(field)  # noqa: N806

        with Session(self.engine) as session:
            update_stmt = (
                update(FieldClass)
                .where(
                    and_(
                        FieldClass.position == field.position,
                        FieldClass.type == field.type,
                        FieldClass.entry_id.in_(entry_ids),
                    )
                )
                .values(value=content)
            )

            session.execute(update_stmt)
            session.commit()

    @property
    def field_types(self) -> dict[str, ValueType]:
        with Session(self.engine) as session:
            return {x.key: x for x in session.scalars(select(ValueType)).all()}

    def get_value_type(self, field_key: str) -> ValueType:
        with Session(self.engine) as session:
            field = unwrap(session.scalar(select(ValueType).where(ValueType.key == field_key)))
            session.expunge(field)
            return field

    def add_field_to_entry(
        self,
        entry_id: int,
        *,
        field: ValueType | None = None,
        field_id: FieldID | str | None = None,
        value: str | datetime | None = None,
    ) -> bool:
        logger.info(
            "[Library][add_field_to_entry]",
            entry_id=entry_id,
            field_type=field,
            field_id=field_id,
            value=value,
        )
        # supply only instance or ID, not both
        assert bool(field) != (field_id is not None)

        if not field:
            if isinstance(field_id, FieldID):
                field_id = field_id.name
            field = self.get_value_type(unwrap(field_id))

        field_model: TextField | DatetimeField
        if field.type in (FieldTypeEnum.TEXT_LINE, FieldTypeEnum.TEXT_BOX):
            field_model = TextField(
                type_key=field.key,
                value=value or "",
            )

        elif field.type == FieldTypeEnum.DATETIME:
            field_model = DatetimeField(
                type_key=field.key,
                value=value,
            )
        else:
            raise NotImplementedError(f"field type not implemented: {field.type}")

        with Session(self.engine) as session:
            try:
                field_model.entry_id = entry_id
                session.add(field_model)
                session.flush()
                session.commit()
            except IntegrityError as e:
                logger.error(e)
                session.rollback()
                return False
                # TODO - trigger error signal

        # recalculate the positions of fields
        self.update_field_position(
            field_class=type(field_model),
            field_type=field.key,
            entry_ids=entry_id,
        )
        return True

    def upsert_text_field(
        self, entry_id: int, field_id: FieldID | str, value: str
    ) -> bool:
        """Create or update the first text field value for an entry.

        Returns whether the stored value changed. This is used by opt-in metadata workers
        so repeated runs are idempotent instead of appending duplicate fields.
        """
        field_key = field_id.name if isinstance(field_id, FieldID) else field_id
        with Session(self.engine, expire_on_commit=False) as session:
            field = session.scalar(
                select(TextField)
                .where(TextField.entry_id == entry_id, TextField.type_key == field_key)
                .order_by(TextField.position, TextField.id)
            )
            if field is None:
                field = TextField(entry_id=entry_id, type_key=field_key, value=value)
                session.add(field)
                changed = True
            else:
                changed = field.value != value
                field.value = value

            session.commit()

        if field is not None:
            self.update_field_position(TextField, field_key, entry_id)
        return changed

    def tag_from_strings(self, strings: list[str] | str) -> list[int]:
        """Create a Tag from a given string."""
        # TODO: Port over tag searching with aliases fallbacks
        # and context clue ranking for string searches.
        tags: list[int] = []

        if isinstance(strings, str):
            strings = [strings]

        with Session(self.engine) as session:
            for string in strings:
                tag = session.scalar(select(Tag).where(Tag.name == string))
                if tag:
                    tags.append(tag.id)
                else:
                    new = session.add(Tag(name=string))  # type: ignore
                    if new:
                        tags.append(new.id)
                        session.flush()
            session.commit()
        return tags

    def add_namespace(self, namespace: Namespace) -> bool:
        """Add a namespace value to the library.

        Args:
            namespace(str): The namespace slug. No special characters
        """
        with Session(self.engine) as session:
            if not namespace.namespace:
                logger.warning("[LIBRARY][add_namespace] Namespace slug must not be empty")
                return False

            slug = namespace.namespace
            try:
                slug = slugify(namespace.namespace)
            except ReservedNamespaceError:
                logger.error(
                    f"[LIBRARY][add_namespace] Will not add a namespace with the reserved prefix:"
                    f"{RESERVED_NAMESPACE_PREFIX}",
                    namespace=namespace,
                )

            namespace_obj = Namespace(
                namespace=slug,
                name=namespace.name,
            )

            try:
                session.add(namespace_obj)
                session.commit()
                return True
            except IntegrityError:
                session.rollback()
                logger.error("IntegrityError")
                return False

    def delete_namespace(self, namespace: Namespace | str):
        """Delete a namespace and any connected data from the library."""
        if isinstance(namespace, str):
            if namespace.startswith(RESERVED_NAMESPACE_PREFIX):
                raise ReservedNamespaceError
        else:
            if namespace.namespace.startswith(RESERVED_NAMESPACE_PREFIX):
                raise ReservedNamespaceError

        with Session(self.engine, expire_on_commit=False) as session:
            try:
                namespace_: Namespace | None = None
                if isinstance(namespace, str):
                    namespace_ = session.scalar(
                        select(Namespace).where(Namespace.namespace == namespace)
                    )
                else:
                    namespace_ = namespace

                if not namespace_:
                    raise Exception
                session.delete(namespace_)
                session.flush()

                colors = session.scalars(
                    select(TagColorGroup).where(TagColorGroup.namespace == namespace_.namespace)
                )
                for color in colors:
                    session.delete(color)
                    session.flush()

                session.commit()

            except IntegrityError as e:
                logger.error(e)
                session.rollback()
                return None

    def add_tag(
        self,
        tag: Tag,
        parent_ids: list[int] | set[int] | None = None,
        alias_names: list[str] | set[str] | None = None,
        alias_ids: list[int] | set[int] | None = None,
    ) -> Tag | None:
        with Session(self.engine, expire_on_commit=False) as session:
            try:
                session.add(tag)
                session.flush()

                if parent_ids is not None:
                    self.update_parent_tags(tag, parent_ids, session)

                if alias_ids is not None and alias_names is not None:
                    self.update_aliases(tag, alias_ids, alias_names, session)

                session.commit()
                session.expunge(tag)
                return tag

            except IntegrityError as e:
                logger.error(e)
                session.rollback()
                return None

    def add_tags_to_entries(
        self,
        entry_ids: int | Iterable[int],
        tag_ids: int | Iterable[int],
        *,
        include_parents: bool = False,
    ) -> int:
        """Add one or more tags to one or more entries.

        When ``include_parents`` is true, the complete parent implication chain for
        each supplied tag is materialized on the entries as well.

        Returns:
            The total number of tags added across all entries.
        """
        total_added: int = 0
        logger.info(
            "[Library][add_tags_to_entries]",
            entry_ids=entry_ids,
            tag_ids=tag_ids,
        )

        entry_ids_ = [entry_ids] if isinstance(entry_ids, int) else list(entry_ids)
        tag_ids_ = [tag_ids] if isinstance(tag_ids, int) else list(tag_ids)
        if include_parents:
            tag_ids_ = list(self.get_implied_tag_ids(tag_ids_))
        with Session(self.engine, expire_on_commit=False) as session:
            for tag_id in tag_ids_:
                for entry_id in entry_ids_:
                    try:
                        session.add(TagEntry(tag_id=tag_id, entry_id=entry_id))
                        total_added += 1
                        session.commit()
                    except IntegrityError:
                        session.rollback()

        return total_added

    def get_implied_tag_ids(
        self, tag_ids: Iterable[int] | int, *, include_self: bool = True
    ) -> set[int]:
        """Return tags implied by the supplied tags through their parent graph.

        A parent relationship is directional: ``kitten`` implies ``cat`` when kitten
        has cat as a parent. The result includes every transitive ancestor, and can
        optionally omit the originally supplied IDs.
        """
        requested_ids = self.__bulk_tag_ids(tag_ids)
        if not requested_ids:
            return set()

        with Session(self.engine) as session:
            existing_ids = set(
                session.scalars(select(Tag.id).where(Tag.id.in_(requested_ids)))
            )
            missing_ids = requested_ids.difference(existing_ids)
            if missing_ids:
                raise BulkTagError(f"Unknown tag IDs: {sorted(missing_ids)}")

            implied_ids = set(requested_ids) if include_self else set()
            pending_ids = set(requested_ids)
            while pending_ids:
                parent_ids = set(
                    session.scalars(
                        select(TagParent.parent_id).where(TagParent.child_id.in_(pending_ids))
                    )
                ).difference(implied_ids)
                implied_ids.update(parent_ids)
                pending_ids = parent_ids

        return implied_ids

    def apply_tag_inheritance(self, entry_ids: Iterable[int] | int | None = None) -> int:
        """Materialize all currently implied parent tags on selected entries.

        The operation is idempotent and applies to the complete library when no entry
        IDs are provided. Existing direct and inherited assignments are preserved.
        """
        selected_entry_ids = (
            None
            if entry_ids is None
            else self.__bulk_tag_ids(entry_ids)
        )

        with Session(self.engine, expire_on_commit=False) as session:
            if selected_entry_ids is None:
                selected_entry_ids = set(session.scalars(select(Entry.id)))
            else:
                existing_entry_ids = set(
                    session.scalars(select(Entry.id).where(Entry.id.in_(selected_entry_ids)))
                )
                missing_entry_ids = selected_entry_ids.difference(existing_entry_ids)
                if missing_entry_ids:
                    raise BulkTagError(f"Unknown entry IDs: {sorted(missing_entry_ids)}")

            if not selected_entry_ids:
                return 0

            parent_ids_by_child: dict[int, set[int]] = {}
            for row in session.scalars(select(TagParent)):
                parent_ids_by_child.setdefault(row.child_id, set()).add(row.parent_id)

            tag_entries = list(
                session.scalars(
                    select(TagEntry).where(TagEntry.entry_id.in_(selected_entry_ids))
                )
            )
            entries_by_id: dict[int, set[int]] = {}
            existing_pairs = {
                (tag_entry.tag_id, tag_entry.entry_id) for tag_entry in tag_entries
            }
            pending_pairs: set[tuple[int, int]] = set()

            for tag_entry in tag_entries:
                pending_tag_ids = list(parent_ids_by_child.get(tag_entry.tag_id, set()))
                seen_tag_ids = {tag_entry.tag_id}
                while pending_tag_ids:
                    implied_tag_id = pending_tag_ids.pop()
                    if implied_tag_id in seen_tag_ids:
                        continue
                    seen_tag_ids.add(implied_tag_id)
                    entries_by_id.setdefault(tag_entry.entry_id, set()).add(implied_tag_id)
                    pending_tag_ids.extend(parent_ids_by_child.get(implied_tag_id, set()))

            for entry_id, implied_tag_ids in entries_by_id.items():
                pending_pairs.update(
                    (tag_id, entry_id)
                    for tag_id in implied_tag_ids
                    if (tag_id, entry_id) not in existing_pairs
                )

            session.add_all(
                TagEntry(tag_id=tag_id, entry_id=entry_id)
                for tag_id, entry_id in pending_pairs
            )
            session.commit()

        return len(pending_pairs)

    def remove_tags_from_entries(
        self, entry_ids: int | list[int] | set[int], tag_ids: int | list[int] | set[int]
    ) -> bool:
        """Remove one or more tags from one or more entries."""
        entry_ids_ = [entry_ids] if isinstance(entry_ids, int) else entry_ids
        tag_ids_ = [tag_ids] if isinstance(tag_ids, int) else tag_ids
        with Session(self.engine, expire_on_commit=False) as session:
            try:
                for tag_id in tag_ids_:
                    for entry_id in entry_ids_:
                        tag_entry = session.scalars(
                            select(TagEntry).where(
                                and_(
                                    TagEntry.tag_id == tag_id,
                                    TagEntry.entry_id == entry_id,
                                )
                            )
                        ).first()
                        if tag_entry:
                            session.delete(tag_entry)
                            session.flush()
                session.commit()
                return True
            except IntegrityError as e:
                logger.error(e)
                session.rollback()
                return False

    def add_color(self, color_group: TagColorGroup) -> TagColorGroup | None:
        with Session(self.engine, expire_on_commit=False) as session:
            try:
                session.add(color_group)
                session.commit()
                session.expunge(color_group)
                return color_group

            except IntegrityError as e:
                logger.error(
                    "[Library] Could not add color, trying to update existing value instead.",
                    error=e,
                )
                session.rollback()
                return None

    def delete_color(self, color: TagColorGroup):
        with Session(self.engine, expire_on_commit=False) as session:
            try:
                session.delete(color)
                session.commit()

            except IntegrityError as e:
                logger.error(e)
                session.rollback()
                return None

    def save_library_backup_to_disk(self) -> Path:
        assert isinstance(self.library_dir, Path)
        makedirs(str(self.library_dir / TS_FOLDER_NAME / BACKUP_FOLDER_NAME), exist_ok=True)

        filename = f"ts_library_backup_{datetime.now(UTC).strftime('%Y_%m_%d_%H%M%S')}.sqlite"

        target_path = self.library_dir / TS_FOLDER_NAME / BACKUP_FOLDER_NAME / filename

        shutil.copy2(
            self.library_dir / TS_FOLDER_NAME / SQL_FILENAME,
            target_path,
        )

        logger.info("Library backup saved to disk.", path=target_path)

        return target_path

    def get_tag(self, tag_id: int) -> Tag | None:
        with Session(self.engine) as session:
            tags_query = select(Tag).options(
                selectinload(Tag.parent_tags),
                selectinload(Tag.aliases),
                joinedload(Tag.color),
            )
            tag = session.scalar(tags_query.where(Tag.id == tag_id))

            if tag is not None:
                session.expunge(tag)

                for parent in tag.parent_tags:
                    session.expunge(parent)

                for alias in tag.aliases:
                    session.expunge(alias)

        return tag

    def get_tag_by_name(self, tag_name: str) -> Tag | None:
        with Session(self.engine) as session:
            statement = (
                select(Tag)
                .options(selectinload(Tag.parent_tags), selectinload(Tag.aliases))
                .outerjoin(TagAlias)
                .where(or_(Tag.name == tag_name, TagAlias.name == tag_name))
            )

            tag = session.scalar(statement)

            if tag is not None:
                session.expunge(tag)

                for parent in tag.parent_tags:
                    session.expunge(parent)

                for alias in tag.aliases:
                    session.expunge(alias)

        return tag

    def get_alias(self, tag_id: int, alias_id: int) -> TagAlias | None:
        with Session(self.engine) as session:
            alias_query = select(TagAlias).where(TagAlias.id == alias_id, TagAlias.tag_id == tag_id)

            return session.scalar(alias_query.where(TagAlias.id == alias_id))

    def get_tag_color(self, slug: str, namespace: str) -> TagColorGroup | None:
        with Session(self.engine) as session:
            statement = select(TagColorGroup).where(
                and_(TagColorGroup.slug == slug, TagColorGroup.namespace == namespace)
            )

            return session.scalar(statement)

    def get_tag_hierarchy(self, tag_ids: Iterable[int]) -> dict[int, Tag]:
        """Get a dictionary containing tags in `tag_ids` and all of their ancestor tags."""
        current_tag_ids: set[int] = set(tag_ids)
        all_tag_ids: set[int] = set()
        all_tags: dict[int, Tag] = {}
        all_tag_parents: dict[int, list[int]] = {}

        with Session(self.engine) as session:
            while len(current_tag_ids) > 0:
                all_tag_ids.update(current_tag_ids)
                statement = select(TagParent).where(TagParent.child_id.in_(current_tag_ids))
                tag_parents = session.scalars(statement).fetchall()
                current_tag_ids.clear()
                for tag_parent in tag_parents:
                    all_tag_parents.setdefault(tag_parent.child_id, []).append(tag_parent.parent_id)
                    current_tag_ids.add(tag_parent.parent_id)
                current_tag_ids = current_tag_ids.difference(all_tag_ids)

            statement = select(Tag).where(Tag.id.in_(all_tag_ids))
            statement = statement.options(
                noload(Tag.parent_tags), selectinload(Tag.aliases), joinedload(Tag.color)
            )
            tags = session.scalars(statement).fetchall()
            for tag in tags:
                all_tags[tag.id] = tag
            for tag in all_tags.values():
                try:
                    # Sqlalchemy tracks this as a change to the parent_tags field
                    tag.parent_tags = {all_tags[p] for p in all_tag_parents.get(tag.id, [])}
                    # When calling session.add with this tag instance sqlalchemy will
                    # attempt to create TagParents that already exist.

                    state: InstanceState[Tag] = inspect(tag)
                    # Prevent sqlalchemy from thinking fields are different from what's committed
                    # committed_state contains original values for fields that have changed.
                    # empty when no fields have changed
                    state.committed_state.clear()
                except KeyError as e:
                    logger.error(
                        "[LIBRARY][get_tag_hierarchy] Tag referenced by TagParent does not exist!",
                        error=e,
                    )

        return all_tags

    def add_parent_tag(self, parent_id: int, child_id: int) -> bool:
        if parent_id == child_id:
            return False

        # open session and save as parent tag
        with Session(self.engine) as session:
            parent_tag = TagParent(
                parent_id=parent_id,
                child_id=child_id,
            )

            try:
                session.add(parent_tag)
                session.commit()
                return True
            except IntegrityError:
                session.rollback()
                logger.error("IntegrityError")
                return False

    def add_alias(self, name: str, tag_id: int) -> bool:
        with Session(self.engine) as session:
            if not name:
                logger.warning("[LIBRARY][add_alias] Alias value must not be empty")
                return False
            alias = TagAlias(
                name=name,
                tag_id=tag_id,
            )

            try:
                session.add(alias)
                session.commit()
                return True
            except IntegrityError:
                session.rollback()
                logger.error("IntegrityError")
                return False

    def remove_parent_tag(self, base_id: int, remove_tag_id: int) -> bool:
        with Session(self.engine) as session:
            p_id = base_id
            r_id = remove_tag_id
            remove = session.query(TagParent).filter_by(parent_id=p_id, child_id=r_id).one()
            session.delete(remove)
            session.commit()

        return True

    def update_tag(
        self,
        tag: Tag,
        parent_ids: list[int] | set[int] | None = None,
        alias_names: list[str] | set[str] | None = None,
        alias_ids: list[int] | set[int] | None = None,
    ) -> None:
        """Edit a Tag in the Library."""
        self.add_tag(tag, parent_ids, alias_names, alias_ids)

    @staticmethod
    def __bulk_tag_ids(values: Iterable[int] | int) -> set[int]:
        if isinstance(values, int):
            return {values}
        try:
            return {int(value) for value in values}
        except (TypeError, ValueError) as error:
            raise BulkTagError("Tag and entry IDs must be integers") from error

    @staticmethod
    def __assert_acyclic_parent_edges(edges: Iterable[tuple[int, int]]) -> None:
        children_by_parent: dict[int, set[int]] = {}
        nodes: set[int] = set()
        for parent_id, child_id in edges:
            nodes.update((parent_id, child_id))
            children_by_parent.setdefault(parent_id, set()).add(child_id)

        visiting: set[int] = set()
        visited: set[int] = set()

        def visit(node_id: int) -> None:
            if node_id in visiting:
                raise BulkTagError("Tag parent relationships cannot contain a cycle")
            if node_id in visited:
                return

            visiting.add(node_id)
            for child_id in children_by_parent.get(node_id, set()):
                visit(child_id)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in nodes:
            visit(node_id)

    def rename_tags(self, renames: Mapping[int, str]) -> dict[int, str]:
        """Rename several tags atomically while preserving their relationships and entries."""
        if not renames:
            return {}

        normalized: dict[int, str] = {}
        for tag_id, name in renames.items():
            if not isinstance(name, str) or not name.strip():
                raise BulkTagError("Tag names must not be empty")
            normalized[int(tag_id)] = name.strip()

        normalized_names = [name.casefold() for name in normalized.values()]
        if len(normalized_names) != len(set(normalized_names)):
            raise BulkTagError("Bulk renames must use distinct tag names")

        with Session(self.engine) as session:
            tags = list(session.scalars(select(Tag)))
            tags_by_id = {tag.id: tag for tag in tags}
            missing_ids = set(normalized).difference(tags_by_id)
            if missing_ids:
                raise BulkTagError(f"Unknown tag IDs: {sorted(missing_ids)}")

            names_by_casefold = {tag.name.casefold(): tag.id for tag in tags}
            renamed_ids = set(normalized)
            for _tag_id, name in normalized.items():
                existing_id = names_by_casefold.get(name.casefold())
                if existing_id is not None and existing_id not in renamed_ids:
                    raise BulkTagError(f"A tag named {name!r} already exists")

            for tag_id, name in normalized.items():
                tags_by_id[tag_id].name = name
            session.commit()

        return normalized

    def bulk_rename_tags(self, renames: Mapping[int, str]) -> dict[int, str]:
        """Compatibility alias for :meth:`rename_tags`."""
        return self.rename_tags(renames)

    def merge_tags(self, source_tag_ids: Iterable[int], target_tag_id: int) -> int:
        """Merge source tags into one target tag in a single transaction.

        Entry assignments, aliases, parent and child relationships, and disambiguation
        references are redirected to the target before the source tags are removed.
        """
        source_ids = self.__bulk_tag_ids(source_tag_ids)
        target_id = int(target_tag_id)
        if not source_ids:
            raise BulkTagError("At least one source tag is required")
        if target_id in source_ids:
            raise BulkTagError("The merge target must not also be a source tag")
        reserved_sources = source_ids.intersection(range(RESERVED_TAG_START, RESERVED_TAG_END))
        if reserved_sources:
            raise BulkTagError("Built-in tags cannot be removed by a merge")

        with Session(self.engine) as session:
            all_ids = source_ids | {target_id}
            tags = list(session.scalars(select(Tag).where(Tag.id.in_(all_ids))))
            tags_by_id = {tag.id: tag for tag in tags}
            missing_ids = all_ids.difference(tags_by_id)
            if missing_ids:
                raise BulkTagError(f"Unknown tag IDs: {sorted(missing_ids)}")

            source_tags = [tags_by_id[tag_id] for tag_id in source_ids]

            source_entry_ids = set(
                session.scalars(
                    select(TagEntry.entry_id).where(TagEntry.tag_id.in_(source_ids))
                )
            )
            target_entry_ids = set(
                session.scalars(select(TagEntry.entry_id).where(TagEntry.tag_id == target_id))
            )
            session.add_all(
                TagEntry(tag_id=target_id, entry_id=entry_id)
                for entry_id in source_entry_ids.difference(target_entry_ids)
            )
            session.execute(delete(TagEntry).where(TagEntry.tag_id.in_(source_ids)))

            target_aliases = list(
                session.scalars(select(TagAlias).where(TagAlias.tag_id == target_id))
            )
            alias_names = {alias.name.casefold() for alias in target_aliases}
            target_name = tags_by_id[target_id].name.casefold()
            aliases_to_add: list[TagAlias] = []
            for source_tag in source_tags:
                source_aliases = list(
                    session.scalars(select(TagAlias).where(TagAlias.tag_id == source_tag.id))
                )
                for alias_name in [source_tag.name, *(alias.name for alias in source_aliases)]:
                    if (
                        not alias_name.strip()
                        or alias_name.casefold() == target_name
                        or alias_name.casefold() in alias_names
                    ):
                        continue
                    aliases_to_add.append(TagAlias(alias_name, target_id))
                    alias_names.add(alias_name.casefold())
            session.add_all(aliases_to_add)
            session.execute(delete(TagAlias).where(TagAlias.tag_id.in_(source_ids)))

            parent_rows = list(session.scalars(select(TagParent)))
            parent_edges: set[tuple[int, int]] = set()
            for row in parent_rows:
                if row.parent_id in source_ids or row.child_id in source_ids:
                    parent_id = target_id if row.parent_id in source_ids else row.parent_id
                    child_id = target_id if row.child_id in source_ids else row.child_id
                    if parent_id != child_id:
                        parent_edges.add((parent_id, child_id))
                else:
                    parent_edges.add((row.parent_id, row.child_id))

            self.__assert_acyclic_parent_edges(parent_edges)
            session.execute(delete(TagParent))
            session.flush()
            session.expunge_all()
            session.add_all(
                TagParent(parent_id=parent_id, child_id=child_id)
                for parent_id, child_id in parent_edges
            )

            session.execute(
                update(Tag)
                .where(Tag.disambiguation_id.in_(source_ids))
                .values(disambiguation_id=target_id)
            )
            session.execute(delete(Tag).where(Tag.id.in_(source_ids)))
            session.commit()

        return target_id

    def bulk_merge_tags(self, source_tag_ids: Iterable[int], target_tag_id: int) -> int:
        """Compatibility alias for :meth:`merge_tags`."""
        return self.merge_tags(source_tag_ids, target_tag_id)

    def split_tag(
        self,
        tag_id: int,
        splits: Mapping[str, Iterable[int]],
        *,
        remove_original: bool = True,
    ) -> dict[str, int]:
        """Create tags from disjoint entry subsets of an existing tag atomically.

        Every assigned entry must already have ``tag_id``. New tags inherit the source
        tag's parent relationships and visual category settings. The source tag itself
        remains available; ``remove_original`` controls only its assignments.
        """
        source_id = int(tag_id)
        if source_id in range(RESERVED_TAG_START, RESERVED_TAG_END):
            raise BulkTagError("Built-in tags cannot be split")
        if not splits:
            raise BulkTagError("At least one split tag is required")

        normalized_splits: dict[str, set[int]] = {}
        names_seen: set[str] = set()
        for raw_name, raw_entry_ids in splits.items():
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise BulkTagError("Split tag names must not be empty")
            name = raw_name.strip()
            name_key = name.casefold()
            if name_key in names_seen:
                raise BulkTagError("Split tag names must be distinct")
            names_seen.add(name_key)
            entry_ids = self.__bulk_tag_ids(raw_entry_ids)
            if not entry_ids:
                raise BulkTagError(f"Split tag {name!r} has no entries")
            normalized_splits[name] = entry_ids

        all_entry_ids = set().union(*normalized_splits.values())
        owners: dict[int, str] = {}
        for name, entry_ids in normalized_splits.items():
            for entry_id in entry_ids:
                if entry_id in owners:
                    raise BulkTagError(
                        f"Entry {entry_id} is assigned to more than one split tag"
                    )
                owners[entry_id] = name

        with Session(self.engine) as session:
            source = session.scalar(select(Tag).where(Tag.id == source_id))
            if source is None:
                raise BulkTagError(f"Unknown tag ID: {source_id}")

            tags = list(session.scalars(select(Tag)))
            names_by_casefold = {tag.name.casefold(): tag.id for tag in tags}
            for name in normalized_splits:
                if name.casefold() in names_by_casefold:
                    raise BulkTagError(f"A tag named {name!r} already exists")

            existing_entry_ids = set(
                session.scalars(
                    select(TagEntry.entry_id).where(
                        TagEntry.tag_id == source_id,
                        TagEntry.entry_id.in_(all_entry_ids),
                    )
                )
            )
            missing_entry_ids = all_entry_ids.difference(existing_entry_ids)
            if missing_entry_ids:
                raise BulkTagError(
                    "All split entries must already have the source tag: "
                    f"{sorted(missing_entry_ids)}"
                )

            source_parent_ids = set(
                session.scalars(
                    select(TagParent.parent_id).where(TagParent.child_id == source_id)
                )
            )
            created_ids: dict[str, int] = {}
            for name in normalized_splits:
                new_tag = Tag(
                    name=name,
                    color_namespace=source.color_namespace,
                    color_slug=source.color_slug,
                    is_category=source.is_category,
                    is_hidden=source.is_hidden,
                )
                session.add(new_tag)
                session.flush()
                created_ids[name] = new_tag.id
                session.add_all(
                    TagParent(parent_id=parent_id, child_id=new_tag.id)
                    for parent_id in source_parent_ids
                )
                session.add_all(
                    TagEntry(tag_id=new_tag.id, entry_id=entry_id)
                    for entry_id in normalized_splits[name]
                )

            if remove_original:
                session.execute(
                    delete(TagEntry).where(
                        TagEntry.tag_id == source_id,
                        TagEntry.entry_id.in_(all_entry_ids),
                    )
                )
            session.commit()

        return created_ids

    def bulk_split_tag(
        self,
        tag_id: int,
        splits: Mapping[str, Iterable[int]],
        *,
        remove_original: bool = True,
    ) -> dict[str, int]:
        """Compatibility alias for :meth:`split_tag`."""
        return self.split_tag(tag_id, splits, remove_original=remove_original)

    def reparent_tags(
        self,
        tag_ids: Iterable[int] | Mapping[int, Iterable[int]],
        parent_ids: Iterable[int] | None = None,
    ) -> dict[int, set[int]]:
        """Replace parent relationships for one or more tags atomically.

        ``tag_ids`` may be a sequence paired with one shared ``parent_ids`` iterable,
        or a mapping of child IDs to their individual parent ID iterables.
        """
        if isinstance(tag_ids, Mapping):
            if parent_ids is not None:
                raise BulkTagError("Parent IDs cannot be supplied with a replacement mapping")
            replacements = {
                int(child_id): self.__bulk_tag_ids(raw_parent_ids)
                for child_id, raw_parent_ids in tag_ids.items()
            }
        else:
            if parent_ids is None:
                raise BulkTagError("Parent IDs are required")
            selected_ids = self.__bulk_tag_ids(tag_ids)
            shared_parent_ids = self.__bulk_tag_ids(parent_ids)
            replacements = {
                child_id: set(shared_parent_ids) for child_id in selected_ids
            }

        if not replacements:
            return {}
        if any(child_id in replacement for child_id, replacement in replacements.items()):
            raise BulkTagError("A tag cannot be its own parent")

        all_parent_ids = set().union(*(parents for parents in replacements.values()))
        all_tag_ids = set(replacements) | all_parent_ids

        with Session(self.engine) as session:
            existing_ids = set(session.scalars(select(Tag.id).where(Tag.id.in_(all_tag_ids))))
            missing_ids = all_tag_ids.difference(existing_ids)
            if missing_ids:
                raise BulkTagError(f"Unknown tag IDs: {sorted(missing_ids)}")

            current_edges = {
                (row.parent_id, row.child_id) for row in session.scalars(select(TagParent))
            }
            proposed_edges = {
                edge for edge in current_edges if edge[1] not in replacements
            }
            proposed_edges.update(
                (parent_id, child_id)
                for child_id, replacement in replacements.items()
                for parent_id in replacement
            )
            self.__assert_acyclic_parent_edges(proposed_edges)

            session.execute(
                delete(TagParent).where(TagParent.child_id.in_(set(replacements)))
            )
            session.add_all(
                TagParent(parent_id=parent_id, child_id=child_id)
                for child_id, replacement in replacements.items()
                for parent_id in replacement
            )

            tags = session.scalars(select(Tag).where(Tag.id.in_(set(replacements))))
            for tag in tags:
                if tag.disambiguation_id not in replacements[tag.id]:
                    tag.disambiguation_id = None
            session.commit()

        return replacements

    def bulk_reparent_tags(
        self,
        tag_ids: Iterable[int] | Mapping[int, Iterable[int]],
        parent_ids: Iterable[int] | None = None,
    ) -> dict[int, set[int]]:
        """Compatibility alias for :meth:`reparent_tags`."""
        return self.reparent_tags(tag_ids, parent_ids)

    def update_color(self, old_color_group: TagColorGroup, new_color_group: TagColorGroup) -> None:
        """Update a TagColorGroup in the Library. If it doesn't already exist, create it."""
        with Session(self.engine) as session:
            existing_color = session.scalar(
                select(TagColorGroup).where(
                    and_(
                        TagColorGroup.namespace == old_color_group.namespace,
                        TagColorGroup.slug == old_color_group.slug,
                    )
                )
            )
            if existing_color:
                update_color_stmt = (
                    update(TagColorGroup)
                    .where(
                        and_(
                            TagColorGroup.namespace == old_color_group.namespace,
                            TagColorGroup.slug == old_color_group.slug,
                        )
                    )
                    .values(
                        slug=new_color_group.slug,
                        namespace=new_color_group.namespace,
                        name=new_color_group.name,
                        primary=new_color_group.primary,
                        secondary=new_color_group.secondary,
                        color_border=new_color_group.color_border,
                    )
                )
                session.execute(update_color_stmt)
                session.flush()
                update_tags_stmt = (
                    update(Tag)
                    .where(
                        and_(
                            Tag.color_namespace == old_color_group.namespace,
                            Tag.color_slug == old_color_group.slug,
                        )
                    )
                    .values(
                        color_namespace=new_color_group.namespace,
                        color_slug=new_color_group.slug,
                    )
                )
                session.execute(update_tags_stmt)
                session.commit()
            else:
                self.add_color(new_color_group)

    def update_aliases(
        self,
        tag: Tag,
        alias_ids: list[int] | set[int],
        alias_names: list[str] | set[str],
        session: Session,
    ):
        prev_aliases = session.scalars(select(TagAlias).where(TagAlias.tag_id == tag.id)).all()

        for alias in prev_aliases:
            if alias.id not in alias_ids or alias.name not in alias_names:
                session.delete(alias)
            else:
                alias_ids.remove(alias.id)
                alias_names.remove(alias.name)

        for alias_name in alias_names:
            alias = TagAlias(alias_name, tag.id)
            session.add(alias)

    def update_parent_tags(self, tag: Tag, parent_ids: list[int] | set[int], session: Session):
        if tag.id in parent_ids:
            parent_ids.remove(tag.id)

        if tag.disambiguation_id not in parent_ids:
            tag.disambiguation_id = None

        # load all tag's parent tags to know which to remove
        prev_parent_tags = session.scalars(
            select(TagParent).where(TagParent.child_id == tag.id)
        ).all()

        for parent_tag in prev_parent_tags:
            if parent_tag.parent_id not in parent_ids:
                session.delete(parent_tag)
            else:
                # no change, remove from list
                parent_ids.remove(parent_tag.parent_id)

                # create remaining items
        for parent_id in parent_ids:
            # add new parent tag
            parent_tag = TagParent(
                parent_id=parent_id,
                child_id=tag.id,
            )
            session.add(parent_tag)

    def get_version(self, key: str) -> int:
        """Get a version value from the DB.

        Args:
            key(str): The key for the name of the version type to set.
        """
        with Session(self.engine) as session:
            engine = sqlalchemy.inspect(self.engine)
            try:
                # "Version" table added in DB_VERSION 101
                if engine and engine.has_table("Version"):
                    version = session.scalar(select(Version).where(Version.key == key))
                    assert version
                    return version.value
                # NOTE: The "Preferences" table has been depreciated as of TagStudio 9.5.4
                # and is set to be removed in a future release.
                else:
                    pref_version = session.scalar(
                        select(Preferences).where(Preferences.key == DB_VERSION_LEGACY_KEY)
                    )
                    assert pref_version
                    assert isinstance(pref_version.value, int)
                    return pref_version.value
            except Exception:
                return 0

    def set_version(self, key: str, value: int) -> None:
        """Set a version value to the DB.

        Args:
            key(str): The key for the name of the version type to set.
            value(int): The version value to set.
        """
        with Session(self.engine) as session:
            try:
                version = session.scalar(select(Version).where(Version.key == key))
                assert version
                version.value = value
                session.add(version)
                session.commit()

                # If a depreciated "Preferences" table is found, update the version value to be read
                # by older TagStudio versions.
                engine = sqlalchemy.inspect(self.engine)
                if engine and engine.has_table("Preferences"):
                    pref = unwrap(
                        session.scalar(
                            select(Preferences).where(Preferences.key == DB_VERSION_LEGACY_KEY)
                        )
                    )
                    pref.value = value  # pyright: ignore
                    session.add(pref)
                    session.commit()
            except (IntegrityError, AssertionError) as e:
                logger.error("[Library][ERROR] Couldn't add default tag color namespaces", error=e)
                session.rollback()

    # TODO: Remove this once the 'preferences' table is removed.
    @deprecated("Use `get_version() for version and `ts_ignore` system for extension exclusion.")
    def prefs(self, key: str | LibraryPrefs):  # pyright: ignore[reportUnknownParameterType]
        # load given item from Preferences table
        with Session(self.engine) as session:
            if isinstance(key, LibraryPrefs):
                return unwrap(
                    session.scalar(select(Preferences).where(Preferences.key == key.name))
                ).value  # pyright: ignore[reportUnknownVariableType]
            else:
                return unwrap(
                    session.scalar(select(Preferences).where(Preferences.key == key))
                ).value  # pyright: ignore[reportUnknownVariableType]

    # TODO: Remove this once the 'preferences' table is removed.
    @deprecated("Use `get_version() for version and `ts_ignore` system for extension exclusion.")
    def set_prefs(self, key: str | LibraryPrefs, value: Any) -> None:  # pyright: ignore[reportExplicitAny]
        # set given item in Preferences table
        with Session(self.engine) as session:
            # load existing preference and update value
            stuff = session.scalars(select(Preferences))
            logger.info([x.key for x in list(stuff)])

            pref: Preferences = unwrap(
                session.scalar(
                    select(Preferences).where(
                        Preferences.key == (key.name if isinstance(key, LibraryPrefs) else key)
                    )
                )
            )

            logger.info("loading pref", pref=pref, key=key, value=value)
            pref.value = value
            session.add(pref)
            session.commit()
            # TODO - try/except

    def mirror_entry_fields(self, *entries: Entry) -> None:
        """Mirror fields among multiple Entry items."""
        fields = {}
        # load all fields
        existing_fields = {field.type_key for field in entries[0].fields}
        for entry in entries:
            for entry_field in entry.fields:
                fields[entry_field.type_key] = entry_field

        # assign the field to all entries
        for entry in entries:
            for field_key, field in fields.items():  # pyright: ignore[reportUnknownVariableType]
                if field_key not in existing_fields:
                    self.add_field_to_entry(
                        entry_id=entry.id,
                        field_id=field.type_key,
                        value=field.value,
                    )

    def merge_entries(self, from_entry: Entry, into_entry: Entry) -> bool:
        """Add fields and tags from the first entry to the second, and then delete the first."""
        success = True
        for field in from_entry.fields:
            result = self.add_field_to_entry(
                entry_id=into_entry.id,
                field_id=field.type_key,
                value=field.value,
            )
            if not result:
                success = False
        tag_ids = [tag.id for tag in from_entry.tags]
        self.add_tags_to_entries(into_entry.id, tag_ids)
        self.remove_entries([from_entry.id])

        return success

    @property
    def tag_color_groups(self) -> dict[str, list[TagColorGroup]]:
        """Return every TagColorGroup in the library."""
        with Session(self.engine) as session:
            color_groups: dict[str, list[TagColorGroup]] = {}
            results = session.scalars(select(TagColorGroup).order_by(asc(TagColorGroup.namespace)))
            for color in results:
                if not color_groups.get(color.namespace):
                    color_groups[color.namespace] = []
                color_groups[color.namespace].append(color)
                session.expunge(color)

            # Add empty namespaces that are available for use.
            empty_namespaces = session.scalars(
                select(Namespace)
                .where(Namespace.namespace.not_in(color_groups.keys()))
                .order_by(asc(Namespace.namespace))
            )
            for en in empty_namespaces:
                if not color_groups.get(en.namespace):
                    color_groups[en.namespace] = []
                session.expunge(en)

        return dict(
            sorted(
                color_groups.items(),
                key=lambda kv: self.get_namespace_name(kv[0]).lower(),
            )
        )

    @property
    def namespaces(self) -> list[Namespace]:
        """Return every Namespace in the library."""
        with Session(self.engine) as session:
            namespaces = session.scalars(select(Namespace).order_by(asc(Namespace.name)))
            return list(namespaces)

    def get_namespace_name(self, namespace: str) -> str:
        with Session(self.engine) as session:
            result = session.scalar(select(Namespace).where(Namespace.namespace == namespace))
            if result:
                session.expunge(result)

        return "" if not result else result.name
