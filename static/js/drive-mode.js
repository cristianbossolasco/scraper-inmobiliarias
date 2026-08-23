(() => {
  "use strict";

  const mapNode = document.getElementById("drive-map");
  const configNode = document.getElementById("drive-map-config");
  if (!mapNode || !configNode || typeof maplibregl === "undefined") return;

  const config = JSON.parse(configNode.textContent);
  const status = document.getElementById("drive-status");
  const statusText = status.querySelector("span");
  const count = document.getElementById("drive-count");
  const startPanel = document.getElementById("drive-start-panel");
  const startButton = document.getElementById("drive-start");
  const controls = document.getElementById("drive-controls");
  const stopButton = document.getElementById("drive-stop");
  const recenterButton = document.getElementById("drive-recenter");
  const typeInput = document.getElementById("drive-property-type");
  const radiusInput = document.getElementById("drive-radius");
  const card = document.getElementById("drive-property-card");
  const cardClose = document.getElementById("drive-card-close");
  const cardDistance = document.getElementById("drive-card-distance");
  const cardPrice = document.getElementById("drive-card-price");
  const cardFacts = document.getElementById("drive-card-facts");
  const cardLocation = document.getElementById("drive-card-location");
  const favoriteButton = document.getElementById("drive-favorite");
  let watchId = null;
  let wakeLock = null;
  let tracking = false;
  let follow = true;
  let lastPosition = null;
  let lastQueryPosition = null;
  let lastQueryAt = 0;
  let pendingRequest = null;
  let latestProperties = [];
  let selectedProperty = null;

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

  map.on("load", () => {
    map.addSource("drive-properties", {
      type: "geojson",
      data: { type: "FeatureCollection", features: [] }
    });
    map.addLayer({
      id: "drive-property-dots",
      type: "circle",
      source: "drive-properties",
      paint: {
        "circle-color": "#ff6659",
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
        "text-size": 13,
        "text-font": ["Open Sans Bold"],
        "text-offset": [0, -1.5],
        "text-anchor": "bottom",
        "text-padding": 7,
        "text-allow-overlap": false
      },
      paint: {
        "text-color": "#ffffff",
        "text-halo-color": "#ff6659",
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
      paint: {
        "circle-radius": 24,
        "circle-color": "rgba(8,117,245,.16)",
        "circle-stroke-width": 0
      }
    });
    map.addLayer({
      id: "drive-user-dot",
      type: "circle",
      source: "drive-user",
      paint: {
        "circle-radius": 8,
        "circle-color": "#0875f5",
        "circle-stroke-color": "#ffffff",
        "circle-stroke-width": 3
      }
    });
    map.on("click", "drive-property-dots", showPropertyFromMap);
    map.on("click", "drive-property-prices", showPropertyFromMap);
    map.on("dragstart", () => {
      if (tracking) {
        follow = false;
        recenterButton.hidden = false;
      }
    });
  });

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

  function radians(value) {
    return value * Math.PI / 180;
  }

  function distanceMeters(a, b) {
    if (!a || !b) return Infinity;
    const earthRadius = 6371008.8;
    const dLat = radians(b.latitude - a.latitude);
    const dLng = radians(b.longitude - a.longitude);
    const lat1 = radians(a.latitude);
    const lat2 = radians(b.latitude);
    const value = Math.sin(dLat / 2) ** 2
      + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
    return 2 * earthRadius * Math.asin(Math.sqrt(value));
  }

  function propertyFeatures(properties) {
    const groups = new Map();
    properties.forEach((property) => {
      if (!groups.has(property.group_id)) groups.set(property.group_id, property);
    });
    return Array.from(groups.values()).map((property) => ({
      type: "Feature",
      id: property.id,
      geometry: { type: "Point", coordinates: [property.longitude, property.latitude] },
      properties: {
        id: property.id,
        group_id: property.group_id,
        group_count: property.group_count,
        marker_label: property.group_count > 1
          ? `${property.group_count} · ${property.group_price_short || "varias"}`
          : property.price_short
      }
    }));
  }

  function refreshPropertySource() {
    const source = map.getSource("drive-properties");
    if (!source) return;
    source.setData({
      type: "FeatureCollection",
      features: propertyFeatures(latestProperties)
    });
  }

  function updateUserSource(position) {
    const source = map.getSource("drive-user");
    if (!source) return;
    source.setData({
      type: "FeatureCollection",
      features: [{
        type: "Feature",
        geometry: { type: "Point", coordinates: [position.longitude, position.latitude] },
        properties: { accuracy: position.accuracy }
      }]
    });
  }

  async function fetchNearby(position, force = false) {
    const now = Date.now();
    const moved = distanceMeters(lastQueryPosition, position);
    if (!force && lastQueryPosition && (moved < 60 || now - lastQueryAt < 5000)) return;
    lastQueryPosition = position;
    lastQueryAt = now;
    if (pendingRequest) pendingRequest.abort();
    pendingRequest = new AbortController();
    const propertyType = typeInput.value;
    try {
      const response = await fetch("/api/recorrido/cercanas/", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
        body: JSON.stringify({
          latitude: position.latitude,
          longitude: position.longitude,
          radius_m: Number(radiusInput.value),
          property_types: propertyType ? [propertyType] : []
        }),
        signal: pendingRequest.signal
      });
      if (response.status === 401) {
        window.location.href = "/accounts/login/?next=/recorrido/";
        return;
      }
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || "No se pudieron consultar propiedades.");
      latestProperties = payload.properties || [];
      refreshPropertySource();
      const uniqueGroups = new Set(latestProperties.map((item) => item.group_id)).size;
      count.textContent = `${uniqueGroups} ${uniqueGroups === 1 ? "propiedad" : "propiedades"} cerca`;
      count.hidden = false;
      setStatus(position.accuracy > 80 ? `GPS impreciso · ±${Math.round(position.accuracy)} m` : "Recorrido activo", position.accuracy > 80 ? "" : "active");
    } catch (error) {
      if (error.name === "AbortError") return;
      setStatus("Sin conexión con el Radar", "error");
    }
  }

  function handlePosition(result) {
    const position = {
      latitude: result.coords.latitude,
      longitude: result.coords.longitude,
      accuracy: result.coords.accuracy,
      heading: result.coords.heading
    };
    lastPosition = position;
    updateUserSource(position);
    if (follow) {
      map.easeTo({
        center: [position.longitude, position.latitude],
        zoom: Math.max(map.getZoom(), 16),
        bearing: Number.isFinite(position.heading) ? position.heading : map.getBearing(),
        pitch: 35,
        duration: 500
      });
    }
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
    if (!("wakeLock" in navigator)) return;
    try {
      wakeLock = await navigator.wakeLock.request("screen");
    } catch (_error) {
      wakeLock = null;
    }
  }

  async function startTracking() {
    if (!("geolocation" in navigator)) {
      setStatus("Este navegador no ofrece GPS", "error");
      return;
    }
    startButton.disabled = true;
    setStatus("Solicitando ubicación...");
    tracking = true;
    follow = true;
    startPanel.hidden = true;
    controls.hidden = false;
    recenterButton.hidden = true;
    await requestWakeLock();
    watchId = navigator.geolocation.watchPosition(handlePosition, handleLocationError, {
      enableHighAccuracy: true,
      maximumAge: 3000,
      timeout: 12000
    });
  }

  async function stopTracking() {
    tracking = false;
    if (watchId !== null) navigator.geolocation.clearWatch(watchId);
    watchId = null;
    if (pendingRequest) pendingRequest.abort();
    pendingRequest = null;
    if (wakeLock) await wakeLock.release().catch(() => {});
    wakeLock = null;
    controls.hidden = true;
    recenterButton.hidden = true;
    card.hidden = true;
    startPanel.hidden = false;
    startButton.disabled = false;
    startPanel.querySelector("h1").textContent = "Recorrido finalizado";
    startPanel.querySelector("p:not(.drive-eyebrow)").textContent = `${new Set(latestProperties.map((item) => item.id)).size} propiedades detectadas en la última actualización.`;
    startButton.textContent = "Iniciar otro recorrido";
    setStatus("Recorrido finalizado", "active");
  }

  function showProperty(property) {
    if (!property) return;
    selectedProperty = property;
    cardDistance.textContent = property.group_count > 1
      ? `${property.group_count} propiedades en este punto`
      : `A ${property.distance_m} m`;
    cardPrice.textContent = property.group_count > 1
      ? `Desde ${property.group_price_short || property.price_short}`
      : property.price_short;
    const facts = [property.type_label];
    if (property.bedrooms !== null) facts.push(`${property.bedrooms} dorm.`);
    if (property.area_m2 !== null) facts.push(`${Math.round(property.area_m2)} m²`);
    cardFacts.textContent = facts.join(" • ");
    const labels = {
      confirmed: "Ubicación confirmada manualmente",
      address: "Ubicación calculada por dirección",
      published: "Ubicación publicada; puede ser aproximada"
    };
    cardLocation.textContent = property.group_suspicious
      ? "Varias publicaciones comparten esta coordenada; puede representar una zona."
      : labels[property.location_reliability];
    favoriteButton.classList.toggle("active", property.is_favorite);
    favoriteButton.textContent = property.is_favorite ? "♥ Guardada" : "♡ Guardar";
    card.hidden = false;
  }

  function showPropertyFromMap(event) {
    const groupId = event.features?.[0]?.properties?.group_id;
    showProperty(latestProperties.find((item) => item.group_id === groupId));
  }

  async function toggleFavorite() {
    if (!selectedProperty) return;
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
      showProperty(selectedProperty);
    } catch (_error) {
      setStatus("No se pudo guardar la propiedad", "error");
    } finally {
      favoriteButton.disabled = false;
    }
  }

  startButton.addEventListener("click", startTracking);
  stopButton.addEventListener("click", stopTracking);
  cardClose.addEventListener("click", () => { card.hidden = true; });
  favoriteButton.addEventListener("click", toggleFavorite);
  recenterButton.addEventListener("click", () => {
    follow = true;
    recenterButton.hidden = true;
    if (lastPosition) handlePosition({ coords: lastPosition });
  });
  [typeInput, radiusInput].forEach((input) => input.addEventListener("change", () => {
    if (lastPosition) fetchNearby(lastPosition, true);
  }));
  document.addEventListener("visibilitychange", () => {
    if (tracking && document.visibilityState === "visible" && !wakeLock) requestWakeLock();
  });
})();
