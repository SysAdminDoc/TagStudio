# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio


"""Portable tag sidecars for interoperability with other media applications."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

SIDECAR_SCHEMA = "tagstudio.sidecar"
SIDECAR_VERSION = 1

XMP_META_NAMESPACE = "adobe:ns:meta/"
RDF_NAMESPACE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"
LIGHTROOM_NAMESPACE = "http://ns.adobe.com/lightroom/1.0/"
TAGSTUDIO_NAMESPACE = "https://tagstud.io/ns/sidecar/1.0/"

ET.register_namespace("x", XMP_META_NAMESPACE)
ET.register_namespace("rdf", RDF_NAMESPACE)
ET.register_namespace("dc", DC_NAMESPACE)
ET.register_namespace("lr", LIGHTROOM_NAMESPACE)
ET.register_namespace("tagstudio", TAGSTUDIO_NAMESPACE)


class SidecarError(ValueError):
    """Raised when a sidecar does not match the supported contract."""


class SidecarFormat(str, Enum):
    """Formats supported by the TagStudio sidecar exporter."""

    JSON = "json"
    XMP = "xmp"


@dataclass(frozen=True, slots=True)
class SidecarDocument:
    """Normalized tags loaded from either supported sidecar format."""

    tags: tuple[str, ...]
    file: str | None = None


def normalize_format(sidecar_format: SidecarFormat | str) -> SidecarFormat:
    """Normalize a format enum or its command-friendly string value."""
    if isinstance(sidecar_format, SidecarFormat):
        return sidecar_format
    try:
        return SidecarFormat(sidecar_format.lower().lstrip("."))
    except (AttributeError, ValueError) as error:
        raise SidecarError(f"Unsupported sidecar format: {sidecar_format}") from error


def sidecar_path(file_path: Path, sidecar_format: SidecarFormat | str) -> Path:
    """Return the conventional sidecar path for a media file."""
    file_path = Path(file_path)
    normalized_format = normalize_format(sidecar_format)
    if normalized_format is SidecarFormat.JSON:
        return file_path.with_name(f"{file_path.name}.tagstudio.json")
    return file_path.with_suffix(".xmp")


def _clean_tags(tags: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for raw_tag in tags:
        if not isinstance(raw_tag, str):
            raise SidecarError("Sidecar tags must be strings")
        tag = raw_tag.strip()
        if tag and tag not in cleaned:
            cleaned.append(tag)
    return tuple(cleaned)


def _qualified(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _add_rdf_list(parent: ET.Element, name: str, values: tuple[str, ...]) -> None:
    property_element = ET.SubElement(parent, _qualified(DC_NAMESPACE, name))
    bag = ET.SubElement(property_element, _qualified(RDF_NAMESPACE, "Bag"))
    for value in values:
        ET.SubElement(bag, _qualified(RDF_NAMESPACE, "li")).text = value


def serialize_json(document: SidecarDocument) -> str:
    """Serialize a normalized document into the stable JSON contract."""
    payload = {
        "file": document.file,
        "schema": SIDECAR_SCHEMA,
        "tags": list(_clean_tags(list(document.tags))),
        "version": SIDECAR_VERSION,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def parse_json(text: str) -> SidecarDocument:
    """Parse and validate the supported JSON sidecar contract."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise SidecarError("Invalid JSON sidecar") from error
    if not isinstance(payload, dict):
        raise SidecarError("JSON sidecar must contain an object")
    if payload.get("schema") != SIDECAR_SCHEMA or payload.get("version") != SIDECAR_VERSION:
        raise SidecarError("Unsupported TagStudio sidecar schema")
    tags = payload.get("tags", [])
    if not isinstance(tags, list):
        raise SidecarError("JSON sidecar tags must be a list")
    file_name = payload.get("file")
    if file_name is not None and not isinstance(file_name, str):
        raise SidecarError("JSON sidecar file must be a string")
    return SidecarDocument(tags=_clean_tags(tags), file=file_name)


def serialize_xmp(document: SidecarDocument) -> str:
    """Serialize tags as ``dc:subject`` and Lightroom hierarchical subjects."""
    tags = _clean_tags(list(document.tags))
    root = ET.Element(_qualified(XMP_META_NAMESPACE, "xmpmeta"))
    rdf = ET.SubElement(root, _qualified(RDF_NAMESPACE, "RDF"))
    description = ET.SubElement(rdf, _qualified(RDF_NAMESPACE, "Description"))
    description.set(_qualified(RDF_NAMESPACE, "about"), "")
    _add_rdf_list(description, "subject", tags)

    hierarchical = ET.SubElement(
        description, _qualified(LIGHTROOM_NAMESPACE, "hierarchicalSubject")
    )
    hierarchical_bag = ET.SubElement(hierarchical, _qualified(RDF_NAMESPACE, "Bag"))
    for tag in tags:
        ET.SubElement(hierarchical_bag, _qualified(RDF_NAMESPACE, "li")).text = tag

    ET.SubElement(description, _qualified(TAGSTUDIO_NAMESPACE, "sidecarVersion")).text = str(
        SIDECAR_VERSION
    )
    if document.file is not None:
        ET.SubElement(description, _qualified(TAGSTUDIO_NAMESPACE, "file")).text = document.file

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def parse_xmp(text: str) -> SidecarDocument:
    """Read standard Dublin Core or Lightroom subject lists from XMP."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise SidecarError("Invalid XMP sidecar") from error

    tags: list[str] = []
    for subject in root.iter(_qualified(DC_NAMESPACE, "subject")):
        for item in subject.iter(_qualified(RDF_NAMESPACE, "li")):
            if item.text:
                tags.append(item.text)

    if not tags:
        for subject in root.iter(_qualified(LIGHTROOM_NAMESPACE, "hierarchicalSubject")):
            for item in subject.iter(_qualified(RDF_NAMESPACE, "li")):
                if item.text:
                    tags.append(item.text.rsplit("|", maxsplit=1)[-1])

    file_element = root.find(f".//{_qualified(TAGSTUDIO_NAMESPACE, 'file')}")
    return SidecarDocument(
        tags=_clean_tags(tags),
        file=file_element.text if file_element is not None else None,
    )


def write_sidecar(
    path: Path, document: SidecarDocument, sidecar_format: SidecarFormat | str
) -> None:
    """Write one sidecar using UTF-8 and deterministic formatting."""
    normalized_format = normalize_format(sidecar_format)
    content = (
        serialize_json(document)
        if normalized_format is SidecarFormat.JSON
        else serialize_xmp(document)
    )
    Path(path).write_text(content, encoding="utf-8", newline="\n")


def read_sidecar(path: Path, sidecar_format: SidecarFormat | str) -> SidecarDocument:
    """Read one sidecar using the explicitly selected format."""
    normalized_format = normalize_format(sidecar_format)
    content = Path(path).read_text(encoding="utf-8")
    return parse_json(content) if normalized_format is SidecarFormat.JSON else parse_xmp(content)
