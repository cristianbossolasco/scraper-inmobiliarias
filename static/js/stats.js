(() => {
  if (typeof Chart === "undefined") return;
  const data = JSON.parse(document.getElementById("chart-data").textContent);
  const colors = ["#176b4d", "#d6a528", "#d95d45", "#386f8f", "#6f5d8f", "#4f7c67"];
  const statusColors = {
    pending: "#176b4d",
    reviewed: "#386f8f",
    favorite: "#d6a528"
  };
  const charts = new Map();
  const filterToggle = document.getElementById("stats-filter-toggle");
  const filterForm = document.getElementById("stats-filter-form");

  if (filterToggle && filterForm) {
    filterToggle.addEventListener("click", () => {
      const isHidden = filterForm.hidden;
      filterForm.hidden = !isHidden;
      filterToggle.setAttribute("aria-expanded", String(isHidden));
    });
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

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;"
    })[char]);
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
    const isPoint = item.id;
    if (isPoint) {
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
          <strong>${escapeHtml(item.label)}</strong>
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
    if (item?.url) window.location.href = item.url;
  }

  function bar(id, title, items) {
    const ctx = document.getElementById(id);
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
        plugins: {
          legend: { display: true },
          tooltip: { enabled: false, external: externalPreview }
        },
        scales: { x: { stacked: true }, y: { stacked: true } },
        onClick: (event, _elements, chartInstance) => navigateFromChart(chartInstance, event)
      }
    });
    charts.set(id, { type: "bar", title, items: parsed });
    return chart;
  }

  function scatter(id, label, values, xTitle) {
    const ctx = document.getElementById(id);
    if (!ctx || !values.length) return null;
    const groups = {
      pending: values.filter((item) => statusOf(item) === "pending"),
      reviewed: values.filter((item) => statusOf(item) === "reviewed"),
      favorite: values.filter((item) => statusOf(item) === "favorite")
    };
    const chart = new Chart(ctx, {
      type: "scatter",
      data: {
        datasets: [
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
        ]
      },
      options: {
        responsive: true,
        onClick: (event, _elements, chartInstance) => navigateFromChart(chartInstance, event),
        plugins: {
          tooltip: { enabled: false, external: externalPreview }
        },
        scales: {
          x: { title: { display: true, text: xTitle } },
          y: { title: { display: true, text: "Precio" } }
        }
      }
    });
    charts.set(id, { type: "scatter", title: label, items: values, xTitle });
    return chart;
  }

  bar("locality-chart", "Publicaciones", data.by_locality);
  bar("neighborhood-chart", "Publicaciones", data.by_neighborhood);
  bar("agency-chart", "Publicaciones", data.by_agency);
  bar("price-chart", "Cantidad", data.price_buckets);
  scatter("surface-price-chart", "Superficie vs precio", data.surface_price, "Superficie");
  scatter("bedrooms-price-chart", "Habitaciones vs precio", data.bedrooms_price, "Habitaciones");

  const modal = document.getElementById("chart-modal");
  const close = document.getElementById("chart-modal-close");
  let modalChart = null;
  document.querySelectorAll(".chart-expand").forEach((button) => {
    button.addEventListener("click", () => {
      const panel = button.closest(".chart-panel");
      const canvas = panel.querySelector("canvas");
      const config = charts.get(canvas.id);
      if (!config || !modal) return;
      document.getElementById("chart-modal-title").textContent = panel.querySelector("h2").textContent;
      if (modalChart) modalChart.destroy();
      modal.showModal();
      if (config.type === "bar") {
        modalChart = bar("chart-modal-canvas", config.title, config.items);
      } else {
        modalChart = scatter("chart-modal-canvas", config.title, config.items, config.xTitle);
      }
    });
  });
  if (close) close.addEventListener("click", () => modal.close());
  lucide.createIcons();
})();
