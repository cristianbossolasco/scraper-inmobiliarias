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
  const submit = () => htmx.trigger(form, "submit");
  const workspace = document.querySelector(".workspace");
  const filtersToggle = document.getElementById("filters-toggle");
  const resizer = document.getElementById("map-resizer");
  const layoutStorage = {
    filters: "radar.filtersCollapsed",
    results: "radar.resultsColumnWidth"
  };

  applyStoredLayout();

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
      refreshMap();
    });
    map.on("click", handleMapClick);
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
      map.getSource("properties").setData(data);
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

  function handleMapClick(event) {
    if (drawMode === "radius") {
      radiusCenter = [event.lngLat.lng, event.lngLat.lat];
      input("radius_lng").value = radiusCenter[0];
      input("radius_lat").value = radiusCenter[1];
      drawRadius();
      drawMode = null;
      document.getElementById("radius-tool").classList.remove("active");
      submit();
      return;
    }
    if (drawMode === "polygon") {
      polygonPoints.push([event.lngLat.lng, event.lngLat.lat]);
      drawPolygon(false);
    }
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
    if (polygonPoints.length < 3) return;
    input("polygon").value = JSON.stringify(polygonPoints);
    drawPolygon(true);
    drawMode = null;
    document.getElementById("polygon-tool").classList.remove("active");
    map.getCanvas().style.cursor = "";
    submit();
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

  document.addEventListener("htmx:afterSwap", (event) => {
    if (event.target.id === "results-pane") {
      lucide.createIcons();
      bindResultTools();
      refreshMap();
    }
  });
  form.addEventListener("submit", () => setTimeout(refreshMap, 100));

  function bindResultTools() {
    const boundsButton = document.getElementById("bounds-filter");
    const radiusButton = document.getElementById("radius-tool");
    const polygonButton = document.getElementById("polygon-tool");
    const clearButton = document.getElementById("clear-geo");
    if (!boundsButton || boundsButton.dataset.bound) return;
    [boundsButton, radiusButton, polygonButton, clearButton].forEach((button) => {
      button.dataset.bound = "1";
    });
    boundsButton.addEventListener("click", () => {
      const bounds = map.getBounds();
      input("south").value = bounds.getSouth();
      input("west").value = bounds.getWest();
      input("north").value = bounds.getNorth();
      input("east").value = bounds.getEast();
      submit();
    });
    radiusButton.addEventListener("click", (event) => {
      drawMode = drawMode === "radius" ? null : "radius";
      event.currentTarget.classList.toggle("active", drawMode === "radius");
      document.getElementById("radius-panel").hidden =
        drawMode !== "radius" && !radiusCenter;
      map.getCanvas().style.cursor = drawMode ? "crosshair" : "";
    });
    polygonButton.addEventListener("click", (event) => {
      if (drawMode === "polygon" && polygonPoints.length >= 3) {
        finishPolygon();
        return;
      }
      polygonPoints = [];
      drawMode = "polygon";
      event.currentTarget.classList.add("active");
      map.getCanvas().style.cursor = "crosshair";
    });
    clearButton.addEventListener("click", () => {
      ["south", "west", "north", "east", "radius_lat", "radius_lng", "polygon"]
        .forEach((name) => { input(name).value = ""; });
      radiusCenter = null;
      polygonPoints = [];
      map.getSource("selection-geometry").setData({
        type: "FeatureCollection",
        features: []
      });
      document.getElementById("radius-panel").hidden = true;
      submit();
    });
  }
  document.getElementById("radius-input").addEventListener("input", (event) => {
    input("radius_km").value = event.target.value;
    document.getElementById("radius-output").value = `${event.target.value} km`;
    drawRadius();
  });
  document.getElementById("radius-input").addEventListener("change", submit);
  document.getElementById("clear-filters").addEventListener("click", () => {
    window.location.href = "/";
  });
  document.getElementById("map-mode").addEventListener("click", () => {
    document.body.classList.toggle("map-full");
    setTimeout(() => map.resize(), 180);
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
      if (!data.features.length) return;
      const bounds = new maplibregl.LngLatBounds();
      data.features.forEach((feature) => bounds.extend(feature.geometry.coordinates));
      map.fitBounds(bounds, { padding: 45, maxZoom: 15 });
    });
  });

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
