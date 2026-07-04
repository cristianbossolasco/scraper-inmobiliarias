(() => {
  lucide.createIcons();

  const operationJobs = new Map();
  const scrapeJobs = new Map();
  const nestedScrapeJobs = new Map();
  const nestedScrapeOperations = new Map();
  const timers = new Map();
  const legacyTimers = new Map();
  const nestedTimers = new Map();
  const jobsList = document.getElementById("jobs-list");
  const initialOperations = readJsonScript("initial-operation-jobs", []);
  const initialScrapes = readJsonScript("initial-scrape-jobs", []);
  let sourceCatalog = readJsonScript("source-catalog", []);
  const stepLabels = {
    scrape: "Scraping",
    geocode: "Geocoding",
    infer_zones: "Inferencia de zonas",
    score_security: "Scoring de seguridad",
    repair_addresses: "Reparar direcciones",
    repair_neighborhoods: "Reparar barrios",
    repair_localities: "Reparar localidades",
    repair_agencies: "Reparar agencias",
    repair_metrics: "Reparar metricas",
    repair_merged_listings: "Separar fusiones",
    merge_properties: "Fusionar duplicados",
  };
  const resultLabels = {
    candidates: "Candidatas",
    cache_only: "Solo cache/local",
    changed: "Cambios",
    conflicts: "Conflictos de zona",
    dry_run: "Simulacion",
    elapsed_seconds: "Tiempo",
    errors: "Errores",
    external_hit: "Geocoding externo OK",
    inferred: "Zonas inferidas",
    located: "Ubicadas",
    matched: "Con score",
    no_result: "Sin resultado",
    note: "Nota",
    planned_sources: "Fuentes planificadas",
    processed: "Procesadas",
    phases: "Fases",
    reprocess_mode: "Reproceso",
    reprocess_stale_days: "Dias antiguas",
    scrape_job_id: "ScrapeJob",
    scrape_status: "Estado scrape",
    skipped: "Omitidas",
    steps: "Steps",
    needs_review: "Para revisar",
  };
  const repairHelp = {
    repair_addresses: "Direcciones limpia domicilios, metadata pegada y puede borrar pins no manuales si cambió el objetivo de geocoding.",
    repair_neighborhoods: "Barrios normaliza nombres de zona y elimina variantes contaminadas.",
    repair_localities: "Localidades mueve barrios mal cargados al campo zona y descarta localidades inválidas.",
    repair_agencies: "Agencias normaliza nombres de inmobiliarias y fusiona/agota agencias huérfanas.",
    repair_metrics: "Métricas reparsea fichas activas para corregir precio, superficies, ambientes y estado.",
    repair_merged_listings: "Separar fusiones detecta propiedades mezcladas por direcciones genéricas y mueve listings a propiedades separadas.",
  };

  const initialNestedScrapeIds = nestedScrapeIdsForJobs(initialOperations);
  initialScrapes.forEach((job) => {
    if (initialNestedScrapeIds.has(Number(job.id))) nestedScrapeJobs.set(Number(job.id), job);
  });
  initialOperations.sort(compareJobs).forEach((job) => renderOperationJob(job));
  initialOperations.filter(isActive).forEach(scheduleOperationPoll);
  initialScrapes
    .filter((job) => !initialNestedScrapeIds.has(Number(job.id)))
    .sort(compareJobs)
    .forEach((job) => renderLegacyScrapeJob(job));
  initialScrapes
    .filter((job) => !initialNestedScrapeIds.has(Number(job.id)) && isActive(job))
    .forEach(scheduleLegacyPoll);
  renderSourceLastRuns();
  sortJobCards();
  updateActionButtons();

  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
  });

  document.getElementById("repair-kind").addEventListener("change", updateRepairHelp);
  updateRepairHelp();

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
    document.getElementById("phase-discover").checked = true;
    document.getElementById("phase-process-new").checked = true;
    document.getElementById("phase-reprocess-existing").checked = false;
    document.getElementById("reprocess-mode").value = "incomplete";
    document.getElementById("reprocess-stale-days").value = "30";
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
    const phases = selectedPhases();
    if (!phases.length) {
      alert("Seleccione al menos una fase.");
      return;
    }
    const fromLatestDiscovery = !phases.includes("discover")
      && (phases.includes("process_new") || phases.includes("reprocess_existing"));
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
          phases,
          scrape_mode: valueOf("scrape-mode"),
          max_pages: optionalInt(valueOf("max-pages")),
          start_page: optionalInt(valueOf("start-page")),
          max_listings: optionalInt(valueOf("max-listings")),
          geocode_limit: optionalInt(valueOf("geocode-limit")) ?? 0,
          request_timeout_seconds: optionalInt(valueOf("request-timeout")),
          max_errors_per_source: optionalInt(valueOf("max-errors")),
          mark_missing: checked("mark-missing"),
          reprocess_mode: valueOf("reprocess-mode"),
          reprocess_stale_days: optionalInt(valueOf("reprocess-stale-days")) ?? 30,
          from_latest_discovery: fromLatestDiscovery,
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
    payload.params = {
      ...(payload.params || {}),
      ui_summary: operationSummary(payload),
      ui_warnings: operationWarnings(payload),
    };
    const draftId = renderOperationDraft(payload);
    activateTab("history");
    try {
      const response = await fetch("/api/operations/jobs/", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-CSRFToken": csrf()},
        body: JSON.stringify(payload),
      });
      const data = await readJson(response);
      if (response.status === 409) {
        removeOperationDraft(draftId);
        renderOperationJob(data, true);
        scheduleOperationPoll(data);
        activateTab("history");
        alert(data.error || `Ya hay una operacion en curso: Job #${data.id}.`);
        return;
      }
      if (!response.ok) throw new Error(data.error || "No se pudo iniciar la operacion.");
      removeOperationDraft(draftId);
      renderOperationJob(data, true);
      scheduleOperationPoll(data);
      activateTab("history");
    } catch (error) {
      markOperationDraftError(draftId, error.message);
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
    if (!isActive(data)) refreshSourceCatalog();
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
    if (!isActive(data)) refreshSourceCatalog();
    if (action !== "cancel") scheduleLegacyPoll(data);
  }

  function renderOperationJob(job, prepend = false) {
    operationJobs.set(job.id, job);
    trackNestedScrapeJobs(job);
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
      ${renderJobSummary(job)}
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
      refreshSourceCatalog();
    }
  }

  function renderSourceLastRuns() {
    const sourceBySlug = new Map(sourceCatalog.map((source) => [source.slug, source]));
    document.querySelectorAll("[data-source-last-run]").forEach((node) => {
      const source = sourceBySlug.get(node.dataset.sourceLastRun);
      node.innerHTML = renderSourcePhaseRuns(source);
    });
  }

  async function refreshSourceCatalog() {
    const response = await fetch("/api/scraping/sources/");
    if (!response.ok) return;
    sourceCatalog = await readJson(response);
    renderSourceLastRuns();
  }

  function renderSourcePhaseRuns(source) {
    if (!source) return renderSourceLastRun(null);
    const runs = source.last_runs_by_phase || {};
    return `
      <span class="source-phase-runs">
        ${renderSourcePhaseRun("Discover", runs.discover, "discover", runs.discover)}
        ${renderSourcePhaseRun("Nuevas+bajas", runs.process_new, "process_new", runs.discover)}
        ${renderSourcePhaseRun("Reproceso", runs.reprocess_existing, "reprocess_existing", runs.discover)}
      </span>
    `;
  }

  function renderSourcePhaseRun(label, run, key, latestDiscover) {
    if (!run) return `<span class="source-phase-run"><strong>${escapeHtml(label)}</strong>: nunca</span>`;
    const when = run.finished_at || run.started_at;
    const date = when ? new Date(when).toLocaleString() : "sin fecha";
    const total = Number(run.total_discovered || run.total_to_process || 0);
    const pending = key === "process_new" && latestDiscover && run.finished_at && latestDiscover.finished_at
      && new Date(latestDiscover.finished_at) > new Date(run.finished_at);
    const suffix = pending ? " · pendiente desde ultimo discover" : "";
    return `
      <span class="source-phase-run">
        <strong>${escapeHtml(label)}</strong>: ${escapeHtml(date)}
        <span class="status-pill ${escapeAttribute(run.status || "")}">${escapeHtml(run.status_label || run.status || "")}</span>
        <span>${Number(run.processed || 0)}/${total} · ${Number(run.created || 0)} nuevas · ${Number(run.updated || 0)} act. · ${Number(run.errors || 0)} err.${escapeHtml(suffix)}</span>
      </span>
    `;
  }

  function renderSourceLastRun(run) {
    if (!run) {
      return `<span class="source-run-empty">Sin ejecuciones registradas</span>`;
    }
    const when = run.started_at ? new Date(run.started_at).toLocaleString() : "sin fecha";
    const total = Number(run.total_to_process || run.total_discovered || 0);
    const processed = Number(run.processed || 0);
    const phaseLine = sourceRunPhases(run);
    const details = sourceRunDetails(run);
    return `
      <span class="source-run-main">
        <span>Ultima ${escapeHtml(when)}</span>
        <span class="status-pill ${escapeAttribute(run.status || "")}">${escapeHtml(run.status_label || run.status || "")}</span>
      </span>
      <span class="source-run-kpis">
        <span>${processed}/${total} procesadas</span>
        <span>${Number(run.created || 0)} nuevas</span>
        <span>${Number(run.updated || 0)} actualizadas</span>
        <span>${Number(run.skipped || 0)} omitidas</span>
        <span>${Number(run.errors || 0)} errores</span>
        <span>${formatDuration(run.elapsed_seconds)}</span>
      </span>
      ${phaseLine ? `<span class="source-run-phases">${phaseLine}</span>` : ""}
      ${details ? `<span class="source-run-details">${details}</span>` : ""}
    `;
  }

  function sourceRunPhases(run) {
    return [
      run.discovery_started_at ? `discovery ${formatDuration(run.discovery_seconds)}` : "",
      run.processing_started_at ? `procesamiento ${formatDuration(run.processing_seconds)}` : "",
      run.geocoding_started_at ? `geocoding ${formatDuration(run.geocoding_seconds)}` : "",
    ].filter(Boolean).map(escapeHtml).join(" · ");
  }

  function sourceRunDetails(run) {
    const items = [
      run.job_id ? `ScrapeJob #${run.job_id}` : "",
      run.scrape_mode_label || "",
      `${Number(run.workers || 1)} worker${Number(run.workers || 1) === 1 ? "" : "s"}`,
      run.max_pages ? `max paginas ${run.max_pages}` : "",
      run.start_page ? `desde pagina ${run.start_page}` : "",
      run.max_listings ? `max fichas ${run.max_listings}` : "",
      run.geocode_limit !== null && run.geocode_limit !== undefined ? `geocode ${run.geocode_limit}` : "",
      run.mark_missing ? "marca ausentes" : "liviano",
    ].filter(Boolean);
    return items.map(escapeHtml).join(" · ");
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
      const nestedJob = nestedScrapeJobs.get(Number(summary.scrape_job_id));
      if (nestedJob) return renderNestedScrapeJob(nestedJob);
      if (Array.isArray(summary.sources)) {
        return renderNestedScrapeJob({
          id: summary.scrape_job_id,
          status: summary.scrape_status || step.status,
          status_label: summary.scrape_status || step.status_label,
          elapsed_seconds: summary.elapsed_seconds || step.elapsed_seconds,
          sources: summary.sources,
        });
      }
      return `<div class="job-meta">ScrapeJob #${summary.scrape_job_id}${summary.scrape_status ? ` · estado ${escapeHtml(summary.scrape_status)}` : ""}</div>`;
    }
    if (summary.output_tail) {
      return `<details class="source-errors"><summary>Salida del comando</summary><pre class="job-log">${escapeHtml(summary.output_tail)}</pre></details>`;
    }
    const keys = Object.keys(summary).filter((key) => !["elapsed_seconds"].includes(key));
    if (!keys.length) return "";
    return `<div class="job-meta">${keys.map((key) => `${escapeHtml(resultLabel(key))}: ${escapeHtml(formatSummaryValue(summary[key]))}`).join(" · ")}</div>`;
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
      refreshSourceCatalog();
    }
  }

  function renderLegacySource(source) {
    const errors = Array.isArray(source.error_urls) ? source.error_urls : [];
    const meta = sourceProgressMeta(source);
    const loadingSnapshot = isLoadingSnapshot(source);
    const discovered = Number(source.discovery_seen || source.total_discovered || 0);
    const discoveryReference = Number(source.discovery_reference_total || 0);
    const progressPercent = source.status === "discovering" && !loadingSnapshot && !Number(source.total_to_process || 0)
      ? (
        discoveryReference
          ? Math.min((discovered / discoveryReference) * 100, 100)
          : (discovered ? Math.max(Number(source.percent || 0), 8) : 0)
      )
      : Number(source.percent || 0);
    const currentUrlLabel = loadingSnapshot
      ? "Ultima URL copiada: "
      : (source.status === "discovering" ? "Ultima URL descubierta: " : "");
    const statusLabel = loadingSnapshot ? "Cargando snapshot" : source.status_label;
    const statusClass = loadingSnapshot ? "running" : source.status;
    return `
      <div class="job-source">
        <div class="job-source-top">
          <div>
            <strong>${escapeHtml(source.name)}</strong>
            <span class="status-pill ${statusClass}">${escapeHtml(statusLabel)}</span>
          </div>
          <small>${source.workers} worker${source.workers === 1 ? "" : "s"} · ${formatDuration(source.elapsed_seconds)}</small>
        </div>
        <div class="progress-bar"><span style="width:${progressPercent}%"></span></div>
        <div class="job-meta">${meta}</div>
        ${source.geocode_pending ? `<div class="job-meta">Geocodificacion: ${source.geocoded}/${source.geocode_pending} ubicadas · ${source.geocode_failed} sin resultado/error</div>` : ""}
        ${source.current_url ? `<div class="current-url">${escapeHtml(currentUrlLabel)}${escapeHtml(source.current_url)}</div>` : ""}
        ${errors.length ? renderSourceErrors(errors) : ""}
        ${source.logs ? `<pre class="job-log">${escapeHtml(source.logs)}</pre>` : ""}
      </div>
    `;
  }

  function isLoadingSnapshot(source) {
    const phases = Array.isArray(source.phases) ? source.phases : [];
    return Boolean(
      source.loading_snapshot
      || (
        source.from_latest_discovery
        && !phases.includes("discover")
        && source.status === "discovering"
      )
    );
  }

  function usesSnapshotWithoutDiscover(source) {
    const phases = Array.isArray(source.phases) ? source.phases : [];
    return Boolean(source.from_latest_discovery && !phases.includes("discover"));
  }

  function sourceProgressMeta(source) {
    const processed = Number(source.processed || 0);
    const toProcess = Number(source.total_to_process || 0);
    const discovered = Number(source.total_discovered || 0);
    const snapshotOnly = usesSnapshotWithoutDiscover(source);
    const outOfPhase = Number(source.snapshot_out_of_phase || 0);
    const pieces = [];
    if (isLoadingSnapshot(source)) {
      pieces.push(`${Number(source.discovery_seen || discovered || 0)} URLs copiadas del snapshot`);
      pieces.push(`${Number(source.errors || 0)} errores`);
      return pieces.map(escapeHtml).join(" · ");
    }
    if (source.status === "discovering") {
      const discoverySeen = Number(source.discovery_seen || discovered || 0);
      const discoveryReference = Number(source.discovery_reference_total || 0);
      pieces.push(
        discoveryReference
          ? `${discoverySeen} descubiertas de ~${discoveryReference}`
          : `${discoverySeen} descubiertas hasta ahora`
      );
      if (outOfPhase) {
        pieces.push(`${outOfPhase} fuera de fase`);
      } else {
        pieces.push(`${Number(source.discovery_new || 0)} nuevas`);
      }
      pieces.push(`${Number(source.discovery_existing || 0)} existentes`);
      pieces.push(`${Number(source.errors || 0)} errores`);
      return pieces.map(escapeHtml).join(" · ");
    }
    if (!toProcess && discovered && Number(source.discovery_seen || 0)) {
      pieces.push(
        snapshotOnly
          ? `${Number(source.discovery_seen || discovered)} URLs del snapshot`
          : `${Number(source.discovery_seen || discovered)} descubiertas`
      );
      if (outOfPhase) {
        pieces.push(`${outOfPhase} fuera de fase`);
      } else {
        pieces.push(`${Number(source.discovery_new || 0)} nuevas`);
      }
      pieces.push(`${Number(source.discovery_existing || 0)} existentes`);
      pieces.push(`${Number(source.total_to_process || 0)} acciones planificadas`);
      pieces.push(`${Number(source.errors || 0)} errores`);
      return pieces.map(escapeHtml).join(" · ");
    }
    if (toProcess) {
      pieces.push(`${processed}/${toProcess} procesadas`);
      if (discovered && discovered !== toProcess) {
        pieces.push(snapshotOnly ? `${discovered} URLs del snapshot` : `${discovered} descubiertas`);
      }
    } else if (discovered) {
      pieces.push(snapshotOnly ? `${discovered} URLs del snapshot` : `${discovered} descubiertas`);
    } else {
      pieces.push(`${processed}/0 procesadas`);
    }
    pieces.push(`${Number(source.created || 0)} nuevas`);
    pieces.push(`${Number(source.updated || 0)} actualizadas`);
    pieces.push(`${Number(source.skipped || 0)} omitidas`);
    pieces.push(`${Number(source.errors || 0)} errores`);
    return pieces.map(escapeHtml).join(" · ");
  }

  function renderNestedScrapeJob(job) {
    const sources = Array.isArray(job.sources) ? job.sources : [];
    const processed = sources.reduce((total, source) => total + Number(source.processed || 0), 0);
    const total = sources.reduce((sum, source) => sum + Number(source.total_to_process || 0), 0);
    const discovered = sources.reduce((sum, source) => sum + Number(source.total_discovered || 0), 0);
    const created = sources.reduce((sum, source) => sum + Number(source.created || 0), 0);
    const updated = sources.reduce((sum, source) => sum + Number(source.updated || 0), 0);
    const errors = sources.reduce((sum, source) => sum + Number(source.errors || 0), 0);
    const phases = Array.isArray(job.phases) ? job.phases : [];
    const snapshotOnly = Boolean(job.from_latest_discovery && !phases.includes("discover"));
    const progressText = total
      ? `${processed}/${total} procesadas`
      : (snapshotOnly ? `${discovered} URLs del snapshot` : `${discovered} descubiertas`);
    return `
      <div class="nested-scrape-job">
        <div class="job-meta nested-scrape-meta">
          <span>ScrapeJob #${job.id}</span>
          <span class="status-pill ${job.status}">${escapeHtml(job.status_label || job.status || "")}</span>
          <span>${progressText} · ${created + updated} cambios · ${errors} errores · ${formatDuration(job.elapsed_seconds)}</span>
        </div>
        <div class="job-source-list nested-scrape-source-list">
          ${sources.map(renderLegacySource).join("")}
        </div>
        ${job.error_log ? `<pre class="job-log">${escapeHtml(job.error_log)}</pre>` : ""}
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
    return `<button class="secondary-button" type="button" data-operation-action="${action}" data-job-id="${id}" title="${escapeAttribute(actionHelp(action))}" ${disabled ? "disabled" : ""}><i data-lucide="${icon}"></i> ${label}</button>`;
  }

  function legacyActionButton(action, id, icon, label, disabled = false) {
    return `<button class="secondary-button" type="button" data-legacy-action="${action}" data-job-id="${id}" title="${escapeAttribute(actionHelp(action))}" ${disabled ? "disabled" : ""}><i data-lucide="${icon}"></i> ${label}</button>`;
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

  function updateRepairHelp() {
    const help = document.getElementById("repair-kind-help");
    if (!help) return;
    const kind = valueOf("repair-kind");
    help.textContent = repairHelp[kind] || "Ejecuta una reparacion en simulacion; aplicar queda disponible desde el job terminado.";
  }

  function operationSummary(payload) {
    const steps = Array.isArray(payload.steps) ? payload.steps : [];
    const summary = [`Modo: ${payload.mode === "dry_run" ? "simulacion" : "aplicar"}`];
    if (steps.length) summary.push(`Flujo: ${steps.map((step) => stepLabel(step.kind)).join(" > ")}`);

    if (steps.some((step) => step.kind === "scrape")) {
      const params = steps.find((step) => step.kind === "scrape").params || {};
      const maxWorker = Math.max(1, ...Object.values(params.workers || {}).map((value) => Number(value || 1)));
      summary.push(`Fuentes: ${formatList(params.sources) || "ninguna"}`);
      summary.push(`Fases: ${formatList(params.phases) || "compatibles"}`);
      summary.push(`Tipo: ${params.scrape_mode === "trial" ? "prueba" : "completo liviano"}`);
      summary.push(`Paginas: ${params.max_pages || "sin limite"} desde ${params.start_page || 1}`);
      summary.push(`Listings: ${params.max_listings || "sin limite"}`);
      summary.push(`Geocoding: ${Number(params.geocode_limit || 0) > 0 ? `hasta ${params.geocode_limit}` : "no, scrape liviano"}`);
      summary.push(`Marcar ausentes: ${yesNo(params.mark_missing)}`);
      if ((params.phases || []).includes("reprocess_existing")) {
        summary.push(`Reproceso: ${params.reprocess_mode || "incomplete"} (${params.reprocess_stale_days || 30} dias)`);
      }
      summary.push(`Workers: hasta ${maxWorker} por fuente`);
    }

    const enrichStep = steps.find((step) => ["geocode", "infer_zones", "score_security"].includes(step.kind));
    if (enrichStep) {
      summary.push(...scopeSummary(enrichStep.params || {}));
      const geocode = steps.find((step) => step.kind === "geocode");
      const zones = steps.find((step) => step.kind === "infer_zones");
      const security = steps.find((step) => step.kind === "score_security");
      if (geocode) {
        summary.push(`Geocoding externo: ${geocode.params && geocode.params.cache_only === false ? "permitido" : "no, cache/local"}`);
        summary.push(`Recalcular existentes: ${yesNo(geocode.params && geocode.params.force)}`);
      }
      if (zones) {
        const distance = zones.params && zones.params.max_distance_m;
        summary.push(`Zonas: distancia ${distance || "default"} m; geocodificar faltantes ${yesNo(zones.params && zones.params.geocode_missing)}`);
      }
      if (security) summary.push(`Seguridad solo faltantes: ${yesNo(security.params && security.params.only_missing)}`);
    }

    if (steps.some((step) => step.kind && step.kind.startsWith("repair_"))) {
      const params = steps[0].params || {};
      summary.push(...scopeSummary(params));
      if (params.max_listings) summary.push(`Max. listings: ${params.max_listings}`);
      if (params.max_properties) summary.push(`Max. propiedades: ${params.max_properties}`);
      if (params.classify_only) summary.push("Metricas: solo clasificar");
      if (params.mark_non_sale) summary.push("Marcara no venta si corresponde");
      if (params.mark_listing_pages) summary.push("Marcara paginas de listado si corresponde");
    }

    if (steps.some((step) => step.kind === "merge_properties")) {
      const params = steps.find((step) => step.kind === "merge_properties").params || {};
      summary.push(`Pares: ${(params.pair || []).length || "ninguno"}`);
      summary.push(`Componentes: ${(params.component || []).length || "ninguno"}`);
      summary.push(`URL tail: ${formatList(params.detect_url_tail_sources) || "desactivado"}`);
    }

    return summary.filter(Boolean);
  }

  function operationWarnings(payload) {
    const steps = Array.isArray(payload.steps) ? payload.steps : [];
    const warnings = [];
    const add = (level, text) => {
      if (!warnings.some((item) => item.text === text)) warnings.push({level, text});
    };
    steps.forEach((step) => {
      const params = step.params || {};
      if (step.kind === "scrape") {
        if (Number(params.geocode_limit || 0) > 0) add("medium", "Activa geocoding post-scrape; para scrape liviano dejar Max. geocod. en 0.");
        if (params.mark_missing) add("high", "Marcar ausentes puede retirar publicaciones no vistas en corridas completas.");
      }
      if (step.kind === "geocode") {
        if (params.cache_only === false) add("medium", "Geocoding externo puede cambiar coordenadas no manuales y depende del proveedor.");
        if (params.force) add("medium", "Recalcular existentes actualiza ubicaciones no manuales ya cargadas.");
      }
      if (step.kind === "infer_zones" && params.geocode_missing) {
        add("medium", "Inferir zonas con geocoding externo puede completar coordenadas faltantes antes de asignar zona.");
      }
      if (step.kind && step.kind.startsWith("repair_")) {
        add(payload.mode === "dry_run" ? "low" : "high", payload.mode === "dry_run"
          ? "Simulacion: no modifica datos hasta usar Aplicar dry-run desde el historial."
          : "Aplicara cambios de reparacion usando una simulacion previa.");
      }
      if (step.kind === "merge_properties") {
        add(payload.mode === "dry_run" ? "medium" : "high", "Merge conserva flags, notas y links, pero puede mover listings entre propiedades; revisar el dry-run.");
      }
    });
    return warnings;
  }

  function renderOperationDraft(payload) {
    const id = `operation-draft-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const node = document.createElement("article");
    node.className = "job-card operation-job-card job-draft-card";
    node.id = id;
    node.dataset.active = "1";
    node.dataset.createdAt = new Date().toISOString();
    node.innerHTML = `
      <div class="job-heading">
        <div>
          <strong>${escapeHtml(payload.title || "Operacion")}</strong>
          <span class="status-pill running">Preparando</span>
          <small>Resumen previo antes de crear el job</small>
        </div>
      </div>
      ${renderSummaryBlock(payload.params && payload.params.ui_summary, payload.params && payload.params.ui_warnings)}
    `;
    jobsList.prepend(node);
    lucide.createIcons();
    sortJobCards();
    return id;
  }

  function removeOperationDraft(id) {
    const node = document.getElementById(id);
    if (node) node.remove();
  }

  function markOperationDraftError(id, message) {
    const node = document.getElementById(id);
    if (!node) return;
    node.dataset.active = "0";
    node.classList.add("failed");
    node.innerHTML = `
      <div class="job-heading">
        <div>
          <strong>No se pudo preparar la operacion</strong>
          <span class="status-pill failed">Error</span>
          <small>${escapeHtml(message || "Error desconocido")}</small>
        </div>
      </div>
    `;
    sortJobCards();
  }

  function renderJobSummary(job) {
    const params = job.params && typeof job.params === "object" ? job.params : {};
    return renderSummaryBlock(params.ui_summary, params.ui_warnings);
  }

  function renderSummaryBlock(summary, warnings) {
    const items = Array.isArray(summary) ? summary.filter(Boolean) : [];
    const notes = Array.isArray(warnings) ? warnings.filter(Boolean) : [];
    if (!items.length && !notes.length) return "";
    return `
      <div class="job-summary">
        ${items.length ? `<div class="job-summary-list">${items.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>` : ""}
        ${notes.length ? `<div class="job-warning-list">${notes.map(renderRiskNote).join("")}</div>` : ""}
      </div>
    `;
  }

  function renderRiskNote(item) {
    const level = riskLevel(item);
    const text = typeof item === "string" ? item : item.text;
    return `<span class="risk-note risk-${level}">${escapeHtml(text || "")}</span>`;
  }

  function riskLevel(item) {
    const level = typeof item === "object" && item ? item.level : "medium";
    return ["low", "medium", "high"].includes(level) ? level : "medium";
  }

  function scopeSummary(params) {
    const items = [];
    if (params.source) items.push(`Fuente: ${params.source}`);
    if (params.sources && params.sources.length) items.push(`Fuentes: ${formatList(params.sources)}`);
    if (params.property_ids && params.property_ids.length) items.push(`IDs: ${formatList(params.property_ids)}`);
    items.push(`Limite: ${params.limit || "todos"}`);
    return items;
  }

  function stepLabel(kind) {
    return stepLabels[kind] || repairLabel(kind) || String(kind || "step").replace(/_/g, " ");
  }

  function resultLabel(key) {
    return resultLabels[key] || String(key).replace(/_/g, " ");
  }

  function formatSummaryValue(value) {
    if (value === null || value === undefined || value === "") return "-";
    if (typeof value === "boolean") return yesNo(value);
    if (Array.isArray(value)) return value.length ? value.map(formatSummaryValue).join(", ") : "ninguno";
    if (typeof value === "object") {
      const text = JSON.stringify(value);
      return text.length > 160 ? `${text.slice(0, 157)}...` : text;
    }
    return String(value);
  }

  function formatList(values) {
    if (!Array.isArray(values) || !values.length) return "";
    if (values.length <= 4) return values.join(", ");
    return `${values.slice(0, 4).join(", ")} +${values.length - 4}`;
  }

  function yesNo(value) {
    return value ? "si" : "no";
  }

  function actionHelp(action) {
    return {
      apply: "Ejecuta en modo aplicar la misma parametria de esta simulacion.",
      cancel: "Pide detener el job; las tareas en curso pueden terminar antes de frenar.",
      retry: "Crea otro job con los mismos parametros.",
      "retry-errors": "Reprocesa solo las URLs fallidas del scraper legacy.",
    }[action] || "Ejecuta esta accion sobre el job.";
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

  function scheduleNestedScrapePoll(scrapeJobId) {
    const id = Number(scrapeJobId);
    if (!id || nestedTimers.has(id)) return;
    fetchNestedScrapeJob(id);
    nestedTimers.set(id, setInterval(() => fetchNestedScrapeJob(id), 1500));
  }

  async function fetchNestedScrapeJob(scrapeJobId) {
    const response = await fetch(`/api/scraping/jobs/${scrapeJobId}/`);
    if (!response.ok) return;
    const job = await readJson(response);
    const id = Number(job.id);
    nestedScrapeJobs.set(id, job);
    rerenderNestedScrapeOperations(id);
    if (!isActive(job) && nestedTimers.has(id)) {
      clearInterval(nestedTimers.get(id));
      nestedTimers.delete(id);
      refreshSourceCatalog();
    }
  }

  function trackNestedScrapeJobs(job) {
    (job.steps || []).forEach((step) => {
      const scrapeJobId = nestedScrapeJobId(step);
      if (!scrapeJobId) return;
      if (!nestedScrapeOperations.has(scrapeJobId)) nestedScrapeOperations.set(scrapeJobId, new Set());
      nestedScrapeOperations.get(scrapeJobId).add(job.id);
      const nestedJob = nestedScrapeJobs.get(scrapeJobId);
      if (isActive(job) || isActive(step) || (nestedJob && isActive(nestedJob))) {
        scheduleNestedScrapePoll(scrapeJobId);
      }
    });
  }

  function rerenderNestedScrapeOperations(scrapeJobId) {
    const operationIds = nestedScrapeOperations.get(scrapeJobId);
    if (!operationIds) return;
    operationIds.forEach((operationId) => {
      const job = operationJobs.get(operationId);
      if (job) renderOperationJob(job);
    });
  }

  function nestedScrapeJobId(step) {
    if (!step || step.kind !== "scrape") return null;
    const summary = step.result_summary || {};
    const id = Number(summary.scrape_job_id);
    return id || null;
  }

  function nestedScrapeIdsForJobs(jobs) {
    const ids = new Set();
    jobs.forEach((job) => {
      (job.steps || []).forEach((step) => {
        const id = nestedScrapeJobId(step);
        if (id) ids.add(id);
      });
    });
    return ids;
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

  function selectedPhases() {
    return [
      ["phase-discover", "discover"],
      ["phase-process-new", "process_new"],
      ["phase-reprocess-existing", "reprocess_existing"],
    ].filter(([id]) => checked(id)).map(([, phase]) => phase);
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
