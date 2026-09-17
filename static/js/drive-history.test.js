"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { createOutbox, connectionMessage } = require("./drive-history.js");

test("free tunnel failure explains expiry without asserting it and warns about unsaved trips", () => {
  const message = connectionMessage({ hostname: "example.run.pinggy-free.link", saving: true });
  assert.match(message, /60 minutos/);
  assert.match(message, /puede haber caducado/);
  assert.match(message, /no se guardó en la PC/);
  assert.match(message, /otro enlace no traslada/);
});

test("offline phone takes precedence over possible tunnel expiry", () => {
  const message = connectionMessage({ hostname: "example.run.pinggy-free.link", online: false });
  assert.match(message, /teléfono no tiene conexión/);
  assert.doesNotMatch(message, /caducado/);
  assert.match(message, /propiedades no se actualizan/);
});

test("general and paid domains do not claim the free tunnel limit", () => {
  for (const hostname of ["localhost", "radar.example.com", "example.a.pinggy.link", "pinggy-free.link.evil.test"]) {
    assert.doesNotMatch(connectionMessage({ hostname }), /60 minutos/);
  }
});

function storage() {
  const data = new Map();
  return { getItem: (key) => data.get(key) || null, setItem: (key, value) => data.set(key, value) };
}
const trip = (id) => ({ sessionId: id, status: "ended", startedAt: 1, endedAt: 2, points: [] });

test("failed uploads survive reload and retry exactly the same session ID", async () => {
  const disk = storage(); const original = trip("first");
  const first = createOutbox({ storage: disk, ownerId: 1, request: async () => { throw new Error("offline"); } });
  assert.equal(first.enqueue(original), true);
  original.points.push({ latitude: 1 });
  await first.sync();
  assert.equal(first.entries().length, 1);
  let sent;
  const restored = createOutbox({ storage: disk, ownerId: 1, request: async (_, options) => { sent = JSON.parse(options.body); return { id: "server-1" }; } });
  await restored.sync();
  assert.equal(sent.sessionId, "first");
  assert.deepEqual(sent.points, []);
  assert.equal(restored.savedId("first"), "server-1");
  assert.equal(restored.entries().length, 0);
});

test("a lost acknowledgement can be retried without replacing its local snapshot", async () => {
  const server = new Map(); let loseResponse = true;
  const box = createOutbox({ storage: storage(), ownerId: 1, request: async (_, options) => {
    const session = JSON.parse(options.body);
    if (!server.has(session.sessionId)) server.set(session.sessionId, session);
    if (loseResponse) { loseResponse = false; throw new Error("connection reset"); }
    return { id: "saved" };
  } });
  box.enqueue(trip("same")); await box.sync();
  box.enqueue({ ...trip("same"), endedAt: 50 }); await box.sync();
  assert.equal(server.size, 1);
  assert.equal(server.get("same").endedAt, 2);
  assert.equal(box.savedId("same"), "saved");
});

test("concurrent sync calls share one request and queues are isolated by user", async () => {
  const disk = storage(); let resolve; let count = 0;
  const box = createOutbox({ storage: disk, ownerId: 1, request: () => { count++; return new Promise((done) => { resolve = done; }); } });
  box.enqueue(trip("one"));
  const a = box.sync(), b = box.sync();
  assert.equal(a, b); await Promise.resolve(); assert.equal(count, 1);
  const other = createOutbox({ storage: disk, ownerId: 2, request: () => {} });
  assert.deepEqual(other.entries(), []);
  resolve({ id: "saved" }); await a;
  assert.equal(box.busy, false);
});

test("quota failure retains the in-memory trip but reports it cannot survive reload", async () => {
  const box = createOutbox({ storage: { getItem: () => null, setItem: () => { throw new Error("quota"); } }, ownerId: 1, request: async () => ({ id: "saved" }) });
  assert.equal(box.enqueue(trip("one")), false);
  assert.equal(box.durable, false);
  assert.equal(box.entries().length, 1);
  await box.sync();
  assert.equal(box.savedId("one"), "saved");
});

test("an interrupted trip is never sent as a finalized history", async () => {
  let count = 0;
  const box = createOutbox({ storage: storage(), ownerId: 1, request: async () => { count++; } });
  box.enqueue({ ...trip("one"), status: "tracking" }); await box.sync();
  assert.equal(count, 0);
});

test("two tabs cannot replace each other's queued trips", async () => {
  const disk = storage();
  const a = createOutbox({ storage: disk, ownerId: 1, request: async () => ({ id: "a" }) });
  const b = createOutbox({ storage: disk, ownerId: 1, request: async () => ({ id: "b" }) });
  a.enqueue(trip("a")); b.enqueue(trip("b"));
  const restored = createOutbox({ storage: disk, ownerId: 1, request: () => {} });
  assert.deepEqual(restored.entries().map((item) => item.sessionId).sort(), ["a", "b"]);
  await a.sync();
  const after = createOutbox({ storage: disk, ownerId: 1, request: () => {} });
  assert.deepEqual(after.entries().map((item) => item.sessionId), ["b"]);
});

test("one invalid trip does not block later trips and deleted trips never resurrect", async () => {
  const box = createOutbox({ storage: storage(), ownerId: 1, request: async (_, options) => {
    const id = JSON.parse(options.body).sessionId;
    if (id !== "good") { const error = new Error("invalid or removed"); error.status = id === "deleted" ? 410 : 400; throw error; }
    return { id: "good-server" };
  } });
  for (const id of ["invalid", "deleted", "good"]) box.enqueue(trip(id));
  await box.sync();
  assert.equal(box.savedId("good"), "good-server");
  assert.deepEqual(box.entries().map((item) => item.sessionId), ["invalid"]);
  assert.equal(box.wasDeleted("deleted"), true);
  box.enqueue(trip("deleted"));
  assert.equal(box.entries().length, 1);
});
