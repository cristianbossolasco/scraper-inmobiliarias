/* Run with node scripts/test_drive_browser.cjs. Uses a disposable SQLite DB only. */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const net = require("node:net");
const { spawn } = require("node:child_process");

const root = path.resolve(__dirname, "..");
const artifacts = path.join(root, "tmp", "drive-block4");
const playwright = require(process.env.PLAYWRIGHT_MODULE || path.join(
  process.env.USERPROFILE, ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright"
));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function availablePort() {
  const server = net.createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  return port;
}

async function waitUntil(check, message, timeout = 15000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await check()) return;
    await sleep(150);
  }
  throw new Error(message);
}

async function assertVisibleAndClickable(page, selector) {
  const result = await page.locator(selector).evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const center = { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
    const hit = document.elementFromPoint(center.x, center.y);
    return {
      inViewport: rect.x >= 0 && rect.y >= 0 && rect.right <= innerWidth && rect.bottom <= innerHeight,
      clickable: hit === element || element.contains(hit),
      width: rect.width,
      height: rect.height
    };
  });
  assert(result.inViewport && result.clickable, `${selector} must be visible and unobscured: ${JSON.stringify(result)}`);
  assert(result.width >= 44 && result.height >= 44, `${selector} needs a usable touch target`);
}

async function touchGesture(cdp, frames) {
  for (let index = 0; index < frames.length; index += 1) {
    await cdp.send("Input.dispatchTouchEvent", {
      type: index === 0 ? "touchStart" : "touchMove",
      touchPoints: frames[index].map(([x, y], id) => ({ x, y, id, radiusX: 2, radiusY: 2, force: 1 }))
    });
    await sleep(55);
  }
  await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await sleep(900);
}

async function assertTraceOutsideSummary(page) {
  // showSummary schedules fitTrace after layout; then wait for its camera animation.
  await page.waitForFunction(() => window.__driveTestMap?.getSource("drive-trace"));
  await sleep(120);
  await page.waitForFunction(() => {
    const map = window.__driveTestMap;
    return map?.getSource("drive-trace") && !map.isMoving();
  });
  const geometry = await page.evaluate(() => {
    const map = window.__driveTestMap;
    const panel = document.getElementById("drive-summary").getBoundingClientRect();
    const viewport = document.getElementById("drive-map").getBoundingClientRect();
    const header = document.querySelector(".drive-header").getBoundingClientRect();
    const coordinates = map.getSource("drive-trace")._data.features.flatMap((feature) => {
      if (feature.geometry.type === "LineString") return feature.geometry.coordinates;
      if (feature.geometry.type === "Point") return [feature.geometry.coordinates];
      return [];
    });
    const points = coordinates.map((coordinate) => {
      const point = map.project(coordinate);
      return { x: point.x + viewport.left, y: point.y + viewport.top };
    });
    return {
      points,
      panel: { left: panel.left, top: panel.top, right: panel.right, bottom: panel.bottom },
      headerBottom: header.bottom,
      width: innerWidth,
      height: innerHeight,
      landscape: matchMedia("(orientation: landscape) and (max-height: 520px)").matches
    };
  });
  assert(geometry.points.length > 0, "Summary must retain saved trace coordinates");
  for (const point of geometry.points) {
    assert(point.x >= 8 && point.x <= geometry.width - 8 && point.y >= geometry.headerBottom + 8 && point.y <= geometry.height - 8,
      `Trace point must be within visible map viewport: ${JSON.stringify({ point, geometry })}`);
    assert(geometry.landscape ? point.x <= geometry.panel.left - 8 : point.y <= geometry.panel.top - 8,
      `Trace point must remain outside summary panel: ${JSON.stringify({ point, geometry })}`);
  }
  return {
    viewport: `${geometry.width}x${geometry.height}`,
    points: geometry.points.length,
    panel: geometry.panel,
    traceBounds: {
      left: Math.min(...geometry.points.map((point) => point.x)),
      right: Math.max(...geometry.points.map((point) => point.x)),
      top: Math.min(...geometry.points.map((point) => point.y)),
      bottom: Math.max(...geometry.points.map((point) => point.y))
    }
  };
}

