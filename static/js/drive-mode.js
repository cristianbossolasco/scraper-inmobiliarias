(() => {
  "use strict";

  const mapNode = document.getElementById("drive-map");
  const configNode = document.getElementById("drive-map-config");
  const utils = window.RadarDriveUtils;
  const discovery = window.RadarDriveDiscovery;
  if (!mapNode || !configNode || !utils || !window.RadarDriveNavigation || !window.RadarDriveHistory || typeof maplibregl === "undefined") return;

  const config = JSON.parse(configNode.textContent);
  const SESSION_KEY = `radar.drive.session.v2.${config.user_id}`;
  const PREVIOUS_SESSION_KEY = "radar.drive.session.v2";
  const LEGACY_SESSION_KEY = "radar.drive.session.v1";
  const SESSION_VERSION = 3;
  const ACTIVE_SESSION_TTL = 6 * 60 * 60 * 1000;
  const ENDED_SESSION_TTL = 24 * 60 * 60 * 1000;
  const ENCOUNTER_DISTANCE_M = 120;
  const AUTO_COOLDOWN_MS = 10000;
  const MAX_TRACE_POINTS = 1500;

  const status = document.getElementById("drive-status");
  const statusText = status.querySelector("span");
  const count = document.getElementById("drive-count");
  const startPanel = document.getElementById("drive-start-panel");
  const startContent = document.getElementById("drive-start-content");
  const startButton = document.getElementById("drive-start");
  const recovery = document.getElementById("drive-recovery");
  const resumeButton = document.getElementById("drive-resume");
  const recoverySummaryButton = document.getElementById("drive-recovery-summary");
  const recoveryDeleteButton = document.getElementById("drive-recovery-delete");
  const controls = document.getElementById("drive-controls");
  const stopButton = document.getElementById("drive-stop");
  const filterSummary = document.getElementById("drive-filter-summary");
  const recenterButton = document.getElementById("drive-recenter");
  const followControls = document.getElementById("drive-follow-controls");
  const followStatus = document.getElementById("drive-follow-status");
  const typeInputs = Array.from(document.querySelectorAll("input[name='drive-property-type']"));
  const bedroomInputs = Array.from(document.querySelectorAll("input[name='drive-bedrooms']"));
  const radiusInput = document.getElementById("drive-radius");
  const priceMinInput = document.getElementById("drive-price-min");
  const priceMaxInput = document.getElementById("drive-price-max");
  const coveredMinInput = document.getElementById("drive-covered-min");
  const landMinInput = document.getElementById("drive-land-min");
  const filterError = document.getElementById("drive-filter-error");
  const startFilterSummary = document.getElementById("drive-start-filter-summary");
  const card = document.getElementById("drive-property-card");
  const cardClose = document.getElementById("drive-card-close");
  const cardDistance = document.getElementById("drive-card-distance");
  const cardGroup = document.getElementById("drive-card-group");
  const cardAddress = document.getElementById("drive-card-address");
  const cardPrice = document.getElementById("drive-card-price");
  const cardFacts = document.getElementById("drive-card-facts");
  const cardAddressNote = document.getElementById("drive-card-address-note");
  const cardLocation = document.getElementById("drive-card-location");
  const cardImage = document.getElementById("drive-card-image");
  const cardImageFallback = document.getElementById("drive-card-image-fallback");
  const cardImageLabel = document.getElementById("drive-card-image-label");
  const cardGroupNav = document.getElementById("drive-card-group-nav");
  const cardPrev = document.getElementById("drive-card-prev");
  const cardNext = document.getElementById("drive-card-next");
  const cardIndex = document.getElementById("drive-card-index");
  const favoriteButton = document.getElementById("drive-favorite");
  const originalLink = document.getElementById("drive-original-link");
  const summary = document.getElementById("drive-summary");
  const summaryDuration = document.getElementById("drive-summary-duration");
  const summaryDistance = document.getElementById("drive-summary-distance");
  const summaryEncounters = document.getElementById("drive-summary-encounters");
  const summaryFavorites = document.getElementById("drive-summary-favorites");
  const summaryFilters = document.getElementById("drive-summary-filters");
  const summaryListSection = document.getElementById("drive-summary-list-section");
  const summaryList = document.getElementById("drive-summary-list");
  const newSessionButton = document.getElementById("drive-new-session");
  const deleteSessionButton = document.getElementById("drive-delete-session");
  const logoutForm = document.getElementById("drive-logout-form");
  const historyPanel = document.getElementById("drive-history-panel");
  const historyList = document.getElementById("drive-history-list");
  const historyMessage = document.getElementById("drive-history-message");
  const historyMore = document.getElementById("drive-history-more");
  const syncStatus = document.getElementById("drive-sync-status");
  const syncRetry = document.getElementById("drive-sync-retry");
  const historyDelete = document.getElementById("drive-delete-history");
  const encounterList = document.getElementById("drive-encounters-list");
  const navigation = window.RadarDriveNavigation.createTracker();

  let watchId = null;
  let wakeLock = null;
  let tracking = false;
  let follow = true;
  let lastPosition = null;
  let lastSuccessfulQueryPosition = null;
  let lastSuccessfulQueryAt = 0;
  let lastQueryAttemptAt = 0;
  let pendingRequest = null;
  let requestGeneration = 0;
  let retryCount = 0;
  let retryTimer = null;
  let latestProperties = [];
  let selectedProperty = null;
  let currentGroup = [];
  let currentGroupIndex = 0;
  let currentDirection = "";
  let cardRequestToken = 0;
  let autoGroupPending = null;
  let lastAutoCardAt = 0;
  let session = null;
  let lastPersistAt = 0;
  let storageAvailable = true;
  let mapReady = false;
  let archivedSession = null;
  let nextHistoryPage = null;
  let historyGeneration = 0;
  let cameraHeading = null;
  let cameraPosition = null;
  let cameraInitialized = false;
  let touchingMap = false;
  let pinchGesture = false;
  let userZooming = false;
  let dragOrigin = null;
  let knownIds = null;
  let previousTraces = [];
  let discoveryPageSize = 20;
  let lastSpokenAt = 0;
  let nearbyTruncated = false;
  let focusedNote = null;
  const hydratedHistoryIds = new Set();
  const audioInput = document.getElementById("drive-audio");
  const audioToggle = document.getElementById("drive-audio-toggle");
  const discoveryFilter = document.getElementById("drive-discovery-filter");
  const discoveryWarning = document.getElementById("drive-discovery-warning");
  let fieldUI = null;
  const detailCache = new Map();
  let outbox;
  let localStorageAdapter;
  try { localStorageAdapter = window.localStorage; } catch (_) { /* handled by outbox */ }
  outbox = window.RadarDriveHistory.createOutbox({
    ownerId: config.user_id, storage: localStorageAdapter, request: historyRequest,
    onChange: updateSyncStatus
  });

  const map = new maplibregl.Map({
    container: mapNode,
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
    ],
    attributionControl: true
  });

  // A two-finger zoom may also generate drag events. Treat the whole gesture as zoom.
  map.touchZoomRotate.disableRotation();
  map.dragRotate.disable();
  map.getCanvasContainer().addEventListener("touchstart", (event) => {
    if (!touchingMap) pinchGesture = false;
    touchingMap = true;
    if (event.touches.length > 1) pinchGesture = true;
  }, { passive: true, capture: true });
  map.getCanvasContainer().addEventListener("touchend", (event) => {
    if (!event.touches.length) {
      touchingMap = false;
      window.setTimeout(updateCamera, 0);
    }
  }, { passive: true });
  map.getCanvasContainer().addEventListener("touchcancel", () => {
    touchingMap = false; pinchGesture = false; dragOrigin = null;
  }, { passive: true });
  map.on("zoomstart", (event) => { if (event.originalEvent) userZooming = true; });
  map.getCanvasContainer().addEventListener("mousedown", () => { pinchGesture = false; }, { capture: true });
  map.on("zoomend", () => {
    if (userZooming) { userZooming = false; window.setTimeout(updateCamera, 0); }
  });

  map.on("load", () => {
    mapReady = true;
    addMapSourcesAndLayers();
    refreshAllSources();
    if (session?.status === "ended") showSummary();
    else updateCamera();
  });

  function addMapSourcesAndLayers() {
    map.addSource("drive-memory", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({ id: "drive-memory-lines", type: "line", source: "drive-memory", paint: { "line-color": "#76879a", "line-width": 3, "line-opacity": 0.4, "line-dasharray": [2, 2] } });
    map.addSource("drive-notes", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({ id: "drive-note-points", type: "circle", source: "drive-notes", paint: { "circle-color": "#a36508", "circle-radius": 9, "circle-stroke-color": "#fff", "circle-stroke-width": 2 } });
    map.on("click", "drive-note-points", () => fieldUI?.open((archivedSession || session)?.sessionId));
    map.addSource("drive-trace", {
      type: "geojson",
      data: { type: "FeatureCollection", features: [] }
    });
    map.addLayer({
      id: "drive-trace-outline",
      type: "line",
      source: "drive-trace",
      filter: ["==", ["geometry-type"], "LineString"],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "rgba(255,255,255,.88)", "line-width": 7, "line-opacity": .78 }
    });
    map.addLayer({
      id: "drive-trace-line",
      type: "line",
      source: "drive-trace",
      filter: ["==", ["geometry-type"], "LineString"],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#0875f5", "line-width": 4, "line-opacity": .78 }
    });
    map.addLayer({
      id: "drive-trace-points",
      type: "circle",
      source: "drive-trace",
      filter: ["==", ["geometry-type"], "Point"],
      paint: {
        "circle-radius": 6,
        "circle-color": ["match", ["get", "kind"], "start", "#2fc44f", "end", "#ff6659", "#0875f5"],
        "circle-stroke-color": "#ffffff",
        "circle-stroke-width": 2
      }
    });
    map.addSource("drive-properties", {
      type: "geojson",
      data: { type: "FeatureCollection", features: [] }
    });
    map.addLayer({
      id: "drive-property-dots",
      type: "circle",
      source: "drive-properties",
      paint: {
        "circle-color": ["case", ["get", "favorite"], "#a84783", ["get", "nearby"], "#ff6659", "#778b9d"],
        "circle-radius": ["case", [">", ["get", "group_count"], 1], 9, 7],
        "circle-stroke-color": "#ffffff",
        "circle-stroke-width": 3
      }
    });
    map.addLayer({
      id: "drive-property-prices",
      type: "symbol",
      source: "drive-properties",
      layout: {
        "text-field": ["get", "marker_label"],
        "text-size": ["interpolate", ["linear"], ["zoom"], 11, 10, 15, 13],
        "text-font": ["Open Sans Bold"],
        "text-offset": [0, -1.5],
        "text-anchor": "bottom",
        "text-padding": 7,
        "text-allow-overlap": false
      },
      paint: {
        "text-color": "#ffffff",
        "text-halo-color": ["case", ["get", "nearby"], "#ff6659", "#657b8e"],
        "text-halo-width": 7,
        "text-halo-blur": 0
      }
    });
    map.addSource("drive-user", {
      type: "geojson",
      data: { type: "FeatureCollection", features: [] }
    });
    map.addLayer({
      id: "drive-user-accuracy",
      type: "circle",
      source: "drive-user",
      paint: { "circle-radius": 24, "circle-color": "rgba(8,117,245,.16)", "circle-stroke-width": 0 }
    });
    map.addLayer({
      id: "drive-user-dot",
      type: "circle",
      source: "drive-user",
      paint: { "circle-radius": 8, "circle-color": "#0875f5", "circle-stroke-color": "#ffffff", "circle-stroke-width": 3 }
    });
    map.on("click", "drive-property-dots", showPropertyFromMap);
    map.on("click", "drive-property-prices", showPropertyFromMap);
    map.on("mouseenter", "drive-property-dots", () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", "drive-property-dots", () => { map.getCanvas().style.cursor = ""; });
    map.on("dragstart", () => { dragOrigin = map.getCenter(); });
    map.on("dragend", () => {
      if (tracking && dragOrigin && !pinchGesture && !userZooming) {
        const origin = map.project(dragOrigin);
        const center = map.project(map.getCenter());
        if (Math.hypot(origin.x - center.x, origin.y - center.y) >= 24) {
          follow = false;
          updateFollowControls();
        }
      }
      dragOrigin = null;
      updateCamera();
    });
  }

  function updateFollowControls() {
    followControls.hidden = !tracking;
    recenterButton.hidden = false;
    followStatus.textContent = follow ? "Seguimiento activo" : "Mapa libre";
    recenterButton.querySelector("span").textContent = follow ? "Centrar auto" : "Seguir auto";
    recenterButton.setAttribute("aria-label", follow ? "Centrar auto" : "Seguir auto");
    followControls.classList.toggle("is-free", !follow);
  }

  function updateCamera() {
    if (!tracking || !follow || !cameraPosition || !mapReady || touchingMap || userZooming || dragOrigin) return;
    map.stop();
    const options = {
      center: [cameraPosition.longitude, cameraPosition.latitude],
      bearing: Number.isFinite(cameraHeading) ? cameraHeading : map.getBearing(),
      pitch: 35, duration: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 400
    };
    if (!cameraInitialized) options.zoom = 16;
    cameraInitialized = true;
    map.easeTo(options);
  }

  function csrfToken() {
    const item = document.cookie.split(";").map((part) => part.trim())
      .find((part) => part.startsWith("csrftoken="));
    return item ? decodeURIComponent(item.split("=")[1]) : "";
  }

  function setStatus(message, kind = "") {
    status.classList.toggle("active", kind === "active");
    status.classList.toggle("error", kind === "error");
    statusText.textContent = message;
  }

  function sessionId() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function filterDraft() {
    return {
      propertyTypes: typeInputs.filter((input) => input.checked).map((input) => input.value),
      radiusM: radiusInput.value,
      bedroomsMin: bedroomInputs.find((input) => input.checked)?.value || "",
      priceMin: priceMinInput.value,
      priceMax: priceMaxInput.value,
      coveredAreaMinM2: coveredMinInput.value,
      landAreaMinM2: landMinInput.value
    };
  }

  function readFilters(showErrors = false) {
    const result = utils.normalizeDriveFilters(filterDraft());
    filterError.hidden = !showErrors || result.valid;
    filterError.textContent = result.errors[0] || "";
    return result;
  }

  function createSession(filters) {
    const now = Date.now();
    return {
      version: SESSION_VERSION,
      sessionId: sessionId(),
      status: "tracking",
      startedAt: now,
      endedAt: null,
      updatedAt: now,
      filters,
      points: [],
      distanceM: 0,
      encounters: {},
      announcedGroupIds: [],
      dismissedGroupIds: [],
      favoritePropertyIds: [],
      details: {}
    };
  }

  function normalizeStoredFilters(filters = {}) {
    if (filters.propertyType !== undefined) {
      return {
        propertyTypes: filters.propertyType ? [filters.propertyType] : [],
        radiusM: Number(filters.radiusM) || 350,
        bedroomsMin: null,
        priceMin: null,
        priceMax: null,
        coveredAreaMinM2: null,
        landAreaMinM2: null
      };
    }
    return utils.normalizeDriveFilters({
      propertyTypes: filters.propertyTypes,
      radiusM: filters.radiusM || 350,
      bedroomsMin: filters.bedroomsMin,
      priceMin: filters.priceMin,
      priceMax: filters.priceMax,
      coveredAreaMinM2: filters.coveredAreaMinM2,
      landAreaMinM2: filters.landAreaMinM2
    }).filters;
  }

  function loadStoredSession() {
    try {
      const currentRaw = localStorage.getItem(SESSION_KEY);
      const legacyRaw = currentRaw ? null : (localStorage.getItem(PREVIOUS_SESSION_KEY) || localStorage.getItem(LEGACY_SESSION_KEY));
      const raw = currentRaw || legacyRaw;
      if (!raw) return null;
      const stored = JSON.parse(raw);
      if (!stored || ![1, 2, SESSION_VERSION].includes(stored.version) || !stored.status) throw new Error("invalid");
      const age = Date.now() - Number(stored.updatedAt || stored.startedAt || 0);
      const ttl = stored.status === "tracking" ? ACTIVE_SESSION_TTL : ENDED_SESSION_TTL;
      // Pending completed trips must survive until the PC acknowledges the upload.
      if ((age < 0 || age > ttl) && (stored.status !== "ended" || stored.historyId || stored.historyDeleted)) {
        localStorage.removeItem(SESSION_KEY);
        localStorage.removeItem(LEGACY_SESSION_KEY);
        return null;
      }
      // Old trips only contain encounters within 120 m; never label them complete.
      stored.version = stored.version === 1 ? 2 : stored.version;
      stored.filters = normalizeStoredFilters(stored.filters);
      stored.points = Array.isArray(stored.points) ? stored.points : [];
      stored.encounters = stored.encounters || {};
      stored.announcedGroupIds = Array.isArray(stored.announcedGroupIds) ? stored.announcedGroupIds : [];
      stored.dismissedGroupIds = Array.isArray(stored.dismissedGroupIds) ? stored.dismissedGroupIds : [];
      stored.favoritePropertyIds = Array.isArray(stored.favoritePropertyIds) ? stored.favoritePropertyIds : [];
      stored.details = stored.details || {};
      if (legacyRaw) {
        localStorage.setItem(SESSION_KEY, JSON.stringify(stored));
        localStorage.removeItem(PREVIOUS_SESSION_KEY);
        localStorage.removeItem(LEGACY_SESSION_KEY);
      }
      return stored;
    } catch (_error) {
      storageAvailable = false;
      try {
        localStorage.removeItem(SESSION_KEY);
        localStorage.removeItem(LEGACY_SESSION_KEY);
      } catch (_ignored) { /* no-op */ }
      return null;
    }
  }

  function persistSession(force = false) {
    if (!session || !storageAvailable) return;
    const now = Date.now();
    if (!force && now - lastPersistAt < 5000) return;
    session.updatedAt = now;
    try {
      localStorage.setItem(SESSION_KEY, JSON.stringify(session));
      lastPersistAt = now;
    } catch (_error) {
      storageAvailable = false;
      setStatus("Recorrido activo; no se puede recuperar tras una recarga", "error");
    }
  }

  function clearStoredSession() {
    try {
      localStorage.removeItem(SESSION_KEY);
      localStorage.removeItem(PREVIOUS_SESSION_KEY);
      localStorage.removeItem(LEGACY_SESSION_KEY);
    } catch (_error) { /* no-op */ }
    session = null;
    lastPersistAt = 0;
    detailCache.clear();
  }

  function applySessionFilters() {
    if (!session?.filters) return;
    typeInputs.forEach((input) => {
      input.checked = session.filters.propertyTypes.includes(input.value);
    });
    const bedroomsValue = session.filters.bedroomsMin === null ? "" : String(session.filters.bedroomsMin);
    bedroomInputs.forEach((input) => { input.checked = input.value === bedroomsValue; });
    radiusInput.value = String(session.filters.radiusM || 350);
    priceMinInput.value = session.filters.priceMin ?? "";
    priceMaxInput.value = session.filters.priceMax ?? "";
    coveredMinInput.value = session.filters.coveredAreaMinM2 ?? "";
    landMinInput.value = session.filters.landAreaMinM2 ?? "";
    updateFilterPreview();
  }

  function compactAmount(value) {
    return new Intl.NumberFormat("es-AR", { maximumFractionDigits: 0 }).format(value);
  }

  function filterSummaryLabel(filters = readFilters().filters) {
    const selectedLabels = typeInputs.filter((input) => filters.propertyTypes.includes(input.value))
      .map((input) => input.nextElementSibling?.textContent?.trim())
      .filter(Boolean);
    const parts = [selectedLabels.length ? selectedLabels.join(", ") : "Todos los tipos"];
    if (filters.bedroomsMin !== null) parts.push(`${filters.bedroomsMin}+ dorm.`);
    if (filters.priceMin !== null || filters.priceMax !== null) {
      const minimum = filters.priceMin === null ? "sin mín." : `USD ${compactAmount(filters.priceMin)}`;
      const maximum = filters.priceMax === null ? "sin máx." : `USD ${compactAmount(filters.priceMax)}`;
      parts.push(`${minimum}–${maximum}`);
    }
    if (filters.coveredAreaMinM2 !== null) parts.push(`${compactAmount(filters.coveredAreaMinM2)}+ m² cub.`);
    if (filters.landAreaMinM2 !== null) parts.push(`${compactAmount(filters.landAreaMinM2)}+ m² lote`);
    parts.push(`${filters.radiusM} m`);
    return parts.join(" · ");
  }

  function updateFilterPreview() {
    const result = readFilters(false);
    startFilterSummary.textContent = result.valid ? filterSummaryLabel(result.filters) : "Revisá los filtros antes de iniciar.";
  }

  function setFilterControlsDisabled(disabled) {
    [
      ...typeInputs,
      ...bedroomInputs,
      radiusInput,
      priceMinInput,
      priceMaxInput,
      coveredMinInput,
      landMinInput
    ].forEach((input) => { input.disabled = disabled; });
  }

  function propertyFeatures(properties) {
    const groups = new Map();
    properties.forEach((property) => {
      if (!Number.isFinite(property.latitude) || !Number.isFinite(property.longitude)) return;
      if (!groups.has(property.group_id)) groups.set(property.group_id, { ...property, group_count: 0, favorite: false });
      const group = groups.get(property.group_id);
      group.group_count += 1;
      group.favorite ||= property.favorite;
    });
    return Array.from(groups.values()).map((property) => ({
      type: "Feature",
      id: property.id,
      geometry: { type: "Point", coordinates: [property.longitude, property.latitude] },
      properties: {
        id: property.id,
        group_id: property.group_id,
        group_count: property.group_count,
        favorite: Boolean(property.favorite),
        nearby: tracking && lastPosition ? utils.distanceMeters(lastPosition, property) <= session.filters.radiusM : false,
        marker_label: property.group_count > 1
          ? `${property.group_count} · ${property.group_price_short || property.price_short || "varias"}`
          : property.price_short || "Sin precio guardado"
      }
    }));
  }

  function refreshPropertySource() {
    if (!mapReady) return;
    map.getSource("drive-properties")?.setData({
      type: "FeatureCollection",
      features: propertyFeatures(visibleProperties())
    });
    refreshMemoryAndNotes();
  }

  function pendingPropertyIds() {
    return new Set((fieldUI?.notes() || []).filter((note) => note.data.action !== "none" && !note.data.done).map((note) => note.data.propertyId));
  }

  function visibleProperties() {
    const visibleSession = archivedSession || session;
    const properties = discovery.properties(visibleSession);
    return tracking ? properties : discovery.filter(properties, discoveryFilter.value, pendingPropertyIds());
  }

  function refreshMemoryAndNotes() {
    if (!mapReady) return;
    const showMemory = document.getElementById("drive-memory").checked && !archivedSession;
    map.getSource("drive-memory")?.setData({ type: "FeatureCollection", features: showMemory ? previousTraces.map((coordinates) => ({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates } })) : [] });
    const id = (archivedSession || session)?.sessionId;
    const noteFeatures = (fieldUI?.notes() || []).filter((note) => note.data.sessionId === id).map((note) => ({ type: "Feature", properties: { id: note.id }, geometry: { type: "Point", coordinates: [note.data.longitude, note.data.latitude] } }));
    if (focusedNote) noteFeatures.push({ type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [focusedNote.longitude, focusedNote.latitude] } });
    map.getSource("drive-notes")?.setData({ type: "FeatureCollection", features: noteFeatures });
  }

  async function loadMemory() {
    try {
      const result = await historyRequest("/api/recorrido/memoria/");
      knownIds = new Set(result.propertyIds);
      for (const pending of outbox.entries()) discovery.properties(pending).forEach((property) => knownIds.add(property.id));
      previousTraces = result.traces;
      document.getElementById("drive-memory-status").textContent = `${knownIds.size} propiedades recordadas de tus salidas guardadas.`;
      refreshMemoryAndNotes();
    } catch (_) {
      knownIds = null;
      document.getElementById("drive-memory-status").textContent = "Sin conexión con tu historial: esta salida conserva sus descubrimientos, pero no puede compararlos con las anteriores.";
    }
  }

  function refreshUserSource() {
    if (!mapReady) return;
    const features = lastPosition && !archivedSession ? [{
      type: "Feature",
      geometry: { type: "Point", coordinates: [lastPosition.longitude, lastPosition.latitude] },
      properties: { accuracy: lastPosition.accuracy }
    }] : [];
    map.getSource("drive-user")?.setData({ type: "FeatureCollection", features });
  }

  function traceFeatures() {
    const visibleSession = archivedSession || session;
    const points = visibleSession?.points || [];
    const features = [];
    if (points.length >= 2) {
      features.push({
        type: "Feature",
        geometry: { type: "LineString", coordinates: points.map((point) => [point.longitude, point.latitude]) },
        properties: {}
      });
    }
    if (points.length) {
      features.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: [points[0].longitude, points[0].latitude] },
        properties: { kind: "start" }
      });
      if (visibleSession?.status === "ended" && points.length > 1) {
        const last = points[points.length - 1];
        features.push({
          type: "Feature",
          geometry: { type: "Point", coordinates: [last.longitude, last.latitude] },
          properties: { kind: "end" }
        });
      }
    }
    return features;
  }

  function refreshTraceSource() {
    if (!mapReady) return;
    map.getSource("drive-trace")?.setData({ type: "FeatureCollection", features: traceFeatures() });
  }

  function refreshAllSources() {
    refreshPropertySource();
    refreshUserSource();
    refreshTraceSource();
  }

  function scheduleRetry() {
    if (!tracking || retryCount >= 3 || retryTimer) return;
    const delays = [3000, 6000, 12000];
    const delay = delays[retryCount];
    retryCount += 1;
    retryTimer = window.setTimeout(() => {
      retryTimer = null;
      if (tracking && lastPosition) fetchNearby(lastPosition, true);
    }, delay);
  }

  async function fetchNearby(position, force = false) {
    if (!tracking) return;
    const now = Date.now();
    const moved = utils.distanceMeters(lastSuccessfulQueryPosition, position);
    if (!force) {
      if (now - lastQueryAttemptAt < 5000) return;
      if (lastSuccessfulQueryPosition && moved < 60 && now - lastSuccessfulQueryAt < 15000) return;
    }
    lastQueryAttemptAt = now;
    if (pendingRequest) pendingRequest.abort();
    const controller = new AbortController();
    pendingRequest = controller;
    const generation = requestGeneration;
    try {
      const response = await fetch("/api/recorrido/cercanas/", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
        body: JSON.stringify({
          latitude: position.latitude,
          longitude: position.longitude,
          radius_m: session.filters.radiusM,
          property_types: session.filters.propertyTypes,
          price_currency: "USD",
          price_min: session.filters.priceMin,
          price_max: session.filters.priceMax,
          bedrooms_min: session.filters.bedroomsMin,
          covered_area_min_m2: session.filters.coveredAreaMinM2,
          land_area_min_m2: session.filters.landAreaMinM2
        }),
        signal: controller.signal
      });
      if (response.status === 401) {
        window.location.href = "/accounts/login/?next=/recorrido/";
        return;
      }
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || "No se pudieron consultar propiedades.");
      if (!tracking || generation !== requestGeneration) return;
      latestProperties = payload.properties || [];
      lastSuccessfulQueryPosition = { ...position };
      lastSuccessfulQueryAt = Date.now();
      retryCount = 0;
      nearbyTruncated = Boolean(payload.truncated);
      recordEncounters(position);
      refreshPropertySource();
      count.textContent = `${latestProperties.length} cerca · ${discovery.properties(session).length} descubiertas`;
      count.hidden = false;
      setStatus(position.accuracy > 80 ? `GPS impreciso · ±${Math.round(position.accuracy)} m` : "Recorrido activo", position.accuracy > 80 ? "" : "active");
      evaluateAutomaticCard(position);
    } catch (error) {
      if (error.name === "AbortError" || generation !== requestGeneration) return;
      setStatus(window.RadarDriveHistory.connectionMessage({ hostname: location.hostname, online: navigator.onLine }), "error");
      scheduleRetry();
    } finally {
      if (pendingRequest === controller) pendingRequest = null;
    }
  }

  function recordTrace(position) {
    if (!session || !utils.usablePosition(position)) return;
    const previous = session.points[session.points.length - 1] || null;
    const evaluation = utils.evaluateTracePoint(previous, position);
    if (!evaluation.accepted) return;
    session.points.push({
      latitude: position.latitude,
      longitude: position.longitude,
      accuracy: position.accuracy,
      timestamp: position.timestamp
    });
    session.distanceM += evaluation.distanceM;
    if (session.points.length > MAX_TRACE_POINTS) {
      session.points = utils.simplifyTrace(session.points, 8);
      if (session.points.length > MAX_TRACE_POINTS) {
        session.points = session.points.filter((_point, index) => index === 0 || index === session.points.length - 1 || index % 2 === 0);
      }
    }
    refreshTraceSource();
    persistSession();
  }

  function recordEncounters(position) {
    if (!session || !utils.usablePosition(position)) return;
    const result = discovery.accumulate(session, latestProperties, position, knownIds);
    discoveryWarning.textContent = result.limited
      ? "Llegaste a 3.000 descubrimientos. Finalizá esta salida e iniciá otra para seguir guardando casas."
      : nearbyTruncated ? "Hay más casas en este radio: se muestran hasta 250 por consulta. Acercate o ajustá los filtros en la próxima salida." : "";
    discoveryWarning.hidden = !discoveryWarning.textContent;
    persistSession(result.added > 0);
  }

  function evaluateAutomaticCard(position) {
    if (!tracking || document.querySelector("dialog[open]") || autoGroupPending || Date.now() - lastAutoCardAt < AUTO_COOLDOWN_MS) return;
    const candidate = utils.chooseProximityCandidate(latestProperties, position, session);
    if (!candidate) return;
    autoGroupPending = candidate.property.group_id;
    showGroup(candidate.property.group_id, candidate.property.id, candidate.direction)
      .then((shown) => {
        if (!shown || !session) return;
        announceProperty(candidate.property);
        session.announcedGroupIds = Array.from(new Set([...session.announcedGroupIds, candidate.property.group_id]));
        lastAutoCardAt = Date.now();
        persistSession(true);
      })
      .finally(() => { autoGroupPending = null; });
  }

  function updateAudio() {
    const supported = Boolean(window.speechSynthesis && window.SpeechSynthesisUtterance);
    audioInput.disabled = !supported; audioToggle.disabled = !supported;
    if (!supported) audioInput.checked = false;
    audioToggle.textContent = audioInput.checked ? "Voz activada" : "Voz apagada";
    audioToggle.setAttribute("aria-pressed", String(audioInput.checked));
    if (!audioInput.checked) window.speechSynthesis?.cancel();
  }

  function announceProperty(property) {
    if (!audioInput.checked || !window.speechSynthesis || property.group_suspicious
        || session.details[property.id]?.seen_before === true || Date.now() - lastSpokenAt < 30000) return;
    const message = property.group_count > 1
      ? "Hay varias propiedades publicadas cerca de tu recorrido."
      : `${property.type_label || "Propiedad"} cerca de tu recorrido. ${property.price_short || ""}. Ubicación según el aviso.`;
    const utterance = new SpeechSynthesisUtterance(message); utterance.lang = "es-AR"; utterance.rate = 1;
    utterance.onerror = () => { audioToggle.textContent = "Voz no disponible"; };
    speechSynthesis.cancel(); speechSynthesis.speak(utterance); lastSpokenAt = Date.now();
  }

  function handlePosition(result) {
    if (!tracking) return;
    const fix = navigation.update(result);
    const position = fix.position;
    if (!position || !fix.canFollow) {
      setStatus(position?.accuracy > 60 ? `GPS impreciso · ±${Math.round(position.accuracy)} m` : "Esperando una ubicación confiable");
      return;
    }
    lastPosition = position;
    cameraHeading = fix.cameraHeading;
    // Hold the camera through stationary GPS drift; keep raw location for queries.
    if (!cameraPosition || fix.moving || position.accuracy < cameraPosition.accuracy / 2
        || utils.distanceMeters(cameraPosition, position) > Math.max(15, cameraPosition.accuracy + position.accuracy)) cameraPosition = position;
    refreshUserSource();
    updateCamera();
    recordTrace(position);
    recordEncounters(position);
    refreshPropertySource();
    evaluateAutomaticCard(position);
    if (position.accuracy <= 150) fetchNearby(position);
    else setStatus(`Buscando mejor GPS · ±${Math.round(position.accuracy)} m`);
  }

  function handleLocationError(error) {
    const messages = {
      1: "Permiso de ubicación denegado",
      2: "Ubicación no disponible",
      3: "El GPS tardó demasiado"
    };
    setStatus(messages[error.code] || "No se pudo obtener la ubicación", "error");
  }

  async function requestWakeLock() {
    if (!("wakeLock" in navigator) || document.visibilityState !== "visible") return;
    if (wakeLock && !wakeLock.released) return;
    try {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => {
        if (wakeLock?.released) wakeLock = null;
      }, { once: true });
    } catch (_error) {
      wakeLock = null;
    }
  }

  function resetRuntimeState() {
    if (mapReady) map.setPadding({ top: 0, bottom: 0, left: 0, right: 0 });
    navigation.reset();
    cameraHeading = null;
    cameraPosition = null;
    cameraInitialized = false;
    requestGeneration += 1;
    if (pendingRequest) pendingRequest.abort();
    pendingRequest = null;
    if (retryTimer) window.clearTimeout(retryTimer);
    retryTimer = null;
    retryCount = 0;
    lastSuccessfulQueryPosition = null;
    lastSuccessfulQueryAt = 0;
    lastQueryAttemptAt = 0;
    lastPosition = null;
    latestProperties = [];
    selectedProperty = null;
    focusedNote = null;
    currentGroup = [];
    currentGroupIndex = 0;
    currentDirection = "";
    autoGroupPending = null;
    lastAutoCardAt = 0;
    cardRequestToken += 1;
    card.hidden = true;
    count.hidden = true;
    refreshPropertySource();
    refreshUserSource();
  }

  async function startTracking(resume = false) {
    if (!("geolocation" in navigator)) {
      setStatus("Este navegador no ofrece GPS", "error");
      return;
    }
    const filterResult = resume
      ? { valid: true, filters: session.filters }
      : readFilters(true);
    if (!filterResult.valid) {
      setStatus("Revisá los filtros", "error");
      return;
    }
    if (!resume && !preserveCompletedSession()) return;
    startButton.disabled = true;
    setStatus("Solicitando ubicación...");
    if (!resume) {
      clearStoredSession();
      session = createSession(filterResult.filters);
    } else {
      session.status = "tracking";
      session.endedAt = null;
      applySessionFilters();
    }
    resetRuntimeState();
    discoveryFilter.value = "all";
    await loadMemory();
    updateAudio();
    tracking = true;
    follow = true;
    archivedSession = null;
    updateFollowControls();
    startPanel.hidden = true;
    summary.hidden = true;
    controls.hidden = false;
    setFilterControlsDisabled(true);
    filterSummary.textContent = filterSummaryLabel(session.filters);
    refreshTraceSource();
    persistSession(true);
    await requestWakeLock();
    watchId = navigator.geolocation.watchPosition(handlePosition, handleLocationError, {
      enableHighAccuracy: true,
      maximumAge: 3000,
      timeout: 12000
    });
  }

  async function stopTracking() {
    tracking = false;
    cardRequestToken += 1;
    window.speechSynthesis?.cancel();
    requestGeneration += 1;
    if (watchId !== null) navigator.geolocation.clearWatch(watchId);
    watchId = null;
    if (pendingRequest) pendingRequest.abort();
    pendingRequest = null;
    if (retryTimer) window.clearTimeout(retryTimer);
    retryTimer = null;
    if (wakeLock && !wakeLock.released) await wakeLock.release().catch(() => {});
    wakeLock = null;
    controls.hidden = true;
    recenterButton.hidden = true;
    followControls.hidden = true;
    card.hidden = true;
    if (!session) session = createSession(readFilters().filters);
    session.status = "ended";
    session.endedAt = Date.now();
    persistSession(true);
    outbox.enqueue(session);
    outbox.sync();
    refreshTraceSource();
    showSummary();
  }

  async function fetchCardDetail(propertyId) {
    if (detailCache.has(propertyId)) return detailCache.get(propertyId);
    const response = await fetch(`/api/recorrido/propiedad/${propertyId}/ficha/`, {
      headers: { "Accept": "application/json" }
    });
    if (response.status === 401) {
      window.location.href = "/accounts/login/?next=/recorrido/";
      throw new Error("Autenticación requerida");
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "No se pudo abrir la ficha.");
    detailCache.set(propertyId, payload);
    if (session && !archivedSession && session.status === "tracking") {
      const existing = session.details[propertyId] || {};
      session.details[propertyId] = { ...payload, ...existing };
      persistSession();
    }
    return payload;
  }

  function groupMembers(groupId) {
    return discovery.properties(archivedSession || session).filter((property) => property.group_id === groupId)
      .sort((a, b) => a.price - b.price || a.id - b.id);
  }

  async function showGroup(groupId, preferredId = null, direction = "") {
    const members = groupMembers(groupId);
    if (!members.length) return false;
    currentGroup = members;
    currentGroupIndex = Math.max(0, preferredId === null ? 0 : members.findIndex((property) => property.id === preferredId));
    currentDirection = direction;
    return renderCurrentCard();
  }

  async function renderCurrentCard() {
    const nearby = currentGroup[currentGroupIndex];
    if (!nearby) return false;
    const token = ++cardRequestToken;
    selectedProperty = null;
    document.getElementById("drive-property-note").disabled = true;
    favoriteButton.disabled = true;
    card.hidden = false;
    if (!tracking) summary.hidden = true;
    cardAddress.textContent = "Cargando ficha...";
    cardPrice.textContent = nearby.price_short;
    cardFacts.textContent = nearby.type_label;
    cardAddressNote.textContent = "Buscando foto y dirección...";
    cardLocation.textContent = "";
    originalLink.hidden = true;
    originalLink.removeAttribute("href");
    showImageFallback();
    try {
      const historical = archivedSession || session?.status === "ended";
      const detail = historical ? nearby : await fetchCardDetail(nearby.id).catch(() => nearby);
      if (token !== cardRequestToken) return false;
      selectedProperty = { ...nearby, ...detail };
      document.getElementById("drive-property-note").disabled = false;
      favoriteButton.disabled = false;
      const liveDistance = lastPosition ? Math.round(utils.distanceMeters(lastPosition, nearby)) : nearby.distance_m;
      const direction = currentDirection || (lastPosition ? utils.relativeDirection(lastPosition, nearby) : "");
      const directionText = direction && direction !== "detras" ? ` · ${direction}` : "";
      cardDistance.textContent = historical ? "Datos guardados de esta salida" : `A ${liveDistance} m${directionText} · ${nearby.seen_before === false ? "Nueva para vos" : nearby.seen_before === true ? "Ya descubierta antes" : "Sin comparar con otras salidas"}`;
      const isGroup = nearby.group_count > 1;
      cardGroup.hidden = !isGroup;
      cardGroup.textContent = isGroup
        ? `${nearby.group_count} propiedades en este punto${nearby.group_truncated ? ` · ${nearby.group_returned_count} disponibles` : ""}`
        : "";
      cardAddress.textContent = detail.address_text || "Dirección no publicada";
      cardPrice.textContent = detail.price_short;
      const facts = [detail.type_label];
      if (detail.bedrooms != null) facts.push(`${detail.bedrooms} dorm.`);
      if (detail.covered_area_m2 != null) facts.push(`${Math.round(detail.covered_area_m2)} m² cub.`);
      if (detail.land_area_m2 != null) facts.push(`${Math.round(detail.land_area_m2)} m² lote`);
      cardFacts.textContent = facts.join(" · ");
      cardAddressNote.textContent = detail.address_label;
      cardLocation.textContent = nearby.group_suspicious
        ? "Varias publicaciones comparten esta coordenada; puede representar una zona."
        : detail.location_label;
      cardImageLabel.textContent = isGroup ? "Foto de uno de los avisos" : "Foto del aviso";
      loadCardImage(detail);
      favoriteButton.classList.toggle("active", detail.is_favorite);
      favoriteButton.hidden = Boolean(historical);
      favoriteButton.textContent = detail.is_favorite ? "♥ Guardada" : "♡ Guardar";
      if (detail.original_url) {
        originalLink.href = detail.original_url;
        originalLink.textContent = detail.original_source_name
          ? `Ver en ${detail.original_source_name} ↗`
          : "Ver publicación original ↗";
        originalLink.hidden = false;
      } else {
        originalLink.hidden = true;
        originalLink.removeAttribute("href");
      }
      cardGroupNav.hidden = currentGroup.length <= 1;
      cardIndex.textContent = `${currentGroupIndex + 1} de ${currentGroup.length}`;
      cardPrev.disabled = currentGroupIndex === 0;
      cardNext.disabled = currentGroupIndex === currentGroup.length - 1;
      return true;
    } catch (_error) {
      if (token !== cardRequestToken) return false;
      card.hidden = true;
      setStatus("No se pudo abrir la ficha de la propiedad", "error");
      return false;
    }
  }

  function showImageFallback() {
    cardImage.removeAttribute("src");
    cardImage.hidden = true;
    cardImageFallback.hidden = false;
  }

  function loadCardImage(detail) {
    showImageFallback();
    if (!detail.image_url) return;
    cardImage.onload = () => {
      cardImageFallback.hidden = true;
      cardImage.hidden = false;
    };
    cardImage.onerror = showImageFallback;
    cardImage.alt = detail.address_text ? `Foto del aviso en ${detail.address_text}` : "Foto del aviso";
    cardImage.src = detail.image_url;
  }

  function showPropertyFromMap(event) {
    const groupId = event.features?.[0]?.properties?.group_id;
    if (groupId) showGroup(groupId);
  }

  async function toggleFavorite() {
    if (!selectedProperty || !session) return;
    const nextValue = !selectedProperty.is_favorite;
    favoriteButton.disabled = true;
    try {
      const response = await fetch(`/api/recorrido/propiedad/${selectedProperty.id}/favorito/`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
        body: JSON.stringify({ is_favorite: nextValue })
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || "No se pudo guardar.");
      selectedProperty.is_favorite = payload.is_favorite;
      const cached = detailCache.get(selectedProperty.id);
      if (cached) cached.is_favorite = payload.is_favorite;
      session.details[selectedProperty.id] = { ...(session.details[selectedProperty.id] || {}), ...selectedProperty };
      const favorites = new Set(session.favoritePropertyIds);
      if (payload.is_favorite) favorites.add(selectedProperty.id);
      else favorites.delete(selectedProperty.id);
      session.favoritePropertyIds = Array.from(favorites);
      favoriteButton.classList.toggle("active", payload.is_favorite);
      favoriteButton.textContent = payload.is_favorite ? "♥ Guardada" : "♡ Guardar";
      refreshPropertySource();
      persistSession(true);
    } catch (_error) {
      setStatus("No se pudo guardar la propiedad", "error");
    } finally {
      favoriteButton.disabled = false;
    }
  }

  function fitTrace() {
    const visibleSession = archivedSession || session;
    if (!mapReady || !visibleSession || summary.hidden) return;
    // Orientation can change immediately before opening history. Synchronize
    // MapLibre's viewport before calculating padding for the summary panel.
    map.stop();
    map.resize();
    map.setPadding({ top: 0, bottom: 0, left: 0, right: 0 });
    const bounds = new maplibregl.LngLatBounds();
    (visibleSession.points || []).forEach((point) => bounds.extend([point.longitude, point.latitude]));
    visibleProperties().filter((property) => Number.isFinite(property.latitude) && Number.isFinite(property.longitude)).forEach((property) => bounds.extend([property.longitude, property.latitude]));
    if (bounds.isEmpty()) return;
    const viewport = mapNode.getBoundingClientRect();
    const panel = summary.getBoundingClientRect();
    const landscape = window.matchMedia("(orientation: landscape) and (max-height: 520px)").matches;
    const padding = landscape
      ? { top: 130, right: Math.max(35, viewport.right - panel.left + 18), bottom: 30, left: 30 }
      : { top: 140, right: 30, bottom: Math.max(35, viewport.bottom - panel.top + 18), left: 30 };
    map.fitBounds(bounds, {
      padding,
      maxZoom: 16,
      pitch: 0,
      bearing: 0,
      duration: 0
    });
  }

  function distanceLabel(distanceM) {
    if (distanceM < 1000) return `${Math.round(distanceM)} m`;
    return `${(distanceM / 1000).toFixed(1).replace(".", ",")} km`;
  }

  function buildSummaryList() {
    const visibleSession = archivedSession || session;
    summaryList.replaceChildren();
    const favorites = visibleSession?.favoritePropertyIds || [];
    favorites.forEach((propertyId) => {
      const detail = visibleSession.details?.[propertyId];
      if (!detail) return;
      const item = document.createElement("div");
      item.className = "drive-summary-item";
      let media;
      if (detail.image_url) {
        media = document.createElement("img");
        media.alt = "";
        media.referrerPolicy = "no-referrer";
        media.decoding = "async";
        media.src = detail.image_url;
        media.addEventListener("error", () => {
          const fallback = document.createElement("div");
          fallback.className = "drive-summary-item-fallback";
          fallback.textContent = "⌂";
          media.replaceWith(fallback);
        }, { once: true });
      } else {
        media = document.createElement("div");
        media.className = "drive-summary-item-fallback";
        media.textContent = "⌂";
      }
      const copy = document.createElement("div");
      const address = document.createElement("strong");
      address.textContent = detail.address_text || "Dirección no publicada";
      const price = document.createElement("span");
      price.textContent = detail.price_short || "";
      copy.append(address, price);
      if (detail.original_url) {
        const link = document.createElement("a");
        link.href = detail.original_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer external";
        link.referrerPolicy = "no-referrer";
        link.textContent = "Ver publicación original ↗";
        link.addEventListener("click", () => persistSession(true));
        copy.append(link);
      }
      item.append(media, copy);
      summaryList.append(item);
    });
    summaryListSection.hidden = summaryList.childElementCount === 0;
  }

  function showSummary() {
    const visibleSession = archivedSession || session;
    if (!visibleSession) return;
    startPanel.hidden = true;
    controls.hidden = true;
    card.hidden = true;
    recenterButton.hidden = true;
    followControls.hidden = true;
    summary.hidden = false;
    summary.scrollTop = 0;
    count.hidden = true;
    setStatus("Recorrido finalizado", "active");
    const endedAt = visibleSession.endedAt || Date.now();
    document.getElementById("drive-summary-title").textContent = archivedSession ? tripDate(visibleSession.startedAt) : "Recorrido finalizado";
    summaryDuration.textContent = utils.formatDuration(endedAt - visibleSession.startedAt);
    summaryDistance.textContent = distanceLabel(visibleSession.distanceM || 0);
    summaryEncounters.textContent = String(discovery.properties(visibleSession).length);
    summaryFavorites.textContent = String((visibleSession.favoritePropertyIds || []).length);
    summaryFilters.textContent = `Filtros usados: ${filterSummaryLabel(visibleSession.filters)}`;
    newSessionButton.hidden = Boolean(archivedSession);
    historyDelete.hidden = !archivedSession?.historyId;
    buildSummaryList();
    buildEncounterList(visibleSession);
    fieldUI?.loadSession(visibleSession.sessionId);
    refreshPropertySource();
    updateSyncStatus();
    refreshTraceSource();
    window.setTimeout(fitTrace, 50);
  }

  function showIdle() {
    tracking = false;
    followControls.hidden = true;
    archivedSession = null;
    summary.hidden = true;
    controls.hidden = true;
    card.hidden = true;
    recenterButton.hidden = true;
    startPanel.hidden = false;
    startContent.hidden = false;
    recovery.hidden = true;
    startButton.disabled = false;
    startButton.textContent = "Iniciar recorrido";
    setFilterControlsDisabled(false);
    filterError.hidden = true;
    latestProperties = [];
    lastPosition = null;
    setStatus("Listo para comenzar");
    count.hidden = true;
    refreshAllSources();
    updateFilterPreview();
  }

  function deleteSessionAndReset() {
    if (!preserveCompletedSession()) return;
    clearStoredSession();
    archivedSession = null;
    resetRuntimeState();
    showIdle();
  }

  function initializeStoredState() {
    session = loadStoredSession();
    if (!session) {
      showIdle();
      return;
    }
    Object.entries(session.details || {}).forEach(([id, detail]) => { if (detail.address_text !== undefined) detailCache.set(Number(id), detail); });
    applySessionFilters();
    refreshTraceSource();
    if (session.status === "ended") {
      showSummary();
      outbox.enqueue(session);
      outbox.sync();
      return;
    }
    startPanel.hidden = false;
    startContent.hidden = true;
    recovery.hidden = false;
    controls.hidden = true;
    summary.hidden = true;
    setStatus("Recorrido interrumpido");
  }

  function tripDate(timestamp) {
    return new Intl.DateTimeFormat("es-AR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(timestamp));
  }

  async function historyRequest(url, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {
        ...options, signal: controller.signal,
        headers: { "Accept": "application/json", "Content-Type": "application/json", "X-CSRFToken": csrfToken() }
      });
      if (response.status === 401 || (response.redirected && new URL(response.url).pathname.startsWith("/accounts/login/"))) {
        const error = new Error("La sesión de acceso venció. Volvé a ingresar en este mismo enlace para sincronizar los pendientes.");
        error.status = 401;
        throw error;
      }
      if (response.status >= 500 || !(response.status === 204 || (response.headers.get("content-type") || "").includes("application/json"))) {
        throw new TypeError("Radar connection unavailable");
      }
      const payload = response.status === 204 ? {} : await response.json();
      if (!response.ok) {
        const error = new Error(payload.error || "La PC no pudo guardar el recorrido.");
        error.status = response.status;
        throw error;
      }
      return payload;
    } catch (error) {
      if (error.name === "AbortError" || error instanceof TypeError || error instanceof SyntaxError) {
        throw new Error(window.RadarDriveHistory.connectionMessage({ hostname: location.hostname, online: navigator.onLine, saving: true }));
      }
      throw error;
    } finally { window.clearTimeout(timeout); }
  }

  function preserveCompletedSession() {
    if (!session || session.status !== "ended" || session.historyId || session.historyDeleted) return true;
    const durable = outbox.enqueue(session);
    outbox.sync();
    if (!durable) {
      window.alert("Este recorrido todavía no está guardado en la PC y el teléfono no pudo conservarlo. Reintentá el guardado antes de comenzar otro.");
      return false;
    }
    return true;
  }

  function updateSyncStatus() {
    if (!outbox) return;
    if (session && !session.historyDeleted && outbox.wasDeleted(session.sessionId)) {
      session.historyDeleted = true;
      persistSession(true);
    }
    if (session && !session.historyId && outbox.savedId(session.sessionId)) {
      session.historyId = outbox.savedId(session.sessionId);
      persistSession(true);
    }
    if (archivedSession && !archivedSession.historyId) archivedSession.historyId = outbox.savedId(archivedSession.sessionId);
    const visibleSession = archivedSession || session;
    const saved = Boolean(visibleSession?.historyId);
    if (saved && !hydratedHistoryIds.has(visibleSession.historyId)) {
      const id = visibleSession.historyId;
      hydratedHistoryIds.add(id);
      historyRequest(`${window.RadarDriveHistory.endpoint}${id}/`).then((record) => {
        visibleSession.details = record.session.details;
        if (visibleSession === session) persistSession(true);
        if (visibleSession === (archivedSession || session) && !summary.hidden) {
          buildSummaryList(); buildEncounterList(visibleSession); refreshPropertySource();
        }
      }).catch(() => { hydratedHistoryIds.delete(id); });
    }
    const deleted = visibleSession?.historyDeleted || (visibleSession && outbox.wasDeleted(visibleSession.sessionId));
    syncStatus.textContent = deleted ? "Este recorrido fue eliminado del historial. Podés eliminar también la copia local."
      : saved ? "Guardado en la PC · disponible en Mis recorridos"
      : outbox.busy ? "Guardando recorrido en la PC…"
      : outbox.errorFor(visibleSession?.sessionId) || outbox.error || "Pendiente de guardar en la PC. Se reintentará al volver la conexión.";
    syncStatus.classList.toggle("is-saved", saved);
    if (!saved && !outbox.durable) syncStatus.textContent += " Mantené esta página abierta hasta sincronizar.";
    syncRetry.hidden = saved || deleted || outbox.busy;
    deleteSessionButton.hidden = Boolean(archivedSession) || !(saved || deleted);
    historyDelete.hidden = !archivedSession?.historyId;
    const pendingCount = outbox.entries().length;
    const pendingLabel = pendingCount ? `Mis recorridos · ${pendingCount} pendiente${pendingCount === 1 ? "" : "s"}` : "Mis recorridos";
    document.getElementById("drive-history-open").textContent = pendingLabel;
    document.getElementById("drive-summary-history").textContent = pendingLabel;
  }

  function safeWebUrl(value) {
    try { const url = new URL(value); return url.protocol === "https:" && !url.username && !url.password ? url.href : null; }
    catch (_) { return null; }
  }

  function buildEncounterList(visibleSession) {
    encounterList.replaceChildren();
    const all = discovery.properties(visibleSession);
    const properties = discovery.filter(all, discoveryFilter.value, pendingPropertyIds());
    document.getElementById("drive-encounters-section").hidden = !all.length;
    const missing = properties.filter((property) => !Number.isFinite(property.latitude) || !Number.isFinite(property.longitude)).length;
    document.getElementById("drive-discovery-help").textContent = `${properties.length} propiedades · gris: descubiertas · coral: cerca ahora · violeta: favoritas. Pasar cerca no confirma una visita.${visibleSession.version < 3 ? " Este recorrido antiguo solo registraba encuentros a 120 m." : ""}${missing ? ` ${missing} sin coordenadas guardadas.` : ""}`;
    document.getElementById("drive-discovery-more").hidden = properties.length <= discoveryPageSize;
    properties.slice(0, discoveryPageSize).forEach((detail) => {
      const item = document.createElement("div");
      item.className = "drive-encounter-item";
      const id = detail.id;
      const title = document.createElement("strong");
      title.textContent = detail?.address_text || `Propiedad #${id}`;
      item.append(title);
      if (detail.image_url) {
        const image = document.createElement("img"); image.src = detail.image_url; image.alt = "Foto del aviso";
        image.loading = "lazy"; image.referrerPolicy = "no-referrer"; image.width = 90; image.height = 66;
        image.addEventListener("error", () => { image.hidden = true; }); item.append(image);
      }
      if (detail?.price_short) { const price = document.createElement("span"); price.textContent = detail.price_short; item.append(price); }
      const meta = document.createElement("span");
      meta.textContent = [detail.favorite ? "♥ Favorita" : "", detail.seen_before === false ? "Nueva para vos" : detail.seen_before === true ? "Ya descubierta antes" : "", detail.passedClose ? "Pasaste cerca" : "Detectada en el radio", detail.firstSeenAt ? new Date(detail.firstSeenAt).toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit" }) : ""].filter(Boolean).join(" · ");
      item.append(meta);
      if (Number.isFinite(detail.latitude) && Number.isFinite(detail.longitude)) {
        const locate = document.createElement("button"); locate.type = "button"; locate.textContent = "Ver casa en el mapa";
        locate.addEventListener("click", () => { focusPoint(detail); showGroup(detail.group_id, id); }); item.append(locate);
        const note = document.createElement("button"); note.type = "button"; note.textContent = "Observación / pendiente";
        note.addEventListener("click", () => fieldUI.openNew(detail)); item.append(note);
      }
      const url = safeWebUrl(detail?.original_url);
      if (url) {
        const link = document.createElement("a"); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
        link.textContent = "Ver publicación original ↗"; item.append(link);
      } else if (!detail.address_text) {
        const hint = document.createElement("span"); hint.textContent = "La ficha se completa al guardar en la PC; luego podés abrirla desde Mis recorridos."; item.append(hint);
      }
      encounterList.append(item);
    });
    updateNextSteps();
  }

  function updateNextSteps() {
    const visibleSession = archivedSession || session;
    if (!visibleSession) return;
    const notes = (fieldUI?.notes() || []).filter((note) => note.data.sessionId === visibleSession.sessionId);
    const pending = notes.filter((note) => note.data.action !== "none" && !note.data.done);
    const signs = notes.filter((note) => note.data.kind === "sign");
    document.getElementById("drive-next-steps").textContent = `${visibleSession.favoritePropertyIds?.length || 0} favoritas para revisar · ${notes.length} notas de campo · ${signs.length} carteles · ${pending.length} próximos pasos pendientes.`;
  }

  function focusPoint(point) {
    follow = false; updateFollowControls();
    if (!mapReady) return;
    focusedNote = point; refreshMemoryAndNotes();
    if (point.sessionId) {
      startPanel.hidden = true; summary.hidden = true; historyPanel.hidden = true;
      document.getElementById("drive-map-back").hidden = false;
    }
    const landscape = window.matchMedia("(orientation: landscape) and (max-height: 520px)").matches;
    map.setPadding({ top: 0, bottom: 0, left: 0, right: 0 });
    map.easeTo({ center: [point.longitude, point.latitude], zoom: 16, pitch: 0, bearing: 0,
      offset: landscape ? [-Math.round(innerWidth * 0.26), 30] : [0, (130 - Math.round(innerHeight * 0.45)) / 2] });
  }

  function restoreCurrentView() {
    archivedSession = null;
    historyPanel.hidden = true;
    if (session?.status === "ended") showSummary();
    else if (session?.status === "tracking") {
      summary.hidden = true; startPanel.hidden = false; startContent.hidden = true; recovery.hidden = false;
      setStatus("Recorrido interrumpido");
    } else showIdle();
    refreshAllSources();
  }

  function historyItem(item, pending = false) {
    const button = document.createElement("button");
    button.type = "button"; button.className = "drive-history-item";
    const title = document.createElement("strong"); title.textContent = tripDate(item.startedAt);
    const metrics = document.createElement("span");
    metrics.textContent = `${distanceLabel(item.distanceM || 0)} · ${utils.formatDuration(item.endedAt - item.startedAt)} · ${item.encounterCount ?? Object.keys(item.encounters || {}).length} ubicaciones`;
    const badge = document.createElement("small"); badge.textContent = pending ? "Pendiente en este teléfono · ver detalle" : "Guardado en la PC · ver recorrido";
    button.append(title, metrics, badge);
    button.addEventListener("click", async () => {
      const generation = ++historyGeneration;
      button.disabled = true;
      try {
        const record = pending ? { session: item } : await historyRequest(`${window.RadarDriveHistory.endpoint}${item.id}/`);
        if (generation !== historyGeneration) return;
        archivedSession = { ...record.session, historyId: record.id || outbox.savedId(item.sessionId) || null };
        archivedSession.filters = normalizeStoredFilters(archivedSession.filters);
        historyPanel.hidden = true;
        showSummary();
        refreshAllSources();
        summary.focus();
      } catch (error) { historyMessage.textContent = error.message; }
      finally { button.disabled = false; }
    });
    return button;
  }

  async function loadHistory(more = false) {
    const generation = ++historyGeneration;
    const url = more && nextHistoryPage ? nextHistoryPage : window.RadarDriveHistory.endpoint;
    if (!more) {
      historyList.replaceChildren();
      outbox.entries().sort((a, b) => b.startedAt - a.startedAt).forEach((item) => historyList.append(historyItem(item, true)));
    }
    historyMore.hidden = true;
    historyMessage.textContent = "Consultando recorridos en la PC…";
    try {
      const result = await historyRequest(url);
      if (generation !== historyGeneration) return;
      result.results.forEach((item) => historyList.append(historyItem(item)));
      nextHistoryPage = result.next;
      historyMore.hidden = !nextHistoryPage;
      historyMessage.textContent = result.count ? `${result.count} recorrido${result.count === 1 ? "" : "s"} guardado${result.count === 1 ? "" : "s"} en tu PC.` : "Aún no hay recorridos guardados en la PC. Aparecerán al finalizar uno.";
    } catch (error) {
      if (generation === historyGeneration) historyMessage.textContent = error.message;
    }
  }

  function openHistory() {
    if (tracking) return;
    startPanel.hidden = true; summary.hidden = true; historyPanel.hidden = false;
    document.getElementById("drive-history-title").focus();
    loadHistory();
  }

  document.getElementById("drive-history-open").addEventListener("click", openHistory);
  document.getElementById("drive-summary-history").addEventListener("click", openHistory);
  document.getElementById("drive-history-close").addEventListener("click", () => { historyGeneration += 1; restoreCurrentView(); });
  historyMore.addEventListener("click", () => loadHistory(true));
  document.getElementById("drive-history-refresh").addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    try { await outbox.sync(); await loadHistory(); }
    finally { document.getElementById("drive-history-refresh").disabled = false; }
  });
  syncRetry.addEventListener("click", () => { if (session?.status === "ended") outbox.enqueue(session); outbox.sync(); });
  window.addEventListener("online", () => outbox.sync());
  historyDelete.addEventListener("click", async () => {
    const record = archivedSession;
    if (!record?.historyId || !window.confirm("¿Eliminar este recorrido, sus notas y fotos del historial de la PC? Tus propiedades favoritas se conservan.")) return;
    historyDelete.disabled = true;
    try {
      await historyRequest(`${window.RadarDriveHistory.endpoint}${record.historyId}/`, { method: "DELETE" });
      fieldUI.clearSession(record.sessionId);
      if (session?.sessionId === record.sessionId) clearStoredSession();
      archivedSession = null;
      openHistory();
    } catch (error) { syncStatus.textContent = error.message; }
    finally { historyDelete.disabled = false; }
  });

  startButton.addEventListener("click", () => startTracking(false));
  resumeButton.addEventListener("click", () => startTracking(true));
  stopButton.addEventListener("click", stopTracking);
  recoverySummaryButton.addEventListener("click", () => {
    session.status = "ended";
    session.endedAt = Date.now();
    persistSession(true);
    outbox.enqueue(session);
    outbox.sync();
    showSummary();
  });
  recoveryDeleteButton.addEventListener("click", () => {
    if (window.confirm("¿Descartar este recorrido interrumpido? Todavía no está guardado en la PC.")) deleteSessionAndReset();
  });
  newSessionButton.addEventListener("click", deleteSessionAndReset);
  deleteSessionButton.addEventListener("click", () => {
    if ((session?.historyId || session?.historyDeleted) && window.confirm(session.historyDeleted
        ? "¿Eliminar la copia de este teléfono?"
        : "¿Eliminar la copia de este teléfono? El recorrido seguirá disponible en Mis recorridos.")) deleteSessionAndReset();
  });
  cardClose.addEventListener("click", () => {
    card.hidden = true;
    cardRequestToken += 1;
    if (!tracking && (archivedSession || session)?.status === "ended") showSummary();
    const groupId = selectedProperty?.group_id;
    if (tracking && session && groupId) {
      session.dismissedGroupIds = Array.from(new Set([...session.dismissedGroupIds, groupId]));
      persistSession(true);
    }
  });
  cardPrev.addEventListener("click", () => {
    if (currentGroupIndex > 0) {
      currentGroupIndex -= 1;
      currentDirection = "";
      renderCurrentCard();
    }
  });
  cardNext.addEventListener("click", () => {
    if (currentGroupIndex < currentGroup.length - 1) {
      currentGroupIndex += 1;
      currentDirection = "";
      renderCurrentCard();
    }
  });
  favoriteButton.addEventListener("click", toggleFavorite);
  originalLink.addEventListener("click", () => persistSession(true));
  recenterButton.addEventListener("click", () => {
    follow = true;
    if (lastPosition) cameraPosition = lastPosition;
    updateFollowControls();
    updateCamera();
  });
  logoutForm?.addEventListener("submit", (event) => {
    if (tracking || (session?.status === "tracking")) {
      event.preventDefault();
      window.alert("Finalizá el recorrido antes de cerrar sesión para poder guardarlo.");
      return;
    }
    if (outbox.entries().length || fieldUI?.pending().length) {
      event.preventDefault();
      window.alert("Hay recorridos o notas pendientes de guardar. Sincronizalos antes de cerrar sesión.");
      return;
    }
    try {
      localStorage.removeItem(SESSION_KEY);
      localStorage.removeItem(PREVIOUS_SESSION_KEY);
      localStorage.removeItem(LEGACY_SESSION_KEY);
    } catch (_error) { /* no-op */ }
  });
  document.addEventListener("visibilitychange", () => {
    if (tracking && document.visibilityState === "visible") requestWakeLock();
    if (document.visibilityState === "visible" && !tracking) outbox.sync();
    if (tracking && document.visibilityState === "hidden") persistSession(true);
  });
  window.addEventListener("pagehide", () => persistSession(true));

  [
    ...typeInputs,
    ...bedroomInputs,
    radiusInput,
    priceMinInput,
    priceMaxInput,
    coveredMinInput,
    landMinInput
  ].forEach((input) => input.addEventListener("input", updateFilterPreview));

  initializeStoredState();
  fieldUI = window.createDriveFieldUI({ ownerId: config.user_id, storage: localStorageAdapter, request: historyRequest,
    context: () => ({ session: archivedSession || session, position: lastPosition, tracking }),
    focusPoint,
    onChange: () => { updateNextSteps(); refreshMemoryAndNotes(); if (!tracking && (archivedSession || session)) buildEncounterList(archivedSession || session); },
  });
  if (session?.status === "ended") fieldUI.loadSession(session.sessionId);
  document.getElementById("drive-field-open").addEventListener("click", () => fieldUI.openNew());
  document.getElementById("drive-map-back").addEventListener("click", () => {
    document.getElementById("drive-map-back").hidden = true; focusedNote = null;
    if (!tracking) restoreCurrentView();
    fieldUI.open(); refreshMemoryAndNotes();
  });
  document.getElementById("drive-property-note").addEventListener("click", () => { if (selectedProperty) fieldUI.openNew(selectedProperty); });
  document.getElementById("drive-notebook-open").addEventListener("click", () => fieldUI.open());
  document.getElementById("drive-summary-notebook").addEventListener("click", () => fieldUI.open((archivedSession || session).sessionId));
  audioInput.addEventListener("change", updateAudio);
  audioToggle.addEventListener("click", () => { audioInput.checked = !audioInput.checked; updateAudio(); });
  document.getElementById("drive-memory").addEventListener("change", refreshMemoryAndNotes);
  window.addEventListener("resize", () => { if (!summary.hidden) window.setTimeout(fitTrace, 100); });
  discoveryFilter.addEventListener("change", () => { discoveryPageSize = 20; buildEncounterList(archivedSession || session); refreshPropertySource(); fitTrace(); });
  document.getElementById("drive-discovery-more").addEventListener("click", () => { discoveryPageSize += 20; buildEncounterList(archivedSession || session); });
  updateAudio();
  loadMemory();
  fieldUI.sync();
  outbox.sync();
})();
