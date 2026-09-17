"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const utils = require("./drive-utils.js");

test("distance and bearing use geographic coordinates", () => {
  const start = { latitude: 0, longitude: 0 };
  const north = { latitude: 0.001, longitude: 0 };
  assert.ok(Math.abs(utils.distanceMeters(start, north) - 111.2) < 0.5);
  assert.ok(Math.abs(utils.bearingDegrees(start, north)) < 0.1);
});

test("relative direction needs movement and distinguishes sides", () => {
  const position = { latitude: 0, longitude: 0, heading: 0, speed: 5 };
  assert.equal(utils.relativeDirection(position, { latitude: 0.001, longitude: 0 }), "adelante");
  assert.equal(utils.relativeDirection(position, { latitude: 0, longitude: 0.001 }), "a tu derecha");
  assert.equal(utils.relativeDirection({ ...position, speed: 0 }, { latitude: 0, longitude: 0.001 }), "");
});

test("trace filter rejects noise, old order and impossible jumps", () => {
  const first = { latitude: -34.59, longitude: -58.64, accuracy: 12, timestamp: 10000 };
  assert.equal(utils.evaluateTracePoint(null, first).accepted, true);
  assert.equal(utils.evaluateTracePoint(first, { ...first, timestamp: 12000 }).reason, "interval");
  assert.equal(utils.evaluateTracePoint(first, { ...first, timestamp: 9000 }).reason, "order");
  const jump = { latitude: -34.49, longitude: -58.64, accuracy: 12, timestamp: 14000 };
  assert.equal(utils.evaluateTracePoint(first, jump).reason, "jump");
});

test("trace simplification preserves endpoints and a meaningful corner", () => {
  const points = [
    { latitude: 0, longitude: 0 },
    { latitude: 0, longitude: 0.0001 },
    { latitude: 0.0002, longitude: 0.0001 },
    { latitude: 0.0002, longitude: 0.0002 }
  ];
  const simplified = utils.simplifyTrace(points, 5);
  assert.equal(simplified[0], points[0]);
  assert.equal(simplified.at(-1), points.at(-1));
  assert.ok(simplified.length >= 3);
});

test("proximity candidate skips announced groups and properties behind", () => {
  const now = 100000;
  const position = {
    latitude: 0,
    longitude: 0,
    accuracy: 10,
    timestamp: now,
    heading: 0,
    speed: 5
  };
  const properties = [
    { id: 1, group_id: "north", latitude: 0.0005, longitude: 0, group_suspicious: false },
    { id: 2, group_id: "south", latitude: -0.0002, longitude: 0, group_suspicious: false },
    { id: 3, group_id: "seen", latitude: 0.0001, longitude: 0, group_suspicious: false }
  ];
  const selected = utils.chooseProximityCandidate(
    properties,
    position,
    { announcedGroupIds: ["seen"], dismissedGroupIds: [] },
    { now }
  );
  assert.equal(selected.property.id, 1);
  assert.equal(selected.direction, "adelante");
});

test("filter normalization accepts formatted whole values", () => {
  const result = utils.normalizeDriveFilters({
    propertyTypes: ["house", "house", "apartment"],
    radiusM: "350",
    bedroomsMin: "3",
    priceMin: "100.000",
    priceMax: "250000",
    coveredAreaMinM2: "90",
    landAreaMinM2: "300"
  });
  assert.equal(result.valid, true);
  assert.deepEqual(result.filters, {
    propertyTypes: ["house", "apartment"],
    radiusM: 350,
    bedroomsMin: 3,
    priceMin: 100000,
    priceMax: 250000,
    coveredAreaMinM2: 90,
    landAreaMinM2: 300
  });
});

test("filter normalization reports invalid ranges and values", () => {
  const result = utils.normalizeDriveFilters({
    radiusM: 350,
    priceMin: "300000",
    priceMax: "200000",
    coveredAreaMinM2: "12,5"
  });
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((error) => error.includes("precio mínimo")));
  assert.ok(result.errors.some((error) => error.includes("Superficie cubierta")));
});
