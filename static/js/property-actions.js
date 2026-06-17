(() => {
  function csrf() {
    const item = document.cookie.split(";").map((part) => part.trim())
      .find((part) => part.startsWith("csrftoken="));
    return item ? decodeURIComponent(item.split("=")[1]) : "";
  }

  function propertyId(element) {
    const container = element.closest("[data-property-id]");
    return container ? container.dataset.propertyId : "";
  }

  function noteBackupKey(id) {
    return `radar.property.${id}.draftNote`;
  }

  function saveDirtyNotes(root, id) {
    const notes = root.querySelector("#personal-notes");
    const status = root.querySelector("#note-status");
    if (!notes || !id || notes.value === (notes.dataset.savedValue || "")) {
      return Promise.resolve();
    }
    if (status) status.textContent = "Guardando nota...";
    return fetch(`/api/propiedad/${id}/nota/`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
      body: JSON.stringify({ personal_notes: notes.value })
    }).then((response) => {
      if (!response.ok) throw new Error("No se pudo guardar la nota");
      return response.json();
    }).then(() => {
      notes.dataset.savedValue = notes.value;
      localStorage.removeItem(noteBackupKey(id));
      if (status) status.textContent = "Nota guardada";
    });
  }

  function bindActions(root = document) {
    root.querySelectorAll(".property-infer-zone:not([data-bound])").forEach((button) => {
      button.dataset.bound = "1";
      button.addEventListener("click", async (event) => {
        event.preventDefault();
        event.stopPropagation();
        const id = propertyId(button);
        if (!id) return;
        const oldTitle = button.title;
        button.disabled = true;
        button.title = "Infiriendo zona...";
        try {
          const response = await fetch(`/api/propiedad/${id}/inferir-territorio/`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
            body: JSON.stringify({})
          });
          const data = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(data.error || "No se pudo inferir zona");
          button.classList.add("active");
          button.title = data.message || "Zona inferida";
        } catch (error) {
          button.title = error.message;
          alert(error.message);
        } finally {
          button.disabled = false;
          setTimeout(() => { button.title = oldTitle; }, 2200);
        }
      });
    });

    root.querySelectorAll(".property-action:not([data-bound])").forEach((button) => {
      button.dataset.bound = "1";
      button.addEventListener("click", async () => {
        const id = propertyId(button);
        const action = button.dataset.action;
        const value = button.dataset.value === "1";
        const payload = {};
        if (action === "favorite") payload.is_favorite = value;
        if (action === "hidden") payload.is_hidden = value;
        if (action === "reviewed") payload.reviewed = value;
        try {
          await saveDirtyNotes(document, id);
          const response = await fetch(`/api/propiedad/${id}/estado/`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
            body: JSON.stringify(payload)
          });
          if (!response.ok) throw new Error("No se pudo guardar");
          const data = await response.json();
          if (action === "favorite") setToggle(button, data.is_favorite);
          if (action === "hidden") setToggle(button, data.is_hidden);
          if (action === "reviewed") setToggle(button, data.reviewed);
          if (action === "hidden") {
            const row = button.closest("[data-property-id]");
            if (row) row.classList.toggle("is-hidden", data.is_hidden);
          }
        } catch (error) {
          alert(error.message);
        }
      });
    });

    const notes = root.querySelector("#personal-notes");
    const save = root.querySelector("#save-notes");
    const status = root.querySelector("#note-status");
    if (notes && save && !save.dataset.bound) {
      save.dataset.bound = "1";
      const id = propertyId(save);
      const backup = id ? localStorage.getItem(noteBackupKey(id)) : "";
      notes.dataset.savedValue = notes.value;
      if (backup && backup !== notes.value) {
        notes.value = backup;
        if (status) status.textContent = "Nota sin guardar restaurada";
      }
      notes.addEventListener("input", () => {
        const noteId = propertyId(save);
        if (noteId) localStorage.setItem(noteBackupKey(noteId), notes.value);
        if (status) status.textContent = "Nota sin guardar";
      });
      save.addEventListener("click", () => {
        const id = propertyId(save);
        fetch(`/api/propiedad/${id}/nota/`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
          body: JSON.stringify({ personal_notes: notes.value })
        }).then((response) => {
          if (!response.ok) throw new Error("No se pudo guardar la nota");
          return response.json();
        }).then(() => {
          notes.dataset.savedValue = notes.value;
          localStorage.removeItem(noteBackupKey(id));
          if (status) status.textContent = "Guardado";
          setTimeout(() => { if (status) status.textContent = ""; }, 1800);
        }).catch((error) => {
          if (status) status.textContent = error.message;
        });
      });
    }

    bindPropertyDataEditor(root);
  }

  function bindPropertyDataEditor(root = document) {
    const panel = root.querySelector(".property-data-editor");
    const form = root.querySelector("#property-data-form");
    const edit = root.querySelector("#edit-property-data");
    const cancel = root.querySelector("#cancel-property-data");
    const actions = root.querySelector(".editor-actions");
    const status = root.querySelector("#property-data-status");
    if (!panel || !form || !edit || edit.dataset.bound) return;
    edit.dataset.bound = "1";
    const controls = Array.from(form.querySelectorAll("input, select, textarea"));
    const initial = snapshot();

    function snapshot() {
      const values = {};
      controls.forEach((control) => {
        values[control.name] = control.value;
      });
      return values;
    }

    function setEditing(enabled) {
      controls.forEach((control) => {
        control.disabled = !enabled;
      });
      edit.hidden = enabled;
      if (actions) actions.hidden = !enabled;
      if (status) status.textContent = "";
    }

    function restore() {
      controls.forEach((control) => {
        control.value = initial[control.name] || "";
      });
      setEditing(false);
    }

    edit.addEventListener("click", () => setEditing(true));
    if (cancel) cancel.addEventListener("click", restore);

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const payload = {};
      controls.forEach((control) => {
        if (control.value !== initial[control.name]) {
          payload[control.name] = control.value;
        }
      });
      if (!Object.keys(payload).length) {
        setEditing(false);
        return;
      }
      const save = root.querySelector("#save-property-data");
      if (save) save.disabled = true;
      fetch(`/api/propiedad/${panel.dataset.propertyId}/datos/`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
        body: JSON.stringify(payload)
      }).then((response) => {
        if (!response.ok) {
          return response.json().then((data) => {
            throw new Error(data.error || "No se pudieron guardar los datos");
          });
        }
        return response.json();
      }).then(() => {
        if (status) status.textContent = "Guardado";
        setTimeout(() => window.location.reload(), 350);
      }).catch((error) => {
        if (status) status.textContent = error.message;
      }).finally(() => {
        if (save) save.disabled = false;
      });
    });
  }

  function setToggle(button, enabled) {
    button.classList.toggle("active", enabled);
    button.dataset.value = enabled ? "0" : "1";
  }

  document.addEventListener("htmx:afterSwap", (event) => bindActions(event.target));
  bindActions();

  function selectableItems() {
    return Array.from(document.querySelectorAll(".property-card, .property-table tbody tr[data-property-id]"));
  }

  function selectItem(item) {
    selectableItems().forEach((candidate) => candidate.classList.remove("is-selected"));
    if (!item) return;
    item.classList.add("is-selected");
    item.scrollIntoView({ block: "nearest" });
  }

  document.addEventListener("keydown", (event) => {
    if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
    const items = selectableItems();
    if (!items.length) return;
    const current = document.querySelector(".property-card.is-selected, .property-table tbody tr.is-selected");
    let index = current ? items.indexOf(current) : 0;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      selectItem(items[Math.min(index + 1, items.length - 1)]);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      selectItem(items[Math.max(index - 1, 0)]);
    } else if (event.key === "Enter" && current) {
      const link = current.querySelector(".card-link, .table-title");
      if (window.RadarPropertyPreview && current.dataset.propertyId) {
        event.preventDefault();
        window.RadarPropertyPreview.open(current.dataset.propertyId);
      } else if (link) {
        window.location.href = link.href;
      }
    } else if (["f", "F", "v", "V", "h", "H"].includes(event.key) && current) {
      const action = event.key.toLowerCase() === "f" ? "favorite" : event.key.toLowerCase() === "v" ? "reviewed" : "hidden";
      const button = current.querySelector(`.property-action[data-action="${action}"]`);
      if (button) button.click();
    }
  });
})();
