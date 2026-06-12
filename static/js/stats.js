(() => {
  const dataNode = document.getElementById("chart-data");
  if (!dataNode?.textContent) return;
  const data = JSON.parse(dataNode.textContent);
  const colors = ["#176b4d", "#d6a528", "#d95d45", "#386f8f", "#6f5d8f", "#4f7c67"];
  const statusColors = {
    pending: "#176b4d",
    reviewed: "#386f8f",
    favorite: "#d6a528"
  };
  const charts = new Map();
  const tabs = document.getElementById("stats-tabs");
  const filterToggle = document.getElementById("stats-filter-toggle");
  const filterForm = document.getElementById("stats-filter-form");
  const filterCollapse = document.getElementById("stats-filter-collapse");
  const propertyModal = document.getElementById("property-preview-modal");
  const propertyModalContent = document.getElementById("property-preview-content");
  const propertyModalClose = document.getElementById("property-preview-close");
  let heatmapMap = null;
  let heatmapPopup = null;
  let propertyPreviewMap = null;
  let propertyPreviewMarker = null;
  let propertyPreviewLocationDraft = null;
  const securityMaps = {};
  let securityMapPopup = null;
  let surfaceRegression = null;
  let dashboardPayloadRendered = false;

  const priceFormatter = new Intl.NumberFormat("es-AR");
  const zoneStorageKey = "stats.filterSectionCollapsed";
  const tabStorageKey = "stats.activeTab";
  const mapStorageKey = "stats.heatmap.initialized";
  const priceMapMetricStorageKey = "stats.priceMap.metric";

  if (filterToggle && filterForm) {
    filterToggle.addEventListener("click", () => {
      const isHidden = filterForm.hidden;
      filterForm.hidden = !isHidden;
      filterToggle.setAttribute("aria-expanded", String(isHidden));
    });
  }

  if (filterCollapse) {
    filterCollapse.addEventListener("click", () => {
      const shell = filterCollapse.closest(".stats-filter-shell");
      if (!shell) return;
      const collapsed = !shell.classList.contains("compact");
      shell.classList.toggle("compact", collapsed);
      localStorage.setItem(zoneStorageKey, collapsed ? "1" : "0");
      if (collapsed) {
        filterCollapse.innerHTML = '<i data-lucide="panel-top-close"></i> Filtros';
      } else {
        filterCollapse.innerHTML = '<i data-lucide="panel-top-open"></i> Secciones';
      }
      lucide.createIcons();
    });
    const persisted = localStorage.getItem(zoneStorageKey) === "1";
    if (persisted) {
      const shell = filterCollapse.closest(".stats-filter-shell");
      if (shell) shell.classList.add("compact");
      filterCollapse.innerHTML = '<i data-lucide="panel-top-close"></i> Filtros';
      lucide.createIcons();
    }
  }

  function initTabs() {
    if (!tabs) return;
    const buttons = tabs.querySelectorAll(".stats-tab");
    const panels = document.querySelectorAll(".stats-tab-panel");
    const panelByTab = new Map();
    panels.forEach((panel) => {
      if (!panel.dataset.tabContent) return;
      panelByTab.set(panel.dataset.tabContent, panel);
      panel.classList.remove("active");
    });
    let activeTab = localStorage.getItem(tabStorageKey) || buttons[0]?.dataset.tab || "overview";
    if (![...buttons].some((button) => button.dataset.tab === activeTab)) {
      activeTab = buttons[0]?.dataset.tab || "overview";
    }

    const activate = (tabName) => {
      buttons.forEach((button) => {
        const selected = button.dataset.tab === tabName;
        button.classList.toggle("active", selected);
        button.setAttribute("aria-selected", String(selected));
      });
      panels.forEach((panel) => {
        panel.classList.toggle("active", panel.dataset.tabContent === tabName);
      });
      localStorage.setItem(tabStorageKey, tabName);
      if (tabName !== "overview") {
        renderDashboardDeferred();
      }
      if (tabName === "spatial") {
        initPriceHeatmap();
        initSecurityMaps();
      }
      if (tabName === "models") {
        setTimeout(() => {
          const surfaceCanvas = document.getElementById("surface-price-chart");
          if (surfaceCanvas && surfaceCanvas.chart) {
            surfaceCanvas.chart.resize();
          }
        }, 50);
      }
    };

    buttons.forEach((button) => {
      button.addEventListener("click", () => activate(button.dataset.tab));
    });

    activate(activeTab);
  }

  function normalize(items) {
    return items.map((item) => Array.isArray(item)
      ? { label: item[0], value: item[1], total: item[1], pending: item[1], reviewed: 0, favorites: 0, url: null }
      : item);
  }

  function statusOf(item) {
    if (item.is_favorite) return "favorite";
    if (item.is_reviewed) return "reviewed";
    return "pending";
  }

  function statusLabel(status) {
    if (status === "favorite") return "Favorita";
    if (status === "reviewed") return "Vista";
    return "Pendiente";
  }

  function formatPrice(item) {
    if (!item.price) return "Consultar";
    return `${item.currency || ""} ${Math.round(item.price).toLocaleString("es-AR")}`.trim();
  }

  function formatListUrl(itemUrl) {
    if (!itemUrl) return null;
    try {
      const url = new URL(itemUrl, window.location.origin);
      if (url.pathname.includes("/properties/")) {
        const returnTo = url.searchParams.get("return_to");
        if (!returnTo) {
          return url.toString();
        }
        const returnUrl = new URL(decodeURIComponent(returnTo), window.location.origin);
        const listUrl = new URL("/", window.location.origin);
        const isDashboard = returnUrl.pathname.includes("/stats") || returnUrl.pathname === "/";
        if (isDashboard) {
          listUrl.search = returnUrl.search;
          return listUrl.toString();
        }
        return returnUrl.toString();
      }
      url.pathname = "/";
      return url.toString();
    } catch (_error) {
      return null;
    }
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
      throw new Error(payload.error || "No se pudo completar la acción.");
    }
    return payload;
  }

  function previewNoteBackupKey(propertyId) {
    return `radar.stats.property.${propertyId}.draftNote`;
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
    if (!propertyModalContent) return;
    const facts = (property.facts || []).slice(0, 12).map((fact) => `
      <div><span>${escapeHtml(fact.label)}</span><strong>${escapeHtml(fact.value)}</strong></div>
    `).join("");
    const sourceLinks = (property.source_links || []).map((link) => `
      <a class="source-button" href="${escapeHtml(link.url)}" target="_blank" rel="noopener">
        <i data-lucide="external-link"></i>
        <span>${escapeHtml(link.label || link.domain || "Publicación")}</span>
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
    const securityBlock = security.coverage_score !== null && security.coverage_score !== undefined ? `
      <div class="property-preview-security">
        <h3>Seguridad proxy</h3>
        <dl>
          <div><dt>Cobertura</dt><dd>${Math.round(Number(security.coverage_score) || 0)}/100</dd></div>
          <div><dt>Riesgo relativo</dt><dd>${Math.round(Number(security.risk_score) || 0)}/100</dd></div>
          <div><dt>Nivel</dt><dd>${escapeHtml(security.level || "-")}</dd></div>
          <div><dt>Zona</dt><dd>${escapeHtml(security.zone_label || "-")}</dd></div>
          <div><dt>Fuente</dt><dd>${escapeHtml(security.source || "sin dato")}</dd></div>
          <div><dt>Cámaras cercanas</dt><dd>${escapeHtml(security.evidence?.nearby_points?.by_type?.camera || 0)}</dd></div>
        </dl>
        <p class="audit-note">Proxy de infraestructura; no representa tasa real de delitos.</p>
      </div>
    ` : "";
    const mapBlock = Number.isFinite(Number(location.latitude)) && Number.isFinite(Number(location.longitude)) ? `
      <div class="property-preview-map-panel">
        <div class="property-preview-map-heading">
          <div>
            <h3>Ubicación</h3>
            <p class="audit-note">Mové el marcador o hacé clic en el mapa para corregir la ubicación.</p>
          </div>
          <button class="secondary-button" type="button" data-preview-save-location>
            <i data-lucide="map-pin-check"></i> Guardar ubicación
          </button>
        </div>
        <div id="property-preview-map" class="property-preview-map"></div>
      </div>
    ` : `
      <div class="property-preview-map-panel">
        <h3>Ubicación</h3>
        <p class="audit-note">Esta propiedad todavía no tiene coordenadas para mostrar en el mapa.</p>
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
    lucide.createIcons();
    initPropertyPreviewMap(property);
  }

  async function loadPropertyPreview(propertyId) {
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

  function openPropertyPreview(propertyId) {
    loadPropertyPreview(propertyId);
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
    if (statusNode) statusNode.textContent = "Notas guardadas.";
    localStorage.removeItem(previewNoteBackupKey(propertyId));
    const textarea = propertyModalContent?.querySelector("#property-preview-notes");
    if (textarea) textarea.dataset.savedValue = notes;
    const dirtyStatus = propertyModalContent?.querySelector("[data-preview-dirty-status]");
    if (dirtyStatus) dirtyStatus.textContent = "";
  }

  async function saveUnsavedPreviewNotes(layout, propertyId, statusNode) {
    const textarea = layout?.querySelector("#property-preview-notes");
    if (!textarea || textarea.value === (textarea.dataset.savedValue || "")) {
      return;
    }
    if (statusNode) statusNode.textContent = "Guardando nota antes del estado...";
    await savePreviewNotes(propertyId, textarea.value, statusNode);
  }

  async function savePreviewLocation(propertyId, statusNode) {
    if (!propertyPreviewLocationDraft) {
      if (statusNode) statusNode.textContent = "Mové el marcador o hacé clic en el mapa antes de guardar.";
      return;
    }
    await requestJson(`/api/propiedad/${propertyId}/ubicación/`, {
      method: "POST",
      body: JSON.stringify(propertyPreviewLocationDraft)
    });
    if (statusNode) statusNode.textContent = "Ubicación guardada.";
  }

  function initPropertyPreviewMap(property) {
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
    if (!container || typeof maplibregl === "undefined" || !Number.isFinite(latitude) || !Number.isFinite(longitude)) {
      return;
    }
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
        if (status) status.textContent = "Ubicación pendiente de guardar.";
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

  function ensurePreview() {
    let tooltip = document.getElementById("chart-preview");
    if (tooltip) return tooltip;
    tooltip = document.createElement("div");
    tooltip.id = "chart-preview";
    tooltip.className = "chart-preview";
    document.body.appendChild(tooltip);
    return tooltip;
  }

  function externalPreview(context) {
    const tooltipModel = context.tooltip;
    const tooltip = ensurePreview();
    if (tooltipModel.opacity === 0) {
      tooltip.style.opacity = 0;
      return;
    }
    const point = tooltipModel.dataPoints?.[0];
    const item = point?.dataset?.metaItems?.[point.dataIndex];
    if (!item) {
      tooltip.style.opacity = 0;
      return;
    }
    if (item.id) {
      const status = statusOf(item);
      tooltip.innerHTML = `
        <div class="chart-preview-card">
          ${item.image ? `<img src="${escapeHtml(item.image)}" alt="">` : `<div class="chart-preview-placeholder"></div>`}
          <div>
            <strong>${escapeHtml(formatPrice(item))}</strong>
            <span>${escapeHtml(item.title || "Propiedad")}</span>
            <small>${escapeHtml(item.address || "")}</small>
            <small>${escapeHtml([item.agency, item.source].filter(Boolean).join(" · "))}</small>
            <em class="${status}">${statusLabel(status)}</em>
          </div>
        </div>
      `;
    } else {
      tooltip.innerHTML = `
        <div class="chart-preview-summary">
          <strong>${escapeHtml(item.label || "")}</strong>
          <span>Total: ${item.total ?? item.value ?? 0}</span>
          <span>Favoritas: ${item.favorites || 0}</span>
          <span>Vistas: ${item.reviewed || 0}</span>
          <span>Pendientes: ${item.pending || 0}</span>
        </div>
      `;
    }
    const rect = context.chart.canvas.getBoundingClientRect();
    tooltip.style.opacity = 1;
    tooltip.style.left = `${rect.left + window.pageXOffset + tooltipModel.caretX + 14}px`;
    tooltip.style.top = `${rect.top + window.pageYOffset + tooltipModel.caretY + 14}px`;
  }

  function navigateFromChart(chart, event) {
    const points = chart.getElementsAtEventForMode(event, "nearest", { intersect: true }, true);
    if (!points.length) return;
    const point = points[0];
    const item = chart.data.datasets[point.datasetIndex].metaItems?.[point.index];
    if (item?.id) {
      openPropertyPreview(item.id);
      return;
    }
    const target = item?.url ? formatListUrl(item.url) : null;
    if (target) {
      window.location.href = target;
    }
  }

  function register(id, title, build) {
    charts.set(id, { title, build });
  }

  function renderBarChart(canvasId, title, items) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;
    const parsed = normalize(items);
    const chart = new Chart(ctx, {
      type: "bar",
      data: {
        labels: parsed.map((item) => item.label),
        datasets: [
          {
            label: "Pendientes",
            data: parsed.map((item) => item.pending || 0),
            backgroundColor: statusColors.pending,
            metaItems: parsed
          },
          {
            label: "Vistas",
            data: parsed.map((item) => item.reviewed || 0),
            backgroundColor: statusColors.reviewed,
            metaItems: parsed
          },
          {
            label: "Favoritas",
            data: parsed.map((item) => item.favorites || 0),
            backgroundColor: statusColors.favorite,
            metaItems: parsed
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: true },
          tooltip: { enabled: false, external: externalPreview }
        },
        scales: { x: { stacked: true }, y: { stacked: true } },
        onClick: (event, _elements, chartInstance) => navigateFromChart(chartInstance, event)
      }
    });
    chart.canvas.chart = chart;
    return chart;
  }

  function bar(id, title, items) {
    const parsed = normalize(items);
    const chart = renderBarChart(id, title, parsed);
    register(id, title, (targetCanvasId) => renderBarChart(targetCanvasId, title, parsed));
    return chart;
  }

  function drawRegressionLine(validItems) {
    if (validItems.length < 3) return null;
    let sumX = 0;
    let sumY = 0;
    let sumXY = 0;
    let sumXX = 0;
    validItems.forEach((item) => {
      sumX += item.x;
      sumY += item.y;
      sumXY += item.x * item.y;
      sumXX += item.x * item.x;
    });
    const count = validItems.length;
    const denominator = count * sumXX - sumX * sumX;
    if (!denominator) return null;
    const slope = (count * sumXY - sumX * sumY) / denominator;
    const intercept = (sumY - slope * sumX) / count;
    const predict = (x) => (slope * x + intercept);
    const residuals = validItems.map((item) => item.y - predict(item.x));
    const residualMean = residuals.reduce((acc, value) => acc + value, 0) / residuals.length;
    const variance = residuals.reduce((acc, value) => acc + (value - residualMean) ** 2, 0)
      / Math.max(1, residuals.length - 1);
    const std = Math.sqrt(variance);
    const minX = Math.min(...validItems.map((item) => item.x));
    const maxX = Math.max(...validItems.map((item) => item.x));
    return {
      slope,
      intercept,
      predict,
      std,
      line: [
        { x: minX, y: predict(minX) },
        { x: maxX, y: predict(maxX) }
      ]
    };
  }

  function renderScatterChart(canvasId, label, values, xTitle, options = {}) {
    const ctx = document.getElementById(canvasId);
    if (!ctx || !values.length) return null;
    const groups = {
      pending: values.filter((item) => statusOf(item) === "pending"),
      reviewed: values.filter((item) => statusOf(item) === "reviewed"),
      favorite: values.filter((item) => statusOf(item) === "favorite")
    };
    const validItems = values.filter((item) =>
      Number.isFinite(item.x) && Number.isFinite(item.y) && item.x > 0 && item.y > 0
    );
    const regression = options.withRegression ? drawRegressionLine(validItems) : null;
    const datasets = [
      {
        label: "Pendientes",
        data: groups.pending.map((item) => ({ x: item.x, y: item.y })),
        backgroundColor: statusColors.pending,
        borderColor: statusColors.pending,
        pointRadius: 3,
        metaItems: groups.pending
      },
      {
        label: "Vistas",
        data: groups.reviewed.map((item) => ({ x: item.x, y: item.y })),
        backgroundColor: statusColors.reviewed,
        borderColor: statusColors.reviewed,
        pointRadius: 4,
        metaItems: groups.reviewed
      },
      {
        label: "Favoritas",
        data: groups.favorite.map((item) => ({ x: item.x, y: item.y })),
        backgroundColor: statusColors.favorite,
        borderColor: "#8b6a10",
        pointRadius: 5,
        metaItems: groups.favorite
      }
    ];
    if (regression) {
      datasets.push({
        label: "Tendencia",
        data: regression.line,
        borderColor: "#6b4d00",
        borderWidth: 2,
        borderDash: [5, 4],
        fill: false,
        pointRadius: 0,
        metaItems: [],
        type: "line",
        order: -1
      });
    }
    const chart = new Chart(ctx, {
      type: "scatter",
      data: { datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        onClick: (event, _elements, chartInstance) => navigateFromChart(chartInstance, event),
        plugins: {
          tooltip: { enabled: false, external: externalPreview }
        },
        scales: {
          x: {
            title: { display: true, text: xTitle },
            grid: { borderColor: "rgba(23,33,29,0.12)" }
          },
          y: {
            title: { display: true, text: options.yTitle || "Precio" },
            ticks: {
              callback: (value) => `${priceFormatter.format(value)}`
            }
          }
        }
      }
    });
    chart.canvas.chart = chart;
    return chart;
  }

  function scatter(id, label, values, xTitle, options = {}) {
    const parsed = [...values];
    const chart = renderScatterChart(id, label, parsed, xTitle, options);
    register(id, label, (targetCanvasId) => renderScatterChart(targetCanvasId, label, parsed, xTitle, options));
    return chart;
  }

  function createZoneVolatilityChart(canvasId, title, sorted) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;
    if (!sorted.length) {
      ctx.hidden = true;
      if (!ctx.parentElement.querySelector(".chart-empty-note")) {
        ctx.insertAdjacentHTML("afterend", '<p class="audit-note chart-empty-note">No hay zonas con precios validos para este filtro.</p>');
      }
      return null;
    }
    ctx.hidden = false;
    const labels = sorted.map((item) => item.label);
    const chart = new Chart(ctx, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "Banda promedio +/- desvío",
            data: sorted.map((item) => [Math.max(0, item.avg - item.std), item.avg + item.std]),
            backgroundColor: "rgba(23, 107, 77, 0.22)",
            borderColor: "#176b4d",
            borderWidth: 1,
            borderSkipped: false,
            borderRadius: {
              topLeft: 7,
              bottomLeft: 7,
              topRight: 7,
              bottomRight: 7
            },
            barThickness: 12,
            metaItems: sorted
          },
          {
            label: "Promedio",
            type: "scatter",
            data: sorted.map((item) => ({ x: item.avg, y: item.label })),
            backgroundColor: "#176b4d",
            borderColor: "#ffffff",
            borderWidth: 2,
            pointRadius: 5,
            pointHoverRadius: 7,
            metaItems: sorted
          },
          {
            label: "Mediana",
            type: "scatter",
            data: sorted.map((item) => ({ x: item.median || item.avg, y: item.label })),
            backgroundColor: "#d6a528",
            borderColor: "#17211d",
            borderWidth: 1,
            pointRadius: 4,
            pointStyle: "rectRot",
            metaItems: sorted
          }
        ]
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        onClick: (event, _elements, chartInstance) => navigateFromChart(chartInstance, event),
        plugins: {
          tooltip: {
            callbacks: {
              label: (context) => {
                const item = context.dataset.metaItems?.[context.dataIndex];
                if (!item) return "";
                if (context.dataset.label === "Banda promedio +/- desvío") {
                  const low = Math.max(0, item.avg - item.std);
                  const high = item.avg + item.std;
                  return `Banda: ${Math.round(low).toLocaleString("es-AR")} a ${Math.round(high).toLocaleString("es-AR")}`;
                }
                if (context.dataset.label === "Mediana") {
                  return `Mediana: ${Math.round(item.median || item.avg).toLocaleString("es-AR")}`;
                }
                return [
                  `Promedio: ${Math.round(item.avg).toLocaleString("es-AR")}`,
                  `Desvío: ${Math.round(item.std).toLocaleString("es-AR")}`,
                  `Cantidad: ${item.total || 0}`,
                  `Coef. variacion: ${item.cv || 0}%`
                ];
              },
            },
            enabled: true
          }
        },
        scales: {
          x: {
            type: "linear",
            title: { display: true, text: "Precio" },
            ticks: { callback: (value) => priceFormatter.format(value) }
          },
          y: {
            type: "category",
            title: { display: true, text: "Zona" }
          }
        }
      }
    });
    chart.data.datasets.forEach((dataset) => {
      dataset.metaItems = sorted;
    });
    chart.canvas.chart = chart;
    return chart;
  }

  function zoneVolatility(id, title, values) {
    const parsed = normalize(values).map((item) => ({
      label: item.label,
      avg: Number(item.avg || 0),
      std: Number(item.std || 0),
      median: Number(item.median || item.avg || 0),
      cv: Number(item.cv || 0),
      total: item.total || 0,
      url: item.url,
      pending: item.pending || 0,
      reviewed: item.reviewed || 0,
      favorites: item.favorites || 0,
      value: item.total || 0
    }));
    const sorted = [...parsed].sort((a, b) => b.total - a.total);
    const chart = createZoneVolatilityChart(id, title, sorted);
    register(id, title, (targetCanvasId) => createZoneVolatilityChart(targetCanvasId, title, sorted));
    return chart;
  }

  function buildSurfaceOutlierRows(items, regression) {
    const outlierContainer = document.getElementById("surface-outliers");
    if (!outlierContainer) return;
    if (!regression) {
      outlierContainer.innerHTML = `
        <div class="audit-note">No hay suficientes puntos para calcular la tendencia.</div>
      `;
      return;
    }
    const baseline = regression.predict;
    const outliers = items
      .filter((item) => Number.isFinite(item.x) && Number.isFinite(item.y) && item.x > 0 && item.y > 0)
      .map((item) => {
        const expected = baseline(item.x);
        return {
          item,
          delta: item.y - expected,
          expected
        };
      })
      .filter((entry) => entry.delta < -Math.max(1, regression.std * 0.25))
      .sort((a, b) => a.delta - b.delta)
      .slice(0, 80);

    if (!outliers.length) {
      outlierContainer.innerHTML = `
        <div class="audit-note">No se detectaron casas claramente por debajo de la tendencia.</div>
      `;
      return;
    }

    const rows = outliers.map((entry) => `
        <tr>
          <td><strong>${escapeHtml(entry.item.title || "Sin título")}</strong></td>
          <td>${Math.round(entry.item.x)} m2</td>
          <td>${Math.round(entry.item.y).toLocaleString("es-AR")}</td>
          <td>${Math.round(entry.expected).toLocaleString("es-AR")}</td>
          <td>${Math.round(entry.delta).toLocaleString("es-AR")}</td>
          <td><button class="text-button property-preview-trigger" type="button" data-property-id="${entry.item.id}">Abrir ficha</button></td>
        </tr>
      `).join("");

    outlierContainer.innerHTML = `
      <div class="anomaly-table">
        <table>
          <thead>
            <tr>
              <th>Propiedad</th>
              <th>Superficie</th>
              <th>Precio</th>
              <th>Precio esperado</th>
              <th>Desvío</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `;
  }

  function scatterWithRegression(id, label, values, xTitle) {
    const chart = scatter(id, label, values, xTitle, { withRegression: true });
    const validItems = values.filter((item) => Number.isFinite(item.x) && Number.isFinite(item.y));
    surfaceRegression = drawRegressionLine(validItems);
    if (id === "surface-price-chart") {
      buildSurfaceOutlierRows(validItems, surfaceRegression);
    }
    return chart;
  }

  function initSecurityMaps() {
    initSecurityMap("security-coverage-map", "coverage");
    initSecurityMap("security-risk-map", "risk");
  }

  function securityMapBounds(featureCollection) {
    const bounds = new maplibregl.LngLatBounds();
    const extendCoords = (coords) => {
      if (!Array.isArray(coords)) return;
      if (coords.length >= 2 && Number.isFinite(Number(coords[0])) && Number.isFinite(Number(coords[1]))) {
        bounds.extend([Number(coords[0]), Number(coords[1])]);
        return;
      }
      coords.forEach(extendCoords);
    };
    (featureCollection?.features || []).forEach((feature) => extendCoords(feature.geometry?.coordinates));
    return bounds.isEmpty() ? null : bounds;
  }

  function securityFillColor(mode) {
    const property = mode === "risk" ? "risk_score" : "coverage_score";
    if (mode === "risk") {
      return ["interpolate", ["linear"], ["get", property], 0, "#e8f5ef", 35, "#ffe0a3", 55, "#f28f5b", 75, "#c43d2f", 100, "#7f1d1d"];
    }
    return ["interpolate", ["linear"], ["get", property], 0, "#f4efe6", 35, "#d9efb4", 55, "#8fd175", 75, "#2f9656", 100, "#07502f"];
  }

  function initSecurityMap(containerId, mode) {
    const container = document.getElementById(containerId);
    if (!container || typeof maplibregl === "undefined") return;
    const layers = data.security?.layers || {};
    if (!layers.zones && !container.dataset.loadingLayers) {
      container.dataset.loadingLayers = "1";
      container.innerHTML = '<div class="audit-note">Cargando capa de seguridad...</div>';
      fetch("/api/seguridad/capas/")
        .then((response) => response.json())
        .then((payload) => {
          data.security = data.security || {};
          data.security.layers = payload;
          data.security.configured = payload.configured;
          container.innerHTML = "";
          initSecurityMap(containerId, mode);
        })
        .catch(() => {
          container.innerHTML = '<div class="audit-note">No se pudo cargar la capa de seguridad.</div>';
        });
      return;
    }
    const zones = layers.zones || { type: "FeatureCollection", features: [] };
    const points = layers.points || { type: "FeatureCollection", features: [] };
    if (!zones.features?.length) {
      container.innerHTML = '<div class="audit-note">No hay capa de seguridad cargada.</div>';
      return;
    }
    const existing = securityMaps[containerId];
    if (existing && existing._loaded) {
      existing.resize();
      return;
    }
    if (existing) {
      existing.once("load", () => existing.resize());
      return;
    }
    fetch("/api/configuracion-mapa/").then((response) => response.json()).then((config) => {
      const map = new maplibregl.Map({
        container: containerId,
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
        center: config.center,
        zoom: config.zoom || 12
      });
      securityMaps[containerId] = map;
      map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
      map.once("load", () => {
        map.addSource(`${containerId}-zones`, { type: "geojson", data: zones });
        map.addSource(`${containerId}-points`, { type: "geojson", data: points });
        map.addLayer({
          id: `${containerId}-zone-fill`,
          type: "fill",
          source: `${containerId}-zones`,
          paint: {
            "fill-color": securityFillColor(mode),
            "fill-opacity": 0.54
          }
        });
        map.addLayer({
          id: `${containerId}-zone-line`,
          type: "line",
          source: `${containerId}-zones`,
          paint: {
            "line-color": mode === "risk" ? "#7f1d1d" : "#07502f",
            "line-width": 1.2,
            "line-opacity": 0.75
          }
        });
        map.addLayer({
          id: `${containerId}-points`,
          type: "circle",
          source: `${containerId}-points`,
          paint: {
            "circle-radius": 3.8,
            "circle-color": [
              "match",
              ["get", "security_type"],
              "camera", "#176b4d",
              "safe_stop", "#386f8f",
              "plate_reader", "#d6a528",
              "police_station", "#17211d",
              "#6f5d8f"
            ],
            "circle-stroke-color": "#fff",
            "circle-stroke-width": 1
          }
        });
        const bounds = securityMapBounds(zones);
        if (bounds) map.fitBounds(bounds, { padding: 30, duration: 0 });
        map.on("click", `${containerId}-zone-fill`, (event) => {
          const feature = event.features?.[0];
          if (!feature) return;
          const props = feature.properties || {};
          const score = mode === "risk" ? props.risk_score : props.coverage_score;
          if (securityMapPopup) securityMapPopup.remove();
          securityMapPopup = new maplibregl.Popup({ offset: 10 })
            .setLngLat(event.lngLat)
            .setHTML(`
              <div class="map-popup">
                <strong>${escapeHtml(props.label || "Zona")}</strong>
                <p>${mode === "risk" ? "Riesgo relativo" : "Cobertura"}: ${Math.round(Number(score) || 0)}/100</p>
                <small>${escapeHtml(props.security_level || "")} · ${escapeHtml(props.source || "")}</small>
              </div>
            `)
            .addTo(map);
        });
        map.on("click", `${containerId}-points`, (event) => {
          const feature = event.features?.[0];
          if (!feature) return;
          const props = feature.properties || {};
          if (securityMapPopup) securityMapPopup.remove();
          securityMapPopup = new maplibregl.Popup({ offset: 10 })
            .setLngLat(feature.geometry.coordinates)
            .setHTML(`
              <div class="map-popup">
                <strong>${escapeHtml(props.name || "Infraestructura")}</strong>
                <p>${escapeHtml(props.security_type || "seguridad")}</p>
                <small>${escapeHtml(props.zone || "")}</small>
              </div>
            `)
            .addTo(map);
        });
        [`${containerId}-zone-fill`, `${containerId}-points`].forEach((layerId) => {
          map.on("mouseenter", layerId, () => { map.getCanvas().style.cursor = "pointer"; });
          map.on("mouseleave", layerId, () => { map.getCanvas().style.cursor = ""; });
        });
        map.resize();
      });
    }).catch(() => {
      container.innerHTML = '<div class="audit-note">No se pudo cargar la configuracion del mapa.</div>';
    });
  }

  function initPriceHeatmap() {
    const container = document.getElementById("price-heatmap-map");
    const emptyNote = document.getElementById("price-heatmap-empty");
    if (!container || typeof maplibregl === "undefined") return;
    const points = Array.isArray(data.heatmap_points) ? data.heatmap_points : [];
    const metricSelect = document.getElementById("price-map-metric");
    if (metricSelect) {
      metricSelect.value = localStorage.getItem(priceMapMetricStorageKey) || "price";
      metricSelect.onchange = () => {
        localStorage.setItem(priceMapMetricStorageKey, metricSelect.value);
        updateHeatmapSource(points);
      };
    }
    if (!points.length) {
      if (emptyNote) emptyNote.hidden = false;
      return;
    }
    if (emptyNote) emptyNote.hidden = true;
    if (heatmapMap && heatmapMap._loaded) {
      setTimeout(() => heatmapMap.resize(), 20);
      updateHeatmapSource(points);
      return;
    }
    if (heatmapMap) {
      heatmapMap.once("load", () => updateHeatmapSource(points));
      return;
    }

    fetch("/api/configuracion-mapa/").then((response) => response.json()).then((config) => {
      heatmapMap = new maplibregl.Map({
        container: "price-heatmap-map",
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
        center: config.center,
        zoom: config.zoom || 12
      });
      heatmapMap.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
      heatmapMap.once("load", () => {
        updateHeatmapSource(points);
      });
      heatmapMap.on("click", "heatmap-points", (event) => {
        const feature = event.features?.[0];
        if (!feature) return;
        const props = feature.properties || {};
        if (heatmapPopup) heatmapPopup.remove();
        heatmapPopup = new maplibregl.Popup({ offset: 12 })
          .setLngLat(feature.geometry.coordinates)
          .setHTML(`
            <div class="map-popup">
              <strong>${props.currency || ""} ${Math.round(Number(props.price) || 0).toLocaleString("es-AR")}</strong>
              <p>${props.title || ""}</p>
              <small>${props.zone || ""}</small><br>
              <button class="text-button" type="button" data-map-preview-id="${props.id || ""}">Abrir ficha</button>
            </div>
          `)
          .addTo(heatmapMap);
      });
      heatmapMap.on("mouseenter", "heatmap-points", () => {
        heatmapMap.getCanvas().style.cursor = "pointer";
      });
      heatmapMap.on("mouseleave", "heatmap-points", () => {
        heatmapMap.getCanvas().style.cursor = "";
      });
    }).catch(() => {
      if (emptyNote) {
        emptyNote.hidden = false;
        emptyNote.textContent = "No se pudo cargar la configuración del mapa.";
      }
    });
  }

  function updateHeatmapSource(points) {
    if (!heatmapMap) return;
    if (!points.length) return;
    const metric = document.getElementById("price-map-metric")?.value || "price";
    const surfaceRows = new Map((data.surface_price || []).map((item) => [String(item.id), item]));
    const features = points
      .filter((item) => Number.isFinite(Number(item.longitude)) && Number.isFinite(Number(item.latitude)))
      .map((item) => ({
        type: "Feature",
        geometry: {
          type: "Point",
          coordinates: [Number(item.longitude), Number(item.latitude)]
        },
        properties: {
          id: item.id,
          title: item.title || "",
          price: Number(item.price) || 0,
          price_m2: Number(item.price_m2) || 0,
          area: Number(item.area) || 0,
          currency: item.currency || "USD",
          zone: item.zone || "Sin zona",
          url: item.url || "",
          is_hidden: item.is_hidden ? 1 : 0,
          metric_value: metricValue(item, metric, surfaceRows),
          metric_label: metricLabel(metric),
          weight: metric === "density" ? 1 : metricValue(item, metric, surfaceRows)
        }
      }));
    if (!features.length) return;

    const values = features.map((item) => Number(item.properties.metric_value)).filter((value) => Number.isFinite(value));
    const low = quantile(values, 0.05);
    const high = quantile(values, 0.95);
    const normalizeWeight = (value) => {
      if (!Number.isFinite(value)) return 0;
      if (!Number.isFinite(high) || high === low) return 0.55;
      return Math.max(0, Math.min(1, (value - low) / (high - low)));
    };
    features.forEach((feature) => {
      feature.properties.weight = metric === "density" ? 1 : normalizeWeight(feature.properties.metric_value);
    });
    renderPriceMapLegend(metric, low, high);

    const focusBounds = calculateHeatmapFocusBounds(features);
    const fitHeatmapToData = () => {
      if (!focusBounds || focusBounds.isEmpty()) return;
      requestAnimationFrame(() => {
        heatmapMap.resize();
        heatmapMap.fitBounds(focusBounds, { padding: 34, maxZoom: 15.5, duration: 0 });
        setTimeout(() => heatmapMap.resize(), 80);
      });
    };

    const source = heatmapMap.getSource("price-heat-source");
    if (!source) {
      heatmapMap.addSource("price-heat-source", {
        type: "geojson",
        data: {
          type: "FeatureCollection",
          features
        }
      });
      heatmapMap.addLayer({
        id: "price-heat",
        type: "heatmap",
        source: "price-heat-source",
        layout: { visibility: metric === "density" ? "visible" : "none" },
        paint: {
          "heatmap-weight": 1,
          "heatmap-intensity": [
            "interpolate",
            ["linear"],
            ["zoom"],
            10, 0.9,
            15, 1.35
          ],
          "heatmap-color": [
            "interpolate",
            ["linear"],
            ["heatmap-density"],
            0, "rgba(32, 94, 210, 0)",
            0.08, "rgba(32, 94, 210, 0.38)",
            0.22, "rgba(18, 181, 255, 0.62)",
            0.38, "rgba(39, 205, 121, 0.72)",
            0.56, "rgba(222, 242, 68, 0.82)",
            0.72, "rgba(255, 188, 43, 0.9)",
            0.88, "rgba(255, 104, 31, 0.94)",
            1, "rgba(220, 24, 24, 0.98)"
          ],
          "heatmap-radius": [
            "interpolate",
            ["linear"],
            ["zoom"],
            10, 28,
            15, 48
          ],
          "heatmap-opacity": 0.82
        }
      });
      heatmapMap.addLayer({
        id: "heatmap-points",
        type: "circle",
        source: "price-heat-source",
        paint: {
          "circle-radius": [
            "interpolate",
            ["linear"],
            ["get", "weight"],
            0, 5,
            1, 12
          ],
          "circle-color": [
            "interpolate",
            ["linear"],
            ["get", "weight"],
            0, "#2f80ed",
            0.25, "#25c7a0",
            0.5, "#d9ef43",
            0.75, "#f59f29",
            1, "#d7191c"
          ],
          "circle-opacity": metric === "density" ? 0.45 : 0.92,
          "circle-stroke-width": 1,
          "circle-stroke-color": "#ffffff"
        },
        minzoom: 0
      });
      localStorage.setItem(mapStorageKey, "1");
      fitHeatmapToData();
      return;
    }

    source.setData({
      type: "FeatureCollection",
      features
    });
    if (heatmapMap.getLayer("price-heat")) {
      heatmapMap.setLayoutProperty("price-heat", "visibility", metric === "density" ? "visible" : "none");
    }
    if (heatmapMap.getLayer("heatmap-points")) {
      heatmapMap.setPaintProperty("heatmap-points", "circle-opacity", metric === "density" ? 0.45 : 0.92);
    }
    fitHeatmapToData();
  }

  function metricLabel(metric) {
    if (metric === "price_m2") return "USD/m2";
    if (metric === "discount") return "Descuento vs tendencia";
    if (metric === "density") return "Densidad";
    return "Precio total";
  }

  function metricValue(item, metric, surfaceRows) {
    if (metric === "price_m2") return Number(item.price_m2) || 0;
    if (metric === "discount") {
      const row = surfaceRows.get(String(item.id));
      const area = Number(row?.x || item.area);
      const price = Number(row?.y || item.price);
      if (!surfaceRegression || !Number.isFinite(area) || !Number.isFinite(price) || area <= 0) return 0;
      const expected = surfaceRegression.predict(area);
      return expected > 0 ? ((expected - price) / expected) * 100 : 0;
    }
    if (metric === "density") return 1;
    return Number(item.price) || 0;
  }

  function quantile(values, q) {
    const sorted = values.filter((value) => Number.isFinite(value)).sort((a, b) => a - b);
    if (!sorted.length) return 0;
    const index = Math.max(0, Math.min(sorted.length - 1, Math.floor((sorted.length - 1) * q)));
    return sorted[index];
  }

  function renderPriceMapLegend(metric, low, high) {
    const legend = document.getElementById("price-map-legend");
    if (!legend) return;
    if (metric === "density") {
      legend.innerHTML = "<span>Modo densidad: rojo = mayor concentracion de publicaciones, no precio mas alto.</span>";
      return;
    }
    const format = (value) => {
      if (metric === "discount") return `${Math.round(value)}%`;
      return `USD ${Math.round(value || 0).toLocaleString("es-AR")}`;
    };
    legend.innerHTML = `
      <span>Bajo ${format(low)}</span>
      <i></i>
      <span>Alto ${format(high)}</span>
    `;
  }

  function calculateHeatmapFocusBounds(features) {
    const coordinates = features.map((feature) => feature.geometry.coordinates);
    if (!coordinates.length) return null;
    const focusedCoordinates = coordinates.length >= 30
      ? trimCoordinateOutliers(coordinates)
      : coordinates;
    const bounds = new maplibregl.LngLatBounds();
    focusedCoordinates.forEach((coordinate) => bounds.extend(coordinate));
    return bounds;
  }

  function trimCoordinateOutliers(coordinates) {
    const longitudes = coordinates.map(([longitude]) => longitude).sort((a, b) => a - b);
    const latitudes = coordinates.map(([, latitude]) => latitude).sort((a, b) => a - b);
    const lowerIndex = Math.floor(coordinates.length * 0.05);
    const upperIndex = Math.max(lowerIndex, Math.ceil(coordinates.length * 0.95) - 1);
    const minLongitude = longitudes[lowerIndex];
    const maxLongitude = longitudes[upperIndex];
    const minLatitude = latitudes[lowerIndex];
    const maxLatitude = latitudes[upperIndex];
    const trimmed = coordinates.filter(([longitude, latitude]) =>
      longitude >= minLongitude
      && longitude <= maxLongitude
      && latitude >= minLatitude
      && latitude <= maxLatitude
    );
    return trimmed.length >= 3 ? trimmed : coordinates;
  }

  function opportunityRows(values, regression) {
    if (!regression) return [];
    return values
      .filter((item) => Number.isFinite(item.x) && Number.isFinite(item.y) && item.x > 0 && item.y > 0)
      .map((item) => {
        const expected = regression.predict(item.x);
        const discount = expected > 0 ? (expected - item.y) / expected * 100 : 0;
        const priceM2Bonus = Number.isFinite(Number(item.price_m2)) ? Math.max(0, 1100 - Number(item.price_m2)) / 35 : 0;
        const locationBonus = ["high", "medium"].includes(item.location_confidence) ? 8 : 0;
        const qualityBonus = Number(item.quality_score || 0) / 12;
        const coverage = Number(item.security_coverage_score);
        const risk = Number(item.security_risk_score);
        const securityBonus = Number.isFinite(coverage) && coverage >= 60 ? 8 : 0;
        const negotiationBonus = Number.isFinite(risk) && risk >= 55 ? 4 : 0;
        const securityTag = Number.isFinite(coverage) && coverage >= 60
          ? "Oportunidad segura"
          : (Number.isFinite(risk) && risk >= 55 ? "Negociable por riesgo" : "Oportunidad");
        return {
          ...item,
          expected,
          discount,
          securityTag,
          opportunity_score: discount + priceM2Bonus + locationBonus + qualityBonus + securityBonus + negotiationBonus
        };
      })
      .filter((item) => item.discount > 3)
      .sort((a, b) => b.opportunity_score - a.opportunity_score)
      .slice(0, 12);
  }

  function renderOpportunityPanel() {
    const container = document.getElementById("opportunity-list");
    if (!container) return;
    const rows = opportunityRows(data.surface_price || [], surfaceRegression);
    if (!rows.length) {
      container.innerHTML = '<div class="audit-note">No hay oportunidades claras con los filtros actuales. Probá ampliar zona, superficie o incluir ocultas.</div>';
      return;
    }
    container.innerHTML = rows.map((item, index) => `
      <article class="opportunity-card">
        <div class="opportunity-rank">#${index + 1}</div>
        ${item.image ? `<img src="${escapeHtml(item.image)}" alt="">` : `<div class="chart-preview-placeholder"></div>`}
        <div>
          <strong>${escapeHtml(formatPrice(item))}</strong>
          <h3>${escapeHtml(item.title || "Propiedad")}</h3>
          <p>${escapeHtml(item.address || item.zone || "")}</p>
          <div class="opportunity-metrics">
            <span>${Math.round(item.discount)}% bajo tendencia</span>
            <span>${item.price_m2 ? `${Math.round(item.price_m2).toLocaleString("es-AR")} /m2` : "Sin m2"}</span>
            <span>${escapeHtml(item.securityTag)}</span>
            ${Number.isFinite(Number(item.security_coverage_score)) ? `<span>Seg. ${Math.round(Number(item.security_coverage_score))}/100</span>` : ""}
            <span>Calidad ${Math.round(item.quality_score || 0)}%</span>
          </div>
          <button class="secondary-button property-preview-trigger" type="button" data-property-id="${item.id}">
            <i data-lucide="panel-right-open"></i> Ver y editar
          </button>
        </div>
      </article>
    `).join("");
    lucide.createIcons();
  }

  function createLiquidityChart() {
    const ctx = document.getElementById("liquidity-chart");
    const rows = Array.isArray(data.liquidity) ? data.liquidity : [];
    if (!ctx || !rows.length || typeof Chart === "undefined") return null;
    const chart = new Chart(ctx, {
      type: "bar",
      data: {
        labels: rows.map((row) => row.label),
        datasets: [
          {
            label: "Publicaciones",
            data: rows.map((row) => row.value),
            backgroundColor: colors[0],
            metaItems: rows
          },
          {
            label: "Persistentes",
            data: rows.map((row) => row.persistent),
            backgroundColor: colors[1],
            metaItems: rows
          },
          {
            label: "Sin movimiento",
            data: rows.map((row) => row.stale),
            backgroundColor: colors[2],
            metaItems: rows
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: true } },
        scales: { x: { stacked: false }, y: { beginAtZero: true } }
      }
    });
    chart.canvas.chart = chart;
    return chart;
  }

  function renderZoneTypeMatrix() {
    const container = document.getElementById("zone-type-matrix");
    const rows = Array.isArray(data.zone_type_matrix) ? data.zone_type_matrix : [];
    if (!container) return;
    if (!rows.length) {
      container.innerHTML = '<div class="audit-note">No hay datos suficientes para cruzar zona y tipo.</div>';
      return;
    }
    container.innerHTML = `
      <table>
        <thead>
          <tr>
            <th>Zona</th>
            <th>Tipo</th>
            <th>Cantidad</th>
            <th>Prom. precio/m2</th>
            <th>Mediana precio/m2</th>
            <th>Desvío precio/m2</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((row) => `
            <tr>
              <td>${escapeHtml(row.zone)}</td>
              <td>${escapeHtml(row.property_type_label)}</td>
              <td>${row.count}</td>
              <td>${row.avg_price_m2 ? Math.round(row.avg_price_m2).toLocaleString("es-AR") : "-"}</td>
              <td>${row.median_price_m2 ? Math.round(row.median_price_m2).toLocaleString("es-AR") : "-"}</td>
              <td>${row.std_price_m2 ? Math.round(row.std_price_m2).toLocaleString("es-AR") : "-"}</td>
              <td><a href="${escapeHtml(formatListUrl(row.url) || row.url || "#")}">Ver grilla</a></td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
  }

  function renderSecurityPanel() {
    const container = document.getElementById("security-price-panel");
    const security = data.security || {};
    if (!container) return;
    if (!security.configured) {
      container.innerHTML = `
        <div class="audit-note">
          No hay capa fina cargada. Agregá polígonos o puntos a <strong>data/seguridad_hurlingham.geojson</strong> para cruzar seguridad con precio.
        </div>
      `;
      return;
    }
    const rows = Array.isArray(security.rows) ? security.rows.slice(0, 10) : [];
    if (!rows.length) {
      container.innerHTML = '<div class="audit-note">La capa existe, pero ninguna propiedad filtrada intersecta zonas con score.</div>';
      return;
    }
    container.innerHTML = rows.map((item) => `
      <div class="security-row">
        <div>
          <strong>${escapeHtml(formatPrice(item))}</strong>
          <span>${escapeHtml(item.zone || item.address || "Sin zona")}</span>
          <small>Cobertura ${Math.round(Number(item.security_coverage_score) || 0)}/100 · Riesgo ${Math.round(Number(item.security_risk_score) || 0)}/100</small>
          <small>${escapeHtml(item.security_zone_label || item.security_label || "sin zona")} · ${escapeHtml(item.security_source || "sin dato")}</small>
        </div>
        <button class="text-button property-preview-trigger" type="button" data-property-id="${item.id}">Abrir</button>
      </div>
    `).join("");
  }

  function renderSecurityArbitrage() {
    const container = document.getElementById("security-arbitrage-panel");
    const rows = Array.isArray(data.security?.arbitrage) ? data.security.arbitrage : [];
    if (!container) return;
    if (!rows.length) {
      container.innerHTML = '<div class="audit-note">Todavía no hay señales fuertes de arbitraje seguridad/precio con estos filtros.</div>';
      return;
    }
    container.innerHTML = `
      <table>
        <thead>
          <tr>
            <th>Señal</th>
            <th>Propiedad</th>
            <th>Zona seguridad</th>
            <th>Precio/m2</th>
            <th>Cobertura</th>
            <th>Riesgo</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((row) => `
            <tr>
              <td><span class="security-badge ${row.kind === "Sobreprecio riesgoso" || row.kind === "Negociable por riesgo" ? "risk" : ""}">${escapeHtml(row.kind)}</span></td>
              <td>${escapeHtml(row.title || row.address || `#${row.id}`)}</td>
              <td>${escapeHtml(row.security_zone_label || "-")}</td>
              <td>${row.price_m2 ? Math.round(row.price_m2).toLocaleString("es-AR") : "-"}</td>
              <td>${Math.round(Number(row.security_coverage_score) || 0)}</td>
              <td>${Math.round(Number(row.security_risk_score) || 0)}</td>
              <td><button class="text-button property-preview-trigger" type="button" data-property-id="${row.id}">Abrir</button></td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
  }

  function initAnomalyModelFilter() {
    const selector = document.getElementById("anomaly-model-filter");
    const rows = Array.from(document.querySelectorAll("[data-anomaly-row]"));
    const cards = Array.from(document.querySelectorAll("[data-anomaly-card]"));
    const empty = document.getElementById("anomaly-filter-empty");
    const details = document.getElementById("anomaly-details");
    const toggle = document.getElementById("anomaly-details-toggle");
    if (!selector) return;

    const apply = () => {
      const selected = selector.value || "";
      let visibleRows = 0;
      rows.forEach((row) => {
        const visible = !selected || row.dataset.model === selected;
        row.hidden = !visible;
        if (visible) visibleRows += 1;
      });
      cards.forEach((card) => {
        card.classList.toggle("active", Boolean(selected) && card.dataset.anomalyCard === selected);
        card.hidden = false;
      });
      if (empty) empty.hidden = visibleRows > 0;
      if (selected && details) details.open = true;
    };

    selector.addEventListener("change", apply);
    if (toggle && details) {
      toggle.addEventListener("click", () => {
        details.open = !details.open;
      });
    }
    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-anomaly-select]");
      if (!button) return;
      event.preventDefault();
      selector.value = button.dataset.anomalySelect || "";
      apply();
      const table = document.querySelector("[data-anomaly-row]")?.closest(".stats-panel");
      if (table) table.scrollIntoView({ block: "start", behavior: "smooth" });
    });
    apply();
  }

  function renderCharts() {
    const locality = bar("locality-chart", "Publicaciones", data.by_locality);
    const neighborhood = bar("neighborhood-chart", "Publicaciones", data.by_neighborhood);
    const agency = bar("agency-chart", "Publicaciones", data.by_agency);
    const price = bar("price-chart", "Cantidad", data.price_buckets);
    const surface = scatterWithRegression("surface-price-chart", "Superficie vs precio", data.surface_price, "Superficie");
    const bedrooms = scatter("bedrooms-price-chart", "Habitaciones vs precio", data.bedrooms_price, "Dormitorios");
    const bedroomsMl = scatter("bedrooms-price-chart-ml", "Habitaciones vs precio", data.bedrooms_price, "Dormitorios");
    const securityRisk = scatter("security-risk-price-chart", "Precio/m2 vs riesgo", data.security?.risk_price || [], "Riesgo relativo", { yTitle: "Precio/m2" });
    const volatility = zoneVolatility("zone-volatility-chart", "Precio medio por zona (y desvío)", data.zone_price_volatility);
    const liquidity = createLiquidityChart();
    renderOpportunityPanel();
    renderZoneTypeMatrix();
    renderSecurityPanel();
    renderSecurityArbitrage();
    [locality, neighborhood, agency, price, surface, bedrooms, bedroomsMl, securityRisk, volatility, liquidity].forEach((chart) => {
      if (!chart) return;
      const canvas = chart.canvas;
      if (!canvas) return;
      canvas.chart = chart;
    });
  }

  function renderDashboardDeferred() {
    if (dashboardPayloadRendered) return;
    dashboardPayloadRendered = true;
    if (typeof Chart !== "undefined") {
      try {
        renderCharts();
      } catch (error) {
        console.error("No se pudieron renderizar los gráficos del dashboard.", error);
      }
    } else {
      renderOpportunityPanel();
      renderZoneTypeMatrix();
      renderSecurityPanel();
      renderSecurityArbitrage();
    }
  }

  initTabs();
  initAnomalyModelFilter();
  const activeStatsTab = document.querySelector(".stats-tab.active")?.dataset.tab || "overview";
  if (activeStatsTab === "overview") {
    const idleRender = () => renderDashboardDeferred();
    if ("requestIdleCallback" in window) {
      window.requestIdleCallback(idleRender, { timeout: 1800 });
    } else {
      window.setTimeout(idleRender, 450);
    }
  } else {
    renderDashboardDeferred();
  }

  document.addEventListener("click", (event) => {
    const previewButton = event.target.closest(".property-preview-trigger,[data-map-preview-id]");
    if (previewButton) {
      event.preventDefault();
      openPropertyPreview(previewButton.dataset.propertyId || previewButton.dataset.mapPreviewId);
    }
  });

  if (propertyModalClose && propertyModal) {
    propertyModalClose.addEventListener("click", () => propertyModal.close());
  }

  if (propertyModalContent) {
    propertyModalContent.addEventListener("click", async (event) => {
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
          const key = stateButton.dataset.previewState;
          const rawValue = stateButton.dataset.value === "1";
          await updatePreviewState(propertyId, key, rawValue, statusNode);
        } else if (notesButton) {
          const notes = layout.querySelector("#property-preview-notes")?.value || "";
          await savePreviewNotes(propertyId, notes, statusNode);
        } else if (locationButton) {
          await savePreviewLocation(propertyId, statusNode);
        }
      } catch (error) {
        if (statusNode) statusNode.textContent = error.message;
      }
    });

    propertyModalContent.addEventListener("input", (event) => {
      if (event.target?.id !== "property-preview-notes") return;
      const layout = event.target.closest(".property-preview-layout");
      const propertyId = layout?.dataset.propertyId;
      if (!propertyId) return;
      localStorage.setItem(previewNoteBackupKey(propertyId), event.target.value);
      const dirtyStatus = layout.querySelector("[data-preview-dirty-status]");
      if (dirtyStatus) dirtyStatus.textContent = "Nota sin guardar.";
    });

    propertyModalContent.addEventListener("submit", async (event) => {
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

  const modal = document.getElementById("chart-modal");
  const close = document.getElementById("chart-modal-close");
  let modalChart = null;
  document.querySelectorAll(".chart-expand").forEach((button) => {
    button.addEventListener("click", () => {
      const panel = button.closest(".chart-panel");
      const canvas = panel?.querySelector("canvas");
      const config = canvas ? charts.get(canvas.id) : null;
      if (!config || !modal) return;
      document.getElementById("chart-modal-title").textContent = panel?.querySelector("h2")?.textContent || config.title || "";
      if (modalChart) modalChart.destroy();
      modal.showModal();
      modalChart = config.build("chart-modal-canvas");
    });
  });
  if (close) close.addEventListener("click", () => modal.close());
  if (modal) {
    modal.addEventListener("cancel", () => {
      if (modalChart) modalChart.destroy();
      modalChart = null;
    });
    modal.addEventListener("close", () => {
      if (modalChart) modalChart.destroy();
      modalChart = null;
    });
  }

  if (localStorage.getItem(mapStorageKey) === "1") {
    setTimeout(() => {
      const isSpatial = document.querySelector('.stats-tab[data-tab="spatial"].active');
      if (isSpatial) {
        initPriceHeatmap();
        initSecurityMaps();
      }
    }, 0);
  }
  lucide.createIcons();
})();

