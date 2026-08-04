# Copyright (C) 2025 Travis Abendshien (CyanVoxel).
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio


"""EXIF facet values and histogram buckets."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from tagstudio.core.library.alchemy.models import Entry
from tagstudio.core.media_metadata import ExifMetadata


class FacetField(StrEnum):
    """The derived metadata dimensions shown in the facets pane."""

    CAMERA_MODEL = "camera_model"
    FOCAL_LENGTH = "focal_length_mm"
    RATING = "rating"


@dataclass(frozen=True, slots=True)
class FacetEvent:
    """One entry's derived values for the supported facets."""

    entry_id: int
    camera_model: str | None
    focal_length_mm: float | None
    rating: float | None


@dataclass(frozen=True, slots=True)
class FacetBucket:
    """A histogram bucket with the entries that contribute to its count."""

    field: FacetField
    value: str
    entry_ids: tuple[int, ...]

    @property
    def count(self) -> int:
        return len(self.entry_ids)


def facet_event_from_metadata(entry: Entry, metadata: ExifMetadata) -> FacetEvent:
    """Build facet values from already-read EXIF metadata."""
    return FacetEvent(
        entry_id=entry.id,
        camera_model=metadata.camera_model,
        focal_length_mm=metadata.focal_length_mm,
        rating=metadata.rating,
    )


def _value_for_field(event: FacetEvent, field: FacetField) -> tuple[str, float | str] | None:
    value = getattr(event, field.value)
    if value is None:
        return None
    if field is FacetField.CAMERA_MODEL:
        return str(value), str(value).casefold()
    if field is FacetField.FOCAL_LENGTH:
        return f"{float(value):g} mm", float(value)
    return f"{float(value):g} / 5", float(value)


def build_facet_buckets(events: Iterable[FacetEvent], field: FacetField) -> list[FacetBucket]:
    """Create stable, ascending histogram buckets for one facet dimension."""
    buckets: dict[str, list[int]] = {}
    sort_values: dict[str, float | str] = {}
    for event in events:
        resolved = _value_for_field(event, field)
        if resolved is None:
            continue
        value, sort_value = resolved
        buckets.setdefault(value, []).append(event.entry_id)
        sort_values[value] = sort_value

    return [
        FacetBucket(field, value, tuple(buckets[value]))
        for value in sorted(buckets, key=lambda item: sort_values[item])
    ]
