# Copyright (C) 2025 Travis Abendshien (CyanVoxel).
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio


"""Small, dependency-light readers for metadata used by secondary views."""

from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any

from PIL import Image

from tagstudio.core.library.alchemy.models import Entry

GPS_INFO_TAG = 0x8825
GPS_LATITUDE_REF = 1
GPS_LATITUDE = 2
GPS_LONGITUDE_REF = 3
GPS_LONGITUDE = 4
CAMERA_MODEL = 0x0110
FOCAL_LENGTH = 0x920A
RATING = 0x4746
RATING_PERCENT = 0x4749
DATE_TIME_ORIGINAL = 0x9003
DEFAULT_GEO_COLOR = "#4f7cff"


@dataclass(frozen=True, slots=True)
class ExifMetadata:
    """The EXIF values needed by map, timeline, and facet views."""

    location: tuple[float, float] | None = None
    date_time_original: str | None = None
    camera_model: str | None = None
    focal_length_mm: float | None = None
    rating: float | None = None


@dataclass(frozen=True, slots=True)
class GeoPoint:
    """A library entry projected onto the map."""

    entry_id: int
    path: str
    latitude: float
    longitude: float
    color: str = DEFAULT_GEO_COLOR
    date_time_original: str | None = None

    def as_feature(self, selected: bool = False) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
        """Return a GeoJSON feature suitable for MapLibre."""
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [self.longitude, self.latitude]},
            "properties": {
                "entry_id": self.entry_id,
                "path": self.path,
                "color": self.color,
                "selected": selected,
            },
        }


def _as_float(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def _coordinate(value: object) -> float | None:
    if isinstance(value, tuple | list):
        if len(value) != 3:
            return None
        degrees, minutes, seconds = (_as_float(part) for part in value)
        if degrees is None or minutes is None or seconds is None:
            return None
        return degrees + minutes / 60 + seconds / 3600
    return _as_float(value)


def _reference(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="ignore").strip().upper()
    return str(value).strip().upper()


def _text(value: object) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _location_from_gps(gps: object) -> tuple[float, float] | None:
    if not isinstance(gps, dict):
        return None
    latitude = _coordinate(gps.get(GPS_LATITUDE))
    longitude = _coordinate(gps.get(GPS_LONGITUDE))
    if (
        latitude is None
        or longitude is None
        or not 0 <= latitude <= 180
        or not 0 <= longitude <= 180
    ):
        return None

    latitude_ref = _reference(gps.get(GPS_LATITUDE_REF, ""))
    longitude_ref = _reference(gps.get(GPS_LONGITUDE_REF, ""))
    if latitude_ref not in {"N", "S"} or longitude_ref not in {"E", "W"}:
        return None
    if latitude > 90 or longitude > 180:
        return None
    return (
        -latitude if latitude_ref == "S" else latitude,
        -longitude if longitude_ref == "W" else longitude,
    )


def read_exif_metadata(path: Path) -> ExifMetadata:
    """Read GPS and capture-time metadata, returning empty data for unsupported files."""
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            try:
                gps = exif.get_ifd(GPS_INFO_TAG)
            except (KeyError, TypeError, ValueError):
                gps = None
            date_time_original = exif.get(DATE_TIME_ORIGINAL)
            if isinstance(date_time_original, bytes):
                date_time_original = date_time_original.decode("ascii", errors="ignore")
            if not isinstance(date_time_original, str):
                date_time_original = None
            rating = _as_float(exif.get(RATING))
            if rating is None:
                rating_percent = _as_float(exif.get(RATING_PERCENT))
                if rating_percent is not None:
                    rating = round(rating_percent / 20, 2)
            return ExifMetadata(
                location=_location_from_gps(gps),
                date_time_original=date_time_original,
                camera_model=_text(exif.get(CAMERA_MODEL)),
                focal_length_mm=_as_float(exif.get(FOCAL_LENGTH)),
                rating=rating,
            )
    except (OSError, ValueError):
        return ExifMetadata()


def entry_tag_color(entry: Entry) -> str:
    """Choose the first configured tag color for a map marker."""
    for tag in sorted(entry.tags, key=lambda item: item.id):
        color = getattr(tag.color, "primary", None)
        if isinstance(color, str) and color:
            return color
    return DEFAULT_GEO_COLOR


def geo_point_from_metadata(entry: Entry, path: Path, metadata: ExifMetadata) -> GeoPoint | None:
    """Build a map point from already-read EXIF metadata."""
    if metadata.location is None:
        return None
    latitude, longitude = metadata.location
    return GeoPoint(
        entry_id=entry.id,
        path=str(path),
        latitude=latitude,
        longitude=longitude,
        color=entry_tag_color(entry),
        date_time_original=metadata.date_time_original,
    )


def geo_point_for_entry(entry: Entry, path: Path) -> GeoPoint | None:
    """Build a map point for an entry when its file contains valid GPS EXIF."""
    return geo_point_from_metadata(entry, path, read_exif_metadata(path))
