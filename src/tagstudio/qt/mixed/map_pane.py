# Copyright (C) 2025 Travis Abendshien (CyanVoxel).
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio


"""Map pane shared by the main window and future location-focused views."""

import json
from collections.abc import Iterable

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from tagstudio.core.media_metadata import GeoPoint
from tagstudio.qt.translations import Translations

MAP_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css">
  <script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
  <style>
    html, body, #map { width: 100%; height: 100%; margin: 0; background: #1e1e1e; }
    #map .maplibregl-ctrl-attrib { background: rgba(30, 30, 30, .78); color: #d8d8d8; }
    #map .maplibregl-ctrl-attrib a { color: #9fc1ff; }
  </style>
</head>
<body>
<div id="map"></div>
<script>
let map = null;
let pendingPoints = [];
let pointClickBound = false;

function removeLayers() {
  if (!map) return;
  for (const layer of ["point-labels", "points", "cluster-count", "clusters"]) {
    if (map.getLayer(layer)) map.removeLayer(layer);
  }
  if (map.getSource("points")) map.removeSource("points");
}

function renderPoints(points) {
  pendingPoints = points || [];
  if (!map || !map.isStyleLoaded()) return;
  removeLayers();
  if (!pendingPoints.length) return;

  const colors = [...new Set(pendingPoints.map((point) => point.properties.color))].slice(0, 12);
  const clusterProperties = {};
  colors.forEach((color, index) => {
    clusterProperties[`tagColor${index}`] = [
      "+", ["case", ["==", ["get", "color"], color], 1, 0], 0
    ];
  });
  const clusterColor = ["case"];
  colors.forEach((color, index) => {
    clusterColor.push([">", ["get", `tagColor${index}`], 0], color);
  });
  clusterColor.push("#4f7cff");

  map.addSource("points", {
    type: "geojson",
    data: {type: "FeatureCollection", features: pendingPoints},
    cluster: true,
    clusterRadius: 48,
    clusterMaxZoom: 14,
    clusterProperties: clusterProperties,
  });
  map.addLayer({
    id: "clusters", type: "circle", source: "points", filter: ["has", "point_count"],
    paint: {
      "circle-color": clusterColor,
      "circle-radius": ["step", ["get", "point_count"], 18, 10, 24, 50, 30],
      "circle-opacity": .86
    }
  });
  map.addLayer({
    id: "cluster-count", type: "symbol", source: "points", filter: ["has", "point_count"],
    layout: {"text-field": "{point_count_abbreviated}", "text-size": 12},
    paint: {"text-color": "#ffffff"}
  });
  map.addLayer({
    id: "points", type: "circle", source: "points", filter: ["!", ["has", "point_count"]],
    paint: {
      "circle-color": ["get", "color"],
      "circle-radius": ["case", ["get", "selected"], 9, 7],
      "circle-stroke-color": "#ffffff",
      "circle-stroke-width": ["case", ["get", "selected"], 3, 1]
    }
  });
  if (!pointClickBound) {
    map.on("click", "points", (event) => {
      const feature = event.features && event.features[0];
      if (!feature) return;
      const popup = new maplibregl.Popup();
      popup.setLngLat(feature.geometry.coordinates).setText(feature.properties.path).addTo(map);
    });
    pointClickBound = true;
  }

  const bounds = new maplibregl.LngLatBounds();
  pendingPoints.forEach((point) => bounds.extend(point.geometry.coordinates));
  if (pendingPoints.length === 1) {
    map.flyTo({center: pendingPoints[0].geometry.coordinates, zoom: 12});
  }
  else map.fitBounds(bounds, {padding: 42, maxZoom: 14});
}

function initializeMap() {
  if (typeof maplibregl === "undefined") return;
  map = new maplibregl.Map({
    container: "map",
    style: {
      version: 8,
      sources: {
        osm: {
          type: "raster",
          tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
          tileSize: 256,
          attribution: "© OpenStreetMap contributors"
        }
      },
      layers: [{id: "osm", type: "raster", source: "osm"}]
    },
    center: [0, 20],
    zoom: 1.2,
    attributionControl: true
  });
  map.on("load", () => renderPoints(pendingPoints));
}

window.renderPoints = renderPoints;
window.addEventListener("load", initializeMap);
</script>
</body>
</html>
"""


class MapPane(QWidget):
    """Display EXIF locations with MapLibre clustering and tag-colored markers."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._points: list[GeoPoint] = []
        self._selected: set[int] = set()

        self.setObjectName("map_panel")
        self.setMinimumWidth(260)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 0, 0, 0)
        layout.setSpacing(6)

        self.title_label = QLabel(Translations["map.title"])
        self.title_label.setObjectName("map_title")
        self.status_label = QLabel(Translations["map.loading"])
        self.status_label.setObjectName("map_status")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.map_view = QWebEngineView()
        self.map_view.setObjectName("map_view")
        self.map_view.loadFinished.connect(self._on_map_loaded)
        self.map_view.setHtml(MAP_HTML, QUrl("https://tagstudio.invalid/map/"))

        layout.addWidget(self.title_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.map_view, 1)

    def _on_map_loaded(self, loaded: bool) -> None:
        if not loaded:
            self.status_label.setText(Translations["map.unavailable"])
            return
        self._render_points()

    def set_points(self, points: Iterable[GeoPoint]) -> None:
        self._points = list(points)
        self.status_label.setText(
            Translations.format("map.locations", count=len(self._points))
            if self._points
            else Translations["map.no_locations"]
        )
        self._render_points()

    def set_selected(self, entry_ids: Iterable[int]) -> None:
        self._selected = set(entry_ids)
        self._render_points()

    def clear(self) -> None:
        self._points.clear()
        self._selected.clear()
        self.status_label.setText(Translations["map.no_locations"])
        self._render_points()

    def _render_points(self) -> None:
        features = [point.as_feature(point.entry_id in self._selected) for point in self._points]
        script = f"window.renderPoints({json.dumps(features, separators=(',', ':'))});"
        self.map_view.page().runJavaScript(script)
