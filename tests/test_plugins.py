# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

from pathlib import Path

import pytest
from PIL import Image

from tagstudio.core.plugins import PluginRegistry, PreviewRequest, PreviewResult


def _request(path: Path) -> PreviewRequest:
    return PreviewRequest(path=path, size=(64, 64), pixel_ratio=1, is_grid_thumb=True)


def test_plugin_registry_dispatches_preview_by_priority_and_normalizes_extensions(tmp_path: Path):
    registry = PluginRegistry()
    registry.register_preview_handler(
        "low",
        lambda _request: Image.new("RGBA", (4, 4), "red"),
        extensions=["PSD"],
        priority=1,
    )
    registry.register_preview_handler(
        "high",
        lambda _request: PreviewResult(Image.new("RGBA", (8, 8), "blue"), cacheable=False),
        extensions=[".psd"],
        priority=10,
    )

    result = registry.render_preview(_request(tmp_path / "art.PSD"))

    assert result is not None
    assert result.image.size == (8, 8)
    assert not result.cacheable


def test_plugin_registry_isolates_preview_failures_and_merges_metadata(tmp_path: Path):
    registry = PluginRegistry()

    def broken(_request):
        raise RuntimeError("broken plugin")

    registry.register_preview_handler("broken", broken, extensions=[".dcm"], priority=20)
    registry.register_preview_handler(
        "fallback",
        lambda _request: Image.new("RGBA", (2, 2), "green"),
        extensions=[".dcm"],
        priority=1,
    )
    registry.register_metadata_extractor(
        "low-metadata",
        lambda _path: {"title": "low", "author": "plugin"},
        extensions=[".dcm"],
        priority=1,
    )
    registry.register_metadata_extractor(
        "high-metadata",
        lambda _path: {"title": "high", "description": "details"},
        extensions=[".dcm"],
        priority=10,
    )

    result = registry.render_preview(_request(tmp_path / "scan.DCM"))
    metadata = registry.extract_metadata(tmp_path / "scan.DCM")

    assert result is not None
    assert result.image.size == (2, 2)
    assert metadata == {
        "title": "high",
        "description": "details",
        "author": "plugin",
    }


def test_plugin_registry_loads_entry_point_register_function(monkeypatch):
    class EntryPoint:
        name = "example"

        @staticmethod
        def load():
            def register(registry: PluginRegistry):
                registry.register_metadata_extractor(
                    "example-metadata", lambda _path: {"source": "example"}
                )

            return register

    monkeypatch.setattr(
        "tagstudio.core.plugins.importlib_metadata.entry_points",
        lambda **_kwargs: [EntryPoint()],
    )
    registry = PluginRegistry()

    assert registry.load_entry_points() == ("example",)
    assert registry.loaded_plugins == ["example"]
    assert registry.extract_metadata(Path("anything.bin")) == {"source": "example"}


def test_plugin_registry_rejects_duplicate_registration_names():
    registry = PluginRegistry()
    registry.register_metadata_extractor("same", lambda _path: {})

    with pytest.raises(ValueError, match="already registered"):
        registry.register_metadata_extractor("same", lambda _path: {})
