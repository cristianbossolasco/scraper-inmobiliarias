(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.RadarDriveUtils = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const EARTH_RADIUS_M = 6371008.8;

  function radians(value) {
    return value * Math.PI / 180;
  }

  function degrees(value) {
    return value * 180 / Math.PI;
  }

  function distanceMeters(a, b) {
    if (!a || !b) return Infinity;
    const dLat = radians(b.latitude - a.latitude);
    const dLng = radians(b.longitude - a.longitude);
    const lat1 = radians(a.latitude);
    const lat2 = radians(b.latitude);
    const value = Math.sin(dLat / 2) ** 2
      + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
    return 2 * EARTH_RADIUS_M * Math.asin(Math.sqrt(value));
  }

  function bearingDegrees(a, b) {
    if (!a || !b) return null;
    const lat1 = radians(a.latitude);
    const lat2 = radians(b.latitude);
    const dLng = radians(b.longitude - a.longitude);
    const y = Math.sin(dLng) * Math.cos(lat2);
    const x = Math.cos(lat1) * Math.sin(lat2)
      - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLng);
    if (Math.abs(x) < 1e-12 && Math.abs(y) < 1e-12) return null;
    return (degrees(Math.atan2(y, x)) + 360) % 360;
  }

  function signedAngleDifference(target, origin) {
    return ((target - origin + 540) % 360) - 180;
  }

  function relativeDirection(position, target) {
    if (position?.heading == null || position?.speed == null) return "";
    const heading = Number(position?.heading);
    const speed = Number(position?.speed);
    if (!Number.isFinite(heading) || !Number.isFinite(speed) || speed < 2) return "";
    const bearing = bearingDegrees(position, target);
    if (!Number.isFinite(bearing)) return "";
    const difference = signedAngleDifference(bearing, heading);
    const absolute = Math.abs(difference);
    if (absolute <= 35) return "adelante";
    if (absolute >= 155) return "detras";
    return difference > 0 ? "a tu derecha" : "a tu izquierda";
  }

  function usablePosition(position, now = Date.now(), options = {}) {
    if (!position) return false;
    const maxAccuracy = options.maxAccuracy ?? 60;
    const maxAgeMs = options.maxAgeMs ?? 10000;
    return Number.isFinite(position.latitude)
      && Number.isFinite(position.longitude)
      && Number.isFinite(position.accuracy)
      && position.accuracy <= maxAccuracy
      && Number.isFinite(position.timestamp)
      && now - position.timestamp >= -1000
      && now - position.timestamp <= maxAgeMs;
  }

  function evaluateTracePoint(previous, next, options = {}) {
    const maxAccuracy = options.maxAccuracy ?? 60;
    const minIntervalMs = options.minIntervalMs ?? 3000;
    const minDistanceM = options.minDistanceM ?? 15;
    const continuityMs = options.continuityMs ?? 30000;
    const maxSpeedKmh = options.maxSpeedKmh ?? 160;
    if (!next || !Number.isFinite(next.latitude) || !Number.isFinite(next.longitude)) {
      return { accepted: false, distanceM: 0, reason: "invalid" };
    }
    if (!Number.isFinite(next.accuracy) || next.accuracy > maxAccuracy) {
      return { accepted: false, distanceM: 0, reason: "accuracy" };
    }
    if (!Number.isFinite(next.timestamp)) {
      return { accepted: false, distanceM: 0, reason: "timestamp" };
    }
    if (!previous) return { accepted: true, distanceM: 0, reason: "first" };
    const elapsedMs = next.timestamp - previous.timestamp;
    if (elapsedMs <= 0) return { accepted: false, distanceM: 0, reason: "order" };
    const distanceM = distanceMeters(previous, next);
    const speedKmh = distanceM / (elapsedMs / 1000) * 3.6;
    if (speedKmh > maxSpeedKmh) {
      return { accepted: false, distanceM: 0, reason: "jump" };
    }
    if (elapsedMs < minIntervalMs) {
      return { accepted: false, distanceM: 0, reason: "interval" };
    }
    if (distanceM >= minDistanceM) {
      return { accepted: true, distanceM, reason: "movement" };
    }
    if (elapsedMs >= continuityMs) {
      return { accepted: true, distanceM: 0, reason: "continuity" };
    }
    return { accepted: false, distanceM: 0, reason: "noise" };
  }

  function projectedPoint(point, originLatitude) {
    return {
      x: radians(point.longitude) * EARTH_RADIUS_M * Math.cos(radians(originLatitude)),
      y: radians(point.latitude) * EARTH_RADIUS_M
    };
  }

  function segmentDistance(point, start, end, originLatitude) {
    const p = projectedPoint(point, originLatitude);
    const a = projectedPoint(start, originLatitude);
    const b = projectedPoint(end, originLatitude);
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    if (dx === 0 && dy === 0) return Math.hypot(p.x - a.x, p.y - a.y);
    const t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / (dx ** 2 + dy ** 2)));
    return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
  }

  function simplifyTrace(points, toleranceM = 8) {
    if (!Array.isArray(points) || points.length <= 2) return Array.isArray(points) ? points.slice() : [];
    const keep = new Uint8Array(points.length);
    keep[0] = 1;
    keep[points.length - 1] = 1;
    const stack = [[0, points.length - 1]];
    const originLatitude = points[0].latitude;
    while (stack.length) {
      const [startIndex, endIndex] = stack.pop();
      let maxDistance = 0;
      let maxIndex = -1;
      for (let index = startIndex + 1; index < endIndex; index += 1) {
        const distance = segmentDistance(
          points[index],
          points[startIndex],
          points[endIndex],
          originLatitude
        );
        if (distance > maxDistance) {
          maxDistance = distance;
          maxIndex = index;
        }
      }
      if (maxIndex >= 0 && maxDistance > toleranceM) {
        keep[maxIndex] = 1;
        stack.push([startIndex, maxIndex], [maxIndex, endIndex]);
      }
    }
    return points.filter((_point, index) => keep[index]);
  }

  function chooseProximityCandidate(properties, position, session, options = {}) {
    if (!usablePosition(position, options.now ?? Date.now(), options)) return null;
    const enterM = options.enterM ?? 120;
    const announced = new Set(session?.announcedGroupIds || []);
    const dismissed = new Set(session?.dismissedGroupIds || []);
    const groups = new Map();
    (properties || []).forEach((property) => {
      if (property.group_suspicious || announced.has(property.group_id) || dismissed.has(property.group_id)) return;
      const distanceM = distanceMeters(position, property);
      if (distanceM > enterM) return;
      const existing = groups.get(property.group_id);
      if (!existing || distanceM < existing.distanceM) {
        groups.set(property.group_id, { property, distanceM });
      }
    });
    const ranked = Array.from(groups.values()).map((item) => {
      const direction = relativeDirection(position, item.property);
      const directionRank = direction === "detras" ? 3 : direction === "adelante" ? 0 : 1;
      return { ...item, direction, directionRank };
    }).filter((item) => item.directionRank < 3);
    ranked.sort((a, b) => a.directionRank - b.directionRank || a.distanceM - b.distanceM || a.property.id - b.property.id);
    return ranked[0] || null;
  }

  function formatDuration(milliseconds) {
    const totalMinutes = Math.max(0, Math.round(milliseconds / 60000));
    if (totalMinutes < 60) return `${totalMinutes} min`;
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    return minutes ? `${hours} h ${minutes} min` : `${hours} h`;
  }

  function optionalWholeNumber(rawValue, label, maximum) {
    const normalized = String(rawValue ?? "").trim().replace(/[.\s]/g, "");
    if (!normalized) return { value: null, error: "" };
    if (!/^\d+$/.test(normalized)) {
      return { value: null, error: `${label} debe ser un número entero.` };
    }
    const value = Number(normalized);
    if (!Number.isSafeInteger(value) || value > maximum) {
      return { value: null, error: `${label} está fuera de rango.` };
    }
    return { value, error: "" };
  }

  function normalizeDriveFilters(draft) {
    const errors = [];
    const propertyTypes = Array.from(new Set(
      (Array.isArray(draft?.propertyTypes) ? draft.propertyTypes : [])
        .filter((value) => typeof value === "string" && value)
    ));
    const radiusM = Number(draft?.radiusM);
    if (!Number.isInteger(radiusM) || radiusM < 200 || radiusM > 1500) {
      errors.push("El radio debe estar entre 200 y 1.500 metros.");
    }
    const fields = {
      bedroomsMin: optionalWholeNumber(draft?.bedroomsMin, "Dormitorios", 12),
      priceMin: optionalWholeNumber(draft?.priceMin, "Precio mínimo", 5000000),
      priceMax: optionalWholeNumber(draft?.priceMax, "Precio máximo", 5000000),
      coveredAreaMinM2: optionalWholeNumber(draft?.coveredAreaMinM2, "Superficie cubierta", 100000),
      landAreaMinM2: optionalWholeNumber(draft?.landAreaMinM2, "Superficie del terreno", 100000)
    };
    Object.values(fields).forEach((field) => {
      if (field.error) errors.push(field.error);
    });
    if (
      fields.priceMin.value !== null
      && fields.priceMax.value !== null
      && fields.priceMin.value > fields.priceMax.value
    ) {
      errors.push("El precio mínimo no puede superar al máximo.");
    }
    return {
      valid: errors.length === 0,
      errors,
      filters: {
        propertyTypes,
        radiusM: Number.isInteger(radiusM) ? radiusM : 350,
        bedroomsMin: fields.bedroomsMin.value,
        priceMin: fields.priceMin.value,
        priceMax: fields.priceMax.value,
        coveredAreaMinM2: fields.coveredAreaMinM2.value,
        landAreaMinM2: fields.landAreaMinM2.value
      }
    };
  }

  return {
    bearingDegrees,
    chooseProximityCandidate,
    distanceMeters,
    evaluateTracePoint,
    formatDuration,
    normalizeDriveFilters,
    relativeDirection,
    signedAngleDifference,
    simplifyTrace,
    usablePosition
  };
});
