(() => {
  lucide.createIcons();

  const operationJobs = new Map();
  const scrapeJobs = new Map();
  const timers = new Map();
  const legacyTimers = new Map();
  const jobsList = document.getElementById("jobs-list");
  const initialOperations = readJsonScript("initial-operation-jobs", []);
  const initialScrapes = readJsonScript("initial-scrape-jobs", []);

  initialOperations.sort(compareJobs).forEach((job) => renderOperationJob(job));
  initialOperations.filter(isActive).forEach(scheduleOperationPoll);
  initialScrapes.sort(compareJobs).forEach((job) => renderLegacyScrapeJob(job));
  initialScrapes.filter(isActive).forEach(scheduleLegacyPoll);
  sortJobCards();
  updateActionButtons();

  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
  });

  document.getElementById("select-all-sources").addEventListener("click", () => {
    document.querySelectorAll("#source-picker input[type=checkbox]").forEach((input) => {
      input.checked = true;
    });
  });

  document.getElementById("clear-all-sources").addEventListener("click", () => {
    document.querySelectorAll("#source-picker input[type=checkbox]").forEach((input) => {
      input.checked = false;
    });
  });

  document.getElementById("apply-bulk-workers").addEventListener("click", () => {
    const value = Math.max(1, Number(document.getElementById("bulk-workers").value || 1));
    document.querySelectorAll("[data-workers-for]").forEach((input) => {
      input.value = value;
      input.dispatchEvent(new Event("input"));
    });
  });

  document.getElementById("safe-preset").addEventListener("click", () => {
    document.getElementById("scrape-mode").value = "complete";
    document.getElementById("max-pages").value = "";
    document.getElementById("start-page").value = "";
    document.getElementById("max-listings").value = "3";
    document.getElementById("request-timeout").value = "25";
    document.getElementById("max-errors").value = "3";
    document.getElementById("geocode-limit").value = "0";
    document.getElementById("mark-missing").checked = false;
    document.querySelectorAll("[data-workers-for]").forEach((input) => {
      input.value = "1";
      input.dispatchEvent(new Event("input"));
    });
  });

  document.querySelectorAll("[data-workers-for]").forEach((input) => {
    input.addEventListener("input", () => {
      const row = input.closest("[data-source-row]");
      const warning = row.querySelector("[data-worker-warning]");
      const workers = Number(input.value || 1);
      const slug = row.dataset.slug;
      warning.textContent = slug === "argenprop"
        ? "Argenprop: usar 1 worker. Mas workers aumentan mucho el riesgo de bloqueo."
        : "Mas de 5 puede saturar la fuente o SQLite.";
      warning.hidden = slug === "argenprop" ? workers <= 1 : workers <= 5;
    });
  });

  document.getElementById("start-scraping").addEventListener("click", () => {
    const selected = [...document.querySelectorAll("#source-picker input[type=checkbox]:checked")];
    if (!selected.length) {
      alert("Seleccione al menos una fuente.");
      return;
    }
    const sources = selected.map((item) => item.value);
    const workers = {};
    selected.forEach((item) => {
      const input = document.querySelector(`[data-workers-for="${cssEscape(item.value)}"]`);
      workers[item.value] = Math.max(1, Number(input.value || 1));
    });
    createOperationJob({
      title: "Scrape liviano",
      kind: "scrape",
      mode: "apply",
      steps: [{
        kind: "scrape",
        mode: "apply",
        params: {
          sources,
          workers,
          scrape_mode: valueOf("scrape-mode"),
          max_pages: optionalInt(valueOf("max-pages")),
          start_page: optionalInt(valueOf("start-page")),
          max_listings: optionalInt(valueOf("max-listings")),
          geocode_limit: optionalInt(valueOf("geocode-limit")) ?? 0,
          request_timeout_seconds: optionalInt(valueOf("request-timeout")),
          max_errors_per_source: optionalInt(valueOf("max-errors")),
          mark_missing: checked("mark-missing"),
        },
      }],
    });
  });

  document.getElementById("run-location-preset").addEventListener("click", () => {
    createOperationJob({
      title: "Completar ubicacion",
      kind: "pipeline",
      mode: "apply",
      steps: [
        {kind: "geocode", mode: "apply", params: enrichmentParams()},
        {kind: "infer_zones", mode: "apply", params: zoneParams()},
        {kind: "score_security", mode: "apply", params: securityParams()},
      ],
    });
  });

  document.querySelectorAll("[data-enrich-step]").forEach((button) => {
    button.addEventListener("click", () => {
      const kind = button.dataset.enrichStep;
      const params = kind === "geocode"
        ? enrichmentParams()
        : kind === "infer_zones"
          ? zoneParams()
          : securityParams();
      createOperationJob({
        title: button.textContent.trim(),
        kind,
        mode: "apply",
        steps: [{kind, mode: "apply", params}],
      });
    });
  });

  document.getElementById("run-repair-dry-run").addEventListener("click", () => {
    const kind = valueOf("repair-kind");
    createOperationJob({
      title: `Simular ${repairLabel(kind)}`,
      kind,
      mode: "dry_run",
      steps: [{kind, mode: "dry_run", params: repairParams()}],
    });
  });

  document.getElementById("run-quality-audit").addEventListener("click", () => {
    const params = repairParams();
    const steps = [
      {kind: "repair_addresses", mode: "dry_run", params},
      {kind: "repair_neighborhoods", mode: "dry_run", params},
      {kind: "repair_localities", mode: "dry_run", params},
      {kind: "repair_agencies", mode: "dry_run", params: {}},
    ];
    if (params.sources && params.sources.length) {
      steps.push({kind: "repair_metrics", mode: "dry_run", params});
      steps.push({kind: "repair_merged_listings", mode: "dry_run", params});
    }
    createOperationJob({
      title: "Auditar calidad",
      kind: "pipeline",
      mode: "dry_run",
      steps,
    });
  });

  document.getElementById("run-merge-dry-run").addEventListener("click", () => {
    createOperationJob({
      title: "Simular merge",
      kind: "merge_properties",
      mode: "dry_run",
      steps: [{kind: "merge_properties", mode: "dry_run", params: mergeParams()}],
    });
  });

  jobsList.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-operation-action], [data-legacy-action]");
    if (!button) return;
    button.disabled = true;
    if (button.dataset.operationAction) {
      await runOperationAction(button.dataset.operationAction, button.dataset.jobId);
    } else {
      await runLegacyAction(button.dataset.legacyAction, button.dataset.jobId);
    }
  });

  async function createOperationJob(payload) {
    const active = activeOperationJob();
    if (active) {
      renderOperationJob(active, true);
      activateTab("history");
      alert(`Ya hay una operacion en curso: Job #${active.id}.`);
      return;
    }
    try {
      const response = await fetch("/api/operations/jobs/", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-CSRFToken": csrf()},
        body: JSON.stringify(payload),
      });
      const data = await readJson(response);
      if (response.status === 409) {
        renderOperationJob(data, true);
        scheduleOperationPoll(data);
        activateTab("history");
        alert(data.error || `Ya hay una operacion en curso: Job #${data.id}.`);
        return;
      }
      if (!response.ok) throw new Error(data.error || "No se pudo iniciar la operacion.");
      renderOperationJob(data, true);
      scheduleOperationPoll(data);
      activateTab("history");
    } catch (error) {
      alert(error.message);
    }
  }

  async function runOperationAction(action, id) {
    const endpoints = {
      cancel: `/api/operations/jobs/${id}/cancel/`,
      retry: `/api/operations/jobs/${id}/retry/`,
      apply: `/api/operations/jobs/${id}/apply-from-dry-run/`,
    };
    const response = await fetch(endpoints[action], {
      method: "POST",
      headers: {"X-CSRFToken": csrf()},
    });
    const data = await readJson(response);
    if (!response.ok) {
      if (response.status === 409) {
        renderOperationJob(data, true);
        scheduleOperationPoll(data);
      }
      alert(data.error || "No se pudo ejecutar la accion.");
      return;
    }
    renderOperationJob(data, action !== "cancel");
    if (action !== "cancel") scheduleOperationPoll(data);
  }

  async function runLegacyAction(action, id) {
    const endpoints = {
      cancel: `/api/scraping/jobs/${id}/cancel/`,
      retry: `/api/scraping/jobs/${id}/retry/`,
      "retry-errors": `/api/scraping/jobs/${id}/retry-errors/`,
    };
    const response = await fetch(endpoints[action], {
      method: "POST",
      headers: {"X-CSRFToken": csrf()},
    });
    const data = await readJson(response);
    if (!response.ok) {
      alert(data.error || "No se pudo ejecutar la accion.");
      return;
    }
    renderLegacyScrapeJob(data, action !== "cancel");
    if (action !== "cancel") scheduleLegacyPoll(data);
  }

  function renderOperationJob(job, prepend = false) {
    operationJobs.set(job.id, job);
    let node = document.getElementById(`operation-job-${job.id}`);
    if (!node) {
      node = document.createElement("article");
      node.className = "job-card operation-job-card";
      node.id = `operation-job-${job.id}`;
      node.dataset.createdAt = job.created_at || "";
      node.dataset.active = isActive(job) ? "1" : "0";
      if (prepend) jobsList.prepend(node);
      else jobsList.append(node);
    }
    node.dataset.active = isActive(job) ? "1" : "0";
    const created = job.created_at ? new Date(job.created_at).toLocaleString() : "";
    const progress = job.total_steps ? `${job.completed_steps}/${job.total_steps} steps` : "";
    node.innerHTML = `
      <div class="job-heading">
        <div>
          <strong>${escapeHtml(job.title || job.kind_label || `Operacion #${job.id}`)}</strong>
          <span class="status-pill ${job.status}">${escapeHtml(job.status_label)}</span>
          <span class="status-pill neutral">${escapeHtml(job.mode_label)}</span>
          <small>Job #${job.id} · ${created}</small>
          <small>${progress} · ${job.processed} procesadas · ${job.changed} cambios · ${job.errors} errores · ${formatDuration(job.elapsed_seconds)}</small>
        </div>
        <div class="job-actions">
          ${isActive(job) ? actionButton("cancel", job.id, "square", job.cancel_requested ? "Cancelando..." : "Cancelar", job.cancel_requested) : ""}
          ${job.can_apply ? actionButton("apply", job.id, "check-check", "Aplicar dry-run") : ""}
          ${canRetry(job) ? actionButton("retry", job.id, "rotate-cw", "Repetir") : ""}
        </div>
      </div>
      ${job.cancel_requested && isActive(job) ? `<div class="job-meta cancelling-meta">Cancelacion solicitada; esperando tareas en curso...</div>` : ""}
      <div class="job-source-list">
        ${(job.steps || []).map(renderOperationStep).join("")}
      </div>
      ${job.logs ? `<pre class="job-log">${escapeHtml(job.logs)}</pre>` : ""}
    `;
    lucide.createIcons();
    sortJobCards();
    updateActionButtons();
    if (!isActive(job) && timers.has(job.id)) {
      clearInterval(timers.get(job.id));
      timers.delete(job.id);
    }
  }

  function renderOperationStep(step) {
    const percent = step.total ? Math.min(100, Math.round((step.processed / step.total) * 100)) : (isActive(step) ? 12 : 100);
    const summary = renderStepSummary(step);
    return `
      <div class="job-source operation-step">
        <div class="job-source-top">
          <div>
            <strong>${escapeHtml(step.kind_label)}</strong>
            <span class="status-pill ${step.status}">${escapeHtml(step.status_label)}</span>
            <span class="status-pill neutral">${escapeHtml(step.mode_label)}</span>
          </div>
          <small>${formatDuration(step.elapsed_seconds)}</small>
        </div>
        <div class="progress-bar" aria-label="Progreso ${escapeHtml(step.kind_label)}">
          <span style="width:${percent}%"></span>
        </div>
        <div class="job-meta">${step.processed}/${step.total || 0} procesadas · ${step.changed} cambios · ${step.skipped} omitidas · ${step.errors} errores</div>
        ${summary}
        ${step.error_log ? `<pre class="job-log error-log">${escapeHtml(step.error_log)}</pre>` : ""}
        ${step.logs ? `<pre class="job-log">${escapeHtml(step.logs)}</pre>` : ""}
      </div>
    `;
  }

  function renderStepSummary(step) {
    const summary = step.result_summary || {};
    if (summary.scrape_job_id) {
      return `<div class="job-meta">ScrapeJob #${summary.scrape_job_id}${summary.scrape_status ? ` · estado ${escapeHtml(summary.scrape_status)}` : ""}</div>`;
    }
    if (summary.output_tail) {
      return `<details class="source-errors"><summary>Salida del comando</summary><pre class="job-log">${escapeHtml(summary.output_tail)}</pre></details>`;
    }
    const keys = Object.keys(summary).filter((key) => !["elapsed_seconds"].includes(key));
    if (!keys.length) return "";
    return `<div class="job-meta">${keys.map((key) => `${escapeHtml(key)}=${escapeHtml(summary[key])}`).join(" · ")}</div>`;
  }

  function renderLegacyScrapeJob(job, prepend = false) {
    scrapeJobs.set(job.id, job);
    let node = document.getElementById(`legacy-scrape-job-${job.id}`);
    if (!node) {
      node = document.createElement("article");
      node.className = "job-card legacy-job-card";
      node.id = `legacy-scrape-job-${job.id}`;
      node.dataset.createdAt = job.created_at || "";
      node.dataset.active = isActive(job) ? "1" : "0";
      if (prepend) jobsList.prepend(node);
      else jobsList.append(node);
    }
    node.dataset.active = isActive(job) ? "1" : "0";
    const errorCount = totalErrorUrls(job);
    node.innerHTML = `
      <div class="job-heading">
        <div>
          <strong>Scrape legacy #${job.id}</strong>
          <span class="status-pill ${job.status}">${escapeHtml(job.status_label)}</span>
          <span class="status-pill neutral">${escapeHtml(job.scrape_mode_label)}</span>
          <small>${job.created_at ? new Date(job.created_at).toLocaleString() : ""}</small>
          <small>${job.mark_missing ? "marca ausentes" : "liviano"} · geocode ${job.geocode_limit ?? 0} · ${formatDuration(job.elapsed_seconds)}</small>
        </div>
        <div class="job-actions">
          ${isActive(job) ? legacyActionButton("cancel", job.id, "square", job.cancel_requested ? "Cancelando..." : "Cancelar", job.cancel_requested) : ""}
          ${!isActive(job) && errorCount ? legacyActionButton("retry-errors", job.id, "list-restart", `Reprocesar ${errorCount} errores`) : ""}
          ${canRetry(job) ? legacyActionButton("retry", job.id, "rotate-cw", "Repetir") : ""}
        </div>
      </div>
      <div class="job-source-list">
        ${(job.sources || []).map(renderLegacySource).join("")}
      </div>
      ${job.error_log ? `<pre class="job-log">${escapeHtml(job.error_log)}</pre>` : ""}
    `;
    lucide.createIcons();
    sortJobCards();
    if (!isActive(job) && legacyTimers.has(job.id)) {
      clearInterval(legacyTimers.get(job.id));
      legacyTimers.delete(job.id);
    }
  }

  function renderLegacySource(source) {
    const errors = Array.isArray(source.error_urls) ? source.error_urls : [];
    return `
      <div class="job-source">
        <div class="job-source-top">
          <div>
            <strong>${escapeHtml(source.name)}</strong>
            <span class="status-pill ${source.status}">${escapeHtml(source.status_label)}</span>
          </div>
          <small>${source.workers} worker${source.workers === 1 ? "" : "s"} · ${formatDuration(source.elapsed_seconds)}</small>
        </div>
        <div class="progress-bar"><span style="width:${source.percent}%"></span></div>
        <div class="job-meta">${source.processed}/${source.total_to_process || 0} procesadas · ${source.created} nuevas · ${source.updated} actualizadas · ${source.errors} errores</div>
        ${source.geocode_pending ? `<div class="job-meta">Geocodificacion: ${source.geocoded}/${source.geocode_pending} ubicadas · ${source.geocode_failed} sin resultado/error</div>` : ""}
        ${source.current_url ? `<div class="current-url">${escapeHtml(source.current_url)}</div>` : ""}
        ${errors.length ? renderSourceErrors(errors) : ""}
        ${source.logs ? `<pre class="job-log">${escapeHtml(source.logs)}</pre>` : ""}
      </div>
    `;
  }

  function renderSourceErrors(errors) {
    return `
      <details class="source-errors" open>
        <summary>${errors.length} URL${errors.length === 1 ? "" : "s"} con error</summary>
        <div class="source-error-list">
          ${errors.map((item) => `
            <div class="source-error-item">
              <a href="${escapeAttribute(item.url || "")}" target="_blank" rel="noopener">${escapeHtml(item.url || "")}</a>
              <span>${escapeHtml(item.error || "Error sin detalle")}</span>
            </div>
          `).join("")}
        </div>
      </details>
    `;
  }

  function actionButton(action, id, icon, label, disabled = false) {
    return `<button class="secondary-button" type="button" data-operation-action="${action}" data-job-id="${id}" ${disabled ? "disabled" : ""}><i data-lucide="${icon}"></i> ${label}</button>`;
  }

  function legacyActionButton(action, id, icon, label, disabled = false) {
    return `<button class="secondary-button" type="button" data-legacy-action="${action}" data-job-id="${id}" ${disabled ? "disabled" : ""}><i data-lucide="${icon}"></i> ${label}</button>`;
  }

  function enrichmentParams() {
    return {
      source: valueOf("enrich-source") || null,
      property_ids: splitList(valueOf("enrich-property-ids")),
      limit: optionalInt(valueOf("enrich-limit")) ?? 50,
      only_with_address: true,
      cache_only: checked("geocode-cache-only"),
      force: checked("geocode-force"),
    };
  }

  function zoneParams() {
    return {
      source: valueOf("enrich-source") || null,
      property_ids: splitList(valueOf("enrich-property-ids")),
      limit: optionalInt(valueOf("enrich-limit")) ?? 50,
      max_distance_m: optionalInt(valueOf("zone-max-distance")),
      geocode_missing: checked("zone-geocode-missing"),
    };
  }

  function securityParams() {
    return {
      source: valueOf("enrich-source") || null,
      property_ids: splitList(valueOf("enrich-property-ids")),
      limit: optionalInt(valueOf("enrich-limit")) ?? 50,
      only_missing: checked("security-only-missing"),
    };
  }

  function repairParams() {
    return {
      sources: splitList(valueOf("repair-sources")),
      property_ids: splitList(valueOf("repair-property-ids")),
      max_listings: optionalInt(valueOf("repair-max-listings")),
      max_properties: optionalInt(valueOf("repair-max-properties")),
      timeout: optionalInt(valueOf("repair-timeout")) ?? 20,
      classify_only: checked("repair-classify-only"),
      mark_non_sale: checked("repair-mark-non-sale"),
      mark_listing_pages: checked("repair-mark-listing-pages"),
    };
  }

  function mergeParams() {
    return {
      pair: splitList(valueOf("merge-pairs")),
      component: splitList(valueOf("merge-components"), ";"),
      detect_url_tail_sources: splitList(valueOf("merge-url-tail-sources")),
    };
  }

  function repairLabel(kind) {
    return {
      repair_addresses: "direcciones",
      repair_neighborhoods: "barrios",
      repair_localities: "localidades",
      repair_agencies: "agencias",
      repair_metrics: "metricas",
      repair_merged_listings: "fusiones",
    }[kind] || "reparacion";
  }

  function activateTab(name) {
    document.querySelectorAll("[data-tab]").forEach((button) => {
      button.classList.toggle("active", button.dataset.tab === name);
    });
    document.querySelectorAll("[data-tab-panel]").forEach((panel) => {
      panel.classList.toggle("active", panel.dataset.tabPanel === name);
    });
  }

  function scheduleOperationPoll(job) {
    if (timers.has(job.id)) return;
    timers.set(job.id, setInterval(async () => {
      const response = await fetch(`/api/operations/jobs/${job.id}/`);
      if (response.ok) renderOperationJob(await readJson(response));
    }, 1500));
  }

  function scheduleLegacyPoll(job) {
    if (legacyTimers.has(job.id)) return;
    legacyTimers.set(job.id, setInterval(async () => {
      const response = await fetch(`/api/scraping/jobs/${job.id}/`);
      if (response.ok) renderLegacyScrapeJob(await readJson(response));
    }, 1500));
  }

  function updateActionButtons() {
    const active = activeOperationJob();
    document.querySelectorAll(
      "#start-scraping, #run-location-preset, [data-enrich-step], #run-repair-dry-run, #run-quality-audit, #run-merge-dry-run"
    ).forEach((button) => {
      button.disabled = Boolean(active);
      button.title = active ? `Ya hay una operacion en curso: Job #${active.id}` : "";
    });
  }

  function activeOperationJob() {
    return [...operationJobs.values()].find(isActive);
  }

  function isActive(job) {
    return ["pending", "running"].includes(job.status);
  }

  function canRetry(job) {
    return ["partial", "failed", "cancelled", "interrupted"].includes(job.status);
  }

  function compareJobs(left, right) {
    const leftActive = isActive(left) ? 1 : 0;
    const rightActive = isActive(right) ? 1 : 0;
    if (leftActive !== rightActive) return rightActive - leftActive;
    const leftCreated = Date.parse(left.created_at || "") || 0;
    const rightCreated = Date.parse(right.created_at || "") || 0;
    if (leftCreated !== rightCreated) return rightCreated - leftCreated;
    return Number(right.id || 0) - Number(left.id || 0);
  }

  function sortJobCards() {
    [...jobsList.children].sort((left, right) => {
      const activeDelta = Number(right.dataset.active || 0) - Number(left.dataset.active || 0);
      if (activeDelta) return activeDelta;
      return (Date.parse(right.dataset.createdAt || "") || 0) - (Date.parse(left.dataset.createdAt || "") || 0);
    }).forEach((node) => jobsList.append(node));
  }

  function totalErrorUrls(job) {
    return (job.sources || []).reduce((total, source) => {
      return total + (Array.isArray(source.error_urls) ? source.error_urls.length : 0);
    }, 0);
  }

  async function readJson(response) {
    const text = await response.text();
    try {
      return JSON.parse(text);
    } catch (error) {
      if (response.status === 403) {
        return {error: "La sesion no tiene token CSRF. Recarga la pagina e intenta de nuevo."};
      }
      return {error: `Respuesta inesperada del servidor (${response.status}).`};
    }
  }

  function readJsonScript(id, fallback) {
    const node = document.getElementById(id);
    if (!node) return fallback;
    try {
      return JSON.parse(node.textContent);
    } catch (error) {
      return fallback;
    }
  }

  function valueOf(id) {
    return document.getElementById(id).value;
  }

  function checked(id) {
    return document.getElementById(id).checked;
  }

  function optionalInt(value) {
    return value === "" || value === null || value === undefined ? null : Number(value);
  }

  function splitList(value, separator = ",") {
    if (!value) return [];
    return String(value)
      .split(separator)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Number(seconds || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    if (hours) return `${hours}h ${minutes}m ${secs}s`;
    if (minutes) return `${minutes}m ${secs}s`;
    return `${secs}s`;
  }

  function csrf() {
    const item = document.cookie.split(";").map((part) => part.trim()).find((part) => part.startsWith("csrftoken="));
    return item ? decodeURIComponent(item.split("=")[1]) : "";
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;",
    }[char]));
  }

  function escapeAttribute(value) {
    return escapeHtml(value).replace(/`/g, "&#096;");
  }

  function cssEscape(value) {
    if (window.CSS && CSS.escape) return CSS.escape(value);
    return String(value).replace(/"/g, '\\"');
  }
})();
