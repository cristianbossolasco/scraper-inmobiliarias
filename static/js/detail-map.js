(() => {
  if (typeof maplibregl === "undefined") {
    return;
  }
  if (window.lucide) {
    window.lucide.createIcons();
  }

  const mapNode = document.getElementById("detail-map");
  const payloadNode = document.getElementById("property-location");
  const configNode = document.getElementById("map-config");
  if (!mapNode || !payloadNode || !configNode) {
    return;
  }

  const location = JSON.parse(payloadNode.textContent);
  const config = JSON.parse(configNode.textContent);
  const hasLocation = location && location.latitude !== null && location.longitude !== null;
  const center = hasLocation
    ? [location.longitude, location.latitude]
    : config.center;

  const map = new maplibregl.Map({
    container: "detail-map",
    style: {
      version: 8,
      sources: {
        osm: {
          type: "raster",
          tiles: [config.tile_url],
          tileSize: 256,
          attribution: config.attribution,
        },
      },
      layers: [{ id: "osm", type: "raster", source: "osm" }],
    },
    center,
    zoom: hasLocation ? (location.precision === "neighborhood" ? 13 : 16) : (config.zoom || 12),
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

  const edit = document.getElementById("edit-location");
  const save = document.getElementById("save-location");
  const label = document.getElementById("location-label");
  const help = document.getElementById("location-help");
  let marker = null;
  let markerElement = null;

  function markerClass(precision, editing = false) {
    const exact = precision === "exact" || precision === "manual";
    return `detail-marker ${exact ? "exact" : "approximate"}${editing ? " editing" : ""}`;
  }

  function ensureMarker(lngLat, draggable = false) {
    if (!markerElement) {
      markerElement = document.createElement("div");
      markerElement.className = markerClass(location.precision);
    }
    if (!marker) {
      marker = new maplibregl.Marker({ element: markerElement, draggable })
        .setLngLat(lngLat)
        .addTo(map);
    } else {
      marker.setLngLat(lngLat);
      marker.setDraggable(draggable);
    }
    markerElement.className = markerClass(location.precision, draggable);
    return marker;
  }

  if (hasLocation) {
    ensureMarker(center, false);
  }

  if (!edit || !save) {
    return;
  }

  function enterEditMode() {
    const editableMarker = ensureMarker(marker ? marker.getLngLat() : map.getCenter(), true);
    edit.hidden = true;
    save.hidden = false;
    markerElement.className = markerClass("manual", true);
    if (label) {
      label.textContent = hasLocation ? "Moviendo pin" : "Elegi la ubicacion";
    }
    editableMarker.setDraggable(true);
  }

  edit.addEventListener("click", enterEditMode);
  map.on("click", (event) => {
    if (save.hidden) {
      return;
    }
    ensureMarker(event.lngLat, true);
  });

  save.addEventListener("click", () => {
    if (!marker) {
      return;
    }
    const point = marker.getLngLat();
    save.disabled = true;
    fetch(`/api/propiedad/${location.id}/ubicacion/`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrf(),
      },
      body: JSON.stringify({ latitude: point.lat, longitude: point.lng }),
    })
      .then((response) => {
        if (!response.ok) {
          throw new Error("No se pudo guardar");
        }
        return response.json();
      })
      .then(() => {
        location.precision = "manual";
        location.has_location = true;
        marker.setDraggable(false);
        markerElement.className = markerClass("manual", false);
        edit.hidden = false;
        save.hidden = true;
        if (label) {
          label.textContent = "Confirmada manualmente";
        }
        if (help) {
          help.textContent = "Esta ubicacion no sera reemplazada por scrapers";
        }
      })
      .catch((error) => {
        if (label) {
          label.textContent = error.message;
        }
      })
      .finally(() => {
        save.disabled = false;
      });
  });

  function csrf() {
    const cookie = document.cookie
      .split(";")
      .map((item) => item.trim())
      .find((item) => item.startsWith("csrftoken="));
    return cookie ? decodeURIComponent(cookie.split("=")[1]) : "";
  }
})();
