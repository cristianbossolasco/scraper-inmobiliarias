(() => {
  let propertyModal = null;
  let propertyModalContent = null;
  let propertyModalClose = null;
  let propertyPreviewMap = null;
  let propertyPreviewMarker = null;
  let propertyPreviewLocationDraft = null;

  function elements() {
    propertyModal = propertyModal || document.getElementById("property-preview-modal");
    propertyModalContent = propertyModalContent || document.getElementById("property-preview-content");
    propertyModalClose = propertyModalClose || document.getElementById("property-preview-close");
    return { propertyModal, propertyModalContent, propertyModalClose };
  }

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;"
    })[char]);
  }

  function getCookie(name) {
    return document.cookie
      .split(";")
      .map((item) => item.trim())
      .find((item) => item.startsWith(`${name}=`))
      ?.split("=")
      .slice(1)
      .join("=") || "";
  }

  async function requestJson(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": decodeURIComponent(getCookie("csrftoken")),
        ...(options.headers || {})
      }
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || "No se pudo completar la accion.");
    }
    return payload;
  }

  function formatPrice(item) {
    if (!item.price) return "Consultar";
    return `${item.currency || ""} ${Math.round(item.price).toLocaleString("es-AR")}`.trim();
  }

  function formatScoreCell(value) {
    if (value === null || value === undefined || value === "") return "-";
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return "-";
    return `${Math.round(parsed)}/100`;
  }

  function formatMeters(value) {
    if (value === null || value === undefined || value === "") return "-";
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return "-";
    return `${Math.round(parsed).toLocaleString("es-AR")} m`;
  }

  function previewNoteBackupKey(propertyId) {
    return `radar.propertyPreview.${propertyId}.draftNote`;
  }

  function renderEditField(field) {
    const value = field.value ?? "";
    const wide = field.input_type === "textarea" || ["title", "address", "description", "features"].includes(field.field);
    if (field.input_type === "textarea") {
      return `
        <label class="${wide ? "wide" : ""}">
          <span>${escapeHtml(field.label)}</span>
          <textarea name="${escapeHtml(field.field)}" rows="${field.rows || 3}">${escapeHtml(value)}</textarea>
        </label>
      `;
    }
    if (field.input_type === "select") {
      return `
        <label class="${wide ? "wide" : ""}">
          <span>${escapeHtml(field.label)}</span>
          <select name="${escapeHtml(field.field)}">
            ${(field.choices || []).map((choice) => `
              <option value="${escapeHtml(choice.value)}" ${String(choice.value) === String(value) ? "selected" : ""}>${escapeHtml(choice.label)}</option>
            `).join("")}
          </select>
        </label>
      `;
    }
    return `
      <label class="${wide ? "wide" : ""}">
        <span>${escapeHtml(field.label)}</span>
        <input name="${escapeHtml(field.field)}" type="${field.input_type || "text"}" value="${escapeHtml(value)}" step="any">
      </label>
    `;
  }

  function renderPropertyPreview(property) {
    const { propertyModalContent } = elements();
    if (!propertyModalContent) return;
    const facts = (property.facts || []).slice(0, 12).map((fact) => `
      <div><span>${escapeHtml(fact.label)}</span><strong>${escapeHtml(fact.value)}</strong></div>
    `).join("");
    const sourceLinks = (property.source_links || []).map((link) => `
      <a class="source-button" href="${escapeHtml(link.url)}" target="_blank" rel="noopener">
        <i data-lucide="external-link"></i>
        <span>${escapeHtml(link.label || link.domain || "Publicacion")}</span>
      </a>
    `).join("");
    const editSections = (property.edit_sections || []).map((section) => `
      <fieldset class="edit-fieldset">
        <legend>${escapeHtml(section.title)}</legend>
        <div class="edit-grid">
          ${(section.fields || []).map(renderEditField).join("")}
        </div>
      </fieldset>
    `).join("");
    const security = property.security || {};
    const location = property.location || {};
    const locationIntelligence = property.location_intelligence || {};
    const securityBlock = security.coverage_score !== null && security.coverage_score !== undefined ? `
      <div class="property-preview-security">
        <h3>Seguridad proxy</h3>
        <dl>
          <div><dt>Cobertura</dt><dd>${Math.round(Number(security.coverage_score) || 0)}/100</dd></div>
          <div><dt>Riesgo relativo</dt><dd>${Math.round(Number(security.risk_score) || 0)}/100</dd></div>
          <div><dt>Nivel</dt><dd>${escapeHtml(security.level || "-")}</dd></div>
          <div><dt>Zona</dt><dd>${escapeHtml(security.zone_label || "-")}</dd></div>
          <div><dt>Fuente</dt><dd>${escapeHtml(security.source || "sin dato")}</dd></div>
          <div><dt>Camaras cercanas</dt><dd>${escapeHtml(security.evidence?.nearby_points?.by_type?.camera || 0)}</dd></div>
        </dl>
        <p class="audit-note">Proxy de infraestructura; no representa tasa real de delitos.</p>
      </div>
    ` : "";
    const locationIntelligenceBlock = locationIntelligence.overall_score !== null && locationIntelligence.overall_score !== undefined ? `
      <div class="property-preview-security property-preview-territory">
        <h3>Contexto territorial</h3>
        <dl>
          <div><dt>Score</dt><dd>${Math.round(Number(locationIntelligence.overall_score) || 0)}/100</dd></div>
          <div><dt>Nivel</dt><dd>${escapeHtml(locationIntelligence.level || "-")}</dd></div>
          <div><dt>Zona</dt><dd>${escapeHtml(locationIntelligence.zone_name || "-")}</dd></div>
          <div><dt>Transporte</dt><dd>${formatScoreCell(locationIntelligence.transport_score)}</dd></div>
          <div><dt>Educacion</dt><dd>${formatScoreCell(locationIntelligence.education_score)}</dd></div>
          <div><dt>Salud</dt><dd>${formatScoreCell(locationIntelligence.health_score)}</dd></div>
          <div><dt>Riesgo hidrico</dt><dd>${locationIntelligence.in_flood_risk_zone ? "Si" : "No"}</dd></div>
          <div><dt>SUBE</dt><dd>${formatMeters(locationIntelligence.nearest_sube_point_m)}</dd></div>
          <div><dt>RENABAP</dt><dd>${formatMeters(locationIntelligence.nearest_renabap_m)}</dd></div>
        </dl>
        <p class="audit-note">RENABAP es contexto urbano/infraestructura; crimen municipal se muestra separado del score.</p>
      </div>
    ` : "";
    const mapBlock = Number.isFinite(Number(location.latitude)) && Number.isFinite(Number(location.longitude)) ? `
      <div class="property-preview-map-panel">
        <div class="property-preview-map-heading">
          <div>
            <h3>Ubicacion</h3>
            <p class="audit-note">Move el marcador o hace clic en el mapa para corregir la ubicacion.</p>
          </div>
          <button class="secondary-button" type="button" data-preview-save-location>
            <i data-lucide="map-pin-check"></i> Guardar ubicacion
          </button>
        </div>
        <div id="property-preview-map" class="property-preview-map"></div>
      </div>
    ` : `
      <div class="property-preview-map-panel">
        <h3>Ubicacion</h3>
        <p class="audit-note">Esta propiedad todavia no tiene coordenadas para mostrar en el mapa.</p>
      </div>
    `;
    propertyModalContent.innerHTML = `
      <div class="property-preview-layout" data-property-id="${property.id}">
        <div class="property-preview-media">
          ${property.image ? `<img src="${escapeHtml(property.image)}" alt="">` : `<div class="image-placeholder"><i data-lucide="image"></i></div>`}
        </div>
        <div class="property-preview-main">
          <div class="property-preview-heading">
            <div>
              <p class="eyebrow">Propiedad #${property.id}</p>
              <h2>${escapeHtml(property.title || "Propiedad")}</h2>
              <p>${escapeHtml([property.address, property.neighborhood, property.locality].filter(Boolean).join(" · "))}</p>
            </div>
            <strong>${escapeHtml(property.price_display || formatPrice(property))}</strong>
          </div>
          <div class="analysis-actions">
            <button class="secondary-button ${property.is_favorite ? "active" : ""}" type="button" data-preview-state="is_favorite" data-value="${property.is_favorite ? "0" : "1"}">
              <i data-lucide="star"></i> Favorita
            </button>
            <button class="secondary-button ${property.reviewed ? "active" : ""}" type="button" data-preview-state="reviewed" data-value="${property.reviewed ? "0" : "1"}">
              <i data-lucide="check-circle"></i> Revisada
            </button>
            <button class="secondary-button ${property.is_hidden ? "danger active" : "danger"}" type="button" data-preview-state="is_hidden" data-value="${property.is_hidden ? "0" : "1"}">
              <i data-lucide="eye-off"></i> Oculta
            </button>
            <a class="secondary-button" href="${escapeHtml(property.detail_url || `/propiedad/${property.id}/`)}">
              <i data-lucide="panel-right-open"></i> Ficha completa
            </a>
            ${property.original_url ? `<a class="primary-button" href="${escapeHtml(property.original_url)}" target="_blank" rel="noopener"><i data-lucide="external-link"></i> Original</a>` : ""}
          </div>
          <div class="metric-grid detail-facts">${facts}</div>
          ${securityBlock}
          ${locationIntelligenceBlock}
          ${mapBlock}
          ${sourceLinks ? `<div class="original-links">${sourceLinks}</div>` : ""}
          <div class="notes-panel">
            <label for="property-preview-notes">Notas</label>
            <textarea id="property-preview-notes" rows="3" data-saved-value="${escapeHtml(property.personal_notes || "")}">${escapeHtml(property.personal_notes || "")}</textarea>
            <div class="editor-actions">
              <button class="secondary-button" type="button" data-preview-save-notes>Guardar notas</button>
              <span class="note-status" data-preview-dirty-status></span>
              <span class="note-status" data-preview-status></span>
            </div>
          </div>
          <div class="property-data-editor">
            <form data-preview-edit-form>
              ${editSections}
              <div class="editor-actions">
                <button class="primary-button" type="submit"><i data-lucide="save"></i> Guardar cambios</button>
                <span class="note-status" data-preview-status></span>
              </div>
            </form>
          </div>
        </div>
      </div>
    `;
    const notes = propertyModalContent.querySelector("#property-preview-notes");
    const dirtyStatus = propertyModalContent.querySelector("[data-preview-dirty-status]");
    const backup = localStorage.getItem(previewNoteBackupKey(property.id));
    if (notes && backup !== null && backup !== notes.value) {
      notes.value = backup;
      if (dirtyStatus) dirtyStatus.textContent = "Nota sin guardar restaurada.";
    }
    if (window.lucide) lucide.createIcons();
    initPropertyPreviewMap(property);
  }

  async function loadPropertyPreview(propertyId) {
    const { propertyModal, propertyModalContent } = elements();
    if (!propertyModal || !propertyModalContent || !propertyId) return;
    propertyModalContent.innerHTML = '<div class="audit-note">Cargando propiedad...</div>';
    if (!propertyModal.open) propertyModal.showModal();
    try {
      const property = await requestJson(`/api/propiedad/${propertyId}/resumen/`, { method: "GET" });
      renderPropertyPreview(property);
    } catch (error) {
      propertyModalContent.innerHTML = `<div class="audit-note">${escapeHtml(error.message)}</div>`;
    }
  }

  async function updatePreviewState(propertyId, key, value, statusNode) {
    const payload = { [key]: value };
    await requestJson(`/api/propiedad/${propertyId}/estado/`, {
      method: "POST",
      body: JSON.stringify(payload)
    });
    if (statusNode) statusNode.textContent = "Estado guardado.";
    await loadPropertyPreview(propertyId);
  }

  async function savePreviewNotes(propertyId, notes, statusNode) {
    await requestJson(`/api/propiedad/${propertyId}/nota/`, {
      method: "POST",
      body: JSON.stringify({ personal_notes: notes })
    });
    const { propertyModalContent } = elements();
    if (statusNode) statusNode.textContent = "Notas guardadas.";
    localStorage.removeItem(previewNoteBackupKey(propertyId));
    const textarea = propertyModalContent?.querySelector("#property-preview-notes");
    if (textarea) textarea.dataset.savedValue = notes;
    const dirtyStatus = propertyModalContent?.querySelector("[data-preview-dirty-status]");
    if (dirtyStatus) dirtyStatus.textContent = "";
  }

  async function saveUnsavedPreviewNotes(layout, propertyId, statusNode) {
    const textarea = layout?.querySelector("#property-preview-notes");
    if (!textarea || textarea.value === (textarea.dataset.savedValue || "")) return;
    if (statusNode) statusNode.textContent = "Guardando nota antes del estado...";
    await savePreviewNotes(propertyId, textarea.value, statusNode);
  }

  async function savePreviewLocation(propertyId, statusNode) {
    if (!propertyPreviewLocationDraft) {
      if (statusNode) statusNode.textContent = "Move el marcador o hace clic en el mapa antes de guardar.";
      return;
    }
    await requestJson(`/api/propiedad/${propertyId}/ubicacion/`, {
      method: "POST",
      body: JSON.stringify(propertyPreviewLocationDraft)
    });
    if (statusNode) statusNode.textContent = "Ubicacion guardada.";
  }

  function initPropertyPreviewMap(property) {
    const { propertyModalContent } = elements();
    if (propertyPreviewMap) {
      propertyPreviewMap.remove();
      propertyPreviewMap = null;
      propertyPreviewMarker = null;
      propertyPreviewLocationDraft = null;
    }
    const container = document.getElementById("property-preview-map");
    const location = property.location || {};
    const latitude = Number(location.latitude);
    const longitude = Number(location.longitude);
    if (!container || typeof maplibregl === "undefined" || !Number.isFinite(latitude) || !Number.isFinite(longitude)) return;
    const configPromise = property.map_config
      ? Promise.resolve(property.map_config)
      : fetch("/api/configuracion-mapa/").then((response) => response.json());
    configPromise.then((config) => {
      propertyPreviewLocationDraft = { latitude, longitude };
      propertyPreviewMap = new maplibregl.Map({
        container: "property-preview-map",
        style: {
          version: 8,
          sources: {
            osm: {
              type: "raster",
              tiles: [config.tile_url],
              tileSize: 256,
              attribution: config.attribution
            }
          },
          layers: [{ id: "osm", type: "raster", source: "osm" }]
        },
        center: [longitude, latitude],
        zoom: 15
      });
      propertyPreviewMap.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
      propertyPreviewMarker = new maplibregl.Marker({ color: "#176b4d", draggable: true })
        .setLngLat([longitude, latitude])
        .addTo(propertyPreviewMap);
      const updateDraft = (lngLat) => {
        propertyPreviewLocationDraft = {
          latitude: Number(lngLat.lat),
          longitude: Number(lngLat.lng)
        };
        const status = propertyModalContent?.querySelector("[data-preview-status]");
        if (status) status.textContent = "Ubicacion pendiente de guardar.";
      };
      propertyPreviewMarker.on("dragend", () => updateDraft(propertyPreviewMarker.getLngLat()));
      propertyPreviewMap.on("click", (event) => {
        propertyPreviewMarker.setLngLat(event.lngLat);
        updateDraft(event.lngLat);
      });
      propertyPreviewMap.once("load", () => {
        propertyPreviewMap.resize();
        setTimeout(() => propertyPreviewMap?.resize(), 80);
      });
    }).catch(() => {
      container.innerHTML = '<div class="audit-note">No se pudo cargar el mapa de la propiedad.</div>';
    });
  }

  async function savePreviewData(propertyId, form, statusNode) {
    const payload = {};
    new FormData(form).forEach((value, key) => {
      payload[key] = value;
    });
    await requestJson(`/api/propiedad/${propertyId}/datos/`, {
      method: "POST",
      body: JSON.stringify(payload)
    });
    if (statusNode) statusNode.textContent = "Cambios guardados.";
    await loadPropertyPreview(propertyId);
  }

  function open(propertyId) {
    loadPropertyPreview(propertyId);
  }

  function close() {
    const { propertyModal } = elements();
    if (propertyModal?.open) propertyModal.close();
  }

  function shouldPreviewClick(event, link) {
    if (!link || event.defaultPrevented) return false;
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
    if (link.target && link.target !== "_self") return false;
    return true;
  }

  function install() {
    const { propertyModal, propertyModalClose, propertyModalContent } = elements();
    if (!propertyModal || propertyModal.dataset.previewBound) return;
    propertyModal.dataset.previewBound = "1";
    propertyModalClose?.addEventListener("click", close);
    propertyModal.addEventListener("click", (event) => {
      if (event.target === propertyModal) close();
    });
    document.addEventListener("click", (event) => {
      const previewButton = event.target.closest(".property-preview-trigger,[data-map-preview-id],[data-property-preview-id]");
      if (previewButton) {
        event.preventDefault();
        open(previewButton.dataset.propertyId || previewButton.dataset.mapPreviewId || previewButton.dataset.propertyPreviewId);
        return;
      }
      const link = event.target.closest(".card-link,.table-title");
      if (!shouldPreviewClick(event, link)) return;
      const container = link.closest("[data-property-id]");
      if (!container?.dataset.propertyId) return;
      event.preventDefault();
      open(container.dataset.propertyId);
    });
    propertyModalContent?.addEventListener("click", async (event) => {
      const layout = event.target.closest(".property-preview-layout");
      const propertyId = layout?.dataset.propertyId;
      if (!propertyId) return;
      const stateButton = event.target.closest("[data-preview-state]");
      const notesButton = event.target.closest("[data-preview-save-notes]");
      const locationButton = event.target.closest("[data-preview-save-location]");
      const statusNode = layout.querySelector("[data-preview-status]");
      try {
        if (stateButton) {
          await saveUnsavedPreviewNotes(layout, propertyId, statusNode);
          await updatePreviewState(propertyId, stateButton.dataset.previewState, stateButton.dataset.value === "1", statusNode);
        } else if (notesButton) {
          await savePreviewNotes(propertyId, layout.querySelector("#property-preview-notes")?.value || "", statusNode);
        } else if (locationButton) {
          await savePreviewLocation(propertyId, statusNode);
        }
      } catch (error) {
        if (statusNode) statusNode.textContent = error.message;
      }
    });
    propertyModalContent?.addEventListener("input", (event) => {
      if (event.target?.id !== "property-preview-notes") return;
      const layout = event.target.closest(".property-preview-layout");
      const propertyId = layout?.dataset.propertyId;
      if (!propertyId) return;
      localStorage.setItem(previewNoteBackupKey(propertyId), event.target.value);
      const dirtyStatus = layout.querySelector("[data-preview-dirty-status]");
      if (dirtyStatus) dirtyStatus.textContent = "Nota sin guardar.";
    });
    propertyModalContent?.addEventListener("submit", async (event) => {
      const form = event.target.closest("[data-preview-edit-form]");
      if (!form) return;
      event.preventDefault();
      const layout = form.closest(".property-preview-layout");
      const propertyId = layout?.dataset.propertyId;
      const statusNode = layout?.querySelector("[data-preview-status]");
      try {
        await savePreviewData(propertyId, form, statusNode);
      } catch (error) {
        if (statusNode) statusNode.textContent = error.message;
      }
    });
  }

  window.RadarPropertyPreview = { open, close, install };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", install);
  } else {
    install();
  }
})();
