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
  const defaultCenter = Array.isArray(config.center) ? config.center : [-58.641, -34.606];
  const hasLocation = location && Number.isFinite(Number(location.latitude)) && Number.isFinite(Number(location.longitude));
  const center = hasLocation
    ? [Number(location.longitude), Number(location.latitude)]
    : defaultCenter;

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
  const geocode = document.getElementById("geocode-location");
  const save = document.getElementById("save-location");
  const label = document.getElementById("location-label");
  const help = document.getElementById("location-help");
  let marker = null;
  let markerElement = null;

  function markerClass(precision, editing = false) {
    const exact = precision === "exact" || precision === "manual";
    return `detail-marker ${exact ? "exact" : "approximate"}${editing ? " editing" : ""}`;
  }

  function precisionLabel(precision) {
    const labels = {
      exact: "Exacta",
      intersection: "Interseccion",
      street: "Calle",
      neighborhood: "Barrio",
      manual: "Confirmada manualmente",
    };
    return labels[precision] || "Ubicacion";
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

  function applyLocationPayload(nextLocation, message) {
    const latitude = Number(nextLocation && nextLocation.latitude);
    const longitude = Number(nextLocation && nextLocation.longitude);
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) {
      throw new Error("La geocodificacion no devolvio coordenadas.");
    }
    location.latitude = latitude;
    location.longitude = longitude;
    location.precision = nextLocation.precision || "";
    location.provider = nextLocation.provider || "";
    location.outside_target = Boolean(nextLocation.outside_target);
    location.manually_corrected = Boolean(nextLocation.manually_corrected);
    location.has_location = true;

    const nextCenter = [longitude, latitude];
    ensureMarker(nextCenter, false);
    map.flyTo({ center: nextCenter, zoom: location.precision === "neighborhood" ? 13 : 16 });
    if (label) {
      label.textContent = precisionLabel(location.precision);
    }
    if (help) {
      help.textContent = location.outside_target
        ? "Fuera del area objetivo"
        : (message || "Ubicacion y zona recalculadas");
    }
    if (edit) {
      edit.hidden = false;
    }
    if (save) {
      save.hidden = true;
    }
  }

  if (hasLocation) {
    ensureMarker(center, false);
  }

  if (!edit || !save) {
    return;
  }

  async function locateFromAddress() {
    if (!geocode) {
      return;
    }
    const previousLabel = label ? label.textContent : "";
    const previousHelp = help ? help.textContent : "";
    geocode.disabled = true;
    edit.disabled = true;
    if (label) {
      label.textContent = "Ubicando direccion...";
    }
    if (help) {
      help.textContent = location.geocode_address_label || "Consultando geocoder";
    }
    try {
      const response = await fetch(`/api/propiedad/${location.id}/inferir-territorio/`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrf(),
        },
        body: JSON.stringify({}),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.error || "No se pudo ubicar por direccion.");
      }
      applyLocationPayload(data.location, data.message);
    } catch (error) {
      if (label) {
        label.textContent = error.message;
      }
      if (help) {
        help.textContent = previousHelp || "Podes corregir el pin manualmente";
      }
    } finally {
      geocode.disabled = false;
      edit.disabled = false;
      if (label && label.textContent === "Ubicando direccion...") {
        label.textContent = previousLabel;
      }
    }
  }

  function enterEditMode() {
    const editableMarker = ensureMarker(marker ? marker.getLngLat() : map.getCenter(), true);
    edit.hidden = true;
    save.hidden = false;
    markerElement.className = markerClass("manual", true);
    if (label) {
      label.textContent = location.has_location ? "Moviendo pin" : "Elegi la ubicacion";
    }
    editableMarker.setDraggable(true);
  }

  if (geocode) {
    geocode.addEventListener("click", locateFromAddress);
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
        location.manually_corrected = true;
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
