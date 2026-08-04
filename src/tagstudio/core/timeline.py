# Copyright (C) 2025 Travis Abendshien (CyanVoxel).
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio


"""Date-based grouping primitives used by the timeline view."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from tagstudio.core.library.alchemy.models import Entry
from tagstudio.core.media_metadata import ExifMetadata, entry_tag_color


class TimelineZoom(StrEnum):
    """The calendar unit used to bucket captured entries."""

    YEAR = "year"
    MONTH = "month"
    DAY = "day"


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """A dated entry ready for timeline grouping."""

    entry_id: int
    path: str
    captured_at: datetime
    color: str


@dataclass(frozen=True, slots=True)
class TimelineGroup:
    """One zoom-level bucket and its entries."""

    key: tuple[int, ...]
    label: str
    entry_ids: tuple[int, ...]
    colors: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.entry_ids)

    @property
    def primary_color(self) -> str:
        return self.colors[0]


def parse_exif_datetime(value: str | None) -> datetime | None:
    """Parse common EXIF and ISO capture-time representations."""
    if not value:
        return None
    normalized = value.replace("\x00", "").strip()
    if not normalized:
        return None

    formats = (
        "%Y:%m:%d %H:%M:%S.%f",
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    )
    for date_format in formats:
        try:
            return datetime.strptime(normalized, date_format)
        except ValueError:
            continue

    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def timeline_event_from_metadata(
    entry: Entry, path: Path, metadata: ExifMetadata
) -> TimelineEvent | None:
    """Build a timeline event when an entry has a valid capture timestamp."""
    captured_at = parse_exif_datetime(metadata.date_time_original)
    if captured_at is None:
        return None
    return TimelineEvent(entry.id, str(path), captured_at, entry_tag_color(entry))


def _group_key(captured_at: datetime, zoom: TimelineZoom) -> tuple[int, ...]:
    if zoom is TimelineZoom.YEAR:
        return (captured_at.year,)
    if zoom is TimelineZoom.MONTH:
        return (captured_at.year, captured_at.month)
    return (captured_at.year, captured_at.month, captured_at.day)


def _group_label(key: tuple[int, ...], zoom: TimelineZoom) -> str:
    year = key[0]
    if zoom is TimelineZoom.YEAR:
        return str(year)
    month = key[1]
    if zoom is TimelineZoom.MONTH:
        return f"{datetime(year, month, 1):%B} {year}"
    day = key[2]
    return f"{datetime(year, month, day):%B} {day}, {year}"


def group_timeline_events(
    events: Iterable[TimelineEvent], zoom: TimelineZoom = TimelineZoom.MONTH
) -> list[TimelineGroup]:
    """Group dated events chronologically at the requested zoom level."""
    zoom = TimelineZoom(zoom)
    buckets: dict[tuple[int, ...], list[TimelineEvent]] = {}
    for event in sorted(events, key=lambda item: (item.captured_at, item.entry_id)):
        buckets.setdefault(_group_key(event.captured_at, zoom), []).append(event)

    groups: list[TimelineGroup] = []
    for key in sorted(buckets):
        bucket = buckets[key]
        colors = tuple(dict.fromkeys(event.color for event in bucket))
        groups.append(
            TimelineGroup(
                key=key,
                label=_group_label(key, zoom),
                entry_ids=tuple(event.entry_id for event in bucket),
                colors=colors,
            )
        )
    return groups
