# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

"""Public extension points for preview rendering and metadata extraction."""

import mimetypes
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path

import structlog
from PIL import Image

logger = structlog.get_logger(__name__)

PLUGIN_API_VERSION = "1"
PLUGIN_ENTRY_POINT_GROUP = "tagstudio.plugins"

MetadataValue = object


@dataclass(frozen=True, slots=True)
class PreviewRequest:
    """The render context supplied to a preview plugin."""

    path: Path
    size: tuple[int, int]
    pixel_ratio: float
    is_grid_thumb: bool


@dataclass(frozen=True, slots=True)
class PreviewResult:
    """A plugin-rendered image and whether TagStudio may cache it."""

    image: Image.Image
    cacheable: bool = True


@dataclass(frozen=True, slots=True)
class PreviewHandlerRegistration:
    """A registered preview handler and its matching rules."""

    name: str
    handler: Callable[[PreviewRequest], Image.Image | PreviewResult | None]
    extensions: frozenset[str]
    mime_types: frozenset[str]
    priority: int


@dataclass(frozen=True, slots=True)
class MetadataExtractorRegistration:
    """A registered metadata extractor and its matching rules."""

    name: str
    extractor: Callable[[Path], Mapping[str, MetadataValue]]
    extensions: frozenset[str]
    mime_types: frozenset[str]
    priority: int


@dataclass(frozen=True, slots=True)
class PluginLoadFailure:
    """A plugin entry point that could not be loaded or registered."""

    name: str
    message: str


class PluginRegistry:
    """Register and dispatch community preview and metadata plugins.

    Plugins are normal Python packages that expose a ``register(registry)``
    function through the ``tagstudio.plugins`` entry-point group. Applications
    and tests may also register handlers directly without entry-point discovery.
    A handler failure is isolated to that handler so a broken optional plugin
    cannot prevent built-in rendering or other extractors from running.
    """

    def __init__(self) -> None:
        self._preview_handlers: list[PreviewHandlerRegistration] = []
        self._metadata_extractors: list[MetadataExtractorRegistration] = []
        self.load_failures: list[PluginLoadFailure] = []
        self.loaded_plugins: list[str] = []

    @staticmethod
    def _normalize_extensions(extensions: Iterable[str]) -> frozenset[str]:
        normalized: set[str] = set()
        for extension in extensions:
            value = extension.strip().lower()
            if not value:
                continue
            normalized.add(value if value.startswith(".") else f".{value}")
        return frozenset(normalized)

    @staticmethod
    def _normalize_mime_types(mime_types: Iterable[str]) -> frozenset[str]:
        return frozenset(mime_type.strip().lower() for mime_type in mime_types if mime_type.strip())

    @staticmethod
    def _validate_name(name: str) -> str:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Plugin registration names must not be empty")
        return normalized

    def register_preview_handler(
        self,
        name: str,
        handler: Callable[[PreviewRequest], Image.Image | PreviewResult | None],
        *,
        extensions: Iterable[str] = (),
        mime_types: Iterable[str] = (),
        priority: int = 0,
    ) -> PreviewHandlerRegistration:
        """Register a preview handler and return its immutable registration."""
        registration = PreviewHandlerRegistration(
            name=self._validate_name(name),
            handler=handler,
            extensions=self._normalize_extensions(extensions),
            mime_types=self._normalize_mime_types(mime_types),
            priority=priority,
        )
        if any(item.name == registration.name for item in self._preview_handlers):
            raise ValueError(f"Preview handler already registered: {registration.name}")
        self._preview_handlers.append(registration)
        return registration

    def register_metadata_extractor(
        self,
        name: str,
        extractor: Callable[[Path], Mapping[str, MetadataValue]],
        *,
        extensions: Iterable[str] = (),
        mime_types: Iterable[str] = (),
        priority: int = 0,
    ) -> MetadataExtractorRegistration:
        """Register a metadata extractor and return its immutable registration."""
        registration = MetadataExtractorRegistration(
            name=self._validate_name(name),
            extractor=extractor,
            extensions=self._normalize_extensions(extensions),
            mime_types=self._normalize_mime_types(mime_types),
            priority=priority,
        )
        if any(item.name == registration.name for item in self._metadata_extractors):
            raise ValueError(f"Metadata extractor already registered: {registration.name}")
        self._metadata_extractors.append(registration)
        return registration

    @staticmethod
    def _matches(path: Path, extensions: frozenset[str], mime_types: frozenset[str]) -> bool:
        if not extensions and not mime_types:
            return True
        path_name = path.name.lower()
        if any(path_name.endswith(extension) for extension in extensions):
            return True
        mime_type = mimetypes.guess_type(path.name, strict=False)[0]
        return mime_type is not None and mime_type.lower() in mime_types

    def render_preview(self, request: PreviewRequest) -> PreviewResult | None:
        """Render with the highest-priority matching plugin, if any."""
        handlers = sorted(
            (
                item
                for item in self._preview_handlers
                if self._matches(request.path, item.extensions, item.mime_types)
            ),
            key=lambda item: (-item.priority, item.name),
        )
        for registration in handlers:
            try:
                result = registration.handler(request)
                if result is None:
                    continue
                if isinstance(result, Image.Image):
                    return PreviewResult(result)
                if isinstance(result, PreviewResult):
                    return result
                raise TypeError(
                    f"Preview handler {registration.name!r} returned an unsupported result"
                )
            except Exception as error:  # plugin failures must not break built-in rendering
                logger.exception(
                    "[Plugins] Preview handler failed", plugin=registration.name, error=error
                )
        return None

    def extract_metadata(self, path: Path) -> dict[str, MetadataValue]:
        """Merge metadata from all matching extractors, with priority wins."""
        extractors = sorted(
            (
                item
                for item in self._metadata_extractors
                if self._matches(path, item.extensions, item.mime_types)
            ),
            key=lambda item: (-item.priority, item.name),
        )
        values: dict[str, MetadataValue] = {}
        for registration in extractors:
            try:
                extracted = registration.extractor(path)
                for key, value in extracted.items():
                    values.setdefault(key, value)
            except Exception as error:  # plugin failures must not break other extractors
                logger.exception(
                    "[Plugins] Metadata extractor failed",
                    plugin=registration.name,
                    error=error,
                )
        return values

    def load_entry_points(self) -> tuple[str, ...]:
        """Load and register all installed ``tagstudio.plugins`` packages."""
        entry_points = importlib_metadata.entry_points(group=PLUGIN_ENTRY_POINT_GROUP)

        loaded: list[str] = []
        for entry_point in sorted(entry_points, key=lambda item: item.name):
            try:
                plugin = entry_point.load()
                register = getattr(plugin, "register", plugin)
                if not callable(register):
                    raise TypeError("entry point must expose a callable register(registry)")
                register(self)
                loaded.append(entry_point.name)
            except Exception as error:  # one broken optional package must not stop startup
                logger.exception(
                    "[Plugins] Could not load plugin", plugin=entry_point.name, error=error
                )
                self.load_failures.append(
                    PluginLoadFailure(name=entry_point.name, message=str(error))
                )
        self.loaded_plugins.extend(loaded)
        return tuple(loaded)

    @property
    def preview_handlers(self) -> tuple[PreviewHandlerRegistration, ...]:
        """Return registered preview handlers for diagnostics and tests."""
        return tuple(self._preview_handlers)

    @property
    def metadata_extractors(self) -> tuple[MetadataExtractorRegistration, ...]:
        """Return registered metadata extractors for diagnostics and tests."""
        return tuple(self._metadata_extractors)
