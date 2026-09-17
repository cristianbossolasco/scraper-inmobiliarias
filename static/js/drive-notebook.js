(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.RadarDriveNotebook = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const endpoint = "/api/recorrido/notas/";
  const actions = { none: "Sin pendiente", review: "Revisar con más tiempo", revisit: "Volver a visitar", daylight: "Volver de día", contact: "Consultar al anunciante" };
  const tags = { quiet: "Calle tranquila", noisy: "Ruido", liked_block: "Me gustó la cuadra", poor_front: "Fachada deteriorada", wrong_location: "Ubicación a revisar" };

  function createStore({ storage, ownerId, request, onChange = () => {} }) {
    const key = `radar.drive.notes.v1.${ownerId}`;
    let pending = {}, cache = {}, error = "", running = null, readable = true;
    try {
      const parsed = JSON.parse(storage.getItem(key) || "{}");
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("invalid");
      pending = parsed;
    } catch (_) { readable = false; error = "No se pudo leer el cuaderno local. Sus datos se conservaron sin reemplazarlos."; }
    function write(next) {
      if (!readable) throw new Error(error);
      // Always read other tabs' pending notes before changing this tab's item.
      storage.setItem(key, JSON.stringify(next));
      pending = next;
    }
    function put(note) {
      if (!readable) throw new Error(error);
      const disk = JSON.parse(storage.getItem(key) || "{}");
      write({ ...disk, ...pending, [note.id]: note });
      error = ""; onChange();
    }
    async function flush() {
      if (!readable) return;
      error = "";
      for (const entry of Object.values(pending)) {
        if (entry.conflict) continue;
        try {
          const saved = await request(`${endpoint}${entry.id}/`, { method: "PUT", body: JSON.stringify(entry) });
          cache[entry.id] = saved;
          const disk = JSON.parse(storage.getItem(key) || "{}");
          if (disk[entry.id]?.mutationId === entry.mutationId) delete disk[entry.id];
          else if (disk[entry.id]?.revision === entry.revision) disk[entry.id].revision = saved.revision;
          write(disk); onChange();
        } catch (e) {
          error = e.status ? e.message : "No se pudo guardar esta nota en la PC. La nota y su foto siguen en este teléfono; reintentá en este mismo enlace.";
          if (e.status === 409 || e.status === 410) {
            const disk = JSON.parse(storage.getItem(key) || "{}");
            if (disk[entry.id]?.mutationId === entry.mutationId) disk[entry.id].conflict = error;
            write(disk);
          }
          break;
        }
      }
    }
    function sync() {
      if (running) return running;
      running = Promise.resolve().then(flush).finally(() => { running = null; onChange(); });
      onChange(); return running;
    }
    function discard(id) {
      const disk = JSON.parse(storage.getItem(key) || "{}");
      delete disk[id]; write(disk); onChange();
    }
    return {
      put, sync, discard,
      ingest: (notes) => { notes.forEach((note) => { cache[note.id] = note; }); onChange(); },
      replaceScope: (scope, notes) => {
        for (const [id, note] of Object.entries(cache)) if (!scope || note.data.sessionId === scope) delete cache[id];
        notes.forEach((note) => { cache[note.id] = note; }); onChange();
      },
      remove: (id) => { delete cache[id]; discard(id); },
      list: () => Object.values({ ...cache, ...pending }).sort((a, b) => b.data.capturedAt - a.data.capturedAt),
      pending: () => Object.values(pending),
      clearSession: (sessionId) => {
        const disk = JSON.parse(storage.getItem(key) || "{}");
        for (const [id, note] of Object.entries(disk)) if (note.data.sessionId === sessionId) delete disk[id];
        for (const [id, note] of Object.entries(cache)) if (note.data.sessionId === sessionId) delete cache[id];
        write(disk); onChange();
      },
      get error() { return error; }, get busy() { return Boolean(running); },
    };
  }

  async function compressPhoto(file) {
    if (!file) return null;
    if (file.size > 20 * 1024 * 1024) throw new Error("Elegí una foto de menos de 20 MB.");
    const bitmap = await createImageBitmap(file);
    try {
      const scale = Math.min(1, 1280 / Math.max(bitmap.width, bitmap.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(bitmap.width * scale); canvas.height = Math.round(bitmap.height * scale);
      const context = canvas.getContext("2d");
      context.fillStyle = "white"; context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      for (const quality of [0.75, 0.55, 0.35]) {
        const result = canvas.toDataURL("image/jpeg", quality);
        if (result.length <= 440000) return result;
      }
      throw new Error("La foto es muy grande. Elegí otra foto.");
    } finally { bitmap.close(); }
  }
  return { createStore, compressPhoto, actions, tags, endpoint };
});
