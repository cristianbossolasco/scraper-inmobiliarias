(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.RadarDriveHistory = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const endpoint = "/api/recorrido/historial/";

  function connectionMessage({ hostname = "", online = true, saving = false } = {}) {
    const pinggyFree = /\.(?:pinggy-free\.link|free\.pinggy\.net)$/i.test(hostname);
    const reason = !online
      ? "El teléfono no tiene conexión a Internet. Activá los datos o recuperá la señal."
      : pinggyFree
        ? "Se perdió el acceso al Radar. El enlace gratuito de Pinggy vence a los 60 minutos y puede haber caducado. Solicitá un enlace nuevo."
        : "No se puede acceder al Radar. Revisá la conexión del teléfono y que la PC y el enlace sigan activos.";
    return reason + (saving
      ? " El recorrido no se guardó en la PC. Conservá esta pestaña y sus datos: abrir otro enlace no traslada los pendientes."
      : " El mapa y el GPS pueden seguir visibles, pero las propiedades no se actualizan.");
  }

  function createOutbox({ storage, ownerId, request, onChange = () => {} }) {
    const key = `radar.drive.pending.v1.${ownerId}`;
    let pending = {};
    const saved = Object.create(null);
    const deleted = new Set();
    const errors = Object.create(null);
    let running = null;
    let error = "";
    let durable = true;
    try {
      const parsed = JSON.parse(storage.getItem(key) || "{}");
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("invalid");
      pending = parsed;
    } catch (_) {
      // Never overwrite unreadable pending data silently.
      durable = false;
      error = "No se pudo leer el guardado local. Mantené esta página abierta hasta sincronizar.";
    }

    function write(removeId = null) {
      if (!durable) return false;
      try {
        // Preserve other tabs' newly enqueued trips. Upload retries are idempotent.
        const disk = JSON.parse(storage.getItem(key) || "{}");
        pending = { ...disk, ...pending };
        if (removeId !== null) delete pending[removeId];
        storage.setItem(key, JSON.stringify(pending));
        return true;
      } catch (_) {
        durable = false;
        error = "Sin espacio para guardar en el teléfono. Mantené esta página abierta hasta sincronizar.";
        return false;
      }
    }

    function enqueue(session) {
      if (!session || session.status !== "ended" || session.historyId || session.historyDeleted || saved[session.sessionId] || deleted.has(session.sessionId)) return true;
      if (!Object.hasOwn(pending, session.sessionId)) {
        const snapshot = JSON.parse(JSON.stringify(session));
        delete snapshot.historyId;
        Object.defineProperty(pending, session.sessionId, { value: snapshot, enumerable: true, configurable: true });
      }
      const ok = write();
      onChange();
      return ok;
    }

    async function flush() {
      error = "";
      for (const [id, snapshot] of Object.entries(pending)) {
        try {
          const result = await request(endpoint, { method: "POST", body: JSON.stringify(snapshot) });
          saved[id] = result.id;
          delete pending[id];
          delete errors[id];
          write(id);
          onChange();
        } catch (e) {
          error = e.message || "No se pudo conectar con la PC.";
          errors[id] = error;
          if (e.status === 410) {
            deleted.add(id);
            delete pending[id];
            delete errors[id];
            write(id);
          } else if (!(e.status >= 400 && e.status < 500 && e.status !== 401 && e.status !== 403)) {
            break;
          }
        }
      }
    }

    function sync() {
      if (running) return running;
      // Defer so busy is visible even if the request fails immediately.
      running = Promise.resolve().then(flush).finally(() => { running = null; onChange(); });
      onChange();
      return running;
    }

    return {
      enqueue, sync,
      get: (id) => Object.hasOwn(pending, id) ? pending[id] : null,
      entries: () => Object.values(pending),
      savedId: (id) => saved[id] || null,
      wasDeleted: (id) => deleted.has(id),
      errorFor: (id) => errors[id] || "",
      get busy() { return Boolean(running); },
      get error() { return error; },
      get durable() { return durable; }
    };
  }
  return { createOutbox, endpoint, connectionMessage };
});
