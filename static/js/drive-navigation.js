(function (root, factory) {
  "use strict";
  const utils = typeof module === "object" && module.exports
    ? require("./drive-utils.js") : root.RadarDriveUtils;
  const api = factory(utils);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.RadarDriveNavigation = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (utils) {
  "use strict";

  function optionalNumber(value) {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function normalizePosition(raw, now = Date.now()) {
    const coords = raw?.coords;
    if (!coords) return null;
    const latitude = optionalNumber(coords.latitude);
    const longitude = optionalNumber(coords.longitude);
    if (latitude === null || longitude === null || Math.abs(latitude) > 90 || Math.abs(longitude) > 180) return null;
    const accuracy = optionalNumber(coords.accuracy);
    const speed = optionalNumber(coords.speed);
    const heading = optionalNumber(coords.heading);
    return {
      latitude,
      longitude,
      accuracy: accuracy !== null && accuracy >= 0 ? accuracy : null,
      speed: speed !== null && speed >= 0 ? speed : null,
      heading: heading !== null && heading >= 0 && heading <= 360 ? heading % 360 : null,
      timestamp: raw.timestamp === undefined ? now : optionalNumber(raw.timestamp)
    };
  }

  function createTracker(options = {}) {
    const settings = {
      maxAccuracy: 60,
      headingAccuracy: 30,
      maxAgeMs: 10000,
      maxSpeedMps: 55,
      startSpeedMps: 2.5,
      stopSpeedMps: 2,
      motionMemoryMs: 4000,
      anchorMaxAgeMs: 12000,
      ...options
    };
    let previous = null;
    let anchor = null;
    let cameraHeading = null;
    let moving = false;
    let lastMotionAt = null;
    let lastHeadingAt = null;

    function reset() {
      previous = null;
      anchor = null;
      cameraHeading = null;
      moving = false;
      lastMotionAt = null;
      lastHeadingAt = null;
    }

    function update(raw, now = Date.now()) {
      const position = normalizePosition(raw, now);
      function result(canFollow, reason) {
        return { position, cameraHeading, canFollow, moving, reason };
      }
      if (!position) return result(false, "invalid");
      if (position.accuracy === null || position.accuracy > settings.maxAccuracy) return result(false, "accuracy");
      if (position.timestamp === null || position.timestamp > now + 1000 || now - position.timestamp > settings.maxAgeMs) {
        return result(false, "stale");
      }
      if (previous) {
        const elapsed = (position.timestamp - previous.timestamp) / 1000;
        if (elapsed <= 0) return result(false, "order");
        // Allow reported GPS uncertainty, but never move the baseline to an outlier.
        const distance = utils.distanceMeters(previous, position);
        if (distance > settings.maxSpeedMps * elapsed + previous.accuracy + position.accuracy) {
          return result(false, "jump");
        }
      }
      previous = { ...position };

      if (position.accuracy > settings.headingAccuracy) {
        anchor = null;
        moving = false;
        position.heading = cameraHeading;
        return result(true, "heading-accuracy");
      }

      if (lastMotionAt === null || position.timestamp - lastMotionAt > settings.motionMemoryMs) moving = false;
      if (position.speed !== null && position.speed < settings.stopSpeedMps) {
        moving = false;
        lastMotionAt = null;
        anchor = { ...position };
        position.heading = cameraHeading;
        return result(true, "stopped");
      }
      if (!anchor || position.timestamp - anchor.timestamp > settings.anchorMaxAgeMs) {
        anchor = { ...position };
        position.heading = cameraHeading;
        return result(true, "awaiting-movement");
      }

      const elapsedMs = position.timestamp - anchor.timestamp;
      const distance = utils.distanceMeters(anchor, position);
      const uncertainty = anchor.accuracy + position.accuracy;
      // A device-reported speed AND course supply additional evidence. Avoid a
      // full accuracy-circle delay at each urban corner when both are present;
      // inferred courses still require displacement above the GPS uncertainty.
      const minDistance = position.speed === null ? Math.max(10, uncertainty)
        : position.heading === null ? Math.max(8, uncertainty * 0.75)
          : Math.max(8, Math.min(15, uncertainty * 0.5));
      if (elapsedMs < 700 || distance < minDistance) {
        position.heading = cameraHeading;
        return result(true, moving ? "moving" : "awaiting-movement");
      }

      const course = utils.bearingDegrees(anchor, position);
      const inferredSpeed = distance / (elapsedMs / 1000);
      anchor = { ...position };
      if (position.speed === null) position.speed = inferredSpeed;
      const threshold = moving ? settings.stopSpeedMps : settings.startSpeedMps;
      if (position.speed < threshold || position.speed > settings.maxSpeedMps || inferredSpeed < settings.stopSpeedMps) {
        moving = false;
        position.heading = cameraHeading;
        return result(true, "slow");
      }

      moving = true;
      lastMotionAt = position.timestamp;
      let candidate = position.heading;
      // A displaced, accurate fix corroborates the heading; use its course when
      // the device reports a wildly contradictory bearing (common GPS spikes).
      if (candidate === null || Math.abs(utils.signedAngleDifference(candidate, course)) > 70) candidate = course;
      if (cameraHeading === null) {
        cameraHeading = candidate;
      } else {
        const seconds = (position.timestamp - lastHeadingAt) / 1000;
        const alpha = Math.min(0.75, Math.max(0.2, 1 - Math.exp(-seconds / 0.9)));
        cameraHeading = (cameraHeading + alpha * utils.signedAngleDifference(candidate, cameraHeading) + 360) % 360;
      }
      lastHeadingAt = position.timestamp;
      position.heading = cameraHeading;
      return result(true, "moving");
    }

    return { update, reset };
  }

  return { createTracker, normalizePosition };
});