async function main() {
  fs.mkdirSync(artifacts, { recursive: true });
  const port = await availablePort();
  const database = path.join(artifacts, `browser-${Date.now()}-${process.pid}.sqlite3`);
  const server = spawn(process.env.PYTHON || "python", [
    path.join(__dirname, "serve_drive_browser.py"), database, String(port)
  ], { cwd: root, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
  let serverOutput = "";
  server.stdout.on("data", (data) => { serverOutput += data; });
  server.stderr.on("data", (data) => { serverOutput += data; });
  let browser;
  let page;
  const base = `http://127.0.0.1:${port}`;
  const errors = [];
  const checks = [];
  const traceVisibility = [];
  try {
    await waitUntil(async () => {
      if (server.exitCode !== null) throw new Error(`Test server exited: ${serverOutput}`);
      try { return (await fetch(`${base}/accounts/login/`)).ok; } catch (_) { return false; }
    }, `Isolated server did not start: ${serverOutput}`, 60000);
    const chrome = process.env.CHROME_EXECUTABLE || "C:/Program Files/Google/Chrome/Application/chrome.exe";
    browser = await playwright.chromium.launch({
      headless: true,
      ...(fs.existsSync(chrome) ? { executablePath: chrome } : {}),
      args: ["--enable-unsafe-swiftshader"]
    });
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      isMobile: true,
      hasTouch: true,
      deviceScaleFactor: 1,
      permissions: ["geolocation"]
    });
    await context.addInitScript(() => {
      window.__spoken = [];
      Object.defineProperty(window, "speechSynthesis", { configurable: true, value: {
        speak: (utterance) => window.__spoken.push(utterance.text), cancel: () => {},
      } });
      window.SpeechSynthesisUtterance = class { constructor(text) { this.text = text; } };
      const watchers = new Map();
      let nextWatch = 0;
      Object.defineProperty(navigator.geolocation, "watchPosition", {
        configurable: true,
        value: (success) => { watchers.set(++nextWatch, success); return nextWatch; }
      });
      Object.defineProperty(navigator.geolocation, "clearWatch", {
        configurable: true,
        value: (id) => watchers.delete(id)
      });
      window.__driveWatchCount = () => watchers.size;
      window.__emitPosition = (overrides = {}) => {
        const position = {
          timestamp: Date.now(),
          coords: { latitude: -34.59, longitude: -58.64, accuracy: 5, heading: 90, speed: 8, ...overrides }
        };
        for (const success of watchers.values()) success(position);
      };
      let library;
      Object.defineProperty(window, "maplibregl", {
        configurable: true,
        get: () => library,
        set: (value) => {
          library = value;
          const RealMap = value.Map;
          value.Map = class TestObservableMap extends RealMap {
            constructor(...args) {
              super(...args);
              window.__driveTestMap = this;
            }
          };
        }
      });
    });
    page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${base}/recorrido/`);
    await page.locator('[name="username"]').fill("browser-test");
    await page.locator('[name="password"]').fill("Browser-test-only-42!");
    await page.locator('button[type="submit"]').click();
    await page.waitForURL("**/recorrido/");
    await page.waitForFunction(() => window.__driveTestMap?.isStyleLoaded());
    await page.locator("#drive-history-open").scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(artifacts, "01-start-mobile.png") });
    await page.locator("#drive-history-open").click();
    await page.locator("#drive-history-panel").waitFor({ state: "visible" });
    await page.locator("#drive-history-close").click();
    checks.push("Historial accesible antes de iniciar");

    await page.locator("#drive-radius").selectOption("1500");
    assert.match(await page.locator("#drive-start-filter-summary").textContent(), /1500/);
    await page.locator("#drive-radius").selectOption("350");
    await page.locator("#drive-audio").check();

    await page.locator("#drive-start").click();
    await page.waitForFunction(() => window.__driveWatchCount() === 1);
    await page.evaluate(() => window.__emitPosition());
    await page.locator("#drive-recenter").waitFor({ state: "visible" });
    await sleep(1000);
    await assertVisibleAndClickable(page, "#drive-recenter");
    if (await page.locator("#drive-property-card").isVisible()) {
      await page.locator("#drive-favorite").click();
      await page.locator("#drive-card-close").click();
    }
    await sleep(3200);
    await page.evaluate(() => window.__emitPosition({ longitude: -58.6397 }));
    await sleep(900);
    const bearing = await page.evaluate(() => window.__driveTestMap.getBearing());
    assert(Math.abs(bearing - 90) < 5, "Real movement should establish an eastward heading");
    for (const heading of [280, 10, 220, 65]) {
      await page.evaluate((nextHeading) => window.__emitPosition({ longitude: -58.6397, speed: 0, heading: nextHeading }), heading);
      await sleep(550);
    }
    const stoppedBearing = await page.evaluate(() => window.__driveTestMap.getBearing());
    assert(Math.abs(((stoppedBearing - bearing + 540) % 360) - 180) < 0.5, "Stopped GPS heading jitter must not rotate map");
    checks.push("Orientación estable detenido con rumbo GPS oscilante");
    assert.equal(await page.evaluate(() => window.__spoken.length), 1, "Voice should announce the nearby property once and avoid repetition");

    assert.equal(await page.evaluate(() => window.__driveTestMap.getSource("drive-properties")._data.features.length), 2,
      "Both close and 250 m discoveries must be drawn");
    await page.locator("#drive-field-open").click();
    await page.locator("#drive-note-kind").selectOption("sign");
    await page.locator("#drive-note-text").fill("Cartel nuevo. Consultar al anunciante.");
    await page.locator("#drive-note-action").selectOption("contact");
    const photo = await page.evaluate(() => {
      const canvas = document.createElement("canvas"); canvas.width = 64; canvas.height = 48;
      canvas.getContext("2d").fillRect(0, 0, 64, 48); return canvas.toDataURL("image/jpeg").split(",")[1];
    });
    await page.locator("#drive-note-photo").setInputFiles({ name: "cartel.jpg", mimeType: "image/jpeg", buffer: Buffer.from(photo, "base64") });
    await page.route("**/api/recorrido/notas/*/", async (route) => {
      if (route.request().method() === "PUT") await route.abort("connectionfailed"); else await route.continue();
    });
    await page.locator("#drive-note-save").click();
    await page.locator("#drive-notebook[open]").waitFor();
    await waitUntil(async () => (await page.locator("#drive-notebook-list").textContent()).includes("Cartel nuevo"), "Offline note should remain visible");
    assert.equal(await page.evaluate(() => Object.values(JSON.parse(localStorage.getItem(Object.keys(localStorage).find((key) => key.startsWith("radar.drive.notes.v1."))))).length), 1);
    await page.screenshot({ path: path.join(artifacts, "08-notebook-pending.png") });
    await page.unroute("**/api/recorrido/notas/*/");
    await page.locator("#drive-notebook-refresh").click();
    await waitUntil(async () => /sincronizado/.test(await page.locator("#drive-notebook-status").textContent()), "Notebook should synchronize photo and note");
    assert.equal(await page.locator("#drive-notebook-list img").count(), 1);
    await page.locator("#drive-notebook-close").click();
    checks.push("Cartel con foto y pendiente se conserva sin conexión y se sincroniza al reintentar");

    const cdp = await context.newCDPSession(page);
    const zoomBefore = await page.evaluate(() => window.__driveTestMap.getZoom());
    await touchGesture(cdp, [
      [[90, 380], [300, 400]], [[105, 380], [285, 400]],
      [[125, 380], [267, 400]], [[145, 380], [250, 400]], [[161, 380], [234, 400]]
    ]);
    const zoomAfter = await page.evaluate(() => window.__driveTestMap.getZoom());
    assert(zoomAfter < zoomBefore - 0.3, `Pinch should zoom out (${zoomBefore} -> ${zoomAfter})`);
    assert.match(await page.locator("#drive-follow-status").textContent(), /Seguimiento activo/i);
    await page.evaluate(() => window.__emitPosition({ longitude: -58.6394 }));
    await sleep(900);
    const followed = await page.evaluate(() => ({
      longitude: window.__driveTestMap.getCenter().lng, zoom: window.__driveTestMap.getZoom()
    }));
    assert(Math.abs(followed.zoom - zoomAfter) < 0.05, "GPS update must retain user-selected zoom");
    assert(Math.abs(followed.longitude + 58.6394) < 0.0001, "GPS update must continue following after pinch");
    checks.push("Pellizco real conserva seguimiento y zoom elegido");

    await touchGesture(cdp, [[[180, 390]], [[195, 396]], [[222, 410]], [[265, 430]]]);
    await waitUntil(async () => /Mapa libre/i.test(await page.locator("#drive-follow-status").textContent()), "One-finger drag should pause tracking");
    await assertVisibleAndClickable(page, "#drive-recenter");
    await page.screenshot({ path: path.join(artifacts, "02-free-map-mobile.png") });
    await page.locator("#drive-recenter").click();
    assert.match(await page.locator("#drive-follow-status").textContent(), /Seguimiento activo/i);
    checks.push("Arrastre deliberado pausa; Seguir auto recupera seguimiento");

    await page.setViewportSize({ width: 844, height: 390 });
    await sleep(350);
    await assertVisibleAndClickable(page, "#drive-recenter");
    await assertVisibleAndClickable(page, "#drive-stop");
    await page.screenshot({ path: path.join(artifacts, "03-tracking-landscape.png") });
    checks.push("Controles utilizables en paisaje corto");
    await page.setViewportSize({ width: 390, height: 844 });

    await sleep(3200);
    for (const longitude of [-58.6380, -58.6365, -58.6353]) {
      await sleep(5100);
      await page.evaluate((lng) => window.__emitPosition({ longitude: lng, speed: 12 }), longitude);
    }
    await waitUntil(async () => /0 cerca/.test(await page.locator("#drive-count").textContent()), "Moving beyond radius must return no nearby properties");
    assert.equal(await page.evaluate(() => window.__driveTestMap.getSource("drive-properties")._data.features.length), 2, "Leaving the radius must retain both discoveries");
    await page.locator("#drive-stop").click();
    await page.locator("#drive-summary").waitFor({ state: "visible" });
    await waitUntil(async () => /guardado.*PC|guardado.*historial/i.test(await page.locator("#drive-sync-status").textContent()), "Ended route should sync to PC", 20000);
    traceVisibility.push(await assertTraceOutsideSummary(page));
    await page.screenshot({ path: path.join(artifacts, "04-summary-mobile.png") });
    await page.locator("#drive-summary-history").click();
    await page.locator("#drive-history-panel").waitFor({ state: "visible" });
    await waitUntil(async () => (await page.locator("#drive-history-list button").count()) > 0, "Saved route should appear in history");
    await page.screenshot({ path: path.join(artifacts, "05-history-mobile.png") });
    await page.locator("#drive-history-list button").first().click();
    await page.locator("#drive-summary").waitFor({ state: "visible" });
    const tracePoints = await page.evaluate(() => {
      const data = window.__driveTestMap.getSource("drive-trace")._data;
      return data.features.filter((item) => item.geometry.type === "LineString").flatMap((item) => item.geometry.coordinates).length;
    });
    assert(tracePoints >= 2, "Opening historical route must render saved trace");
    assert.equal(await page.evaluate(() => window.__driveTestMap.getSource("drive-properties")._data.features.length), 2, "History must draw all discoveries from the beginning of the trip");
    assert.equal(await page.locator("#drive-encounters-list .drive-encounter-item").count(), 2);
    await page.locator("#drive-discovery-filter").selectOption("close");
    assert.equal(await page.locator("#drive-encounters-list .drive-encounter-item").count(), 1, "Only the near property was passed within 120 m");
    await page.locator("#drive-discovery-filter").selectOption("all");
    await page.locator("#drive-encounters-list button").filter({ hasText: "Ver casa en el mapa" }).first().click();
    await page.locator("#drive-property-card").waitFor({ state: "visible" });
    assert.match(await page.locator("#drive-card-distance").textContent(), /Datos guardados/);
    await page.locator("#drive-card-close").click();
    await page.locator("#drive-encounters-list button").filter({ hasText: "Observación / pendiente" }).first().click();
    await page.locator("#drive-note-text").fill("Volver a ver la fachada de día");
    await page.locator("#drive-note-action").selectOption("daylight");
    await page.screenshot({ path: path.join(artifacts, "09-field-note-editor.png") });
    await page.locator("#drive-note-save").click();
    await waitUntil(async () => /sincronizado/.test(await page.locator("#drive-notebook-status").textContent()), "Historical property note should save");
    await page.locator("#drive-notebook-close").click();
    await page.locator("#drive-discovery-filter").selectOption("pending");
    assert.equal(await page.locator("#drive-encounters-list .drive-encounter-item").count(), 1, "Pending filter should include the property with a follow-up");
    await page.locator("#drive-summary-notebook").click();
    const propertyNote = page.locator("#drive-notebook-list article").filter({ hasText: "Volver a ver la fachada" });
    await propertyNote.getByRole("button", { name: "Marcar resuelto" }).click();
    await waitUntil(async () => /sincronizado/.test(await page.locator("#drive-notebook-status").textContent()), "Completed follow-up should save");
    await page.locator("#drive-notebook-close").click();
    assert.equal(await page.locator("#drive-encounters-list .drive-encounter-item").count(), 0);
    await page.locator("#drive-discovery-filter").selectOption("all");
    checks.push("Casas detectadas a 250 m persisten al salir del radio y en mapa/lista del historial con filtro de cercanía");
    checks.push("Finalización sincroniza, historial abre traza persistida");

    await page.locator("#drive-summary-history").click();
    await page.setViewportSize({ width: 844, height: 390 });
    await page.locator("#drive-history-list .drive-history-item").first().click();
    await page.locator("#drive-summary").waitFor({ state: "visible" });
    traceVisibility.push(await assertTraceOutsideSummary(page));
    await page.screenshot({ path: path.join(artifacts, "07-history-landscape.png") });
    checks.push("Traza visible fuera del resumen vertical y horizontal");

    await page.locator("#drive-summary-history").click();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator("#drive-history-close").click();
    await page.locator("#drive-new-session").click();
    await page.locator("#drive-start").click();
    await page.waitForFunction(() => window.__driveWatchCount() === 1);
    await page.evaluate(() => window.__emitPosition());
    await waitUntil(async () => (await page.locator("#drive-count").textContent()).includes("2 cerca"), "Next trip should load properties");
    assert.equal(await page.evaluate(() => window.__spoken.length), 1, "Previously discovered property should not be announced as new in a second trip");
    const prior = await page.evaluate(() => {
      const key = Object.keys(localStorage).find((key) => /^radar\.drive\.session\.v2\./.test(key));
      return Object.values(JSON.parse(localStorage.getItem(key)).details).every((property) => property.seen_before === true);
    });
    assert(prior, "Second trip should recognize discoveries from the first saved trip");
    checks.push("Pendientes se resuelven; memoria entre salidas y audio evitan repetir descubrimientos conocidos");
    await page.route("**/api/recorrido/historial/", async (route) => {
      if (route.request().method() === "POST") await route.abort("connectionfailed");
      else await route.continue();
    });
    await page.locator("#drive-stop").click();
    await page.locator("#drive-sync-retry").waitFor({ state: "visible" });
    const pendingCount = () => page.evaluate(() => {
      const key = Object.keys(localStorage).find((item) => item.startsWith("radar.drive.pending.v1."));
      return Object.keys(JSON.parse(localStorage.getItem(key) || "{}")).length;
    });
    assert.equal(await pendingCount(), 1, "Unsynced trip must be queued durably");
    await page.reload();
    await page.locator("#drive-sync-retry").waitFor({ state: "visible" });
    assert.equal(await pendingCount(), 1, "Reload must retain pending trip without duplicates");
    traceVisibility.push(await assertTraceOutsideSummary(page));
    await page.screenshot({ path: path.join(artifacts, "06-pending-sync-mobile.png") });
    await page.unroute("**/api/recorrido/historial/");
    await page.locator("#drive-sync-retry").click();
    await waitUntil(async () => (await pendingCount()) === 0, "Restored PC connection should clear pending trip");
    await page.locator("#drive-summary-history").click();
    await waitUntil(async () => (await page.locator("#drive-history-list .drive-history-item").count()) === 2, "History should contain two different trips, each once");
    await page.locator("#drive-history-refresh").click();
    await sleep(600);
    assert.equal(await page.locator("#drive-history-list .drive-history-item").count(), 2, "Repeat sync must not duplicate trips");
    checks.push("Sin conexión conserva pendiente tras recarga; reintento guarda una vez");
    assert.deepEqual(errors, [], "Browser should have no uncaught JavaScript errors");
    console.log(JSON.stringify({ ok: true, checks, traceVisibility, database, screenshots: artifacts }, null, 2));
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(artifacts, "failure.png") }).catch(() => {});
    console.error(JSON.stringify({ ok: false, error: error.stack, errors, serverOutput: serverOutput.slice(-5000) }, null, 2));
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
