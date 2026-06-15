(() => {
  const dataNode = document.getElementById("chart-data");
  if (!dataNode?.textContent) return;
  let data = JSON.parse(dataNode.textContent);
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
  let heatmapMap = null;
  let heatmapPopup = null;
  const securityMaps = {};
  let securityMapPopup = null;
  let locationValueMap = null;
  let locationValueMapPopup = null;
  let locationLayerPromise = null;
  let crimeMap = null;
  let crimeMapPopup = null;
  let crimeLayerPromise = null;
  let crimeChartsRendered = false;
  let surfaceRegression = null;
  let dashboardPayloadRendered = false;
  let dashboardDataPromise = null;
  let dashboardRenderPromise = null;

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
      const renderPromise = tabName === "overview" ? Promise.resolve(data) : renderDashboardDeferred();
      if (tabName === "spatial") {
        renderPromise.then(() => {
          initPriceHeatmap();
          initLocationValueMap();
          initSecurityMaps();
          initCrimeMap();
        }).catch(() => {});
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

  function formatNumber(value) {
    if (value === null || value === undefined || value === "") return "-";
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return "-";
    return Math.round(parsed).toLocaleString("es-AR");
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

  async function loadDashboardData() {
    if (data.loaded || !data.data_url) return data;
    if (!dashboardDataPromise) {
      dashboardDataPromise = fetch(data.data_url, { headers: { "Accept": "application/json" } })
        .then(async (response) => {
          const payload = await response.json().catch(() => ({}));
          if (!response.ok) {
            throw new Error(payload.error || "No se pudieron cargar los datos del dashboard.");
          }
          data = { ...data, ...payload, loaded: true };
          return data;
        })
        .catch((error) => {
          dashboardDataPromise = null;
          document.querySelectorAll(".chart-panel canvas").forEach((canvas) => {
            const panel = canvas.closest(".chart-panel");
            if (panel && !panel.querySelector(".chart-empty-note")) {
              panel.insertAdjacentHTML("beforeend", `<p class="audit-note chart-empty-note">${escapeHtml(error.message)}</p>`);
            }
          });
          throw error;
        });
    }
    return dashboardDataPromise;
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
      window.RadarPropertyPreview?.open(item.id);
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

  function zoneSampleCount(item) {
    return Number(item?.total || item?.value || 0);
  }

  function zoneSampleLabel(item) {
    return `${item.label} (n=${zoneSampleCount(item).toLocaleString("es-AR")})`;
  }

  function zoneSampleIntensity(item, maxTotal) {
    if (!maxTotal) return 1;
    const ratio = Math.max(0, zoneSampleCount(item)) / maxTotal;
    return 0.45 + 0.55 * Math.sqrt(ratio);
  }

  function withZoneSampleVisuals(items) {
    const maxTotal = Math.max(...items.map(zoneSampleCount), 0);
    return items.map((item) => ({
      ...item,
      sampleLabel: zoneSampleLabel(item),
      sampleIntensity: zoneSampleIntensity(item, maxTotal)
    }));
  }

  function rgbaColor(rgb, alpha) {
    return `rgba(${rgb}, ${Math.max(0, Math.min(1, alpha)).toFixed(3)})`;
  }

  function zoneSampleTooltip(item) {
    const count = zoneSampleCount(item);
    const noun = count === 1 ? "propiedad" : "propiedades";
    return `Muestra: ${count.toLocaleString("es-AR")} ${noun} con precio válido`;
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
    const displayItems = withZoneSampleVisuals(sorted);
    const labels = displayItems.map((item) => item.sampleLabel);
    const chart = new Chart(ctx, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "Rango mínimo-máximo",
            data: displayItems.map((item) => [item.min, item.max]),
            backgroundColor: displayItems.map((item) => rgbaColor("47, 64, 56", 0.08 * item.sampleIntensity)),
            borderColor: displayItems.map((item) => rgbaColor("47, 64, 56", 0.55 * item.sampleIntensity)),
            borderWidth: 1,
            borderSkipped: false,
            borderRadius: {
              topLeft: 4,
              bottomLeft: 4,
              topRight: 4,
              bottomRight: 4
            },
            barThickness: 4,
            grouped: false,
            metaItems: displayItems
          },
          {
            label: "Banda promedio +/- desvío",
            data: displayItems.map((item) => [Math.max(0, item.avg - item.std), item.avg + item.std]),
            backgroundColor: displayItems.map((item) => rgbaColor("23, 107, 77", 0.22 * item.sampleIntensity)),
            borderColor: displayItems.map((item) => rgbaColor("23, 107, 77", item.sampleIntensity)),
            borderWidth: 1,
            borderSkipped: false,
            borderRadius: {
              topLeft: 7,
              bottomLeft: 7,
              topRight: 7,
              bottomRight: 7
            },
            barThickness: 12,
            grouped: false,
            metaItems: displayItems
          },
          {
            label: "Promedio",
            type: "scatter",
            data: displayItems.map((item) => ({ x: item.avg, y: item.sampleLabel })),
            backgroundColor: displayItems.map((item) => rgbaColor("23, 107, 77", item.sampleIntensity)),
            borderColor: "#ffffff",
            borderWidth: 2,
            pointRadius: 5,
            pointHoverRadius: 7,
            metaItems: displayItems
          },
          {
            label: "Mediana",
            type: "scatter",
            data: displayItems.map((item) => ({ x: item.median || item.avg, y: item.sampleLabel })),
            backgroundColor: displayItems.map((item) => rgbaColor("214, 165, 40", item.sampleIntensity)),
            borderColor: displayItems.map((item) => rgbaColor("23, 33, 29", item.sampleIntensity)),
            borderWidth: 1,
            pointRadius: 4,
            pointStyle: "rectRot",
            metaItems: displayItems
          },
          {
            label: "Mínimo",
            type: "scatter",
            data: displayItems.map((item) => ({ x: item.min, y: item.sampleLabel })),
            backgroundColor: displayItems.map((item) => rgbaColor("244, 234, 211", 0.45 + 0.55 * item.sampleIntensity)),
            borderColor: displayItems.map((item) => rgbaColor("114, 86, 23", item.sampleIntensity)),
            borderWidth: 1,
            pointRadius: 4,
            pointHoverRadius: 6,
            pointStyle: "triangle",
            pointRotation: 270,
            metaItems: displayItems
          },
          {
            label: "Máximo",
            type: "scatter",
            data: displayItems.map((item) => ({ x: item.max, y: item.sampleLabel })),
            backgroundColor: displayItems.map((item) => rgbaColor("244, 234, 211", 0.45 + 0.55 * item.sampleIntensity)),
            borderColor: displayItems.map((item) => rgbaColor("114, 86, 23", item.sampleIntensity)),
            borderWidth: 1,
            pointRadius: 4,
            pointHoverRadius: 6,
            pointStyle: "triangle",
            pointRotation: 90,
            metaItems: displayItems
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
                const sample = zoneSampleTooltip(item);
                if (context.dataset.label === "Rango mínimo-máximo") {
                  return [
                    `Rango: ${Math.round(item.min).toLocaleString("es-AR")} a ${Math.round(item.max).toLocaleString("es-AR")}`,
                    sample
                  ];
                }
                if (context.dataset.label === "Banda promedio +/- desvío") {
                  const low = Math.max(0, item.avg - item.std);
                  const high = item.avg + item.std;
                  return [
                    `Banda: ${Math.round(low).toLocaleString("es-AR")} a ${Math.round(high).toLocaleString("es-AR")}`,
                    sample
                  ];
                }
                if (context.dataset.label === "Mínimo") {
                  return [`Mínimo: ${Math.round(item.min).toLocaleString("es-AR")}`, sample];
                }
                if (context.dataset.label === "Máximo") {
                  return [`Máximo: ${Math.round(item.max).toLocaleString("es-AR")}`, sample];
                }
                if (context.dataset.label === "Mediana") {
                  return [`Mediana: ${Math.round(item.median || item.avg).toLocaleString("es-AR")}`, sample];
                }
                return [
                  `Mínimo: ${Math.round(item.min).toLocaleString("es-AR")}`,
                  `Promedio: ${Math.round(item.avg).toLocaleString("es-AR")}`,
                  `Mediana: ${Math.round(item.median || item.avg).toLocaleString("es-AR")}`,
                  `Máximo: ${Math.round(item.max).toLocaleString("es-AR")}`,
                  `Desvío: ${Math.round(item.std).toLocaleString("es-AR")}`,
                  sample,
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
      min: Number(item.min ?? item.avg ?? 0),
      max: Number(item.max ?? item.avg ?? 0),
      q1: Number(item.q1 ?? item.median ?? item.avg ?? 0),
      q3: Number(item.q3 ?? item.median ?? item.avg ?? 0),
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

  function createZoneBoxplotChart(canvasId, title, sorted) {
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
    const displayItems = withZoneSampleVisuals(sorted);
    const labels = displayItems.map((item) => item.sampleLabel);
    const chart = new Chart(ctx, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "Bigote mínimo-máximo",
            data: displayItems.map((item) => [item.min, item.max]),
            backgroundColor: displayItems.map((item) => rgbaColor("47, 64, 56", 0.08 * item.sampleIntensity)),
            borderColor: displayItems.map((item) => rgbaColor("47, 64, 56", 0.65 * item.sampleIntensity)),
            borderWidth: 1,
            borderSkipped: false,
            borderRadius: 4,
            barThickness: 4,
            grouped: false,
            metaItems: displayItems
          },
          {
            label: "Caja Q1-Q3 (IQR)",
            data: displayItems.map((item) => [item.q1, item.q3]),
            backgroundColor: displayItems.map((item) => rgbaColor("23, 107, 77", 0.22 * item.sampleIntensity)),
            borderColor: displayItems.map((item) => rgbaColor("23, 107, 77", item.sampleIntensity)),
            borderWidth: 1.5,
            borderSkipped: false,
            borderRadius: {
              topLeft: 7,
              bottomLeft: 7,
              topRight: 7,
              bottomRight: 7
            },
            barThickness: 18,
            grouped: false,
            metaItems: displayItems
          },
          {
            label: "Mediana",
            type: "scatter",
            data: displayItems.map((item) => ({ x: item.median || item.avg, y: item.sampleLabel })),
            backgroundColor: displayItems.map((item) => rgbaColor("214, 165, 40", item.sampleIntensity)),
            borderColor: displayItems.map((item) => rgbaColor("23, 33, 29", item.sampleIntensity)),
            borderWidth: 1,
            pointRadius: 5,
            pointHoverRadius: 7,
            pointStyle: "rectRot",
            metaItems: displayItems
          },
          {
            label: "Promedio",
            type: "scatter",
            data: displayItems.map((item) => ({ x: item.avg, y: item.sampleLabel })),
            backgroundColor: displayItems.map((item) => rgbaColor("23, 107, 77", item.sampleIntensity)),
            borderColor: "#ffffff",
            borderWidth: 2,
            pointRadius: 4,
            pointHoverRadius: 6,
            metaItems: displayItems
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
                const sample = zoneSampleTooltip(item);
                if (context.dataset.label === "Bigote mínimo-máximo") {
                  return [
                    `Bigote: ${Math.round(item.min).toLocaleString("es-AR")} a ${Math.round(item.max).toLocaleString("es-AR")}`,
                    sample
                  ];
                }
                if (context.dataset.label === "Caja Q1-Q3 (IQR)") {
                  return [
                    `IQR: ${Math.round(item.q1).toLocaleString("es-AR")} a ${Math.round(item.q3).toLocaleString("es-AR")}`,
                    sample
                  ];
                }
                if (context.dataset.label === "Mediana") {
                  return [`Mediana: ${Math.round(item.median || item.avg).toLocaleString("es-AR")}`, sample];
                }
                return [
                  `Mínimo: ${Math.round(item.min).toLocaleString("es-AR")}`,
                  `Q1: ${Math.round(item.q1).toLocaleString("es-AR")}`,
                  `Mediana: ${Math.round(item.median || item.avg).toLocaleString("es-AR")}`,
                  `Q3: ${Math.round(item.q3).toLocaleString("es-AR")}`,
                  `Máximo: ${Math.round(item.max).toLocaleString("es-AR")}`,
                  `Promedio: ${Math.round(item.avg).toLocaleString("es-AR")}`,
                  sample
                ];
              }
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
      dataset.metaItems = displayItems;
    });
    chart.canvas.chart = chart;
    return chart;
  }

  function zoneBoxplot(id, title, values) {
    const parsed = normalize(values).map((item) => ({
      label: item.label,
      avg: Number(item.avg || 0),
      median: Number(item.median || item.avg || 0),
      min: Number(item.min ?? item.avg ?? 0),
      max: Number(item.max ?? item.avg ?? 0),
      q1: Number(item.q1 ?? item.median ?? item.avg ?? 0),
      q3: Number(item.q3 ?? item.median ?? item.avg ?? 0),
      total: item.total || 0,
      url: item.url,
      pending: item.pending || 0,
      reviewed: item.reviewed || 0,
      favorites: item.favorites || 0,
      value: item.total || 0
    }));
    const sorted = [...parsed].sort((a, b) => b.total - a.total);
    const chart = createZoneBoxplotChart(id, title, sorted);
    register(id, title, (targetCanvasId) => createZoneBoxplotChart(targetCanvasId, title, sorted));
    return chart;
  }

  function buildSurfaceOutlierRows(items, regression) {
    const outlierContainer = document.getElementById("surface-outliers");
    if (!outlierContainer) return;
    const comparableItems = items
      .filter((item) =>
        Number.isFinite(Number(item.x))
        && Number.isFinite(Number(item.y))
        && Number.isFinite(Number(item.expected_price))
        && Number(item.comparable_count || 0) >= 5
      );
    if (!comparableItems.length) {
      outlierContainer.innerHTML = `
        <div class="audit-note">No hay suficientes comparables para calcular la tendencia.</div>
      `;
      return;
    }
    const outliers = comparableItems
      .map((item) => {
        const expected = Number(item.expected_price);
        return {
          item,
          delta: Number(item.y) - expected,
          expected
        };
      })
      .filter((entry) => Number(entry.item.discount) > 3)
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
          <td>${escapeHtml(entry.item.comparable_group || "-")} (${Math.round(Number(entry.item.comparable_count) || 0)})</td>
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
              <th>Comparables</th>
              <th></th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `;
  }

  function scatterWithRegression(id, label, values, xTitle) {
    const chart = scatter(id, label, values, xTitle, { withRegression: id !== "surface-price-chart" });
    const validItems = values.filter((item) => Number.isFinite(item.x) && Number.isFinite(item.y));
    surfaceRegression = id === "surface-price-chart" ? null : drawRegressionLine(validItems);
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

  function upsertGeoJsonSource(map, sourceId, data) {
    const source = map.getSource(sourceId);
    if (source && typeof source.setData === "function") {
      source.setData(data);
      return;
    }
    if (!source) {
      map.addSource(sourceId, { type: "geojson", data });
    }
  }

  function addLayerIfMissing(map, layer) {
    if (!map.getLayer(layer.id)) {
      map.addLayer(layer);
    }
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
        upsertGeoJsonSource(map, `${containerId}-zones`, zones);
        upsertGeoJsonSource(map, `${containerId}-points`, points);
        addLayerIfMissing(map, {
          id: `${containerId}-zone-fill`,
          type: "fill",
          source: `${containerId}-zones`,
          paint: {
            "fill-color": securityFillColor(mode),
            "fill-opacity": 0.54
          }
        });
        addLayerIfMissing(map, {
          id: `${containerId}-zone-line`,
          type: "line",
          source: `${containerId}-zones`,
          paint: {
            "line-color": mode === "risk" ? "#7f1d1d" : "#07502f",
            "line-width": 1.2,
            "line-opacity": 0.75
          }
        });
        addLayerIfMissing(map, {
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
        if (!map._radarHandlersBound) {
          map._radarHandlersBound = true;
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
        }
        map.resize();
      });
    }).catch(() => {
      container.innerHTML = '<div class="audit-note">No se pudo cargar la configuracion del mapa.</div>';
    });
  }

  function locationValueFillColor() {
    return [
      "interpolate",
      ["linear"],
      ["get", "overall_score"],
      0, "#f3efe6",
      40, "#f6c97b",
      55, "#b9d984",
      70, "#42a873",
      85, "#0b6448",
      100, "#053d2d"
    ];
  }

  function loadLocationLayers() {
    if (data.location_intelligence?.layers) return Promise.resolve(data.location_intelligence.layers);
    if (locationLayerPromise) return locationLayerPromise;
    locationLayerPromise = fetch("/api/inteligencia-territorial/capas/")
      .then((response) => response.json())
      .then((payload) => {
        data.location_intelligence = data.location_intelligence || {};
        data.location_intelligence.layers = payload;
        data.location_intelligence.configured = payload.configured || data.location_intelligence.configured;
        return payload;
      });
    return locationLayerPromise;
  }

  function initLocationValueMap() {
    const container = document.getElementById("location-value-map");
    if (!container || typeof maplibregl === "undefined") return;
    loadLocationLayers()
      .then((payload) => {
        const zones = payload.zones || { type: "FeatureCollection", features: [] };
        if (!zones.features?.length) {
          container.innerHTML = '<div class="audit-note">No hay capa territorial cargada.</div>';
          return;
        }
        renderLocationValueLegend();
        if (locationValueMap && locationValueMap._loaded) {
          locationValueMap.resize();
          return;
        }
        if (locationValueMap) {
          locationValueMap.once("load", () => locationValueMap.resize());
          return;
        }
        fetch("/api/configuracion-mapa/").then((response) => response.json()).then((config) => {
          locationValueMap = new maplibregl.Map({
            container: "location-value-map",
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
          locationValueMap.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
          locationValueMap.once("load", () => {
            upsertGeoJsonSource(locationValueMap, "location-value-zones", zones);
            addLayerIfMissing(locationValueMap, {
              id: "location-value-fill",
              type: "fill",
              source: "location-value-zones",
              paint: {
                "fill-color": locationValueFillColor(),
                "fill-opacity": 0.58
              }
            });
            addLayerIfMissing(locationValueMap, {
              id: "location-value-line",
              type: "line",
              source: "location-value-zones",
              paint: {
                "line-color": "#0b4f3a",
                "line-width": 1.2,
                "line-opacity": 0.72
              }
            });
            const bounds = securityMapBounds(zones);
            if (bounds) locationValueMap.fitBounds(bounds, { padding: 30, duration: 0 });
            if (!locationValueMap._radarHandlersBound) {
              locationValueMap._radarHandlersBound = true;
              locationValueMap.on("click", "location-value-fill", (event) => {
                const feature = event.features?.[0];
                if (!feature) return;
                const props = feature.properties || {};
                if (locationValueMapPopup) locationValueMapPopup.remove();
                locationValueMapPopup = new maplibregl.Popup({ offset: 10 })
                  .setLngLat(event.lngLat)
                  .setHTML(`
                    <div class="map-popup">
                      <strong>${escapeHtml(props.zone_name || "Zona")}</strong>
                      <p>Score territorial: ${formatScoreCell(props.overall_score)}</p>
                      <small>Transporte ${formatScoreCell(props.transport_score)} · Educación ${formatScoreCell(props.education_score)} · Salud ${formatScoreCell(props.health_score)}</small><br>
                      <small>Riesgo hídrico ${props.in_flood_risk_zone ? "sí" : "no"} · RENABAP ${formatMeters(props.nearest_renabap_m)}</small>
                    </div>
                  `)
                  .addTo(locationValueMap);
              });
              locationValueMap.on("mouseenter", "location-value-fill", () => { locationValueMap.getCanvas().style.cursor = "pointer"; });
              locationValueMap.on("mouseleave", "location-value-fill", () => { locationValueMap.getCanvas().style.cursor = ""; });
            }
            locationValueMap.resize();
          });
        }).catch(() => {
          container.innerHTML = '<div class="audit-note">No se pudo cargar la configuracion del mapa.</div>';
        });
      })
      .catch(() => {
        container.innerHTML = '<div class="audit-note">No se pudo cargar la capa territorial.</div>';
      });
  }

  function renderLocationValueLegend() {
    const legend = document.getElementById("location-value-map-legend");
    if (!legend) return;
    legend.innerHTML = `
      <span>Bajo 0</span>
      <i></i>
      <span>Alto 100</span>
    `;
  }

  function crimeMetrics() {
    return data.crime?.metrics || data.crime?.summary?.metrics || data.crime?.layers?.summary?.metrics || {};
  }

  function loadCrimeLayers() {
    if (data.crime?.layers) return Promise.resolve(data.crime.layers);
    if (crimeLayerPromise) return crimeLayerPromise;
    crimeLayerPromise = fetch("/api/crimen/capas/")
      .then((response) => response.json())
      .then((payload) => {
        data.crime = data.crime || {};
        data.crime.layers = payload;
        if (payload.summary?.metrics) {
          data.crime.metrics = payload.summary.metrics;
        }
        return payload;
      });
    return crimeLayerPromise;
  }

  function renderCrimeKpis() {
    const container = document.getElementById("crime-kpi-panel");
    if (!container) return;
    const crime = data.crime || {};
    const metrics = crimeMetrics();
    if (!crime.configured || !Object.keys(metrics).length) {
      container.innerHTML = '<div class="audit-note">No hay paquete de crimen configurado en data/geo/crime_*.</div>';
      return;
    }
    const period = `${metrics.crime_metric_window_start_year || "?"}-${metrics.crime_metric_window_end_year || "?"}`;
    const cards = [
      ["Total reportado", metrics.reported_crimes_total],
      ["Contra propiedad", metrics.reported_property_crime_count],
      ["Robos", metrics.reported_robbery_count],
      ["Hurtos", metrics.reported_theft_count],
      ["Vehiculos", metrics.reported_vehicle_crime_count],
      ["Homicidios", metrics.reported_homicide_count],
      ["Victimas homicidio", metrics.reported_homicide_victim_count],
      ["Lesiones", metrics.reported_injury_count],
      ["Integridad sexual", metrics.reported_sexual_integrity_count]
    ];
    container.innerHTML = cards.map(([label, value]) => `
      <div class="crime-kpi">
        <span>${escapeHtml(label)}</span>
        <strong>${formatNumber(value)}</strong>
        <small>${escapeHtml(period)} · municipio</small>
      </div>
    `).join("");
  }

  function crimeGroupLabel(group) {
    const labels = {
      homicidio: "Homicidios",
      hurto: "Hurtos",
      hurto_vehiculo: "Hurto vehicular",
      integridad_sexual: "Integridad sexual",
      lesiones: "Lesiones",
      robo: "Robos",
      robo_vehiculo: "Robo vehicular",
      vehiculo: "Vehiculos",
      propiedad: "Propiedad",
      extorsion: "Extorsion",
      secuestro: "Secuestro"
    };
    return labels[group] || String(group || "Sin dato").replaceAll("_", " ");
  }

  function metricWindow() {
    const metrics = crimeMetrics();
    return {
      start: Number(metrics.crime_metric_window_start_year) || 2017,
      end: Number(metrics.crime_metric_window_end_year) || 2024
    };
  }

  function destroyCanvasChart(canvasId) {
    const canvas = document.getElementById(canvasId);
    if (canvas?.chart) {
      canvas.chart.destroy();
      canvas.chart = null;
    }
    return canvas;
  }

  function renderCrimeMonthlyChart(payload, canvasId = "crime-monthly-chart") {
    const canvas = destroyCanvasChart(canvasId);
    if (!canvas || typeof Chart === "undefined") return null;
    const rows = payload.timeseries?.monthly || [];
    if (!rows.length) {
      canvas.insertAdjacentHTML("afterend", '<p class="audit-note chart-empty-note">No hay serie SNIC mensual disponible.</p>');
      return null;
    }
    const measureSelect = document.getElementById("crime-monthly-measure");
    const measure = measureSelect?.value || "cantidad_hechos";
    const { start, end } = metricWindow();
    const filtered = rows.filter((row) =>
      Number(row.period_year) >= start
      && Number(row.period_year) <= end
      && Number(row[measure]) > 0
    );
    const labels = [...new Set(filtered.map((row) => row.period))].sort();
    const totalsByGroup = {};
    filtered.forEach((row) => {
      totalsByGroup[row.crime_group] = (totalsByGroup[row.crime_group] || 0) + Number(row[measure] || 0);
    });
    const preferred = ["robo", "hurto", "robo_vehiculo", "hurto_vehiculo", "lesiones", "integridad_sexual", "homicidio"];
    const groups = Object.keys(totalsByGroup)
      .sort((a, b) => (preferred.indexOf(a) === -1 ? 99 : preferred.indexOf(a)) - (preferred.indexOf(b) === -1 ? 99 : preferred.indexOf(b)) || totalsByGroup[b] - totalsByGroup[a])
      .slice(0, 7);
    const lookup = new Map(filtered.map((row) => [`${row.period}|${row.crime_group}`, Number(row[measure] || 0)]));
    const chart = new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: groups.map((group, index) => ({
          label: crimeGroupLabel(group),
          data: labels.map((label) => lookup.get(`${label}|${group}`) || 0),
          borderColor: colors[index % colors.length],
          backgroundColor: colors[index % colors.length],
          tension: 0.24,
          pointRadius: 0,
          borderWidth: 2
        }))
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { display: true, position: "bottom" } },
        scales: {
          x: { ticks: { autoSkip: true, maxTicksLimit: 12 } },
          y: { beginAtZero: true }
        }
      }
    });
    canvas.chart = chart;
    if (canvasId === "crime-monthly-chart") {
      register(canvasId, "Crimen mensual SNIC", (targetCanvasId) => renderCrimeMonthlyChart(payload, targetCanvasId));
    }
    if (canvasId === "crime-monthly-chart" && measureSelect && !measureSelect.dataset.bound) {
      measureSelect.dataset.bound = "1";
      measureSelect.addEventListener("change", () => {
        loadCrimeLayers().then(renderCrimeMonthlyChart).catch(() => {});
      });
    }
    return chart;
  }

  function annualCrimeRows(rows, start, end) {
    const annual = {};
    rows.forEach((row) => {
      const year = Number(row.period_year);
      if (!year || year < start || year > end) return;
      annual[year] = annual[year] || {};
      annual[year][row.crime_group] = (annual[year][row.crime_group] || 0) + Number(row.cantidad_hechos || 0);
    });
    return annual;
  }

  function renderCrimePropertyChart(payload, canvasId = "crime-property-chart") {
    const canvas = destroyCanvasChart(canvasId);
    if (!canvas || typeof Chart === "undefined") return null;
    const rows = payload.timeseries?.property_monthly || [];
    if (!rows.length) {
      canvas.insertAdjacentHTML("afterend", '<p class="audit-note chart-empty-note">No hay serie SAT Propiedad disponible.</p>');
      return null;
    }
    const { start, end } = metricWindow();
    const annual = annualCrimeRows(rows, start, end);
    const labels = Object.keys(annual).sort();
    const totals = {};
    labels.forEach((year) => {
      Object.entries(annual[year]).forEach(([group, value]) => {
        totals[group] = (totals[group] || 0) + Number(value || 0);
      });
    });
    const preferred = ["robo", "hurto", "robo_vehiculo", "hurto_vehiculo", "extorsion", "secuestro"];
    const groups = Object.keys(totals)
      .sort((a, b) => (preferred.indexOf(a) === -1 ? 99 : preferred.indexOf(a)) - (preferred.indexOf(b) === -1 ? 99 : preferred.indexOf(b)) || totals[b] - totals[a])
      .slice(0, 7);
    const chart = new Chart(canvas, {
      type: "bar",
      data: {
        labels,
        datasets: groups.map((group, index) => ({
          label: crimeGroupLabel(group),
          data: labels.map((year) => annual[year]?.[group] || 0),
          backgroundColor: colors[index % colors.length]
        }))
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: true, position: "bottom" } },
        scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true } }
      }
    });
    canvas.chart = chart;
    if (canvasId === "crime-property-chart") {
      register(canvasId, "SAT Propiedad", (targetCanvasId) => renderCrimePropertyChart(payload, targetCanvasId));
    }
    return chart;
  }

  function renderCrimeSeasonality(payload) {
    const container = document.getElementById("crime-seasonality-panel");
    if (!container) return;
    const rows = payload.timeseries?.property_seasonality || [];
    if (!rows.length) {
      container.innerHTML = '<div class="audit-note">No hay datos para el heatmap de estacionalidad.</div>';
      return;
    }
    const { start, end } = metricWindow();
    const selected = rows.filter((row) => Number(row.period_year) >= start && Number(row.period_year) <= end);
    const years = [...new Set(selected.map((row) => Number(row.period_year)))].sort((a, b) => a - b);
    const months = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"];
    const values = new Map(selected.map((row) => [`${row.period_year}-${row.period_month}`, Number(row.value || 0)]));
    const maxValue = Math.max(...selected.map((row) => Number(row.value || 0)), 1);
    container.innerHTML = `
      <table class="crime-seasonality-table">
        <thead>
          <tr><th>Anio</th>${months.map((month) => `<th>${month}</th>`).join("")}</tr>
        </thead>
        <tbody>
          ${years.map((year) => `
            <tr>
              <th>${year}</th>
              ${months.map((_month, index) => {
                const value = values.get(`${year}-${index + 1}`) || 0;
                const alpha = 0.12 + Math.min(0.78, (value / maxValue) * 0.78);
                return `<td style="background-color: rgba(189, 92, 61, ${alpha.toFixed(3)});" title="${year}-${String(index + 1).padStart(2, "0")}: ${formatNumber(value)}">${formatNumber(value)}</td>`;
              }).join("")}
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
  }

  function renderCrimeCharts(payload) {
    renderCrimeMonthlyChart(payload);
    renderCrimePropertyChart(payload);
    renderCrimeSeasonality(payload);
    crimeChartsRendered = true;
  }

  function renderCrimeZoneInsights() {
    const container = document.getElementById("crime-zone-insights-panel");
    const rows = Array.isArray(data.crime?.zone_insights) ? data.crime.zone_insights : [];
    if (!container) return;
    if (!rows.length) {
      container.innerHTML = '<div class="audit-note">No hay zonas con datos cruzables para los filtros actuales.</div>';
      return;
    }
    container.innerHTML = `
      <table>
        <thead>
          <tr>
            <th>Zona</th>
            <th>Propiedades</th>
            <th>Mediana USD/m2</th>
            <th>Cobertura seg.</th>
            <th>Riesgo seg.</th>
            <th>Centroides SAT-HD</th>
            <th>Victimas</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((row) => `
            <tr>
              <td>${escapeHtml(row.zone)}</td>
              <td>${formatNumber(row.property_count)}</td>
              <td>${formatNumber(row.median_price_m2)}</td>
              <td>${formatNumber(row.avg_security_coverage)}</td>
              <td>${formatNumber(row.avg_security_risk)}</td>
              <td>${formatNumber(row.homicide_radio_event_count)}</td>
              <td>${formatNumber(row.homicide_radio_victim_count)}</td>
              <td><a href="${escapeHtml(formatListUrl(row.url) || row.url || "#")}">Ver zona</a></td>
            </tr>
          `).join("")}
        </tbody>
      </table>
      <p class="audit-note">Precision baja: ${escapeHtml(rows[0]?.precision_note || "crimen municipal y puntos aproximados.")}</p>
    `;
  }

  function populateCrimeMapYearFilter(payload) {
    const select = document.getElementById("crime-map-year");
    if (!select || select.dataset.loaded) return;
    const years = [...new Set((payload.homicide_points?.features || [])
      .map((feature) => Number(feature.properties?.period_year))
      .filter((year) => Number.isFinite(year)))]
      .sort((a, b) => a - b);
    select.innerHTML = '<option value="">Todos</option>' + years.map((year) => `<option value="${year}">${year}</option>`).join("");
    select.dataset.loaded = "1";
    select.addEventListener("change", () => updateCrimeMapSource(payload));
  }

  function filteredCrimePoints(payload) {
    const selectedYear = document.getElementById("crime-map-year")?.value || "";
    const features = payload.homicide_points?.features || [];
    if (!selectedYear) return { type: "FeatureCollection", features };
    return {
      type: "FeatureCollection",
      features: features.filter((feature) => String(feature.properties?.period_year || "") === selectedYear)
    };
  }

  function updateCrimeMapSource(payload) {
    if (!crimeMap || !crimeMap.getSource("crime-homicide-points")) return;
    upsertGeoJsonSource(crimeMap, "crime-homicide-points", filteredCrimePoints(payload));
  }

  function renderCrimeMapLegend(payload) {
    const legend = document.getElementById("crime-map-legend");
    if (!legend) return;
    const metrics = payload.summary?.metrics || crimeMetrics();
    legend.innerHTML = `
      <span>Total ${formatNumber(metrics.reported_crimes_total)}</span>
      <span>Propiedad ${formatNumber(metrics.reported_property_crime_count)}</span>
      <span>Homicidios ${formatNumber(metrics.reported_homicide_count)}</span>
      <span class="crime-dot"></span>
      <span>Centroide SAT-HD aproximado</span>
    `;
  }

  function initCrimeMap() {
    const container = document.getElementById("crime-context-map");
    if (!container || typeof maplibregl === "undefined") return;
    loadCrimeLayers()
      .then((payload) => {
        populateCrimeMapYearFilter(payload);
        renderCrimeMapLegend(payload);
        const zones = payload.zones || { type: "FeatureCollection", features: [] };
        if (!payload.configured || !zones.features?.length) {
          container.innerHTML = '<div class="audit-note">No hay capa de crimen cargada.</div>';
          return;
        }
        if (crimeMap && crimeMap._loaded) {
          crimeMap.resize();
          updateCrimeMapSource(payload);
          return;
        }
        if (crimeMap) {
          crimeMap.once("load", () => updateCrimeMapSource(payload));
          return;
        }
        fetch("/api/configuracion-mapa/").then((response) => response.json()).then((config) => {
          crimeMap = new maplibregl.Map({
            container: "crime-context-map",
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
          crimeMap.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
          crimeMap.once("load", () => {
            upsertGeoJsonSource(crimeMap, "crime-zones", zones);
            upsertGeoJsonSource(crimeMap, "crime-homicide-points", filteredCrimePoints(payload));
            addLayerIfMissing(crimeMap, {
              id: "crime-zone-fill",
              type: "fill",
              source: "crime-zones",
              paint: {
                "fill-color": "#eef3ee",
                "fill-opacity": 0.42
              }
            });
            addLayerIfMissing(crimeMap, {
              id: "crime-zone-line",
              type: "line",
              source: "crime-zones",
              paint: {
                "line-color": "#5d6f66",
                "line-width": 1.1,
                "line-opacity": 0.8
              }
            });
            addLayerIfMissing(crimeMap, {
              id: "crime-homicide-points",
              type: "circle",
              source: "crime-homicide-points",
              paint: {
                "circle-radius": ["interpolate", ["linear"], ["get", "victims_count"], 1, 5, 4, 9],
                "circle-color": [
                  "match",
                  ["get", "clase_arma"],
                  "Arma de fuego", "#8f2d36",
                  "Arma blanca", "#b05a2b",
                  "Objeto contundente", "#6f5d8f",
                  "#2f4056"
                ],
                "circle-opacity": 0.86,
                "circle-stroke-color": "#ffffff",
                "circle-stroke-width": 1.2
              }
            });
            const bounds = securityMapBounds(zones);
            if (bounds) crimeMap.fitBounds(bounds, { padding: 30, duration: 0 });
            if (!crimeMap._radarHandlersBound) {
              crimeMap._radarHandlersBound = true;
              crimeMap.on("click", "crime-zone-fill", (event) => {
                const feature = event.features?.[0];
                if (!feature) return;
                const props = feature.properties || {};
                if (crimeMapPopup) crimeMapPopup.remove();
                crimeMapPopup = new maplibregl.Popup({ offset: 10 })
                  .setLngLat(event.lngLat)
                  .setHTML(`
                    <div class="map-popup">
                      <strong>${escapeHtml(props.label || "Zona")}</strong>
                      <p>Total municipal: ${formatNumber(props.reported_crimes_total)}</p>
                      <p>Propiedad: ${formatNumber(props.reported_property_crime_count)} · Homicidios: ${formatNumber(props.reported_homicide_count)}</p>
                      <small>${escapeHtml(props.crime_data_scope || "municipio")} · precision ${escapeHtml(props.crime_spatial_precision || "low")}</small>
                    </div>
                  `)
                  .addTo(crimeMap);
              });
              crimeMap.on("click", "crime-homicide-points", (event) => {
                const feature = event.features?.[0];
                if (!feature) return;
                const props = feature.properties || {};
                if (crimeMapPopup) crimeMapPopup.remove();
                crimeMapPopup = new maplibregl.Popup({ offset: 10 })
                  .setLngLat(feature.geometry.coordinates)
                  .setHTML(`
                    <div class="map-popup">
                      <strong>SAT-HD ${escapeHtml(props.period_year || "")}</strong>
                      <p>Victimas: ${formatNumber(props.victims_count)} · Zona: ${escapeHtml(props.assigned_zone_name || "")}</p>
                      <p>${escapeHtml(props.tipo_lugar || "")} · ${escapeHtml(props.clase_arma || "")}</p>
                      <small>Centroide de radio censal; ubicacion exacta: no.</small>
                    </div>
                  `)
                  .addTo(crimeMap);
              });
              ["crime-zone-fill", "crime-homicide-points"].forEach((layerId) => {
                crimeMap.on("mouseenter", layerId, () => { crimeMap.getCanvas().style.cursor = "pointer"; });
                crimeMap.on("mouseleave", layerId, () => { crimeMap.getCanvas().style.cursor = ""; });
              });
            }
            crimeMap.resize();
          });
        }).catch(() => {
          container.innerHTML = '<div class="audit-note">No se pudo cargar la configuracion del mapa.</div>';
        });
      })
      .catch(() => {
        container.innerHTML = '<div class="audit-note">No se pudo cargar la capa de crimen.</div>';
      });
  }

  function renderCrimeAsyncPanels() {
    loadCrimeLayers()
      .then((payload) => {
        if (!crimeChartsRendered || document.getElementById("crime-monthly-measure")?.dataset.bound !== "1") {
          renderCrimeCharts(payload);
        }
      })
      .catch(() => {
        ["crime-monthly-chart", "crime-property-chart"].forEach((id) => {
          const canvas = document.getElementById(id);
          if (canvas && !canvas.parentElement.querySelector(".chart-empty-note")) {
            canvas.insertAdjacentHTML("afterend", '<p class="audit-note chart-empty-note">No se pudo cargar la serie de crimen.</p>');
          }
        });
        const seasonality = document.getElementById("crime-seasonality-panel");
        if (seasonality) {
          seasonality.innerHTML = '<div class="audit-note">No se pudo cargar la estacionalidad.</div>';
        }
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
              ${Number.isFinite(Number(props.location_value_score)) ? `<small>Territorial ${Math.round(Number(props.location_value_score))}/100 · ${escapeHtml(props.location_value_level || "")}</small><br>` : ""}
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
          location_value_score: Number(item.location_value_score),
          location_value_level: item.location_value_level || "",
          location_value_zone: item.location_value_zone || "",
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
    if (metric === "location_value") return "Score territorial";
    if (metric === "discount") return "Descuento vs tendencia";
    if (metric === "density") return "Densidad";
    return "Precio total";
  }

  function metricValue(item, metric, surfaceRows) {
    if (metric === "price_m2") return Number(item.price_m2) || 0;
    if (metric === "location_value") return Number(item.location_value_score) || 0;
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
      if (metric === "location_value") return `${Math.round(value || 0)}/100`;
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

  function opportunityRows(values) {
    return values
      .filter((item) =>
        Number.isFinite(Number(item.discount))
        && Number(item.discount) > 3
        && Number(item.comparable_count || 0) >= 5
      )
      .map((item) => {
        const discount = Number(item.discount);
        const priceM2Bonus = Number.isFinite(Number(item.price_m2)) ? Math.max(0, 1100 - Number(item.price_m2)) / 35 : 0;
        const locationBonus = ["high", "medium"].includes(item.location_confidence) ? 8 : 0;
        const qualityBonus = Number(item.quality_score || 0) / 12;
        const coverage = Number(item.security_coverage_score);
        const risk = Number(item.security_risk_score);
        const territory = Number(item.location_value_score);
        const flood = Number(item.location_flood_penalty_score);
        const securityBonus = Number.isFinite(coverage) && coverage >= 60 ? 8 : 0;
        const negotiationBonus = Number.isFinite(risk) && risk >= 55 ? 4 : 0;
        const territoryBonus = Number.isFinite(territory) && territory >= 65 ? 10 : 0;
        const floodPenalty = Number.isFinite(flood) && flood >= 55 ? -8 : 0;
        const securityTag = Number.isFinite(coverage) && coverage >= 60
          ? "Oportunidad segura"
          : (Number.isFinite(risk) && risk >= 55 ? "Negociable por riesgo" : "Oportunidad");
        const territoryTag = Number.isFinite(territory) && territory >= 65
          ? "Buen contexto territorial"
          : "";
        return {
          ...item,
          discount,
          securityTag,
          territoryTag,
          opportunity_score: discount + priceM2Bonus + locationBonus + qualityBonus + securityBonus + negotiationBonus + territoryBonus + floodPenalty
        };
      })
      .sort((a, b) => b.opportunity_score - a.opportunity_score)
      .slice(0, 12);
  }

  function renderOpportunityPanel() {
    const container = document.getElementById("opportunity-list");
    if (!container) return;
    const rows = opportunityRows(data.surface_price || []);
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
            <span>${escapeHtml(item.comparable_group || "Comparables")}</span>
            <span>${Math.round(Number(item.comparable_count) || 0)} comps</span>
            <span>${item.price_m2 ? `${Math.round(item.price_m2).toLocaleString("es-AR")} /m2` : "Sin m2"}</span>
            <span>${escapeHtml(item.securityTag)}</span>
            ${item.territoryTag ? `<span>${escapeHtml(item.territoryTag)}</span>` : ""}
            ${Number.isFinite(Number(item.location_value_score)) ? `<span>Terr. ${Math.round(Number(item.location_value_score))}/100</span>` : ""}
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

  function createLiquidityChart(canvasId = "liquidity-chart") {
    const ctx = document.getElementById(canvasId);
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
    if (canvasId === "liquidity-chart") {
      register(canvasId, "Liquidez del inventario", (targetCanvasId) => createLiquidityChart(targetCanvasId));
    }
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
          No hay capa fina cargada. Agregá polígonos o puntos a <strong>data/geo/security/security_zones_hurlingham.geojson</strong> para cruzar seguridad con precio.
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

  function renderLocationValuePanel() {
    const container = document.getElementById("location-value-panel");
    const payload = data.location_intelligence || {};
    if (!container) return;
    const rows = Array.isArray(payload.rows) ? payload.rows.slice(0, 10) : [];
    if (!rows.length) {
      container.innerHTML = '<div class="audit-note">Todavía no hay propiedades con score territorial para estos filtros.</div>';
      return;
    }
    container.innerHTML = rows.map((item) => `
      <div class="security-row">
        <div>
          <strong>${escapeHtml(formatPrice(item))}</strong>
          <span>${escapeHtml(item.zone || item.address || "Sin zona")}</span>
          <small>Territorial ${formatScoreCell(item.location_value_score)} · ${escapeHtml(item.location_value_level || "-")}</small>
          <small>Transporte ${formatScoreCell(item.location_transport_score)} · Flood ${formatScoreCell(item.location_flood_penalty_score)}</small>
        </div>
        <button class="text-button property-preview-trigger" type="button" data-property-id="${item.id}">Abrir</button>
      </div>
    `).join("");
  }

  function renderLocationOpportunities() {
    const container = document.getElementById("location-opportunity-panel");
    const rows = Array.isArray(data.location_intelligence?.opportunities)
      ? data.location_intelligence.opportunities
      : [];
    if (!container) return;
    if (!rows.length) {
      container.innerHTML = '<div class="audit-note">No hay señales territoriales fuertes con estos filtros.</div>';
      return;
    }
    container.innerHTML = `
      <table>
        <thead>
          <tr>
            <th>Señal</th>
            <th>Propiedad</th>
            <th>Zona</th>
            <th>Precio/m2</th>
            <th>Score</th>
            <th>Transporte</th>
            <th>Flood</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((row) => `
            <tr>
              <td><span class="security-badge ${row.kind === "Sobreprecio con riesgo" ? "risk" : ""}">${escapeHtml(row.kind)}</span></td>
              <td>${escapeHtml(row.title || row.address || `#${row.id}`)}</td>
              <td>${escapeHtml(row.location_value_zone || row.zone || "-")}</td>
              <td>${row.price_m2 ? Math.round(row.price_m2).toLocaleString("es-AR") : "-"}</td>
              <td>${formatScoreCell(row.location_value_score || row.territorial_score)}</td>
              <td>${formatScoreCell(row.location_transport_score || row.transport_score)}</td>
              <td>${formatScoreCell(row.location_flood_penalty_score || row.flood_penalty_score)}</td>
              <td><button class="text-button property-preview-trigger" type="button" data-property-id="${row.id}">Abrir</button></td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
  }

  function renderLocationZoneMatrix() {
    const container = document.getElementById("location-zone-matrix-panel");
    const rows = Array.isArray(data.location_intelligence?.zones)
      ? data.location_intelligence.zones
      : [];
    if (!container) return;
    if (!rows.length) {
      container.innerHTML = '<div class="audit-note">No hay matriz territorial disponible para estos filtros.</div>';
      return;
    }
    container.innerHTML = `
      <table>
        <thead>
          <tr>
            <th>Zona</th>
            <th>Propiedades</th>
            <th>Score prom.</th>
            <th>Mediana USD/m2</th>
            <th>Transporte</th>
            <th>Flood</th>
            <th>RENABAP/contexto</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((row) => `
            <tr>
              <td>${escapeHtml(row.zone || "-")}</td>
              <td>${row.property_count || 0}</td>
              <td>${formatScoreCell(row.avg_score)}</td>
              <td>${formatNumber(row.median_price_m2)}</td>
              <td>${formatScoreCell(row.avg_transport_score)}</td>
              <td>${formatScoreCell(row.avg_flood_penalty)}</td>
              <td>${formatScoreCell(row.avg_urban_informality)}</td>
              <td><a href="${escapeHtml(formatListUrl(row.url) || row.url || "#")}">Ver zona</a></td>
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
    const locationValuePrice = scatter("location-value-price-chart", "Precio/m2 vs score territorial", data.location_intelligence?.value_price || [], "Score territorial", { yTitle: "Precio/m2" });
    const securityRisk = scatter("security-risk-price-chart", "Precio/m2 vs riesgo", data.security?.risk_price || [], "Riesgo relativo", { yTitle: "Precio/m2" });
    const volatility = zoneVolatility("zone-volatility-chart", "Precio medio por zona (y desvío)", data.zone_price_volatility);
    const boxplot = zoneBoxplot("zone-boxplot-chart", "Diagrama de caja por zona", data.zone_price_volatility);
    const liquidity = createLiquidityChart();
    renderOpportunityPanel();
    renderZoneTypeMatrix();
    renderSecurityPanel();
    renderSecurityArbitrage();
    renderLocationValuePanel();
    renderLocationOpportunities();
    renderLocationZoneMatrix();
    renderCrimeKpis();
    renderCrimeZoneInsights();
    renderCrimeAsyncPanels();
    [locality, neighborhood, agency, price, surface, bedrooms, bedroomsMl, locationValuePrice, securityRisk, volatility, boxplot, liquidity].forEach((chart) => {
      if (!chart) return;
      const canvas = chart.canvas;
      if (!canvas) return;
      canvas.chart = chart;
    });
  }

  function renderDashboardDeferred() {
    if (dashboardRenderPromise) return dashboardRenderPromise;
    dashboardRenderPromise = loadDashboardData().then(() => {
      if (dashboardPayloadRendered) return data;
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
        renderCrimeKpis();
        renderCrimeZoneInsights();
        renderCrimeAsyncPanels();
      }
      return data;
    });
    return dashboardRenderPromise;
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

  const modal = document.getElementById("chart-modal");
  const close = document.getElementById("chart-modal-close");
  const modalCanvas = document.getElementById("chart-modal-canvas");
  const modalContent = document.getElementById("chart-modal-content");
  let modalChart = null;
  const clearModalContent = () => {
    if (modalChart) modalChart.destroy();
    modalChart = null;
    if (modalContent) {
      modalContent.replaceChildren();
      modalContent.hidden = true;
    }
    if (modalCanvas) modalCanvas.hidden = false;
  };
  document.querySelectorAll(".chart-expand").forEach((button) => {
    button.addEventListener("click", async () => {
      const panel = button.closest(".chart-panel");
      const canvas = panel?.querySelector("canvas");
      const mapTarget = panel?.querySelector(".stats-heatmap-map");
      const htmlTarget = panel?.querySelector(".crime-seasonality-panel");
      if (mapTarget) {
        try {
          await renderDashboardDeferred();
          await (panel.requestFullscreen ? panel.requestFullscreen() : mapTarget.requestFullscreen?.());
          setTimeout(() => {
            if (mapTarget.id === "price-heatmap-map") initPriceHeatmap();
            if (mapTarget.id === "location-value-map") initLocationValueMap();
            if (mapTarget.id === "security-coverage-map" || mapTarget.id === "security-risk-map") initSecurityMaps();
            if (mapTarget.id === "crime-context-map") initCrimeMap();
          }, 180);
        } catch (_error) {}
        return;
      }
      if (canvas && !charts.has(canvas.id)) {
        try {
          await renderDashboardDeferred();
        } catch (_error) {
          return;
        }
      }
      if (!canvas && htmlTarget) {
        try {
          await renderDashboardDeferred();
        } catch (_error) {
          return;
        }
        if (!modal || !modalContent || !modalCanvas) return;
        document.getElementById("chart-modal-title").textContent = panel?.querySelector("h2")?.textContent || "";
        clearModalContent();
        modalCanvas.hidden = true;
        modalContent.hidden = false;
        modalContent.appendChild(htmlTarget.cloneNode(true));
        modal.showModal();
        return;
      }
      const config = canvas ? charts.get(canvas.id) : null;
      if (!config || !modal) return;
      document.getElementById("chart-modal-title").textContent = panel?.querySelector("h2")?.textContent || config.title || "";
      clearModalContent();
      modal.showModal();
      modalChart = config.build("chart-modal-canvas");
    });
  });
  if (close) close.addEventListener("click", () => modal.close());
  if (modal) {
    modal.addEventListener("cancel", () => {
      clearModalContent();
    });
    modal.addEventListener("close", () => {
      clearModalContent();
    });
  }

  if (localStorage.getItem(mapStorageKey) === "1") {
    setTimeout(() => {
      const isSpatial = document.querySelector('.stats-tab[data-tab="spatial"].active');
      if (isSpatial) {
        renderDashboardDeferred().then(() => {
          initPriceHeatmap();
          initLocationValueMap();
          initSecurityMaps();
        }).catch(() => {});
      }
    }, 0);
  }
  lucide.createIcons();
})();

