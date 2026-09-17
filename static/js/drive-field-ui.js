(() => {
  "use strict";
  window.createDriveFieldUI = ({ ownerId, storage, request, context, onChange, focusPoint }) => {
    const api = window.RadarDriveNotebook;
    const byId = (id) => document.getElementById(id);
    const notebook = byId("drive-notebook"), editor = byId("drive-note-editor"), form = byId("drive-note-form");
    const status = byId("drive-notebook-status"), error = byId("drive-note-error");
    let scope = null, draft = null, nextPage = null, generation = 0, recognition = null;
    let store;
    function changed() { render(); onChange(); }
    store = api.createStore({ storage, ownerId, request, onChange: changed });
    const button = (text, handler) => {
      const element = document.createElement("button"); element.type = "button"; element.textContent = text;
      element.addEventListener("click", handler); return element;
    };
    function render() {
      if (!store) return;
      const notes = store.list().filter((note) => (!scope || note.data.sessionId === scope)
        && (!byId("drive-notebook-pending").checked || (note.data.action !== "none" && !note.data.done)));
      byId("drive-notebook-list").replaceChildren();
      status.textContent = store.error || (store.busy ? "Guardando en tu PC…" : store.pending().length ? `${store.pending().length} notas pendientes de guardar en tu PC. Conservá este enlace hasta sincronizar.` : "Cuaderno sincronizado con tu PC.");
      if (!notes.length) {
        const empty = document.createElement("p"); empty.textContent = "Todavía no hay registros para esta selección."; byId("drive-notebook-list").append(empty);
      }
      for (const note of notes) {
        const item = document.createElement("article"); item.className = "drive-encounter-item";
        const title = document.createElement("strong"); title.textContent = note.data.title || (note.data.kind === "sign" ? "Cartel encontrado" : "Observación de campo");
        const meta = document.createElement("span"); meta.textContent = `${new Date(note.data.capturedAt).toLocaleString("es-AR")} · ${note.data.accuracy == null ? "Ubicación del aviso" : `GPS ±${Math.round(note.data.accuracy)} m`}`;
        const text = document.createElement("p"); text.textContent = note.data.text;
        const labels = document.createElement("span"); labels.textContent = note.data.tags.map((tag) => api.tags[tag]).join(" · ");
        const action = document.createElement("strong"); action.textContent = `${note.data.done ? "✓ Resuelto · " : ""}${api.actions[note.data.action]}`;
        item.append(title, meta, text, labels, action);
        const photoUrl = note.photoData || note.photoUrl;
        if (photoUrl) { const img = document.createElement("img"); img.className = "drive-note-preview"; img.alt = "Foto tomada en la salida"; img.src = photoUrl; img.loading = "lazy"; item.append(img); }
        item.append(button("Ver lugar en el mapa", () => { notebook.close(); focusPoint(note.data); }));
        if (note.data.kind === "sign") {
          const matches = document.createElement("div");
          const search = button("Buscar avisos a 200 m del cartel", async () => {
            search.disabled = true; matches.textContent = "Buscando coincidencias cercanas…";
            try {
              const nearby = await request("/api/recorrido/cercanas/", { method: "POST", body: JSON.stringify({ latitude: note.data.latitude, longitude: note.data.longitude, radius_m: 200, property_types: [] }) });
              matches.replaceChildren();
              const hint = document.createElement("p"); hint.textContent = nearby.count ? "Avisos cercanos, sin confirmar que correspondan al cartel." : "No hay avisos elegibles a 200 m en la base actual."; matches.append(hint);
              for (const property of nearby.properties.slice(0, 10)) {
                const candidate = document.createElement("div");
                const inspect = button(`${property.type_label} · ${property.price_short} · ${property.distance_m} m`, async () => {
                  inspect.disabled = true;
                  try {
                    const detail = await request(`/api/recorrido/propiedad/${property.id}/ficha/`);
                    const text = document.createElement("p"); text.textContent = detail.address_text || "Dirección no publicada"; candidate.append(text);
                    if (detail.original_url) {
                      const url = new URL(detail.original_url);
                      if (url.protocol === "https:" && !url.username && !url.password) {
                        const link = document.createElement("a"); link.textContent = "Ver publicación original ↗"; link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer"; candidate.append(link);
                      }
                    }
                  } catch (e) { status.textContent = e.message; inspect.disabled = false; }
                });
                candidate.append(inspect); matches.append(candidate);
              }
              if (nearby.count > 10) { const hint = document.createElement("p"); hint.textContent = "Se muestran los 10 avisos más cercanos."; matches.append(hint); }
            } catch (e) { matches.textContent = e.message; }
            finally { search.disabled = false; }
          });
          item.append(search, matches);
        }
        if (note.conflict) {
          const conflict = document.createElement("p"); conflict.textContent = `${note.conflict} Tu texto se conserva acá; copialo antes de descartar esta versión.`; item.append(conflict);
          item.append(button("Descartar versión pendiente", () => {
            if (window.confirm("¿Descartar esta versión local de la nota?")) { store.discard(note.id); refresh(); }
          }));
        } else if (note.data.action !== "none") {
          const toggle = button(note.data.done ? "Volver a pendiente" : "Marcar resuelto", async () => {
            try {
              store.put({ ...note, data: { ...note.data, done: !note.data.done }, mutationId: crypto.randomUUID() });
              await store.sync();
            } catch (_) { status.textContent = "No hay espacio para guardar. Sincronizá antes de continuar."; }
          });
          // A pending mutation must be acknowledged before another edit can change it.
          toggle.disabled = store.pending().some((entry) => entry.id === note.id);
          item.append(toggle);
        }
        item.append(button("Eliminar nota", async () => {
          if (!window.confirm("¿Eliminar esta nota y su foto del cuaderno?")) return;
          try {
            const pending = store.pending().find((entry) => entry.id === note.id);
            if (pending) { status.textContent = "Sincronizá la nota antes de eliminarla."; return; }
            await request(`${api.endpoint}${note.id}/`, { method: "DELETE" }); store.remove(note.id);
          } catch (e) { status.textContent = e.message; }
        }));
        byId("drive-notebook-list").append(item);
      }
    }
    async function refresh(more = false) {
      const token = ++generation;
      const page = more ? nextPage : 1;
      if (!page) return;
      status.textContent = "Consultando tu cuaderno…";
      try {
        await store.sync();
        const query = new URLSearchParams({ page: String(page) });
        if (scope) query.set("session", scope);
        const result = await request(`${api.endpoint}?${query}`);
        if (token !== generation) return;
        if (more) store.ingest(result.results); else store.replaceScope(scope, result.results);
        nextPage = result.nextPage;
        byId("drive-notebook-more").hidden = !nextPage;
      } catch (e) { status.textContent = e.message; }
    }
    async function loadSession(id) {
      try {
        let page = 1;
        const notes = [];
        do {
          const result = await request(`${api.endpoint}?${new URLSearchParams({ session: id, page: String(page) })}`);
          notes.push(...result.results); page = result.nextPage;
        } while (page);
        store.replaceScope(id, notes);
      } catch (_) { /* Local pending observations remain visible when offline. */ }
    }
    function open(id = null) {
      scope = id; byId("drive-notebook-title").textContent = id ? "Cuaderno de esta salida" : "Cuaderno de campo";
      render(); notebook.showModal(); refresh();
    }
    function openNew(property = null) {
      const current = context();
      if (!current.session) { window.alert("Iniciá un recorrido para registrar una observación."); return; }
      if (current.tracking && (!current.position || Date.now() - current.position.timestamp > 15000 || (current.position.speed || 0) > 1.5)) {
        window.alert("Esperá a estar detenido y con una ubicación reciente para tomar notas o fotos."); return;
      }
      const location = property || current.position;
      if (!location || !Number.isFinite(location.latitude) || !Number.isFinite(location.longitude)) { window.alert("No hay una ubicación guardada para esta observación."); return; }
      form.reset();
      draft = { sessionId: current.session.sessionId, kind: property ? "property" : "place", propertyId: property?.id || null,
        title: property?.address_text || (property ? `Propiedad #${property.id}` : "Observación de este lugar"),
        latitude: location.latitude, longitude: location.longitude, accuracy: property ? null : location.accuracy,
        capturedAt: Date.now(), text: "", tags: [], action: "none", done: false };
      byId("drive-note-kind").value = draft.kind;
      byId("drive-note-kind").querySelector("option[value='property']").disabled = !property;
      byId("drive-note-context").textContent = `${draft.title} · ${property ? "Ubicación publicada, puede ser aproximada" : `GPS ±${Math.round(location.accuracy)} m`}`;
      error.textContent = ""; editor.showModal();
    }
    byId("drive-note-close").addEventListener("click", () => editor.close());
    editor.addEventListener("close", () => { recognition?.abort(); recognition = null; });
    byId("drive-notebook-close").addEventListener("click", () => notebook.close());
    byId("drive-notebook-refresh").addEventListener("click", () => refresh());
    byId("drive-notebook-more").addEventListener("click", () => refresh(true));
    byId("drive-notebook-all").addEventListener("click", () => { scope = null; byId("drive-notebook-title").textContent = "Cuaderno de campo"; render(); refresh(); });
    byId("drive-notebook-pending").addEventListener("change", render);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const save = byId("drive-note-save"); save.disabled = true; error.textContent = "Guardando…";
      try {
        const data = { ...draft, kind: byId("drive-note-kind").value, text: byId("drive-note-text").value.trim(),
          tags: [...document.querySelectorAll(".drive-note-tags input:checked")].map((input) => input.value), action: byId("drive-note-action").value };
        if (data.kind === "sign") data.title = "Cartel encontrado";
        if (!data.text && !data.tags.length && data.action === "none" && data.kind !== "sign") throw new Error("Agregá una observación o un pendiente.");
        const note = { id: crypto.randomUUID(), mutationId: crypto.randomUUID(), revision: 0, data,
          photoData: await api.compressPhoto(byId("drive-note-photo").files[0]) };
        try { store.put(note); } catch (_) { throw new Error("No hay espacio para guardar la nota en este teléfono. Sincronizá las pendientes o quitá la foto."); }
        editor.close(); open(data.sessionId); store.sync();
      } catch (e) { error.textContent = e.message; }
      finally { save.disabled = false; }
    });
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (Recognition) {
      byId("drive-note-dictate").hidden = false; byId("drive-dictation-hint").hidden = false;
      byId("drive-note-dictate").addEventListener("click", () => {
        if (recognition) { recognition.stop(); return; }
        const speech = new Recognition(); recognition = speech; speech.lang = "es-AR"; speech.continuous = false; speech.interimResults = false;
        speech.onresult = (event) => {
          const text = byId("drive-note-text"); text.value = `${text.value} ${event.results[0][0].transcript}`.trim().slice(0, 2000);
          error.textContent = `Texto agregado a «${draft.title}». Revisalo y guardá la nota.`;
        };
        speech.onerror = () => { error.textContent = "No se pudo dictar. Podés escribir la nota o usar el micrófono del teclado."; };
        speech.onend = () => { recognition = null; byId("drive-note-dictate").textContent = "Dictar nota"; };
        try { speech.start(); byId("drive-note-dictate").textContent = "Terminar dictado"; } catch (_) { recognition = null; error.textContent = "El dictado no está disponible en este momento."; }
      });
    }
    window.addEventListener("online", () => store.sync());
    return { open, openNew, loadSession, clearSession: (id) => store.clearSession(id), notes: () => store.list(), pending: () => store.pending(), sync: () => store.sync() };
  };
})();
