"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const discovery = require("./drive-discovery.js");
const utils = require("./drive-utils.js");
const now = 1700000000000;
const position = { latitude: -34.59, longitude: -58.64, accuracy: 5, timestamp: now };
const property = { id: 1, latitude: -34.588, longitude: -58.64, group_id: "a", price_short: "USD 100k" };
const session = () => ({ encounters: {}, details: {}, favoritePropertyIds: [] });

test("a discovery beyond 120 m survives leaving the radius and keeps its first snapshot", () => {
  const trip = session();
  assert.equal(discovery.accumulate(trip, [property], position, new Set(), now).added, 1);
  assert.equal(discovery.properties(trip)[0].passedClose, false);
  discovery.accumulate(trip, [], { ...position, latitude: -34.58 }, new Set(), now);
  discovery.accumulate(trip, [{ ...property, price_short: "USD 120k" }], { ...position, latitude: property.latitude }, new Set(), now);
  const saved = discovery.properties(trip);
  assert.equal(saved.length, 1);
  assert.equal(saved[0].price_short, "USD 100k");
  assert.equal(saved[0].firstSeenAt, now);
  assert.equal(saved[0].passedClose, true);
  assert.equal(saved[0].seen_before, false);
});

test("shared coordinates retain all IDs, known status and selective review filters", () => {
  const trip = session();
  discovery.accumulate(trip, [property, { ...property, id: 2 }], position, new Set([2]), now);
  trip.favoritePropertyIds = [2];
  const properties = discovery.properties(trip);
  assert.equal(properties.length, 2);
  assert.equal(Object.keys(trip.encounters).length, 1);
  assert.deepEqual(discovery.filter(properties, "new").map((p) => p.id), [1]);
  assert.deepEqual(discovery.filter(properties, "favorites").map((p) => p.id), [2]);
  assert.deepEqual(discovery.filter(properties, "pending", new Set([1])).map((p) => p.id), [1]);
});

test("bad GPS does not create discoveries and unknown history is not called new", () => {
  const trip = session();
  discovery.accumulate(trip, [property], { ...position, accuracy: 300 }, null, now);
  assert.equal(discovery.properties(trip).length, 0);
  discovery.accumulate(trip, [property], position, null, now);
  assert.equal(discovery.properties(trip)[0].seen_before, null);
  assert.equal(discovery.filter(discovery.properties(trip), "new").length, 0);
});

test("legacy history renders saved coordinates without fabricating missing ones", () => {
  const trip = { encounters: { a: { propertyIds: [1, 2], minDistanceM: 90, firstSeenAt: now } }, details: { 1: { id: 1, latitude: -34.59, longitude: -58.64 } } };
  const properties = discovery.properties(trip);
  assert.equal(properties[0].group_id, "a");
  assert.equal(properties[1].latitude, undefined);
  assert.equal(properties[1].passedClose, true);
});

test("discovery limit is explicit and the maximum radius is 1500 m", () => {
  const trip = session();
  const many = Array.from({ length: discovery.MAX_PROPERTIES + 1 }, (_, i) => ({ ...property, id: i + 1, group_id: String(i) }));
  const result = discovery.accumulate(trip, many, position, null, now);
  assert.equal(result.limited, true);
  assert.equal(discovery.properties(trip).length, discovery.MAX_PROPERTIES);
  assert.equal(utils.normalizeDriveFilters({ radiusM: 1500 }).valid, true);
  assert.equal(utils.normalizeDriveFilters({ radiusM: 1501 }).valid, false);
});
