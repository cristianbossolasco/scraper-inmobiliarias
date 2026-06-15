(() => {
  const mapNode = document.getElementById("territory-map");
  const treeNode = document.getElementById("territory-tree");
  const detailNode = document.getElementById("territory-detail");
  const searchInput = document.getElementById("territory-search");
  const clearSearch = document.getElementById("territory-clear-search");
  const statusNode = document.getElementById("territory-status");
  if (!mapNode || typeof maplibregl === "undefined") return;

  const state = {
    map: null,
    payload: null,
    featureIndex: new Map(),
    nodes: [],
    selected: null,
    selectedFeature: null,
    selectedGroup: null,
    clickCandidates: new Map()
  };

  const sourceIds = {
    partido: "territory-partido",
    localidades: "territory-localidades",
    zonas: "territory-zonas",
    microzonas: "territory-microzonas",
    gaps: "territory-gaps",
    evidence: "territory-evidence",
    selected: "territory-selected"
  };

  const layerGroups = {
    partido: ["partido-fill", "partido-line"],
    localidades: ["localidades-fill", "localidades-line"],
    zonas: ["zonas-fill", "zonas-line"],
    microzonas: ["microzonas-fill", "microzonas-line"],
    gaps: ["gaps-fill", "gaps-line"],
    evidence: ["evidence-points"],
    selected: ["selected-fill", "selected-line"]
  };

  const layerToGroup = {
    "partido-fill": "partido",
    "localidades-fill": "localidades",
    "zonas-fill": "zonas",
    "microzonas-fill": "microzonas",
    "gaps-fill": "gaps"
  };

  const clickLayerPriority = ["microzonas-fill", "zonas-fill", "gaps-fill", "localidades-fill", "partido-fill"];

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;"
    })[char]);
  }

  function formatNumber(value, digits = 2) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return "-";
    return parsed.toLocaleString("es-AR", {
      maximumFractionDigits: digits,
      minimumFractionDigits: 0
    });
  }

  function labelOf(props = {}) {
    return props.canonical_name
      || props.microzone_name
      || props.zone_name
      || props.locality_name
      || props.partido_name
      || props.gap_id
      || "";
  }

  function normalizeText(value) {
    return String(value || "")
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase();
  }

  function coordinatesOf(value, output = []) {
    if (!Array.isArray(value)) return output;
    if (value.length >= 2 && Number.isFinite(Number(value[0])) && Number.isFinite(Number(value[1]))) {
      output.push([Number(value[0]), Number(value[1])]);
      return output;
    }
    value.forEach((item) => coordinatesOf(item, output));
    return output;
  }

  function boundsOfFeature(feature) {
    const coordinates = coordinatesOf(feature?.geometry?.coordinates || []);
    if (!coordinates.length) return null;
    const bounds = new maplibregl.LngLatBounds();
    coordinates.forEach((coordinate) => bounds.extend(coordinate));
    return bounds;
  }

  function fitFeature(feature) {
    const bounds = boundsOfFeature(feature);
    if (!bounds || bounds.isEmpty()) return;
    const width = state.map.getCanvas().clientWidth;
    const padding = width < 900
      ? { top: 50, right: 50, bottom: 50, left: 50 }
      : { top: 70, right: 320, bottom: 70, left: 180 };
    state.map.fitBounds(bounds, {
      padding,
      maxZoom: feature.geometry?.type === "Point" ? 16 : 14.6,
      duration: 450
    });
  }

  function emptyCollection() {
    return { type: "FeatureCollection", features: [] };
  }

  function addSource(id, data) {
    state.map.addSource(id, { type: "geojson", data: data || emptyCollection() });
  }

  function confidenceColor() {
    return [
      "match",
      ["get", "source_confidence"],
      "medium_high", "#176b4d",
      "medium", "#386f8f",
      "medium_low", "#d6a528",
      "low", "#d95d45",
      "very_low", "#9b3f1f",
      "#6f5d8f"
    ];
  }

  function addLayers() {
    addSource(sourceIds.partido, state.payload.layers.partido.geojson);
    addSource(sourceIds.localidades, state.payload.layers.localidades.geojson);
    addSource(sourceIds.zonas, state.payload.layers.zonas.geojson);
    addSource(sourceIds.microzonas, state.payload.layers.microzonas.geojson);
    addSource(sourceIds.gaps, state.payload.layers.gaps.geojson);
    addSource(sourceIds.evidence, state.payload.evidence.barrio_ingles_points);
    addSource(sourceIds.selected, emptyCollection());

    state.map.addLayer({
      id: "partido-fill",
      type: "fill",
      source: sourceIds.partido,
      paint: { "fill-color": "#17231e", "fill-opacity": 0.04 }
    });
    state.map.addLayer({
      id: "partido-line",
      type: "line",
      source: sourceIds.partido,
      paint: { "line-color": "#17231e", "line-width": 2.8, "line-opacity": 0.92 }
    });
    state.map.addLayer({
      id: "localidades-fill",
      type: "fill",
      source: sourceIds.localidades,
      paint: { "fill-color": "#386f8f", "fill-opacity": 0.1 }
    });
    state.map.addLayer({
      id: "localidades-line",
      type: "line",
      source: sourceIds.localidades,
      paint: { "line-color": "#245d78", "line-width": 1.9, "line-opacity": 0.9, "line-dasharray": [2, 1] }
    });
    state.map.addLayer({
      id: "zonas-fill",
      type: "fill",
      source: sourceIds.zonas,
      paint: { "fill-color": confidenceColor(), "fill-opacity": 0.22 }
    });
    state.map.addLayer({
      id: "zonas-line",
      type: "line",
      source: sourceIds.zonas,
      paint: { "line-color": "#0f3f2f", "line-width": 1.35, "line-opacity": 0.86 }
    });
    state.map.addLayer({
      id: "microzonas-fill",
      type: "fill",
      source: sourceIds.microzonas,
      paint: { "fill-color": "#d95d45", "fill-opacity": 0.36 }
    });
    state.map.addLayer({
      id: "microzonas-line",
      type: "line",
      source: sourceIds.microzonas,
      paint: { "line-color": "#9b3f1f", "line-width": 2.2 }
    });
    state.map.addLayer({
      id: "gaps-fill",
      type: "fill",
      source: sourceIds.gaps,
      paint: { "fill-color": "#c95740", "fill-opacity": 0.2 }
    });
    state.map.addLayer({
      id: "gaps-line",
      type: "line",
      source: sourceIds.gaps,
      paint: { "line-color": "#9b3f1f", "line-width": 1.2, "line-dasharray": [3, 2] }
    });
    state.map.addLayer({
      id: "evidence-points",
      type: "circle",
      source: sourceIds.evidence,
      layout: { visibility: "none" },
      paint: {
        "circle-color": ["case", ["==", ["get", "used_for_hull"], true], "#176b4d", "#d6a528"],
        "circle-radius": 4,
        "circle-stroke-width": 1.4,
        "circle-stroke-color": "#ffffff",
        "circle-opacity": 0.88
      }
    });
    state.map.addLayer({
      id: "selected-fill",
      type: "fill",
      source: sourceIds.selected,
      paint: { "fill-color": "#f2b84b", "fill-opacity": 0.16 }
    });
    state.map.addLayer({
      id: "selected-line",
      type: "line",
      source: sourceIds.selected,
      paint: { "line-color": "#f2b84b", "line-width": 2, "line-opacity": 0.9, "line-dasharray": [1, 1.4] }
    });
  }

  function indexFeatures() {
    state.featureIndex.clear();
    Object.entries(state.payload.layers).forEach(([layerName, layer]) => {
      (layer.geojson?.features || []).forEach((feature) => {
        const props = feature.properties || {};
        const id = feature.id || props.gap_id || normalizeText(labelOf(props)).replace(/[^a-z0-9]+/g, "");
        state.featureIndex.set(`${layerName}:${id}`, { layerName, feature });
        state.featureIndex.set(`${props.level_name || ""}:${id}`, { layerName, feature });
      });
    });
  }

  function flattenNodes(node, output = []) {
    output.push(node);
    (node.children || []).forEach((child) => flattenNodes(child, output));
    return output;
  }

  function renderTree() {
    state.nodes = flattenNodes(state.payload.tree);
    treeNode.innerHTML = renderNode(state.payload.tree, 0);
    applyTreeFilter();
  }

  function renderNode(node, depth) {
    const review = node.needs_manual_review ? '<span class="territory-review-dot" title="Requiere revision manual" aria-label="Requiere revision manual"></span>' : "";
    const children = (node.children || []).map((child) => renderNode(child, depth + 1)).join("");
    return `
      <div class="territory-tree-node" data-node-row data-node-id="${escapeHtml(node.id)}" style="--depth:${depth}">
        <button type="button" data-node-id="${escapeHtml(node.id)}">
          <span>${escapeHtml(node.label)}</span>
          ${review}
          <small>${escapeHtml(node.level_name || node.level)}</small>
        </button>
      </div>
      ${children}
    `;
  }

  function applyTreeFilter() {
    const query = normalizeText(searchInput?.value || "");
    document.querySelectorAll("[data-node-row]").forEach((row) => {
      const node = state.nodes.find((item) => item.id === row.dataset.nodeId);
      if (!node || !query) {
        row.hidden = false;
        return;
      }
      const haystack = normalizeText(`${node.label} ${node.level_name}`);
      row.hidden = !haystack.includes(query);
    });
  }

  function findFeatureForNode(node) {
    const sourceName = {
      partido: "partido",
      localidad: "localidades",
      zona: "zonas",
      microzona: "microzonas"
    }[node.level];
    if (!sourceName) return null;
    return state.featureIndex.get(`${sourceName}:${node.feature_id}`)?.feature || null;
  }

  function groupForNode(node) {
    return {
      partido: "partido",
      localidad: "localidades",
      zona: "zonas",
      microzona: "microzonas"
    }[node.level] || null;
  }

  function isGroupVisible(group) {
    const checkbox = document.querySelector(`[data-layer-toggle="${group}"]`);
    return !checkbox || checkbox.checked;
  }

  function isLayerVisible(layerId) {
    if (!state.map.getLayer(layerId)) return false;
    const group = layerToGroup[layerId];
    return !group || isGroupVisible(group);
  }

  function clickableLayers() {
    return clickLayerPriority.filter(isLayerVisible);
  }

  function featureStableId(feature) {
    const props = feature?.properties || {};
    return String(props.feature_id || feature?.id || props.gap_id || normalizeText(labelOf(props)).replace(/[^a-z0-9]+/g, ""));
  }

  function featureCandidateKey(feature, layerId) {
    return `${layerId}:${featureStableId(feature)}`;
  }

  function candidateLabel(candidate) {
    const props = candidate.feature?.properties || {};
    const layerName = props.level_name || layerToGroup[candidate.layerId] || "";
    return `${labelOf(props) || props.gap_id || "Territorio"} - ${layerName}`;
  }

  function candidatesAtPoint(point) {
    const layers = clickableLayers();
    if (!layers.length) return [];
    const seen = new Set();
    return state.map.queryRenderedFeatures(point, { layers })
      .map((feature) => {
        const layerId = feature.layer?.id || "";
        return { key: featureCandidateKey(feature, layerId), layerId, feature };
      })
      .filter((candidate) => {
        if (!candidate.layerId || seen.has(candidate.key)) return false;
        seen.add(candidate.key);
        return true;
      })
      .sort((left, right) => clickLayerPriority.indexOf(left.layerId) - clickLayerPriority.indexOf(right.layerId));
  }

  function selectCandidate(candidate, options = {}) {
    if (!candidate) return;
    const props = candidate.feature?.properties || {};
    const featureId = featureStableId(candidate.feature);
    const featureLabel = normalizeText(labelOf(props)).replace(/[^a-z0-9]+/g, "");
    const node = state.nodes.find((item) => (
      item.feature_id === featureId
      || (
        normalizeText(item.label).replace(/[^a-z0-9]+/g, "") === featureLabel
        && item.level_name === (props.level_name || item.level_name)
      )
    ));
    if (node) {
      selectNode(node.id, { ...options, selectedLayer: candidate.layerId });
    } else {
      selectFeature(candidate.feature, candidate.layerId, options);
    }
  }

  function updateSelectedFeature(feature, group) {
    state.selectedFeature = feature || null;
    state.selectedGroup = group || null;
    if (!state.map.getSource(sourceIds.selected)) return;
    state.map.getSource(sourceIds.selected).setData(
      feature && (!group || isGroupVisible(group))
        ? { type: "FeatureCollection", features: [feature] }
        : emptyCollection()
    );
  }

  function selectNode(nodeId, options = {}) {
    const node = state.nodes.find((item) => item.id === nodeId);
    if (!node) return;
    const feature = findFeatureForNode(node);
    state.selected = node;
    document.querySelectorAll("[data-node-row]").forEach((row) => {
      row.classList.toggle("active", row.dataset.nodeId === nodeId);
    });
    updateDetail(node, feature, { selectedLayer: options.selectedLayer || "" });
    updateSelectedFeature(feature, groupForNode(node));
    if (feature && options.fit !== false) fitFeature(feature);
  }

  function selectFeature(feature, layerName, options = {}) {
    const props = feature?.properties || {};
    const title = labelOf(props) || props.gap_id || "Territorio";
    const node = {
      id: `${layerName}:${feature.id || props.gap_id || title}`,
      label: title,
      level: props.level_name || layerName,
      level_name: props.level_name || layerName,
      feature_id: String(feature.id || props.gap_id || ""),
      needs_manual_review: Boolean(props.needs_manual_review)
    };
    updateDetail(node, feature, { selectedLayer: layerName });
    document.querySelectorAll("[data-node-row]").forEach((row) => row.classList.remove("active"));
    updateSelectedFeature(feature, layerToGroup[layerName] || null);
    if (options.fit !== false) fitFeature(feature);
  }

  function updateDetail(node, feature, meta = {}) {
    const props = feature?.properties || {};
    const featureId = featureStableId(feature);
    const selectedLayer = meta.selectedLayer || "";
    detailNode.innerHTML = `
      <p class="eyebrow">${escapeHtml(node.level_name || node.level)}</p>
      <h2>${escapeHtml(node.label)}</h2>
      <dl>
        <div><dt>Feature seleccionado</dt><dd>${escapeHtml(selectedLayer || "-")} / ${escapeHtml(featureId || "-")}</dd></div>
        <div><dt>Nivel</dt><dd>${escapeHtml(node.level_name || node.level)}</dd></div>
        <div><dt>Confianza</dt><dd>${escapeHtml(props.source_confidence || "-")}</dd></div>
        <div><dt>Área</dt><dd>${formatNumber(props.area_km2, 3)} km2</dd></div>
        <div><dt>Revisión</dt><dd>${props.needs_manual_review ? "Si - punto rojo en el arbol" : "No"}</dd></div>
        <div><dt>Localidad</dt><dd>${escapeHtml(props.parent_locality || props.locality || "-")}</dd></div>
        <div><dt>Zona padre</dt><dd>${escapeHtml(props.parent_zone || "-")}</dd></div>
        <div><dt>Relación OSM</dt><dd>${escapeHtml(props.relation_id || props.osm_relation_id || "-")}</dd></div>
        <div><dt>Método</dt><dd>${escapeHtml(props.source_method || "-")}</dd></div>
      </dl>
      ${detailNote(props)}
    `;
  }

  function detailNote(props) {
    const notes = [];
    if (props.needs_manual_review) {
      notes.push("El punto rojo indica que esta capa requiere revision manual de calidad.");
    }
    if (props.source_warning) notes.push(props.source_warning);
    if (props.likely_missing_zone_candidates) {
      notes.push(`Candidatos probables: ${props.likely_missing_zone_candidates}`);
    }
    if (props.evidence_point_count !== undefined) {
      notes.push(`Evidencia Barrio Inglés: ${props.evidence_points_used_for_hull || 0} puntos usados de ${props.evidence_point_count || 0}.`);
    }
    if (!notes.length) return "";
    return `<p class="audit-note">${escapeHtml(notes.join(" "))}</p>`;
  }

  function popupForFeature(feature, lngLat) {
    const props = feature.properties || {};
    const title = labelOf(props) || props.gap_id || "Territorio";
    const html = `
      <div class="map-popup territory-popup">
        <strong>${escapeHtml(title)}</strong>
        <p>${escapeHtml(props.level_name || props.source_confidence || "")}</p>
        <small>${escapeHtml(props.source_confidence || "sin confianza")} · ${formatNumber(props.area_km2, 3)} km2</small>
      </div>
    `;
    new maplibregl.Popup({ offset: 12 }).setLngLat(lngLat).setHTML(html).addTo(state.map);
  }

  function popupForCandidates(candidates, lngLat) {
    if (!candidates.length) return;
    if (candidates.length === 1) {
      popupForFeature(candidates[0].feature, lngLat);
      return;
    }
    state.clickCandidates.clear();
    candidates.forEach((candidate) => state.clickCandidates.set(candidate.key, candidate));
    const buttons = candidates.map((candidate, index) => `
      <button type="button" class="territory-candidate-button" data-territory-candidate="${escapeHtml(candidate.key)}">
        ${escapeHtml(index === 0 ? `Seleccionado: ${candidateLabel(candidate)}` : candidateLabel(candidate))}
      </button>
    `).join("");
    new maplibregl.Popup({ offset: 12 })
      .setLngLat(lngLat)
      .setHTML(`
        <div class="map-popup territory-popup">
          <strong>Click ambiguo</strong>
          <p>Elegi el poligono que queres auditar.</p>
          <div class="territory-candidate-list">${buttons}</div>
        </div>
      `)
      .addTo(state.map);
  }

  function installInteractions() {
    clickLayerPriority.forEach((layerId) => {
      state.map.on("mouseenter", layerId, () => { state.map.getCanvas().style.cursor = "pointer"; });
      state.map.on("mouseleave", layerId, () => { state.map.getCanvas().style.cursor = ""; });
    });
    state.map.on("click", (event) => {
      const candidates = candidatesAtPoint(event.point);
      if (!candidates.length) return;
      selectCandidate(candidates[0], { fit: false, selectedLayer: candidates[0].layerId });
      popupForCandidates(candidates, event.lngLat);
    });
    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-territory-candidate]");
      if (!button) return;
      const candidate = state.clickCandidates.get(button.dataset.territoryCandidate);
      selectCandidate(candidate, { fit: false, selectedLayer: candidate?.layerId || "" });
    });
    state.map.on("click", "evidence-points", (event) => {
      const feature = event.features?.[0];
      if (!feature) return;
      const props = feature.properties || {};
      new maplibregl.Popup({ offset: 10 })
        .setLngLat(event.lngLat)
        .setHTML(`
          <div class="map-popup territory-popup">
            <strong>Barrio Inglés</strong>
            <p>${props.used_for_hull ? "Punto usado para hull" : "Punto de evidencia"}</p>
            <small>${escapeHtml(props.precision || "")} · ${escapeHtml(props.matched_zone || "")}</small>
          </div>
        `)
        .addTo(state.map);
    });
  }

  function setGroupVisibility(group, visible) {
    (layerGroups[group] || []).forEach((layerId) => {
      if (state.map.getLayer(layerId)) {
        state.map.setLayoutProperty(layerId, "visibility", visible ? "visible" : "none");
      }
    });
    if (group === state.selectedGroup) {
      updateSelectedFeature(state.selectedFeature, state.selectedGroup);
    }
  }

  function installControls() {
    treeNode.addEventListener("click", (event) => {
      const button = event.target.closest("[data-node-id]");
      if (!button) return;
      selectNode(button.dataset.nodeId);
    });
    document.querySelectorAll("[data-layer-toggle]").forEach((checkbox) => {
      checkbox.addEventListener("change", () => {
        setGroupVisibility(checkbox.dataset.layerToggle, checkbox.checked);
      });
    });
    searchInput?.addEventListener("input", applyTreeFilter);
    clearSearch?.addEventListener("click", () => {
      if (searchInput) searchInput.value = "";
      applyTreeFilter();
      searchInput?.focus();
    });
  }

  async function init() {
    try {
      const [config, payload] = await Promise.all([
        fetch("/api/configuracion-mapa/").then((response) => response.json()),
        fetch("/api/jerarquia-geografica/capas/").then((response) => response.json())
      ]);
      state.payload = payload;
      state.map = new maplibregl.Map({
        container: "territory-map",
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
      state.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
      state.map.on("load", () => {
        addLayers();
        indexFeatures();
        renderTree();
        installInteractions();
        installControls();
        setGroupVisibility("evidence", false);
        const root = state.nodes[0];
        if (root) selectNode(root.id);
        if (statusNode) {
          statusNode.textContent = payload.configured ? "OK" : "Incompleto";
          statusNode.classList.toggle("success", Boolean(payload.configured));
        }
        lucide.createIcons();
      });
    } catch (error) {
      if (statusNode) {
        statusNode.textContent = "Error";
        statusNode.classList.add("failed");
      }
      if (treeNode) {
        treeNode.innerHTML = `<p class="audit-note">${escapeHtml(error.message || "No se pudo cargar el mapa territorial.")}</p>`;
      }
    }
  }

  init();
})();
