# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

"""Headless command-line workflows for TagStudio libraries."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO, cast

import structlog

from tagstudio.core.library.alchemy.enums import BrowsingState
from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.library.alchemy.models import Tag
from tagstudio.core.library.mirror import MirrorTarget, export_read_only_mirror


class CliError(ValueError):
    """Raised when a CLI request cannot be completed."""


class _StderrProxy:
    """Resolve stderr at write time so embedded CLI calls remain capture-safe."""

    def write(self, message: str) -> int:
        return sys.stderr.write(message)

    def flush(self) -> None:
        sys.stderr.flush()


def _configure_cli_logging() -> None:
    """Keep machine-readable command output on stdout and diagnostics on stderr."""
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=cast(TextIO, _StderrProxy()))
    )


@contextmanager
def _opened_library(path: Path):
    library = Library()
    status = library.open_library(Path(path).expanduser())
    if not status.success:
        message = status.message or "Could not open the TagStudio library."
        raise CliError(message)
    try:
        yield library
    finally:
        library.close()


def _canonical(path: Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _all_entry_ids(library: Library, *, include_hidden: bool = True) -> list[int]:
    state = BrowsingState(show_hidden_entries=include_hidden)
    return list(library.search_library(state, page_size=0).ids)


def _entry_record(library: Library, entry_id: int) -> dict[str, object] | None:
    entry = library.get_entry_full(entry_id, with_fields=False, with_tags=True)
    if entry is None:
        return None
    return {
        "id": entry.id,
        "path": _canonical(library.resolve_entry_path(entry)).as_posix(),
        "relative_path": Path(entry.path).as_posix(),
        "tags": sorted(
            (tag.name for tag in entry.tags),
            key=lambda name: (name.casefold(), name),
        ),
    }


def _write_records(records: list[dict[str, object]], output_format: str) -> None:
    if output_format == "paths":
        for record in records:
            sys.stdout.write(f"{record['path']}\n")
        return
    if output_format == "jsonl":
        for record in records:
            sys.stdout.write(f"{json.dumps(record, ensure_ascii=False, sort_keys=True)}\n")
        return
    sys.stdout.write(f"{json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True)}\n")


def _resolve_cli_path(library: Library, raw_path: Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return _canonical(path)
    if library.library_dir is not None:
        library_relative = _canonical(Path(library.library_dir) / path)
        if library_relative.exists():
            return library_relative
    return _canonical(path)


def _entry_ids_for_paths(library: Library, paths: Iterable[Path]) -> set[int]:
    requested_paths = [_resolve_cli_path(library, path) for path in paths]
    selected: set[int] = set()
    for entry_id in _all_entry_ids(library):
        entry = library.get_entry(entry_id)
        if entry is None:
            continue
        entry_path = _canonical(library.resolve_entry_path(entry))
        if any(
            entry_path == requested
            or (requested.is_dir() and entry_path.is_relative_to(requested))
            for requested in requested_paths
        ):
            selected.add(entry_id)
    return selected


def _selected_entry_ids(
    library: Library,
    *,
    paths: Iterable[Path],
    query: str | None,
    entry_ids: Iterable[int],
    include_hidden: bool,
) -> list[int]:
    selected: set[int] = set()
    if query is not None:
        try:
            state = (
                BrowsingState(show_hidden_entries=include_hidden)
                if not query
                else BrowsingState.from_search_query(query).with_show_hidden_entries(
                    include_hidden
                )
            )
            selected.update(library.search_library(state, page_size=0).ids)
        except Exception as error:  # noqa: BLE001
            raise CliError(f"Invalid search query: {error}") from error
    selected.update(_entry_ids_for_paths(library, paths))
    for entry_id in entry_ids:
        if library.get_entry(entry_id) is None:
            raise CliError(f"Entry does not exist: {entry_id}")
        selected.add(entry_id)
    if not selected:
        raise CliError("Select entries with --path, --query, or --entry-id.")
    return sorted(selected)


def _run_query(args: argparse.Namespace) -> int:
    with _opened_library(args.library) as library:
        try:
            state = (
                BrowsingState(show_hidden_entries=args.include_hidden)
                if not args.query
                else BrowsingState.from_search_query(args.query).with_show_hidden_entries(
                    args.include_hidden
                )
            )
            entry_ids = library.search_library(state, page_size=0).ids
        except Exception as error:  # noqa: BLE001
            raise CliError(f"Invalid search query: {error}") from error
        records = [
            record
            for entry_id in entry_ids
            if (record := _entry_record(library, entry_id)) is not None
        ]
    _write_records(records, args.format)
    return 0


def _run_tag(args: argparse.Namespace) -> int:
    tag_names = list(dict.fromkeys(name.strip() for name in args.tags if name.strip()))
    if not tag_names:
        raise CliError("At least one non-empty tag name is required.")

    with _opened_library(args.library) as library:
        selected_ids = _selected_entry_ids(
            library,
            paths=args.path,
            query=args.query,
            entry_ids=args.entry_id,
            include_hidden=args.include_hidden,
        )
        created_tags: list[str] = []
        added_assignments = 0
        removed_assignments = 0
        warnings: list[str] = []
        resolved_tags: list[Tag] = []

        for name in tag_names:
            tag = library.get_tag_by_name(name)
            if tag is None and not args.no_create:
                tag = library.add_tag(Tag(name=name))
                if tag is not None:
                    created_tags.append(name)
            if tag is None:
                warnings.append(f"Tag does not exist and was not created: {name}")
                continue
            resolved_tags.append(tag)

        for entry_id in selected_ids:
            entry = library.get_entry_full(entry_id, with_fields=False, with_tags=True)
            if entry is None:
                warnings.append(f"Entry disappeared during operation: {entry_id}")
                continue
            if args.replace:
                current_ids = [tag.id for tag in entry.tags]
                if current_ids:
                    library.remove_tags_from_entries(entry_id, current_ids)
                    removed_assignments += len(current_ids)
            if args.remove:
                current_tag_ids = {current.id for current in entry.tags}
                for tag in resolved_tags:
                    if tag.id in current_tag_ids and library.remove_tags_from_entries(
                        entry_id, tag.id
                    ):
                        removed_assignments += 1
            else:
                for tag in resolved_tags:
                    added_assignments += library.add_tags_to_entries(
                        entry_id,
                        tag.id,
                        include_parents=args.include_parents,
                    )

    sys.stdout.write(
        f"{json.dumps(
            {
                "added_assignments": added_assignments,
                "command": "tag",
                "created_tags": created_tags,
                "entries": len(selected_ids),
                "removed_assignments": removed_assignments,
                "tags": tag_names,
                "warnings": warnings,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )}\n"
    )
    return 0


def _run_export(args: argparse.Namespace) -> int:
    entry_ids = args.entry_id
    with _opened_library(args.library) as library:
        if args.query is not None:
            try:
                state = (
                    BrowsingState()
                    if not args.query
                    else BrowsingState.from_search_query(args.query)
                )
                entry_ids = list(library.search_library(state, page_size=0).ids)
            except Exception as error:  # noqa: BLE001
                raise CliError(f"Invalid search query: {error}") from error
        result = export_read_only_mirror(
            library,
            args.target,
            args.destination,
            entry_ids=entry_ids,
            overwrite=not args.no_overwrite,
            include_sidecars=not args.no_sidecars,
            dry_run=args.dry_run,
        )

    sys.stdout.write(
        f"{json.dumps(
            {
                "command": "export",
                "destination": result.destination.as_posix(),
                "entries_seen": result.entries_seen,
                "files_copied": result.files_copied,
                "manifest": result.manifest_path.as_posix(),
                "sidecars_written": result.sidecars_written,
                "skipped_entries": result.skipped_entries,
                "target": result.target.value,
                "warnings": list(result.warnings),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )}\n"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the standalone CLI parser."""
    parser = argparse.ArgumentParser(
        prog="tagstudio",
        description="Run headless TagStudio library workflows.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    query = commands.add_parser("query", help="Search a library and emit entry records.")
    query.add_argument("library", type=Path, help="TagStudio library directory.")
    query.add_argument("query", nargs="?", default="", help="TagStudio search expression.")
    query.add_argument(
        "--format",
        choices=("json", "jsonl", "paths"),
        default="json",
        help="Output format (default: json).",
    )
    query.add_argument(
        "--include-hidden",
        action="store_true",
        help="Include entries carrying hidden tags.",
    )
    query.set_defaults(handler=_run_query)

    tag = commands.add_parser("tag", help="Add or remove tags from selected entries.")
    tag.add_argument("library", type=Path, help="TagStudio library directory.")
    tag.add_argument("tags", nargs="+", help="Tag names to add, replace, or remove.")
    selector = tag.add_mutually_exclusive_group()
    selector.add_argument(
        "--path",
        action="append",
        default=[],
        type=Path,
        help="File or directory to select; repeat for multiple paths.",
    )
    selector.add_argument("--query", help="TagStudio search expression selecting entries.")
    selector.add_argument(
        "--entry-id",
        action="append",
        default=[],
        type=int,
        help="Entry ID to select; repeat for multiple IDs.",
    )
    operation = tag.add_mutually_exclusive_group()
    operation.add_argument(
        "--remove",
        action="store_true",
        help="Remove the named tags instead of adding them.",
    )
    operation.add_argument(
        "--replace",
        action="store_true",
        help="Remove existing tags before adding the named tags.",
    )
    tag.add_argument(
        "--no-create",
        action="store_true",
        help="Do not create missing tags; report them as warnings.",
    )
    tag.add_argument(
        "--include-parents",
        action="store_true",
        help="Materialize parent tags when adding tags.",
    )
    tag.add_argument(
        "--include-hidden",
        action="store_true",
        help="Include hidden-tag entries when selecting by query.",
    )
    tag.set_defaults(handler=_run_tag)

    export = commands.add_parser("export", help="Export a read-only external photo-manager mirror.")
    export.add_argument("library", type=Path, help="TagStudio library directory.")
    export.add_argument("destination", type=Path, help="Mirror destination directory.")
    export.add_argument(
        "--target",
        choices=tuple(target.value for target in MirrorTarget),
        default=MirrorTarget.NEXTCLOUD.value,
        help="External target profile (default: nextcloud).",
    )
    export.add_argument("--query", help="Export only entries matching this search expression.")
    export.add_argument(
        "--entry-id",
        action="append",
        default=None,
        type=int,
        help="Export only this entry ID; repeat for multiple IDs.",
    )
    export.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Skip files already present in the destination.",
    )
    export.add_argument(
        "--no-sidecars",
        action="store_true",
        help="Copy media without generated XMP tag sidecars.",
    )
    export.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the export without writing destination files.",
    )
    export.set_defaults(handler=_run_export)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one CLI command and return its process exit code."""
    _configure_cli_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except CliError as error:
        parser.error(str(error))
    return 2
