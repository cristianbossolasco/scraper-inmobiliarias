"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const navigation = require("./drive-navigation.js");
const utils = require("./drive-utils.js");

function fix(eastM, northM, timestamp, overrides = {}) {
  return {
    coords: { latitude: northM / 111195, longitude: eastM / 111195, accuracy: 5, heading: null, speed: null, ...overrides },
    timestamp
  };
}

function update(tracker, eastM, northM, timestamp, overrides = {}) {
  return tracker.update(fix(eastM, northM, timestamp, overrides), timestamp);
}

test("missing heading and speed stay unknown instead of becoming north and zero", () => {
  const position = navigation.normalizePosition(fix(0, 0, 1000));
  assert.equal(position.heading, null);
  assert.equal(position.speed, null);
  assert.equal(navigation.normalizePosition(fix(0, 0, 1000, { heading: 0, speed: 0 })).heading, 0);
  const tracker = navigation.createTracker();
  assert.equal(update(tracker, 0, 0, 1000).cameraHeading, null);
});

test("real displacement establishes heading, stationary headings never rotate it", () => {
  const tracker = navigation.createTracker();
  update(tracker, 0, 0, 1000, { heading: 90, speed: 10 });
  assert.equal(update(tracker, 10, 0, 2000, { heading: 90, speed: 10 }).cameraHeading, 90);
  for (let index = 0; index < 15; index += 1) {
    const result = update(tracker, 10 + index % 3, index % 2, 3000 + index * 1000, { heading: (index * 37) % 360, speed: 0 });
    assert.equal(result.cameraHeading, 90);
    assert.equal(result.moving, false);
  }
});

test("GPS noise with missing speed does not invent movement or orientation", () => {
  const tracker = navigation.createTracker();
  for (let index = 0; index < 30; index += 1) {
    const result = update(tracker, (index % 5) * 2, (index % 3) * 2, 1000 + index * 1000, { accuracy: 15, heading: index * 10 });
    assert.equal(result.cameraHeading, null);
    assert.equal(result.moving, false);
  }
});

test("missing speed and heading are inferred from sufficiently accurate movement", () => {
  const tracker = navigation.createTracker();
  update(tracker, 0, 0, 1000);
  assert.equal(update(tracker, 4, 0, 2000).cameraHeading, null);
  assert.equal(update(tracker, 8, 0, 3000).cameraHeading, null);
  const result = update(tracker, 12, 0, 4000);
  assert.equal(result.moving, true);
  assert.ok(Math.abs(result.position.speed - 4) < 0.01);
  assert.ok(Math.abs(result.cameraHeading - 90) < 0.01);
});

test("hysteresis maintains a slow-moving car then freezes it below stop speed", () => {
  const tracker = navigation.createTracker();
  update(tracker, 0, 0, 1000, { heading: 90, speed: 10 });
  update(tracker, 10, 0, 2000, { heading: 90, speed: 10 });
  assert.equal(update(tracker, 19, 0, 6000, { heading: 90, speed: 2.25 }).moving, true);
  const stopped = update(tracker, 21, 0, 7000, { heading: 15, speed: 1.9 });
  assert.equal(stopped.moving, false);
  assert.equal(stopped.cameraHeading, 90);
  assert.equal(update(tracker, 30, 0, 11000, { heading: 90, speed: 2.25 }).moving, false);
});

test("heading smoothing takes the short arc from 359 to 1 degrees", () => {
  const tracker = navigation.createTracker();
  update(tracker, 0, 0, 1000, { heading: 359, speed: 10 });
  assert.equal(update(tracker, 0, 10, 2000, { heading: 359, speed: 10 }).cameraHeading, 359);
  const result = update(tracker, 0, 20, 3000, { heading: 1, speed: 10 });
  assert.ok(Math.abs(utils.signedAngleDifference(result.cameraHeading, 0)) < 1);
});

test("corroborated turns and restarts converge instead of locking the old heading", () => {
  const tracker = navigation.createTracker();
  update(tracker, 0, 0, 1000, { heading: 0, speed: 10 });
  update(tracker, 0, 10, 2000, { heading: 0, speed: 10 });
  update(tracker, 0, 10, 3000, { heading: 185, speed: 0 });
  let result;
  for (let index = 1; index <= 4; index += 1) {
    result = update(tracker, index * 10, 10, 3000 + index * 1000, { heading: 90, speed: 10 });
  }
  assert.ok(result.cameraHeading > 88 && result.cameraHeading < 91);
  assert.equal(result.moving, true);
});

test("a contradictory heading spike uses actual course without a camera spin", () => {
  const tracker = navigation.createTracker();
  update(tracker, 0, 0, 1000, { heading: 90, speed: 10 });
  update(tracker, 10, 0, 2000, { heading: 90, speed: 10 });
  const result = update(tracker, 20, 0, 3000, { heading: 270, speed: 10 });
  assert.ok(Math.abs(result.cameraHeading - 90) < 0.01);
});

test("a reported urban turn responds within three seconds with twenty-metre GPS accuracy", () => {
  const tracker = navigation.createTracker();
  update(tracker, 0, 0, 1000, { accuracy: 20, heading: 0, speed: 6 });
  update(tracker, 0, 18, 4000, { accuracy: 20, heading: 0, speed: 6 });
  let result;
  for (let index = 1; index <= 3; index += 1) {
    result = update(tracker, index * 6, 18, 4000 + index * 1000, { accuracy: 20, heading: 90, speed: 6 });
  }
  assert.ok(result.cameraHeading > 60 && result.cameraHeading < 90);
  for (let index = 4; index <= 6; index += 1) {
    result = update(tracker, index * 6, 18, 4000 + index * 1000, { accuracy: 20, heading: 90, speed: 6 });
  }
  assert.ok(result.cameraHeading > 83 && result.cameraHeading < 90);
});

test("stale, poor, invalid, reordered and jumping fixes never poison the good baseline", () => {
  const tracker = navigation.createTracker();
  update(tracker, 0, 0, 1000, { heading: 90, speed: 10 });
  update(tracker, 10, 0, 2000, { heading: 90, speed: 10 });
  const rejected = [
    [fix(20, 0, 3000, { accuracy: 100 }), 3000, "accuracy"],
    [fix(20, 0, 3000), 15000, "stale"],
    [fix(20, 0, 8000), 3000, "stale"],
    [fix(20, 0, 1000), 3000, "order"],
    [fix(10000, 0, 3000), 3000, "jump"],
    [fix(20, 0, 3000, { latitude: null }), 3000, "invalid"],
    [fix(20, 0, 3000, { accuracy: -1 }), 3000, "accuracy"]
  ];
  for (const [raw, now, reason] of rejected) {
    const result = tracker.update(raw, now);
    assert.equal(result.canFollow, false);
    assert.equal(result.reason, reason);
    assert.equal(result.cameraHeading, 90);
  }
  assert.equal(update(tracker, 20, 0, 3000, { heading: 90, speed: 10 }).canFollow, true);
});

test("imprecise-but-usable fixes follow location without changing the heading", () => {
  const tracker = navigation.createTracker();
  update(tracker, 0, 0, 1000, { heading: 90, speed: 10 });
  update(tracker, 10, 0, 2000, { heading: 90, speed: 10 });
  const result = update(tracker, 20, 0, 3000, { accuracy: 40, heading: 270, speed: 10 });
  assert.equal(result.canFollow, true);
  assert.equal(result.cameraHeading, 90);
  tracker.reset();
  assert.equal(update(tracker, 0, 0, 4000).cameraHeading, null);
});
