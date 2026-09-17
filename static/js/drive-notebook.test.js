"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { createStore } = require("./drive-notebook.js");
const storage = () => { const data = new Map(); return { getItem: (key) => data.get(key) || null, setItem: (key, value) => data.set(key, value) }; };
const entry = { id: "abc", revision: 0, mutationId: "mutation-1", data: { sessionId: "trip", text: "Volver de día", capturedAt: 10 }, photoData: "data:image/jpeg;base64,example" };

test("offline notes including photos survive reload; retries use the same mutation", async () => {
  const disk = storage();
  const first = createStore({ storage: disk, ownerId: 1, request: async () => { throw new Error("offline"); } });
  first.put(entry); await first.sync();
  assert.equal(first.pending().length, 1);
  const reloaded = createStore({ storage: disk, ownerId: 1, request: async (_url, options) => {
    assert.deepEqual(JSON.parse(options.body), entry);
    return { id: entry.id, revision: 1, data: entry.data, photoUrl: "/photo/" };
  } });
  await reloaded.sync();
  assert.equal(reloaded.pending().length, 0);
  assert.equal(reloaded.list()[0].photoUrl, "/photo/");
  assert.equal(reloaded.list()[0].photoData, undefined);
});

test("conflicts preserve local text and do not repeatedly overwrite the server", async () => {
  let calls = 0;
  const store = createStore({ storage: storage(), ownerId: 1, request: async () => { calls++; throw Object.assign(new Error("changed"), { status: 409 }); } });
  store.put(entry); await store.sync(); await store.sync();
  assert.equal(calls, 1);
  assert.equal(store.pending()[0].data.text, "Volver de día");
  assert.equal(store.pending()[0].conflict, "changed");
});

test("owners are isolated and quota failures are reported before accepting a note", () => {
  const disk = storage();
  const first = createStore({ storage: disk, ownerId: 1, request: async () => {} });
  first.put(entry);
  assert.equal(createStore({ storage: disk, ownerId: 2, request: async () => {} }).pending().length, 0);
  const full = createStore({ storage: { getItem: () => null, setItem: () => { throw new Error("quota"); } }, ownerId: 1, request: async () => {} });
  assert.throws(() => full.put(entry), /quota/);
  assert.equal(full.pending().length, 0);
});

test("a newer local edit cannot be removed by an older upload acknowledgement", async () => {
  const disk = storage(); let release;
  const store = createStore({ storage: disk, ownerId: 1, request: () => new Promise((resolve) => { release = resolve; }) });
  store.put(entry); const running = store.sync(); await Promise.resolve();
  store.put({ ...entry, mutationId: "mutation-2", data: { ...entry.data, text: "Nueva nota" } });
  release({ id: entry.id, revision: 1, data: entry.data }); await running;
  assert.equal(store.pending()[0].data.text, "Nueva nota");
  assert.equal(store.pending()[0].revision, 1);
});

test("unreadable pending data is never overwritten", async () => {
  let writes = 0;
  const store = createStore({ storage: { getItem: () => "null", setItem: () => { writes++; } }, ownerId: 1, request: async () => {} });
  assert.throws(() => store.put(entry));
  await store.sync();
  assert.equal(writes, 0);
});
