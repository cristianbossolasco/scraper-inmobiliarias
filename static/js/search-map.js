(() => {
  let map;
  let popup;
  let drawMode = null;
  let polygonPoints = [];
  let radiusCenter = null;
  const form = document.getElementById("search-form");
  if (!form || typeof maplibregl === "undefined") return;

  const input = (name) => form.querySelector(`[name="${name}"]`);
  const query = () => new URLSearchParams(new FormData(form)).toString();
  const workspace = document.querySelector(".workspace");
  const filtersToggle = document.getElementById("filters-toggle");
  const mapModeButton = document.getElementById("map-mode");
  const resizer = document.getElementById("map-resizer");
  const layoutStorage = {
    filters: "radar.filtersCollapsed",
    results: "radar.resultsColumnWidth",
    mapMode: "radar.mapMode"
  };

  const POLYGON_CLOSE_METERS = 24;
  let mapModeEnabled = false;
  let radiusButton;
  let polygonButton;
  let clearGeoButton;
  let lastMapFeatures = [];
  let geoStatus = null;

  function markFiltersPending() {
    form.dataset.pending = "1";
    const apply = form.querySelector('button[type="submit"]');
    if (apply) {
      apply.classList.add("needs-apply");
      apply.title = "Hay cambios pendientes. Presiona Aplicar para buscar.";
    }
  }

  applyStoredLayout();
  applyStoredMapMode();

  fetch("/api/configuracion-mapa/").then((r) => r.json()).then((config) => {
    map = new maplibregl.Map({
      container: "map",
      style: {
        version: 8,
        sources: {
          osm: {
            type: "raster",
            tiles: [config.tile_url],
            tileSize: 256,
            attribution: config.attribution
          }
        },
        layers: [{ id: "osm", type: "raster", source: "osm" }]
      },
      center: config.center,
      zoom: config.zoom,
      maxBounds: [
        [config.bounds.west - 0.08, config.bounds.south - 0.08],
        [config.bounds.east + 0.08, config.bounds.north + 0.08]
      ]
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.on("load", () => {
      installPropertyLayers();
      installGeometryLayers();
      restoreGeometryFromInputs();
      refreshMap();
    });
    map.on("click", handleMapClick);
    map.on("dblclick", handleMapDoubleClick);
    map.on("click", "clusters", expandCluster);
    map.on("click", "exact-points", showPopup);
    map.on("click", "approximate-points", showPopup);
    ["exact-points", "approximate-points", "clusters"].forEach((layer) => {
      map.on("mouseenter", layer, () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", layer, () => {
        map.getCanvas().style.cursor = drawMode ? "crosshair" : "";
      });
    });
  });

  function installPropertyLayers() {
    map.addSource("properties", {
      type: "geojson",
      data: { type: "FeatureCollection", features: [] },
      cluster: true,
      clusterMaxZoom: 14,
      clusterRadius: 45
    });
    map.addLayer({
      id: "clusters", type: "circle", source: "properties",
      filter: ["has", "point_count"],
      paint: {
        "circle-color": "#17211d",
        "circle-radius": ["step", ["get", "point_count"], 17, 20, 22, 80, 28],
        "circle-stroke-width": 2, "circle-stroke-color": "#ffffff"
      }
    });
    map.addLayer({
      id: "cluster-count", type: "symbol", source: "properties",
      filter: ["has", "point_count"],
      layout: { "text-field": ["get", "point_count_abbreviated"], "text-size": 12 },
      paint: { "text-color": "#ffffff" }
    });
    map.addLayer({
      id: "approximate-halo", type: "circle", source: "properties",
      filter: ["all", ["!", ["has", "point_count"]], ["==", ["get", "exact"], false]],
      paint: { "circle-color": "rgba(214,165,40,.25)", "circle-radius": 13 }
    });
    map.addLayer({
      id: "approximate-points", type: "circle", source: "properties",
      filter: ["all", ["!", ["has", "point_count"]], ["==", ["get", "exact"], false]],
      paint: {
        "circle-color": "#d6a528", "circle-radius": 7,
        "circle-stroke-width": 2, "circle-stroke-color": "#ffffff"
      }
    });
    map.addLayer({
      id: "exact-points", type: "circle", source: "properties",
      filter: ["all", ["!", ["has", "point_count"]], ["==", ["get", "exact"], true]],
      paint: {
        "circle-color": "#176b4d", "circle-radius": 7,
        "circle-stroke-width": 2, "circle-stroke-color": "#ffffff"
      }
    });
  }

  function installGeometryLayers() {
    map.addSource("selection-geometry", {
      type: "geojson",
      data: { type: "FeatureCollection", features: [] }
    });
    map.addLayer({
      id: "selection-fill", type: "fill", source: "selection-geometry",
      filter: ["==", ["geometry-type"], "Polygon"],
      paint: { "fill-color": "#176b4d", "fill-opacity": 0.14 }
    });
    map.addLayer({
      id: "selection-line", type: "line", source: "selection-geometry",
      paint: { "line-color": "#176b4d", "line-width": 2, "line-dasharray": [2, 1] }
    });
  }

  function refreshMap() {
    if (!map || !map.getSource("properties")) return;
    const params = new URLSearchParams(new FormData(form));
    params.delete("page");
    fetch(`/api/propiedades/?${params}`).then((r) => r.json()).then((data) => {
      lastMapFeatures = data.features || [];
      map.getSource("properties").setData(data);
      if (!lastMapFeatures.length) return;
      if (!mapModeEnabled) return;
      fitMapToFeatures(lastMapFeatures, { duration: 0 });
    });
  }

  function featureCoordinates(features) {
    return (features || [])
      .map((feature) => feature?.geometry?.coordinates)
      .filter((coordinates) => Array.isArray(coordinates)
        && Number.isFinite(Number(coordinates[0]))
        && Number.isFinite(Number(coordinates[1])));
  }

  function trimCoordinateOutliers(coordinates) {
    if (coordinates.length < 25) return coordinates;
    const longitudes = coordinates.map(([longitude]) => longitude).sort((a, b) => a - b);
    const latitudes = coordinates.map(([, latitude]) => latitude).sort((a, b) => a - b);
    const lowerIndex = Math.floor(coordinates.length * 0.04);
    const upperIndex = Math.max(lowerIndex, Math.ceil(coordinates.length * 0.96) - 1);
    const minLongitude = longitudes[lowerIndex];
    const maxLongitude = longitudes[upperIndex];
    const minLatitude = latitudes[lowerIndex];
    const maxLatitude = latitudes[upperIndex];
    const trimmed = coordinates.filter(([longitude, latitude]) =>
      longitude >= minLongitude
      && longitude <= maxLongitude
      && latitude >= minLatitude
      && latitude <= maxLatitude
    );
    return trimmed.length >= 3 ? trimmed : coordinates;
  }

  function boundsFromCoordinates(coordinates) {
    const bounds = new maplibregl.LngLatBounds();
    coordinates.forEach((coordinatesPair) => bounds.extend(coordinatesPair));
    return bounds;
  }

  function fitMapToFeatures(features, options = {}) {
    if (!map) return;
    const coordinates = trimCoordinateOutliers(featureCoordinates(features));
    if (!coordinates.length) return;
    const bounds = boundsFromCoordinates(coordinates);
    if (bounds.isEmpty()) return;
    const padding = options.padding || {
      top: 76,
      right: 76,
      bottom: 76,
      left: 76
    };
    const fit = () => {
      map.resize();
      map.fitBounds(bounds, {
        padding,
        maxZoom: options.maxZoom || 15,
        duration: options.duration ?? 450
      });
    };
    requestAnimationFrame(() => {
      fit();
      setTimeout(fit, 90);
    });
  }

  function showPopup(event) {
    const feature = event.features[0];
    const p = feature.properties;
    const price = p.price
      ? `${p.currency} ${Number(p.price).toLocaleString("es-AR")}`
      : "Consultar";
    if (popup) popup.remove();
    popup = new maplibregl.Popup({ offset: 12 })
      .setLngLat(feature.geometry.coordinates)
      .setHTML(
        `<div class="map-popup"><strong>${price}</strong>` +
        `<p>${escapeHtml(p.title)}</p><small>${escapeHtml(p.precision_label)}</small>` +
        `<br><a href="${p.detail_url}">Ver propiedad</a></div>`
      ).addTo(map);
  }

  function expandCluster(event) {
    const feature = event.features[0];
    map.getSource("properties")
      .getClusterExpansionZoom(feature.properties.cluster_id)
      .then((zoom) => map.easeTo({ center: feature.geometry.coordinates, zoom }));
  }

  function distanceInMeters(pointA, pointB) {
    const degreesToRadians = (value) => value * (Math.PI / 180);
    const [lngA, latA] = pointA;
    const [lngB, latB] = pointB;
    const dLat = degreesToRadians(latB - latA);
    const dLng = degreesToRadians(lngB - lngA);
    const latAcos = Math.cos(degreesToRadians(latA));
    const latBcos = Math.cos(degreesToRadians(latB));
    const haversine = Math.sin(dLat / 2) ** 2 + latAcos * latBcos * Math.sin(dLng / 2) ** 2;
    return 2 * 6371000 * Math.asin(Math.min(1, Math.sqrt(haversine)));
  }

  function isNearFirstPoint(point) {
    if (polygonPoints.length < 3) return false;
    return distanceInMeters(point, polygonPoints[0]) <= POLYGON_CLOSE_METERS;
  }

  function handleMapClick(event) {
    if (drawMode === "radius") {
      radiusCenter = [event.lngLat.lng, event.lngLat.lat];
      input("radius_lng").value = radiusCenter[0];
      input("radius_lat").value = radiusCenter[1];
      drawRadius();
      setDrawMode(null);
      markFiltersPending();
      return;
    }
    if (drawMode === "polygon") {
      const current = [event.lngLat.lng, event.lngLat.lat];
      if (isNearFirstPoint(current)) {
        finishPolygon();
        return;
      }
      polygonPoints.push(current);
      drawPolygon(false);
    }
  }

  function handleMapDoubleClick(event) {
    if (drawMode !== "polygon") return;
    event.preventDefault();
    finishPolygon();
  }

  function drawPolygon(closed) {
    if (polygonPoints.length < 2) return;
    const coordinates = closed ? [...polygonPoints, polygonPoints[0]] : polygonPoints;
    const geometry = closed
      ? { type: "Polygon", coordinates: [coordinates] }
      : { type: "LineString", coordinates };
    map.getSource("selection-geometry").setData({
      type: "FeatureCollection",
      features: [{ type: "Feature", geometry }]
    });
  }

  function finishPolygon() {
    if (polygonPoints.length < 3) {
      setGeoStatus("El area necesita al menos 3 puntos.", false);
      return;
    }
    const polygon = [...polygonPoints];
    input("polygon").value = JSON.stringify(polygon);
    drawPolygon(true);
    drawMode = null;
    if (polygonButton) polygonButton.classList.remove("active");
    document.getElementById("radius-panel").hidden = true;
    if (map) {
      updateCursorForMode();
      map.doubleClickZoom.enable();
    }
    polygonPoints = polygon;
    drawPolygon(true);
    setGeoStatus("Area lista. Aplicando filtro...", true);
    if (form.requestSubmit) {
      form.requestSubmit();
    } else {
      form.submit();
    }
  }

  function clearDrawnGeometry(keepMode = null) {
    polygonPoints = [];
    radiusCenter = null;
    drawMode = keepMode;
    map.getSource("selection-geometry").setData({
      type: "FeatureCollection",
      features: []
    });
    if (polygonButton) polygonButton.classList.toggle("active", keepMode === "polygon");
    if (radiusButton) radiusButton.classList.toggle("active", keepMode === "radius");
    document.getElementById("radius-panel").hidden = keepMode !== "radius";
    if (map) {
      updateCursorForMode();
      map.doubleClickZoom[keepMode === "polygon" ? "disable" : "enable"]();
    }
  }

  function setDrawMode(nextMode) {
    if (nextMode === "radius") {
      clearDrawnGeometry("radius");
      return;
    }
    if (nextMode === "polygon") {
      polygonPoints = [];
      clearDrawnGeometry("polygon");
      return;
    }
    clearDrawnGeometry(null);
  }

  function updateCursorForMode() {
    if (!map) return;
    map.getCanvas().style.cursor = drawMode === "radius" || drawMode === "polygon" ? "crosshair" : "";
  }

  function drawRadius() {
    if (!radiusCenter) return;
    const radius = Number(input("radius_km").value || 2);
    const points = [];
    for (let i = 0; i <= 64; i += 1) {
      const angle = (i / 64) * Math.PI * 2;
      const lat = radiusCenter[1] + (radius / 111.32) * Math.sin(angle);
      const lng = radiusCenter[0] +
        (radius / (111.32 * Math.cos(radiusCenter[1] * Math.PI / 180))) * Math.cos(angle);
      points.push([lng, lat]);
    }
    map.getSource("selection-geometry").setData({
      type: "FeatureCollection",
      features: [{ type: "Feature", geometry: { type: "Polygon", coordinates: [points] } }]
    });
  }

  function ensureGeoStatus() {
    if (geoStatus && document.body.contains(geoStatus)) return geoStatus;
    const tools = document.querySelector(".geo-tools");
    if (!tools) return null;
    geoStatus = document.createElement("span");
    geoStatus.className = "geo-status";
    tools.appendChild(geoStatus);
    return geoStatus;
  }

  function setGeoStatus(message, active) {
    const status = ensureGeoStatus();
    if (!status) return;
    status.textContent = message || "";
    status.hidden = !message;
    status.classList.toggle("active", Boolean(active));
  }

  function restoreGeometryFromInputs() {
    const rawPolygon = input("polygon")?.value;
    if (rawPolygon) {
      try {
        const parsed = JSON.parse(rawPolygon);
        if (Array.isArray(parsed) && parsed.length >= 3) {
          polygonPoints = parsed;
          drawPolygon(true);
          setGeoStatus("Area aplicada", true);
          return;
        }
      } catch (_error) {
        setGeoStatus("No se pudo restaurar el area.", false);
      }
    }
    const lat = Number(input("radius_lat")?.value);
    const lng = Number(input("radius_lng")?.value);
    if (Number.isFinite(lat) && Number.isFinite(lng)) {
      radiusCenter = [lng, lat];
      drawRadius();
      setGeoStatus("Radio aplicado", true);
    }
  }

  document.addEventListener("htmx:afterSwap", (event) => {
    if (event.target.id === "results-pane") {
      lucide.createIcons();
      bindResultTools();
      restoreGeometryFromInputs();
      refreshMap();
    }
  });
  form.addEventListener("submit", () => {
    form.dataset.pending = "";
    const apply = form.querySelector('button[type="submit"]');
    if (apply) {
      apply.classList.remove("needs-apply");
      apply.removeAttribute("title");
    }
    setTimeout(refreshMap, 100);
  });

  function bindResultTools() {
    const boundsButton = document.getElementById("bounds-filter");
    radiusButton = document.getElementById("radius-tool");
    polygonButton = document.getElementById("polygon-tool");
    clearGeoButton = document.getElementById("clear-geo");
    if (!boundsButton || boundsButton.dataset.bound) return;
    [boundsButton, radiusButton, polygonButton, clearGeoButton].forEach((button) => {
      button.dataset.bound = "1";
    });

    boundsButton.addEventListener("click", () => {
      const bounds = map.getBounds();
      input("south").value = bounds.getSouth();
      input("west").value = bounds.getWest();
      input("north").value = bounds.getNorth();
      input("east").value = bounds.getEast();
      markFiltersPending();
    });

    radiusButton.addEventListener("click", (event) => {
      if (drawMode === "radius") {
        setDrawMode(null);
      } else {
        setDrawMode("radius");
      }
      event.currentTarget.classList.toggle("active", drawMode === "radius");
      document.getElementById("radius-panel").hidden =
        drawMode !== "radius" && !radiusCenter;
      updateCursorForMode();
    });

    polygonButton.addEventListener("click", (event) => {
      if (drawMode === "polygon") {
        if (polygonPoints.length >= 3) {
          finishPolygon();
        } else {
          setDrawMode(null);
        }
        return;
      }
      setDrawMode("polygon");
      event.currentTarget.classList.add("active");
      updateCursorForMode();
    });

    clearGeoButton.addEventListener("click", () => {
      ["south", "west", "north", "east", "radius_lat", "radius_lng", "polygon"]
        .forEach((name) => { input(name).value = ""; });
      setDrawMode(null);
      document.getElementById("radius-panel").hidden = true;
      setGeoStatus("", false);
      markFiltersPending();
    });
  }

  document.getElementById("radius-input").addEventListener("input", (event) => {
    input("radius_km").value = event.target.value;
    document.getElementById("radius-output").value = `${event.target.value} km`;
    drawRadius();
    markFiltersPending();
  });

  document.getElementById("clear-filters").addEventListener("click", () => {
    window.location.href = "/";
  });

  mapModeButton.addEventListener("click", () => {
    applyMapMode(!mapModeEnabled);
  });

  filtersToggle.addEventListener("click", () => {
    const collapsed = !document.body.classList.contains("filters-collapsed");
    setFiltersCollapsed(collapsed);
    localStorage.setItem(layoutStorage.filters, collapsed ? "1" : "0");
    resizeMapSoon();
  });

  installResizableMap();

  document.getElementById("fit-map").addEventListener("click", () => {
    fetch(`/api/propiedades/?${query()}`).then((r) => r.json()).then((data) => {
      lastMapFeatures = data.features || [];
      fitMapToFeatures(lastMapFeatures);
    });
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !drawMode) return;
    if (event.target.tagName === "INPUT" || event.target.tagName === "TEXTAREA") return;
    setDrawMode(null);
  });

  function applyMapMode(enabled) {
    mapModeEnabled = enabled;
    document.body.classList.toggle("map-full", enabled);
    localStorage.setItem(layoutStorage.mapMode, enabled ? "1" : "0");
    if (!map) return;
    requestAnimationFrame(() => {
      map.resize();
      setTimeout(() => map.resize(), 80);
      setTimeout(() => {
        map.resize();
        if (enabled && lastMapFeatures.length) {
          fitMapToFeatures(lastMapFeatures, { duration: 0 });
        }
      }, 180);
    });
  }

  function applyStoredMapMode() {
    applyMapMode(localStorage.getItem(layoutStorage.mapMode) === "1");
  }

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value || "";
    return div.innerHTML;
  }

  function applyStoredLayout() {
    setFiltersCollapsed(localStorage.getItem(layoutStorage.filters) === "1");
    const storedWidth = Number(localStorage.getItem(layoutStorage.results) || 0);
    if (storedWidth > 0) {
      workspace.style.setProperty("--results-col", `${storedWidth}px`);
    }
  }

  function setFiltersCollapsed(collapsed) {
    document.body.classList.toggle("filters-collapsed", collapsed);
    filtersToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    filtersToggle.setAttribute("title", collapsed ? "Mostrar filtros" : "Ocultar filtros");
    filtersToggle.setAttribute("aria-label", collapsed ? "Mostrar filtros" : "Ocultar filtros");
    filtersToggle.innerHTML = `<i data-lucide="${collapsed ? "panel-left-open" : "panel-left-close"}"></i>`;
    lucide.createIcons();
  }

  function installResizableMap() {
    if (!resizer || !workspace) return;
    let dragging = false;
    resizer.addEventListener("pointerdown", (event) => {
      if (window.matchMedia("(max-width: 760px)").matches) return;
      dragging = true;
      resizer.setPointerCapture(event.pointerId);
      document.body.classList.add("resizing-map");
    });
    resizer.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      const results = document.getElementById("results-pane");
      const mapPanel = document.querySelector(".map-panel");
      const left = results.getBoundingClientRect().left;
      const mapRight = mapPanel.getBoundingClientRect().right;
      const minResults = 340;
      const minMap = 360;
      const width = Math.max(minResults, Math.min(event.clientX - left, mapRight - left - minMap));
      workspace.style.setProperty("--results-col", `${Math.round(width)}px`);
      localStorage.setItem(layoutStorage.results, String(Math.round(width)));
      resizeMapSoon();
    });
    resizer.addEventListener("pointerup", (event) => {
      if (!dragging) return;
      dragging = false;
      resizer.releasePointerCapture(event.pointerId);
      document.body.classList.remove("resizing-map");
      resizeMapSoon();
    });
    resizer.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const current = Number(localStorage.getItem(layoutStorage.results) || document.getElementById("results-pane").getBoundingClientRect().width);
      const next = current + (event.key === "ArrowLeft" ? -40 : 40);
      workspace.style.setProperty("--results-col", `${Math.max(340, next)}px`);
      localStorage.setItem(layoutStorage.results, String(Math.max(340, next)));
      resizeMapSoon();
    });
  }

  function resizeMapSoon() {
    if (!map) return;
    setTimeout(() => map.resize(), 80);
  }

  bindResultTools();
  lucide.createIcons();
})();
