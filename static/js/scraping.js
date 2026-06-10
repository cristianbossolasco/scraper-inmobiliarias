(() => {
  lucide.createIcons();

  const jobs = new Map();
  const timers = new Map();
  const jobsList = document.getElementById("jobs-list");
  const initial = JSON.parse(document.getElementById("initial-scrape-jobs").textContent);

  initial.forEach(renderJob);
  initial.filter(isActive).forEach(schedulePoll);

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
    document.getElementById("scrape-mode").value = "trial";
    document.getElementById("max-pages").value = "";
    document.getElementById("start-page").value = "";
    document.getElementById("max-listings").value = "3";
    document.getElementById("request-timeout").value = "25";
    document.getElementById("max-errors").value = "3";
    document.getElementById("geocode-limit").value = "3";
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

  document.getElementById("start-scraping").addEventListener("click", async () => {
    const selected = [...document.querySelectorAll("#source-picker input[type=checkbox]:checked")];
    const payload = {
      sources: selected.map((item) => item.value),
      workers: {},
      scrape_mode: document.getElementById("scrape-mode").value,
      max_pages: optionalInt(document.getElementById("max-pages").value),
      start_page: optionalInt(document.getElementById("start-page").value),
      max_listings: optionalInt(document.getElementById("max-listings").value),
      geocode_limit: optionalInt(document.getElementById("geocode-limit").value),
      request_timeout_seconds: optionalInt(document.getElementById("request-timeout").value),
      max_errors_per_source: optionalInt(document.getElementById("max-errors").value),
    };
    selected.forEach((item) => {
      const input = document.querySelector(`[data-workers-for="${cssEscape(item.value)}"]`);
      payload.workers[item.value] = Math.max(1, Number(input.value || 1));
    });
    try {
      const response = await fetch("/api/scraping/jobs/", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrf(),
        },
        body: JSON.stringify(payload),
      });
      const data = await readJson(response);
      if (!response.ok) throw new Error(data.error || "No se pudo iniciar.");
      renderJob(data, true);
      schedulePoll(data);
    } catch (error) {
      alert(error.message);
    }
  });

  jobsList.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-cancel-job], [data-retry-job]");
    if (!button) return;
    button.disabled = true;
    const isRetry = Boolean(button.dataset.retryJob);
    if (!isRetry) {
      button.innerHTML = `<i data-lucide="square"></i> Cancelando...`;
      lucide.createIcons();
    }
    const id = button.dataset.cancelJob || button.dataset.retryJob;
    const action = isRetry ? "retry" : "cancel";
    const response = await fetch(`/api/scraping/jobs/${id}/${action}/`, {
      method: "POST",
      headers: {"X-CSRFToken": csrf()},
    });
    const data = await readJson(response);
    if (!response.ok) {
      alert(data.error || "No se pudo ejecutar la accion.");
      button.disabled = false;
      return;
    }
    renderJob(data, isRetry);
    if (isRetry) schedulePoll(data);
  });

  function renderJob(job, prepend = false) {
    jobs.set(job.id, job);
    let node = document.getElementById(`scrape-job-${job.id}`);
    if (!node) {
      node = document.createElement("article");
      node.className = "job-card";
      node.id = `scrape-job-${job.id}`;
      if (prepend) jobsList.prepend(node);
      else jobsList.append(node);
    }
    const created = job.created_at ? new Date(job.created_at).toLocaleString() : "";
    node.innerHTML = `
      <div class="job-heading">
        <div>
          <strong>Job #${job.id}</strong>
          <span class="status-pill ${job.status}">${job.status_label}</span>
          <span class="status-pill neutral">${job.scrape_mode_label}</span>
          <small>${created}</small>
        </div>
        <div class="job-actions">
          ${isActive(job) ? `<button class="secondary-button" type="button" data-cancel-job="${job.id}" ${job.cancel_requested ? "disabled" : ""}><i data-lucide="square"></i> ${job.cancel_requested ? "Cancelando..." : "Cancelar"}</button>` : ""}
          ${canRetry(job) ? `<button class="secondary-button" type="button" data-retry-job="${job.id}"><i data-lucide="rotate-cw"></i> Repetir</button>` : ""}
        </div>
      </div>
      ${job.cancel_requested && isActive(job) ? `<div class="job-meta cancelling-meta">Cancelacion solicitada; esperando tareas en curso...</div>` : ""}
      ${renderDbWriter(job.db_writer)}
      <div class="job-source-list">
        ${job.sources.map(renderSource).join("")}
      </div>
      ${job.error_log ? `<pre class="job-log">${escapeHtml(job.error_log)}</pre>` : ""}
    `;
    lucide.createIcons();
    if (!isActive(job) && timers.has(job.id)) {
      clearInterval(timers.get(job.id));
      timers.delete(job.id);
    }
  }

  function renderSource(source) {
    const counts = `${source.processed}/${source.total_to_process || 0} procesadas · ${source.created} nuevas · ${source.updated} actualizadas · ${source.skipped} omitidas · ${source.errors} errores`;
    const geocodeCounts = source.geocode_pending
      ? `<div class="job-meta">Geocodificacion: ${source.geocoded}/${source.geocode_pending} ubicadas · ${source.geocode_failed} sin resultado/error</div>`
      : "";
    return `
      <div class="job-source">
        <div class="job-source-top">
          <div>
            <strong>${escapeHtml(source.name)}</strong>
            <span class="status-pill ${source.status}">${source.status_label}</span>
          </div>
          <small>${source.workers} worker${source.workers === 1 ? "" : "s"}</small>
        </div>
        <div class="progress-bar" aria-label="Progreso ${escapeHtml(source.name)}">
          <span style="width:${source.percent}%"></span>
        </div>
        <div class="job-meta">${counts}</div>
        ${geocodeCounts}
        ${source.current_url ? `<div class="current-url">${escapeHtml(source.current_url)}</div>` : ""}
        ${source.logs ? `<pre class="job-log">${escapeHtml(source.logs)}</pre>` : ""}
      </div>
    `;
  }

  function renderDbWriter(stats) {
    if (!stats || !stats.safe_sqlite) return "";
    return `
      <div class="job-meta db-writer-meta">
        Modo seguro SQLite activo Â· ${stats.completed}/${stats.queued} escrituras completadas Â· ${stats.lock_retries} reintentos por bloqueo Â· espera max. ${stats.max_wait_seconds || 0}s
      </div>
    `;
  }

  function schedulePoll(job) {
    if (timers.has(job.id)) return;
    timers.set(job.id, setInterval(async () => {
      const response = await fetch(`/api/scraping/jobs/${job.id}/`);
      if (response.ok) renderJob(await readJson(response));
    }, 1500));
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

  function isActive(job) {
    return ["pending", "running"].includes(job.status);
  }

  function canRetry(job) {
    return ["partial", "failed", "cancelled", "interrupted"].includes(job.status);
  }

  function optionalInt(value) {
    return value === "" ? null : Number(value);
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

  function cssEscape(value) {
    if (window.CSS && CSS.escape) return CSS.escape(value);
    return String(value).replace(/"/g, '\\"');
  }
})();
