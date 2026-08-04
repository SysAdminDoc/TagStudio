# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio


from pathlib import Path

from PIL import Image, TiffImagePlugin

from tagstudio.core.media_metadata import read_exif_metadata


def _write_exif_image(path: Path, *, latitude_ref: str = "N") -> None:
    image = Image.new("RGB", (8, 8), color="white")
    exif = Image.Exif()
    gps = exif.get_ifd(0x8825)
    gps[1] = latitude_ref
    gps[2] = (
        TiffImagePlugin.IFDRational(40, 1),
        TiffImagePlugin.IFDRational(30, 1),
        TiffImagePlugin.IFDRational(0, 1),
    )
    gps[3] = "W"
    gps[4] = (
        TiffImagePlugin.IFDRational(74, 1),
        TiffImagePlugin.IFDRational(0, 1),
        TiffImagePlugin.IFDRational(0, 1),
    )
    exif[0x0110] = "Canon EOS R5"
    exif[0x920A] = TiffImagePlugin.IFDRational(35, 1)
    exif[0x4746] = 4
    exif[0x9003] = "2024:01:02 03:04:05"
    image.save(path, exif=exif)


def test_read_exif_metadata_converts_gps_and_preserves_capture_time(tmp_path: Path):
    image_path = tmp_path / "photo.jpg"
    _write_exif_image(image_path)

    metadata = read_exif_metadata(image_path)

    assert metadata.location == (40.5, -74.0)
    assert metadata.date_time_original == "2024:01:02 03:04:05"
    assert metadata.camera_model == "Canon EOS R5"
    assert metadata.focal_length_mm == 35.0
    assert metadata.rating == 4.0


def test_read_exif_metadata_applies_southern_latitude(tmp_path: Path):
    image_path = tmp_path / "photo.jpg"
    _write_exif_image(image_path, latitude_ref="S")

    assert read_exif_metadata(image_path).location == (-40.5, -74.0)


def test_read_exif_metadata_ignores_non_images(tmp_path: Path):
    invalid_path = tmp_path / "notes.txt"
    invalid_path.write_text("not an image", encoding="utf-8")

    assert read_exif_metadata(invalid_path).location is None
